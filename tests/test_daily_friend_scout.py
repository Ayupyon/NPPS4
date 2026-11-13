import asyncio
import contextlib
import importlib
import pathlib
import sqlite3
import tempfile
import types
import unittest
import unittest.mock

import alembic.migration
import alembic.operations
import fastapi
import httpx
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.ext.asyncio

import npps4.app.app as app
import npps4.config.config as config
import npps4.const as const
import npps4.data as data
import npps4.db.main as main
import npps4.game.secretbox as secretbox_game
import npps4.idol.core as core
import npps4.idol.error as error
import npps4.idol.session as session
import npps4.release_key as release_key
import npps4.system.achievement as achievement
import npps4.system.album as album
import npps4.system.item as item
import npps4.system.museum as museum
import npps4.system.reward as reward
import npps4.system.secretbox as secretbox
import npps4.system.unit as unit
import npps4.system.user as user
import npps4.util as util

REVISION = importlib.import_module("npps4.alembic.versions.2026_11_12_0200-e091c292a3da_daily_friend_scout")
RESET = 1794254400
LEGACY_SCHEMA = """
CREATE TABLE destiny_scout_account (user_id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE);
CREATE TABLE destiny_daily_friend_scout (
    account_uid TEXT NOT NULL, day_jst INTEGER NOT NULL, claimed_at_utc INTEGER NOT NULL,
    PRIMARY KEY (account_uid, day_jst)
);
CREATE TABLE destiny_scout_log (
    draw_uid TEXT NOT NULL PRIMARY KEY, account_uid TEXT NOT NULL, user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL, drawn_at_utc TEXT NOT NULL, draw_type TEXT NOT NULL, pool_id INTEGER NOT NULL,
    draw_count INTEGER NOT NULL, cost_type INTEGER NOT NULL, cost_item_id INTEGER NOT NULL,
    n_count INTEGER NOT NULL, r_count INTEGER NOT NULL, sr_count INTEGER NOT NULL,
    ssr_count INTEGER NOT NULL, ur_count INTEGER NOT NULL
);
CREATE INDEX destiny_scout_log_account_time ON destiny_scout_log(account_uid, drawn_at_utc);
CREATE TRIGGER destiny_scout_account_insert AFTER INSERT ON user BEGIN
    INSERT INTO destiny_scout_account(user_id, uid) VALUES (NEW.id, lower(hex(randomblob(16))));
END;
INSERT INTO destiny_scout_account(user_id, uid) SELECT id, lower(hex(randomblob(16))) FROM user;
"""


class DailyFriendScoutTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(directory) / "main.sqlite3"
        with contextlib.closing(sqlite3.connect(self.path, isolation_level=None)) as database:
            database.execute("PRAGMA journal_mode=WAL")
        self.engine = sqlalchemy.ext.asyncio.create_async_engine(
            f"sqlite+aiosqlite:///{self.path}", connect_args={"autocommit": False, "timeout": 5}
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = sqlalchemy.ext.asyncio.async_sessionmaker(self.engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", self.sessions))
        excluded_tables = {REVISION.QUOTA_TABLE, REVISION.ACCOUNT_TABLE, REVISION.LOG_TABLE}
        async with self.engine.begin() as connection:
            metadata = main.User.metadata
            tables = [table for table in metadata.sorted_tables if table.name not in excluded_tables]
            await connection.run_sync(metadata.create_all, tables=tables)
        async with self.sessions.begin() as database:
            player = main.User(social_point=1000)
            database.add(player)
            await database.flush()
            self.player_id = player.id

        self.now = RESET - 1
        gameplay = config.CONFIG_DATA.gameplay
        self.enterContext(
            unittest.mock.patch.multiple(
                gameplay,
                game_day_offset_minutes=240,
                master_challenge_daily_limit=None,
                daily_friend_scout=True,
                scout_audit=False,
                secretbox_cost_multiplier=1,
            )
        )
        self.enterContext(unittest.mock.patch.object(util, "time", side_effect=lambda: self.now))
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(unittest.mock.patch.object(session, "try_cleanup_tokens"))
        self.enterContext(unittest.mock.patch.object(reward, "try_cleanup_incentive"))
        self.enterContext(unittest.mock.patch.object(item, "get_buff_item_ids", return_value=[]))
        self.enterContext(unittest.mock.patch.object(item, "get_reinforce_item_ids", return_value=[]))
        self.enterContext(unittest.mock.patch.object(item, "get_recovery_items", return_value=[]))
        self.enterContext(
            unittest.mock.patch.object(achievement, "check", return_value=achievement.AchievementContext())
        )
        self.enterContext(unittest.mock.patch.object(achievement, "prepare_daily_achievements"))
        self.enterContext(unittest.mock.patch.object(album, "has_ever_got_unit", return_value=False))
        self.enterContext(unittest.mock.patch.object(album, "update"))
        self.enterContext(unittest.mock.patch.object(unit, "is_support_member", return_value=True))
        self.enterContext(
            unittest.mock.patch.object(
                unit, "get_unit_info", return_value=types.SimpleNamespace(rarity=5, attribute_id=1, disable_rank_up=1)
            )
        )
        self.enterContext(
            unittest.mock.patch.object(
                museum,
                "get_museum_info_data",
                return_value=museum.MuseumInfoData(parameter=museum.MuseumParameterData(), contents_id_list=[]),
            )
        )
        self.pool_id = util.java_hash_code(secretbox.DAILY_FRIEND_POOL)
        self.assertIn(self.pool_id, data.get().secretbox_data)

        async def finalize(context: session.SchoolIdolUserParams):
            context.token = session.TokenData(user_id=self.player_id, client_key=b"", server_key=b"")

        self.enterContext(unittest.mock.patch.object(session.SchoolIdolUserParams, "finalize", finalize))
        application = fastapi.FastAPI()
        application.include_router(app.main)
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://test/main.php/secretbox/",
                headers={
                    "Authorize": "consumerKey=lovelive_test",
                    "Client-Version": "59.4",
                    "LANG": "jp",
                    "Platform-Type": "2",
                    "X-Message-Code": "test",
                },
            )
        )

    async def migrate(self):
        def upgrade(connection: sqlalchemy.Connection):
            context = alembic.migration.MigrationContext.configure(connection)
            with alembic.operations.Operations.context(context):
                REVISION.upgrade()

        async with self.engine.begin() as connection:
            await connection.run_sync(upgrade)

    async def list_buttons(self):
        payload = (await self.client.post("all")).json()["response_data"]
        return next(
            page["button_list"]
            for category in payload["member_category_list"]
            for page in category["page_list"]
            if page["secret_box_info"]["secret_box_id"] == self.pool_id
        )

    async def draw(self, button: int = 3, cost: int = 1, *, action: str = "multi", pool_id: int | None = None):
        request = secretbox_game.SecretboxPonRequest(
            id=secretbox.encode_cost_id(self.pool_id, button, cost),
            secret_box_id=self.pool_id if pool_id is None else pool_id,
            unit_type_ids=[],
        )
        return await self.client.post(
            action,
            data={"request_data": request.model_dump_json()},
        )

    async def test_free_and_paid_button_payloads(self):
        await self.migrate()
        free, paid = await self.list_buttons()
        self.assertEqual(free["secret_box_button_type"], const.SECRETBOX_BUTTON_TYPE.FREE_MULTI)
        self.assertEqual(free["show_cost"]["cost_type"], const.SECRETBOX_COST_TYPE.FRIEND)
        self.assertEqual(free["show_cost"]["unit_count"], 11)
        self.assertEqual(
            {key: free["cost_list"][0][key] for key in ("id", "payable", "type", "amount", "unit_count")},
            {
                "id": secretbox.encode_cost_id(self.pool_id, 3, 1),
                "payable": True,
                "type": const.SECRETBOX_COST_TYPE.FREE_TICKET,
                "amount": 0,
                "unit_count": 11,
            },
        )
        self.assertEqual(paid["secret_box_button_type"], const.SECRETBOX_BUTTON_TYPE.NORMAL_MULTI)
        self.assertEqual(paid["cost_list"][0]["amount"], 1000)
        self.assertEqual(paid["cost_list"][0]["type"], const.SECRETBOX_COST_TYPE.FRIEND)
        self.assertTrue(paid["cost_list"][0]["payable"])

        free_draw = (await self.draw()).json()["response_data"]
        self.assertEqual(len(free_draw["secret_box_items"]["unit"]), 11)
        self.assertEqual(free_draw["after_user_info"]["social_point"], 1000)
        self.assertEqual(
            [button["secret_box_button_type"] for button in free_draw["button_list"]],
            [const.SECRETBOX_BUTTON_TYPE.NORMAL_SINGLE, const.SECRETBOX_BUTTON_TYPE.NORMAL_MULTI],
        )
        for response in ((await self.draw(action="pon")), (await self.draw())):
            self.assertEqual(
                response.json()["response_data"]["error_code"],
                error.ERROR_CODE_SECRET_BOX_REMAINING_COST_IS_NOT_ENOUGH,
            )
        paid_draw = (await self.draw(button=2)).json()["response_data"]
        self.assertEqual(len(paid_draw["secret_box_items"]["unit"]), 10)
        self.assertEqual(paid_draw["after_user_info"]["social_point"], 0)
        self.assertFalse(paid_draw["button_list"][1]["cost_list"][0]["payable"])
        async with self.sessions() as database:
            self.assertIsNone(await database.get(main.DestinyScoutAccount, self.player_id))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyScoutLog)))

        self.now = RESET
        self.assertEqual((await self.list_buttons())[0]["cost_list"], free["cost_list"])

    async def test_disabled_and_invalid_costs(self):
        await self.migrate()
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "daily_friend_scout", False):
            self.assertEqual(
                [button["secret_box_button_type"] for button in await self.list_buttons()],
                [const.SECRETBOX_BUTTON_TYPE.NORMAL_SINGLE, const.SECRETBOX_BUTTON_TYPE.NORMAL_MULTI],
            )
            rejected = await self.draw()
        for response in (rejected, await self.draw(button=0), await self.draw(cost=0), await self.draw(pool_id=0)):
            self.assertEqual(
                response.json()["response_data"]["error_code"], error.ERROR_CODE_SECRET_BOX_COST_TYPE_IS_NOT_SPECIFIED
            )

    async def test_concurrent_draw_and_audit_failure_rolls_back_quota_cards_and_cost(self):
        await self.migrate()
        config.CONFIG_DATA.gameplay.scout_audit = True
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TRIGGER reject_audit BEFORE INSERT ON destiny_scout_log "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
            )
        for button in (3, 2):
            with self.assertRaises(sqlalchemy.exc.IntegrityError):
                await self.draw(button=button)
        async with self.sessions() as database:
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DailyFriendScout)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.UnitSupporter)))
            self.assertIsNone(await database.get(main.DestinyScoutAccount, self.player_id))
            self.assertEqual((await database.get_one(main.User, self.player_id)).social_point, 1000)
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER reject_audit")

        responses = await asyncio.gather(self.draw(), self.draw(action="pon"))
        self.assertEqual(sorted(response.json()["status_code"] for response in responses), [200, 600])
        async with self.sessions() as database:
            self.assertEqual(
                await database.scalar(sqlalchemy.select(sqlalchemy.func.sum(main.UnitSupporter.amount))), 11
            )
            scout = (await database.scalars(sqlalchemy.select(main.DestinyScoutLog))).one()
            self.assertEqual(
                (scout.draw_type, scout.draw_count, scout.ssr_count, scout.ur_count), ("free_multi", 11, 11, 0)
            )
            uid = (await database.get_one(main.DestinyScoutAccount, self.player_id)).uid
            self.assertEqual(scout.account_uid, uid)

        self.assertEqual((await self.draw(button=2)).json()["status_code"], 200)
        async with session.BasicSchoolIdolContext() as context:
            await user.delete_user(context, self.player_id)
        async with self.sessions() as database:
            self.assertIsNone(await database.get(main.DestinyScoutAccount, self.player_id))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DailyFriendScout)))
            self.assertEqual(
                list(await database.scalars(sqlalchemy.select(main.DestinyScoutLog.account_uid))), [uid, uid]
            )

    async def test_legacy_quota_and_audit_survive_upgrade(self):
        game_day = util.get_days_since_unix(self.now)
        with contextlib.closing(sqlite3.connect(self.path)) as database, database:
            database.executescript(LEGACY_SCHEMA)
            (uid,) = database.execute(
                "SELECT uid FROM destiny_scout_account WHERE user_id=?", (self.player_id,)
            ).fetchone()
            database.execute("INSERT INTO destiny_daily_friend_scout VALUES (?, ?, ?)", (uid, game_day, self.now))
            database.execute(
                "INSERT INTO destiny_scout_log VALUES (?, ?, ?, 'Kemp', 'legacy', 'free_multi', ?, 11, 0, 0, 10, 1, 0, 0, 0)",
                ("a" * 32, uid, self.player_id, self.pool_id),
            )
        await self.migrate()
        await self.migrate()
        async with self.sessions() as database:
            claim = await database.get_one(main.DailyFriendScout, (self.player_id, game_day))
            self.assertEqual(claim.claimed_at_utc, self.now)

        self.assertEqual(
            (await self.draw()).json()["response_data"]["error_code"],
            error.ERROR_CODE_SECRET_BOX_REMAINING_COST_IS_NOT_ENOUGH,
        )
        config.CONFIG_DATA.gameplay.scout_audit = True
        self.assertEqual((await self.draw(button=2)).json()["status_code"], 200)
        async with self.sessions() as database:
            logs = (await database.scalars(sqlalchemy.select(main.DestinyScoutLog))).all()
            self.assertCountEqual(
                [(log.account_uid, log.draw_type, log.draw_count) for log in logs],
                [(uid, "free_multi", 11), (uid, "friend_points_multi", 10)],
            )
