import json
import collections.abc
import types
import unittest
import unittest.mock

import fastapi
import sqlalchemy.ext.asyncio

from npps4 import idoltype
from npps4 import util
from npps4.config import config
from npps4.db import main
from npps4.game import live as live_game
from npps4.idol import core
from npps4.idol import error
from npps4.idol import session
from npps4.system import achievement
from npps4.system import live
from npps4.system import master_challenge
from npps4.system import user

from typing import cast


class NativeGameplayTest(unittest.IsolatedAsyncioTestCase):
    def test_game_day_does_not_change_protocol_time(self):
        boundary = 1794254400
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "game_day_offset_minutes", 240):
            self.assertEqual(util.get_next_day_timestamp(boundary - 1), boundary)
            self.assertEqual(util.game_day_datetime(boundary - 1).day, 9)
            self.assertEqual(util.game_day_datetime(boundary).day, 10)
            self.assertEqual(util.datetime(boundary).hour, 5)

    async def test_ranking_signature_does_not_change_shared_verification(self):
        context = object.__new__(session.SchoolIdolAuthParams)
        token = session.TokenData(user_id=1, client_key=b"c" * 32, server_key=b"s" * 32)
        context.token = token
        context.raw_request_data = b"{}"
        base, appkey = config.get_base_xorpad(), config.get_application_key()
        cross = util.xorbytes(base[16:], appkey[:16]) + util.xorbytes(base[:16], appkey[16:])
        shared = util.xorbytes(token.client_key, token.server_key)

        with (
            unittest.mock.patch.object(config, "is_maintenance", return_value=False),
            unittest.mock.patch.object(config, "need_xmc_verify", return_value=True),
        ):
            for key in (shared, cross):
                context.x_message_code = util.hmac_sha1(context.raw_request_data, key).hex()
                self.assertIsNone(await core.client_check(context, False, idoltype.XMCVerifyMode.SHARED_OR_CROSS))
            with self.assertRaises(fastapi.HTTPException) as rejected:
                await core.client_check(context, False, idoltype.XMCVerifyMode.SHARED)
            self.assertEqual(rejected.exception.status_code, 422)

    async def test_daily_rollover_preserves_paid_rows_and_rejects_repeat_claims(self):
        engine = sqlalchemy.ext.asyncio.create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            metadata = main.User.metadata
            await connection.run_sync(
                metadata.create_all, tables=[metadata.tables["user"], metadata.tables["achievement"]]
            )

        now = 1794254400
        day = util.get_days_since_unix(now)
        async with sqlalchemy.ext.asyncio.AsyncSession(engine, expire_on_commit=False) as database:
            context = session.BasicSchoolIdolContext()
            definition = types.SimpleNamespace(
                achievement_id=4,
                params1=1,
                params2=3,
                auto_reward_flag=False,
                reset_type=1,
                start_date="2020-01-01 00:00:00",
                end_date=None,
            )
            with (
                unittest.mock.patch.object(
                    context,
                    "db",
                    types.SimpleNamespace(
                        main=database,
                        achievement=types.SimpleNamespace(
                            get=unittest.mock.AsyncMock(return_value=types.SimpleNamespace(default_open_flag=True))
                        ),
                    ),
                ),
                unittest.mock.patch.object(
                    achievement, "get_achievement_info", unittest.mock.AsyncMock(return_value=definition)
                ),
                unittest.mock.patch.object(
                    achievement,
                    "get_achievement_ids_from_category",
                    unittest.mock.AsyncMock(return_value=[1, 2, 2, 4]),
                ),
            ):
                player = main.User()
                database.add(player)
                await database.flush()
                rows = []
                for number, kind, period, updated, paid in (
                    (1, 50, day - 1, now - 86400, False),
                    (2, 50, 0, now, True),
                    (3, 50, day, now, False),
                    (4, 53, day, now, False),
                ):
                    row = main.Achievement(
                        user_id=player.id,
                        achievement_id=number,
                        achievement_type=kind,
                        achievement_filter_category_id=1,
                        reset_type=1,
                        reset_value=period,
                        insert_date=updated,
                        update_date=updated,
                        count=3,
                        is_accomplished=True,
                        is_reward_claimed=paid,
                    )
                    rows.append(row)
                    database.add(row)
                await achievement.prepare_daily_achievements(context, player, now)
                self.assertEqual((rows[0].count, rows[0].reset_value), (0, day))
                self.assertTrue(rows[1].is_reward_claimed)
                self.assertEqual(rows[1].reset_value, day)
                self.assertEqual(rows[3].count, 1)
                self.assertFalse(rows[3].is_accomplished)
                self.assertIsNone(await achievement.reserve_achievement_reward(context, player, rows[0], now))
                with self.assertRaisesRegex(RuntimeError, "reward failed"):
                    async with database.begin_nested():
                        self.assertIs(
                            await achievement.reserve_achievement_reward(context, player, rows[2], now), definition
                        )
                        raise RuntimeError("reward failed")
                await database.refresh(rows[2])
                self.assertFalse(rows[2].is_reward_claimed)
                self.assertIs(await achievement.reserve_achievement_reward(context, player, rows[2], now), definition)
                self.assertIsNone(await achievement.reserve_achievement_reward(context, player, rows[2], now))

    async def test_master_quota_replay_rollover_and_failed_start(self):
        engine = sqlalchemy.ext.asyncio.create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            metadata = main.User.metadata
            await connection.run_sync(
                metadata.create_all,
                tables=[
                    metadata.tables[name]
                    for name in (
                        "user",
                        "live_in_progress",
                        "destiny_master_challenge_day",
                        "destiny_master_challenge_start",
                        "destiny_reward_egg_live",
                    )
                ],
            )

        async with sqlalchemy.ext.asyncio.AsyncSession(engine, expire_on_commit=False) as database:
            player = main.User(game_coin=100)
            database.add(player)
            await database.flush()
            await database.commit()
            context = object.__new__(session.SchoolIdolUserParams)
            session.BasicSchoolIdolContext.__init__(context)
            context.token = session.TokenData(user_id=player.id, client_key=b"", server_key=b"")
            request = live_game.LivePlayRequest(
                party_user_id=player.id, is_training=True, unit_deck_id=1, live_difficulty_id=123, lp_factor=4
            )
            response = live_game.LivePlayResponse(rank_info=[], energy_full_time="", over_max_energy=0, live_list=[])
            play = cast(
                collections.abc.Callable[
                    [session.SchoolIdolUserParams, live_game.LivePlayRequest],
                    collections.abc.Awaitable[live_game.LivePlayResponse],
                ],
                live_game.live_play,
            )

            async def start(
                context: session.SchoolIdolUserParams,
                player: main.User,
                request: live_game.LivePlayRequest,
                *,
                master_challenge_request_key: str,
            ):
                player.game_coin -= 1
                await live.register_live_in_progress(
                    context,
                    player,
                    player,
                    request.lp_factor,
                    request.unit_deck_id,
                    b"{}",
                    master_challenge_request_key=master_challenge_request_key,
                )
                return response

            async def fail_after_start(
                context: session.SchoolIdolUserParams,
                player: main.User,
                request: live_game.LivePlayRequest,
                *,
                master_challenge_request_key: str,
            ):
                await start(context, player, request, master_challenge_request_key=master_challenge_request_key)
                raise RuntimeError("start failed")

            def set_identity(command_number: int):
                context.raw_request_data = json.dumps({"timeStamp": 10, "commandNum": command_number}).encode()

            with (
                unittest.mock.patch.object(context, "db", types.SimpleNamespace(main=database)),
                unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "master_challenge_daily_limit", 1),
                unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "marathon_policy", None),
                unittest.mock.patch.object(util, "time", return_value=1794254400) as game_time,
                unittest.mock.patch.object(user, "get_current", unittest.mock.AsyncMock(return_value=player)),
                unittest.mock.patch.object(
                    master_challenge, "validate_entry", unittest.mock.AsyncMock(return_value=True)
                ),
                unittest.mock.patch.object(live_game, "start_live", side_effect=fail_after_start) as start_live,
            ):
                set_identity(1)
                with self.assertRaisesRegex(RuntimeError, "start failed"):
                    await play(context, request)
                await database.refresh(player)
                self.assertEqual(player.game_coin, 100)
                self.assertIsNone(await live.get_live_in_progress(context, player))
                self.assertEqual(await master_challenge.get_energy(context, player), (1, 1))

                start_live.side_effect = start
                self.assertEqual(await play(context, request), response)
                self.assertEqual(await play(context, request), response)
                self.assertEqual(player.game_coin, 99)
                self.assertEqual(await master_challenge.get_energy(context, player), (0, 1))
                await live.clean_live_in_progress(context, player)

                set_identity(2)
                with self.assertRaises(error.IdolError) as exhausted:
                    await play(context, request)
                self.assertEqual(
                    exhausted.exception.error_code,
                    error.ERROR_CODE_LIVE_NOT_ENOUGH_TRAINING_ENERGY_CANT_RECOVERY,
                )
                game_time.return_value += 86400
                self.assertEqual(await master_challenge.get_energy(context, player), (1, 1))
                self.assertEqual(await play(context, request), response)
                set_identity(1)
                with self.assertRaises(error.IdolError) as ended:
                    await play(context, request)
                self.assertEqual(ended.exception.detail, "This MASTER start has already ended")
                self.assertEqual(player.game_coin, 98)
