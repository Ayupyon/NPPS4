import asyncio
import contextlib
import hashlib
import importlib
import json
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
import pydantic
import sqlalchemy
import sqlalchemy.ext.asyncio

import npps4.app.app as app
import npps4.config.config as config
import npps4.db.main as main
import npps4.download.download as download
import npps4.game.live as game_live
import npps4.idol.core as core
import npps4.idol.error as error
import npps4.idol.session as session
import npps4.release_key as release_key
import npps4.system.achievement as achievement
import npps4.system.advanced as advanced
import npps4.system.effort as effort
import npps4.system.item as item
import npps4.system.lbonus as lbonus
import npps4.system.live as live
import npps4.system.live_model as live_model
import npps4.system.museum as museum
import npps4.system.ranking as ranking
import npps4.system.reward as reward
import npps4.system.reward_egg as reward_egg
import npps4.system.unit as unit
import npps4.system.user as user
import npps4.util as util

from typing import Any

REVISION = importlib.import_module("npps4.alembic.versions.2026_11_12_0300-c84df2e71a5b_reward_eggs")
POLICY_FILE = pathlib.Path(config.ROOT_DIR) / "resources/events/reward_egg_policy.json"
LEGACY_SCHEMA = """
CREATE TABLE destiny_reward_egg (
  user_id INTEGER PRIMARY KEY REFERENCES user(id) ON DELETE CASCADE,
  pending_event INTEGER NOT NULL DEFAULT 0, pending_spec INTEGER NOT NULL DEFAULT 0,
  saved_spec INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
  last_choice INTEGER, openings INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE destiny_reward_egg_receipt (
  user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  action TEXT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, response TEXT NOT NULL,
  PRIMARY KEY(user_id,action,request_key)
);
CREATE TABLE destiny_reward_egg_live (
  user_id INTEGER PRIMARY KEY REFERENCES user(id) ON DELETE CASCADE,
  live_in_progress_id INTEGER NOT NULL, live_difficulty_id INTEGER NOT NULL
);
"""


class RewardEggTest(unittest.IsolatedAsyncioTestCase):
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
        async with self.engine.begin() as connection:
            metadata = main.User.metadata
            await connection.run_sync(
                metadata.create_all,
                tables=[table for table in metadata.sorted_tables if not table.name.startswith("destiny_reward_egg")],
            )
        async with self.sessions.begin() as database:
            player = main.User(current_live_effort_point=51)
            database.add(player)
            await database.flush()
            self.player_id = player.id

        self.policy = reward_egg.Policy.model_validate_json(POLICY_FILE.read_bytes())
        self.policy.custom_offer_probability = 1
        self.policy.eggs[1].rewards[1] = [reward_egg.WeightedReward(type="SOCIAL_POINT", id=2, amount=1000, weight=100)]
        self.enterContext(unittest.mock.patch.object(reward_egg, "get_policy", return_value=self.policy))
        self.enterContext(
            unittest.mock.patch.object(
                effort, "get_effort_spec", return_value=types.SimpleNamespace(capacity=500000, limited_capacity=200000)
            )
        )
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(unittest.mock.patch.object(session, "try_cleanup_tokens"))
        self.enterContext(unittest.mock.patch.object(reward, "try_cleanup_incentive"))
        self.enterContext(unittest.mock.patch.object(item, "get_recovery_items", return_value=[]))
        self.enterContext(unittest.mock.patch.object(item, "get_item_category_for_type_1000", return_value=42))
        self.enterContext(
            unittest.mock.patch.object(achievement, "check", return_value=achievement.AchievementContext())
        )
        self.clock = self.enterContext(unittest.mock.patch.object(util, "time", return_value=1794254400))
        self.enterContext(
            unittest.mock.patch.object(
                museum,
                "get_museum_info_data",
                return_value=museum.MuseumInfoData(parameter=museum.MuseumParameterData(), contents_id_list=[]),
            )
        )
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "master_challenge_daily_limit", None))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "marathon_policy", None))

        async def calendar(context: session.BasicSchoolIdolContext, year: int, month: int):
            return [
                lbonus.LoginBonusCalendar(day=day, day_of_the_week=0, received=False, item=item.game_coin(10))
                for day in range(1, 32)
            ]

        async def finalize(context: session.SchoolIdolUserParams):
            context.token = session.TokenData(user_id=self.player_id, client_key=b"", server_key=b"")

        self.enterContext(unittest.mock.patch.object(lbonus, "get_calendar", calendar))
        self.enterContext(unittest.mock.patch.object(session.SchoolIdolUserParams, "finalize", finalize))
        application = fastapi.FastAPI()
        application.include_router(app.main)
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://test/main.php/",
                headers={
                    "Authorize": "consumerKey=lovelive_test",
                    "Client-Version": "59.4",
                    "LANG": "jp",
                    "Platform-Type": "2",
                    "X-Message-Code": "test",
                },
            )
        )

    async def migrate(self, operation: str = "upgrade"):
        def apply(connection: sqlalchemy.Connection):
            context = alembic.migration.MigrationContext.configure(connection)
            with alembic.operations.Operations.context(context):
                getattr(REVISION, operation)()

        async with self.engine.begin() as connection:
            await connection.run_sync(apply)

    async def post(self, route: str, body: dict[str, Any] | list[dict[str, Any]]):
        return await self.client.post(route, data={"request_data": json.dumps(body)})

    def live_body(self, *, command: int, difficulty: int = 6):
        return {
            "timeStamp": 10,
            "commandNum": command,
            "live_difficulty_id": difficulty,
            "is_training": False,
            "perfect_cnt": 1,
            "great_cnt": 0,
            "good_cnt": 0,
            "bad_cnt": 0,
            "miss_cnt": 0,
            "remain_hp": 10,
            "max_combo": 1,
            "score_smile": 100,
            "score_cute": 0,
            "score_cool": 0,
            "love_cnt": 0,
            "precise_score_log": {"live_setting": {"precise_score_auto_update_flag": False}},
            "event_point": 0,
            "event_id": None,
        }

    def stub_live_resources(self):
        setting = types.SimpleNamespace(
            live_setting_id=1,
            live_track_id=1,
            difficulty=4,
            attribute_icon_id=1,
            ac_flag=0,
            swing_flag=0,
            c_rank_score=101,
            b_rank_score=200,
            a_rank_score=300,
            s_rank_score=400,
            c_rank_combo=2,
        )
        notes = [
            live_model.LiveNote(timing_sec=1, notes_attribute=1, notes_level=1, effect=1, effect_value=0, position=1)
        ]
        stats = advanced.LiveDeckStats()
        deck = advanced.LiveDeckInfo(
            unit_deck_id=1,
            total_smile=0,
            total_cute=0,
            total_cool=0,
            total_hp=1,
            total_status=stats,
            center_bonus=stats,
            si_bonus=stats,
            unit_list=[],
        )
        calculator = self.enterContext(unittest.mock.patch.object(advanced, "TeamStatCalculator"))
        calculator.return_value.get_live_stats = unittest.mock.AsyncMock(return_value=deck)
        self.enterContext(unittest.mock.patch.object(live, "get_live_setting_from_difficulty_id", return_value=setting))
        self.enterContext(
            unittest.mock.patch.object(
                live, "get_live_info_table", return_value=types.SimpleNamespace(capital_type=1, capital_value=0)
            )
        )
        self.enterContext(
            unittest.mock.patch.object(
                live, "get_live_info", return_value=live_model.LiveInfoWithNotes(live_difficulty_id=6, notes_list=notes)
            )
        )
        self.enterContext(unittest.mock.patch.object(live, "get_achieved_goal_id_list", return_value=[]))
        self.enterContext(unittest.mock.patch.object(live, "get_goal_rewards", return_value=[]))
        self.enterContext(unittest.mock.patch.object(live, "get_live_ranks", return_value=(5, 5, 5)))
        self.enterContext(
            unittest.mock.patch.object(
                live, "get_live_track_info", return_value=types.SimpleNamespace(member_category=1)
            )
        )
        self.enterContext(unittest.mock.patch.object(unit, "load_unit_deck", return_value=(None, [])))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit_center", return_value=1))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit", return_value=None))
        self.enterContext(unittest.mock.patch.object(unit, "count_units", return_value=0))
        self.enterContext(unittest.mock.patch.object(unit, "process_quick_add", return_value=0))
        self.enterContext(
            unittest.mock.patch.object(unit, "add_love_by_deck", return_value=types.SimpleNamespace(before=[]))
        )
        self.enterContext(
            unittest.mock.patch.object(unit, "tupleize_unit_id_and_type", return_value=((1,) * 9, (1,) * 9))
        )
        self.enterContext(
            unittest.mock.patch.object(
                unit, "quick_create_by_unit_add", return_value=types.SimpleNamespace(as_item_reward=item.game_coin(1))
            )
        )
        self.enterContext(
            unittest.mock.patch.object(
                config,
                "get_live_unit_drop_protocol",
                return_value=types.SimpleNamespace(get_live_drop_unit=unittest.mock.AsyncMock(return_value=1)),
            )
        )
        self.enterContext(unittest.mock.patch.object(ranking, "try_cleanup_daily_rankings"))

    async def test_live_offer_login_progress_selection_and_exact_retry(self):
        await self.migrate()
        self.stub_live_resources()
        playing = await self.post(
            "live/play",
            {
                "live_difficulty_id": 6,
                "unit_deck_id": 1,
                "party_user_id": self.player_id,
                "lp_factor": 1,
                "is_training": False,
            },
        )
        self.assertEqual(playing.json()["status_code"], 200)
        async with self.sessions() as database:
            binding = await database.get_one(main.DestinyRewardEggLive, self.player_id)
            self.assertEqual(binding.live_difficulty_id, 6)
        live_response = (await self.post("live/reward", self.live_body(command=1))).json()["response_data"]
        self.assertEqual(live_response["limited_effort_box"]["limited_effort_event_id"], 1)
        async with self.sessions() as database:
            self.assertIsNone(await database.get(main.DestinyRewardEggLive, self.player_id))

        login_body = {"timeStamp": 10, "commandNum": 1}
        login = (await self.post("lbonus/execute", login_body)).json()["response_data"]
        self.assertEqual(
            login["limited_effort_box"],
            {"limited_effort_event_id": 1, "live_effort_point_box_spec_id": 1, "capacity": 200000},
        )
        self.assertEqual(login["effort_point"][0]["after"], 100151)
        self.assertNotIn("limited_effort_event_id", login["effort_point"][0])
        select = {"timeStamp": 10, "commandNum": 2, "no_agree": 0}
        for _ in range(2):
            self.assertEqual((await self.post("effortPoint/selectLimitedBox", select)).json()["response_data"], [])
        conflict = await self.post("effortPoint/selectLimitedBox", select | {"no_agree": 1})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((await self.post("lbonus/execute", login_body)).json()["response_data"], login)

        async with self.sessions.begin() as database:
            player = await database.get_one(main.User, self.player_id)
            self.assertEqual((player.current_live_effort_point, player.limited_effort_event_id), (100151, 1))
            player.current_limited_effort_point = 149999

        self.clock.return_value += 86400
        opening_body = login_body | {"commandNum": 3}
        responses = await asyncio.gather(
            self.post("lbonus/execute", opening_body), self.post("lbonus/execute", opening_body)
        )
        first, replay = (response.json()["response_data"] for response in responses)
        self.assertEqual(first, replay)
        self.assertEqual(first["limited_effort_box"], {})
        egg, restored = first["effort_point"]
        self.assertEqual((egg["limited_effort_event_id"], egg["before"], egg["after"]), (1, 149999, 200000))
        self.assertEqual(egg["rewards"][0]["amount"], 1000)
        self.assertEqual((restored["before"], restored["after"]), (100151, 150150))
        self.assertNotIn("limited_effort_event_id", restored)
        async with self.sessions() as database:
            player = await database.get_one(main.User, self.player_id)
            state = await database.get_one(main.DestinyRewardEgg, self.player_id)
            self.assertEqual(
                (player.social_point, player.current_live_effort_point, player.limited_effort_event_id),
                (1000, 150150, 0),
            )
            self.assertEqual((state.saved_spec, state.openings, state.generation, state.last_choice), (0, 1, 1, 0))
            self.assertEqual(
                await database.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(main.LoginBonus)), 2
            )

    async def test_failed_login_grant_preserves_active_egg_and_no_receipt(self):
        await self.migrate()
        self.policy.eggs[1].rewards[2] = [reward_egg.WeightedReward(type="ITEM", id=1000, amount=5, weight=100)]
        async with self.sessions.begin() as database:
            player = await database.get_one(main.User, self.player_id)
            player.limited_effort_event_id = 1
            player.live_effort_point_box_spec_id = 2
            player.current_limited_effort_point = 100000
            database.add(main.DestinyRewardEgg(user_id=player.id, saved_spec=1))
        body = {"timeStamp": 10, "commandNum": 1}
        with (
            unittest.mock.patch.object(advanced, "add_item", side_effect=RuntimeError("grant failed")),
            self.assertRaisesRegex(RuntimeError, "grant failed"),
        ):
            await self.post("lbonus/execute", body)
        async with self.sessions() as database:
            player = await database.get_one(main.User, self.player_id)
            state = await database.get_one(main.DestinyRewardEgg, self.player_id)
            self.assertEqual((player.current_limited_effort_point, state.openings, player.social_point), (100000, 0, 0))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.LoginBonus)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.Incentive)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyRewardEggReceipt)))

        opening = (await self.post("lbonus/execute", body)).json()["response_data"]
        egg, restored = opening["effort_point"]
        self.assertEqual((egg["before"], egg["after"]), (100000, 200000))
        self.assertEqual((restored["before"], restored["after"]), (51, 51))
        self.assertEqual((egg["live_effort_point_box_spec_id"], restored["live_effort_point_box_spec_id"]), (2, 1))
        self.assertEqual((egg["rewards"][0]["item_id"], egg["rewards"][0]["item_category_id"]), (1000, 42))
        async with self.sessions() as database:
            inventory = (await database.scalars(sqlalchemy.select(main.Item))).one()
            self.assertEqual((inventory.item_id, inventory.amount), (1000, 5))

    async def test_adopt_legacy_receipts_and_binding(self):
        old_body = self.live_body(command=1)
        old_wire = {"server_timestamp": 9, "event_info": {"event_id": 7}, "limited_effort_box": {"capacity": 200000}}
        digest = hashlib.sha256(json.dumps(old_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.sessions.begin() as database:
            playing = main.LiveInProgress(
                user_id=self.player_id, party_user_id=self.player_id, lp_factor=1, unit_deck_id=1, deck_data=b"{}"
            )
            database.add(playing)
            await database.flush()
            playing_id = playing.id
        with contextlib.closing(sqlite3.connect(self.path)) as database, database:
            database.executescript(LEGACY_SCHEMA)
            database.execute("INSERT INTO destiny_reward_egg VALUES (?, 1, 2, 0, 7, NULL, 3)", (self.player_id,))
            database.execute("INSERT INTO destiny_reward_egg_live VALUES (?, ?, 6)", (self.player_id, playing_id))
            database.execute(
                "INSERT INTO destiny_reward_egg_receipt VALUES (?, 'live', '10:1', ?, ?)",
                (self.player_id, digest, json.dumps(old_wire)),
            )
        await self.migrate()
        await self.migrate("downgrade")
        await self.migrate()

        self.assertEqual((await self.post("live/reward", old_body)).json()["response_data"], old_wire)
        conflict = await self.post("live/reward", old_body | {"score_smile": 101})
        self.assertEqual(conflict.status_code, 409)
        wrong_song = await self.post("live/reward", self.live_body(command=2, difficulty=7))
        self.assertEqual(wrong_song.status_code, 409)
        self.assertIn("validated Live start", wrong_song.json()["response_data"]["detail"])
        async with session.BasicSchoolIdolContext() as context:
            player = await context.db.main.get_one(main.User, self.player_id)
            playing = await live.get_live_in_progress(context, player)
            assert playing is not None
            self.assertTrue(await reward_egg.verify_live_binding(context, playing, 6))
            state = await reward_egg.get_state(context, self.player_id)
            assert state is not None
            self.assertEqual((state.pending_spec, state.generation, state.openings), (2, 7, 3))
            await live.clean_live_in_progress(context, player)

        async with self.sessions() as database:
            self.assertIsNone(await database.get(main.DestinyRewardEggLive, self.player_id))
            self.assertIsNotNone(await database.get(main.DestinyRewardEggReceipt, (self.player_id, "live", "10:1")))
        async with session.BasicSchoolIdolContext() as context:
            await user.delete_user(context, self.player_id)
        async with self.sessions() as database:
            self.assertIsNone(await database.get(main.DestinyRewardEgg, self.player_id))
            self.assertIsNone(await database.get(main.DestinyRewardEggReceipt, (self.player_id, "live", "10:1")))

    async def test_native_live_receipt_keeps_settlement_extension_fields(self):
        await self.migrate()
        wire = reward_egg.WireResponse({"event_info": {"event_id": 7}, "limited_effort_box": {}})
        body = self.live_body(command=1)
        with unittest.mock.patch.object(game_live, "settle_live_reward", return_value=wire) as operation:
            first = await self.post("live/reward", body)
            replay = await self.post("live/reward", body)
        self.assertEqual(first.json()["response_data"], wire.root)
        self.assertEqual(replay.json()["response_data"], wire.root)
        operation.assert_awaited_once()

    async def test_failed_batch_settlement_rolls_back_before_returning_error(self):
        await self.migrate()

        async def fail(context: session.SchoolIdolUserParams, request: game_live.LiveRewardRequest):
            player = await context.db.main.get_one(main.User, self.player_id)
            player.social_point += 1000
            await context.db.main.flush()
            raise error.IdolError(detail="settlement failed", http_code=409)

        body = self.live_body(command=1) | {"module": "live", "action": "reward"}
        with unittest.mock.patch.object(game_live, "settle_live_reward", side_effect=fail):
            response = await self.post("api", [body])
        self.assertEqual(response.json()["response_data"][0]["result"]["detail"], "settlement failed")
        async with self.sessions() as database:
            player = await database.get_one(main.User, self.player_id)
            self.assertEqual(player.social_point, 0)
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyRewardEggReceipt)))

    async def test_default_login_keeps_regular_effort_response(self):
        await self.migrate()
        with unittest.mock.patch.object(reward_egg, "get_policy", return_value=None):
            response = await self.client.post("lbonus/execute")
        body = response.json()["response_data"]
        self.assertEqual(body["limited_effort_box"], [])
        self.assertEqual(
            body["effort_point"],
            [{"before": 51, "after": 100051, "live_effort_point_box_spec_id": 1, "capacity": 500000, "rewards": []}],
        )
        async with self.sessions() as database:
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyRewardEggReceipt)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyRewardEgg)))

    def test_policy_file_and_weighted_fallback(self):
        partner = self.policy.eggs[3].member_type_id
        self.assertEqual(reward_egg.offer_weights(self.policy, partner, partner, 2), {3: 80, 2: 20})
        self.assertEqual(reward_egg.offer_weights(self.policy, None, partner, None), {3: 40, 1: 30, 2: 30})
        invalid = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        invalid["eggs"]["3"]["rewards"]["1"][0]["weight"] = -1
        with self.assertRaises(pydantic.ValidationError):
            reward_egg.Policy.model_validate(invalid)


class RewardEggPolicyTest(unittest.TestCase):
    def test_loads_opt_in_policy_with_configured_client_databases(self):
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        policy = reward_egg.Policy.model_validate_json(POLICY_FILE.read_bytes())
        awards: set[int] = set()
        backgrounds: set[int] = set()
        items: set[int] = set()
        supporters: set[int] = set()
        skills: set[tuple[int, int | None]] = set()
        for egg in policy.eggs.values():
            for entries in egg.rewards.values():
                for entry in entries:
                    if entry.requires_award is not None:
                        awards.add(entry.requires_award)
                    match entry.type:
                        case "AWARD":
                            awards.add(entry.id)
                        case "BACKGROUND":
                            backgrounds.add(entry.id)
                        case "ITEM":
                            items.add(entry.id)
                        case "UNIT":
                            supporters.add(entry.id)
                        case "SCHOOL_IDOL_SKILL":
                            skills.add((entry.id, egg.member_type_id if entry.category == "personal_gem" else None))
        with contextlib.closing(sqlite3.connect(directory / "effort.db_")) as database, database:
            database.execute(
                "CREATE TABLE limited_effort_box_asset_m (limited_effort_event_id, live_effort_point_box_spec_id)"
            )
            database.executemany(
                "INSERT INTO limited_effort_box_asset_m VALUES (?, ?)",
                [(event, tier) for event, egg in policy.eggs.items() for tier in egg.rewards],
            )
        with contextlib.closing(sqlite3.connect(directory / "item.db_")) as database, database:
            database.executescript(
                "CREATE TABLE award_m (award_id); CREATE TABLE background_m (background_id); CREATE TABLE kg_item_m (item_id);"
            )
            database.executemany("INSERT INTO award_m VALUES (?)", [(award_id,) for award_id in awards])
            database.executemany(
                "INSERT INTO background_m VALUES (?)", [(background_id,) for background_id in backgrounds]
            )
            database.executemany("INSERT INTO kg_item_m VALUES (?)", [(item_id,) for item_id in items])
        with contextlib.closing(sqlite3.connect(directory / "unit.db_")) as database, database:
            database.executescript(
                "CREATE TABLE unit_m (unit_id, disable_rank_up); "
                "CREATE TABLE unit_removable_skill_m (unit_removable_skill_id, target_type, target_reference_type, size, effect_value);"
            )
            database.executemany("INSERT INTO unit_m VALUES (?, 1)", [(unit_id,) for unit_id in supporters])
            database.executemany("INSERT INTO unit_removable_skill_m VALUES (?, ?, 2, 4, 29)", skills)
        self.addCleanup(reward_egg._load_policy.cache_clear)
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "reward_egg_policy", None):
            self.assertIsNone(reward_egg.get_policy())
        with (
            unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "reward_egg_policy", str(POLICY_FILE)),
            unittest.mock.patch.object(
                download, "get_db_path", side_effect=lambda name: str(directory / f"{name}.db_")
            ),
        ):
            self.assertEqual(reward_egg.get_policy(), policy)
            reward_egg._load_policy.cache_clear()
            with contextlib.closing(sqlite3.connect(directory / "effort.db_")) as database, database:
                database.execute("DELETE FROM limited_effort_box_asset_m WHERE limited_effort_event_id = 20")
            with self.assertRaisesRegex(ValueError, "Missing reward egg assets"):
                reward_egg.get_policy()
