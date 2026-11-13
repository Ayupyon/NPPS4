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
import sqlalchemy
import sqlalchemy.ext.asyncio

import npps4.app.app as app
import npps4.config.config as config
import npps4.db.main as main
import npps4.db.marathon as schema
import npps4.download.download as download
import npps4.game.live as game_live
import npps4.idol.core as core
import npps4.idol.session as session
import npps4.release_key as release_key
import npps4.system.advanced as advanced
import npps4.system.event_charts as event_charts
import npps4.system.event_home as event_home
import npps4.system.event_song_goal as event_song_goal
import npps4.system.item as item
import npps4.system.live as live
import npps4.system.live_model as live_model
import npps4.system.marathon as marathon
import npps4.system.reward_egg as reward_egg
import npps4.system.user as user
import npps4.util as util

from typing import Any

REVISION = importlib.import_module("npps4.alembic.versions.2026_11_12_0500-f65d1c7a52e0_marathon")
RULE_FILE = pathlib.Path(config.ROOT_DIR) / "resources/events/marathon_rules.json"
LEGACY_SCHEMA = """
CREATE TABLE sif005_live(
 user_id INTEGER PRIMARY KEY REFERENCES user(id) ON DELETE CASCADE, period_id INTEGER,
 difficulty_id INTEGER NOT NULL, party_user_id INTEGER NOT NULL, training INTEGER NOT NULL,
 is_event_song INTEGER NOT NULL, factor INTEGER NOT NULL, cost INTEGER NOT NULL,
 started_at INTEGER NOT NULL, rule_json TEXT, cheer_percent INTEGER NOT NULL,
 cheer_snapshot TEXT NOT NULL, notes_json TEXT NOT NULL, token_indices TEXT NOT NULL,
 play_key TEXT NOT NULL, play_hash TEXT NOT NULL);
CREATE TABLE sif005_receipt(
 user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE, action TEXT NOT NULL,
 request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, response TEXT NOT NULL,
 PRIMARY KEY(user_id,action,request_key));
"""


class MarathonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(directory) / "main.sqlite3"
        with contextlib.closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        self.engine = sqlalchemy.ext.asyncio.create_async_engine(
            f"sqlite+aiosqlite:///{self.path}", connect_args={"autocommit": False}
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = sqlalchemy.ext.asyncio.async_sessionmaker(self.engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", self.sessions))
        async with self.engine.begin() as connection:
            metadata = main.User.metadata
            await connection.run_sync(
                metadata.create_all,
                tables=[table for table in metadata.sorted_tables if table not in schema.tables],
            )
        async with self.sessions.begin() as database:
            player = main.User(game_coin=20)
            database.add(player)
            database.add(main.DestinyFriendMutex(id=1, revision=0))
            await database.flush()
            self.player_id = player.id

        data = json.loads(RULE_FILE.read_bytes())
        self.rule = next(rule for rule in data["periods"] if rule["key"] == data["selected_period_key"])
        self.rule["point_rewards"] = [
            {
                "id": "first-event-points",
                "points": 4,
                "delivery": "direct",
                "items": [{"type": "GAME_COIN", "id": 3, "amount": 5}],
            }
        ]
        self.rule["score_rank_rewards"] = []
        self.rule["point_rank_rewards"] = []
        self.song = self.rule["songs"][-1]
        self.difficulty_id = self.song["id"]
        self.time = self.enterContext(
            unittest.mock.patch.object(util, "time", return_value=marathon.timestamp(self.rule["start"]) + 5)
        )
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "marathon_policy", str(RULE_FILE)))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "master_challenge_daily_limit", None))
        self.policy = self.enterContext(
            unittest.mock.patch.object(marathon, "get_policy", return_value={"periods": [self.rule]})
        )
        self.enterContext(unittest.mock.patch.object(reward_egg, "get_policy", return_value=None))
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(unittest.mock.patch.object(session, "try_cleanup_tokens"))
        self.enterContext(unittest.mock.patch.object(item, "get_recovery_items", return_value=[]))
        self.enterContext(unittest.mock.patch.object(live, "get_special_live_rotation_difficulty_id", return_value={}))
        self.setting = types.SimpleNamespace(
            live_setting_id=self.song["setting_id"],
            difficulty=4,
            ac_flag=0,
            swing_flag=0,
            c_rank_score=10,
            b_rank_score=20,
            a_rank_score=30,
            s_rank_score=40,
            c_rank_combo=1,
            b_rank_combo=2,
            a_rank_combo=3,
            s_rank_combo=4,
        )
        self.enterContext(unittest.mock.patch.object(live, "get_live_setting", return_value=self.setting))
        self.goal = {
            "setting_id": self.song["setting_id"],
            "goals": [
                {
                    "live_goal_reward_id": 42,
                    "live_goal_type": 3,
                    "rank": 4,
                    "add_type": 1000,
                    "item_id": 3,
                    "item_category_id": 0,
                    "amount": 15,
                }
            ],
        }
        self.enterContext(unittest.mock.patch.object(event_song_goal, "definition", return_value=self.goal))
        self.enterContext(
            unittest.mock.patch.object(
                marathon, "rank_profile", return_value={"user_data": {"user_id": self.player_id}}
            )
        )
        self.note = live_model.LiveNote(
            timing_sec=1, notes_attribute=1, notes_level=1, effect=1, effect_value=0, position=1
        )

        async def finalize(context: session.SchoolIdolUserParams) -> None:
            context.token = session.TokenData(user_id=self.player_id, client_key=b"", server_key=b"")

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
        self.base_settlement = self.enterContext(
            unittest.mock.patch.object(game_live, "settle_live_reward", side_effect=self.settle_base)
        )

    async def migrate(self, operation: str = "upgrade") -> None:
        def apply(connection: sqlalchemy.Connection) -> None:
            context = alembic.migration.MigrationContext.configure(connection)
            with alembic.operations.Operations.context(context):
                getattr(REVISION, operation)()

        async with self.engine.begin() as connection:
            await connection.run_sync(apply)

    async def post(self, route: str, body: dict[str, Any] | list[dict[str, Any]] | None = None):
        response = await self.client.post(route, data={"request_data": json.dumps(body or {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def seed_period(self) -> int:
        async with session.BasicSchoolIdolContext() as context:
            await marathon.lock(context)
            record = await marathon.register_period(context, self.rule)
            await context.db.main.execute(
                schema.progress.insert().values(period_id=record["id"], user_id=self.player_id, tokens=10000, points=3)
            )
            return record["id"]

    def reward_body(self, command: str = "unit.test.1") -> dict[str, Any]:
        return {
            "timeStamp": 100,
            "commandNum": command,
            "live_difficulty_id": self.difficulty_id,
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
            "event_point": 0,
            "event_id": self.rule["event_id"],
            "precise_score_log": {"live_setting": {"precise_score_auto_update_flag": False}},
        }

    async def settle_base(
        self, context: session.SchoolIdolUserParams, request: game_live.LiveRewardRequest
    ) -> marathon.Wire:
        current = await user.get_current(context)
        clear = await live.get_live_clear_data(context, current, request.live_difficulty_id, True)
        clear.clear_cnt += 1
        clear.hi_combo_cnt = request.max_combo
        clear.hi_score = request.score_smile
        current.game_coin += 10
        setting = await live.get_live_setting_from_difficulty_id(context, request.live_difficulty_id)
        assert setting is not None
        beatmap = await live.get_live_info(context, request.live_difficulty_id, setting)
        assert beatmap is not None
        self.assertEqual(beatmap.notes_list, [self.note])
        self.assertEqual(await live.get_achieved_goal_id_list(context, clear), [])
        await live.clean_live_in_progress(context, current)
        return marathon.Wire({"goal_accomp_info": {"achieved_ids": [], "rewards": []}})

    async def seed_run(self, period_id: int) -> None:
        async with self.sessions.begin() as database:
            database.add(
                main.LiveInProgress(
                    user_id=self.player_id,
                    party_user_id=self.player_id,
                    lp_factor=2,
                    unit_deck_id=1,
                    deck_data=b'{"unit_list":[]}',
                )
            )
            await database.execute(
                schema.live.insert().values(
                    user_id=self.player_id,
                    period_id=period_id,
                    difficulty_id=self.difficulty_id,
                    party_user_id=self.player_id,
                    training=0,
                    is_event_song=1,
                    factor=2,
                    cost=self.song["cost"] * 2,
                    started_at=self.time.return_value,
                    rule_json=marathon.dump(self.rule),
                    cheer_percent=100,
                    cheer_snapshot="[]",
                    notes_json=marathon.dump([self.note.model_dump()]),
                    token_indices="[]",
                    play_key="100:old.play",
                    play_hash="a" * 64,
                )
            )

    async def test_legacy_snapshot_settles_with_policy_off_and_replays(self):
        with contextlib.closing(sqlite3.connect(self.path)) as database, database:
            database.executescript(LEGACY_SCHEMA)
        await self.migrate()
        period_id = await self.seed_period()
        await self.seed_run(period_id)
        old = self.reward_body("old.reward")
        async with self.sessions.begin() as database:
            await database.execute(
                schema.receipt.insert().values(
                    user_id=self.player_id,
                    action="reward",
                    request_key="100:old.reward",
                    payload_hash=hashlib.sha256(marathon.dump(old).encode()).hexdigest(),
                    response=marathon.dump({"event_info": {"legacy": True}}),
                )
            )
            old_egg = self.reward_body("old.egg")
            database.add(
                main.DestinyRewardEggReceipt(
                    user_id=self.player_id,
                    action="live",
                    request_key="100:old.egg",
                    payload_hash=hashlib.sha256(
                        json.dumps(old_egg, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    response=marathon.dump({"event_info": {"legacy_egg": True}}),
                )
            )
        await self.migrate("downgrade")
        await self.migrate()

        config.CONFIG_DATA.gameplay.marathon_policy = None
        self.policy.return_value = {"periods": []}
        self.assertEqual((await self.post("live/reward", old))["response_data"], {"event_info": {"legacy": True}})
        self.assertEqual(
            (await self.post("live/reward", old_egg))["response_data"], {"event_info": {"legacy_egg": True}}
        )
        first = (await self.post("live/reward", self.reward_body()))["response_data"]
        self.assertEqual(first["event_info"]["event_point_info"]["base_event_point"], self.song["base_point"] * 2)
        self.assertEqual(first["event_info"]["event_point_info"]["after_event_point"], 10000)
        self.assertEqual(first["goal_accomp_info"]["achieved_ids"], [42])
        self.assertEqual(first["event_info"]["event_reward_info"][0]["required_event_point"], 4)
        self.assertEqual((await self.post("live/reward", self.reward_body()))["response_data"], first)
        self.base_settlement.assert_awaited_once()
        async with self.sessions() as database:
            persisted = (await database.execute(sqlalchemy.select(schema.progress))).mappings().one()
            self.assertEqual((persisted["clears"], persisted["event_clears"], persisted["score"]), (1, 1, 100))
            self.assertGreater(persisted["points"], 3)
            self.assertEqual(await database.scalar(sqlalchemy.select(schema.song_goal.c.goal_id)), 42)
            self.assertEqual((await database.get_one(main.User, self.player_id)).game_coin, 50)
            self.assertIsNone(await database.scalar(sqlalchemy.select(schema.live.c.user_id)))
            storage = await database.scalar(sqlalchemy.select(main.LiveClear.live_difficulty_id))
            self.assertEqual(storage, -(period_id * 100000 + self.difficulty_id))

        config.CONFIG_DATA.gameplay.marathon_policy = str(RULE_FILE)
        self.policy.return_value = {"periods": [self.rule]}
        self.time.return_value = marathon.timestamp(self.rule["end"]) + self.rule["settlement_grace_seconds"]
        rank = (await self.post("ranking/eventLive", {"event_id": self.rule["event_id"]}))["response_data"]
        self.assertEqual(rank["items"][0]["score"], 100)
        async with self.sessions() as database:
            self.assertEqual(
                await database.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(schema.ranking_snapshot)),
                2,
            )

    async def test_failed_batch_without_eggs_rolls_back_base_rewards_and_run(self):
        await self.migrate()
        await self.seed_run(await self.seed_period())
        with unittest.mock.patch.object(event_song_goal, "settle", side_effect=RuntimeError("grant failed")):
            failed = await self.post("api", [dict(self.reward_body(), module="live", action="reward")])
        self.assertNotEqual(failed["response_data"][0]["status"], 200)
        async with self.sessions() as database:
            self.assertEqual((await database.get_one(main.User, self.player_id)).game_coin, 20)
            self.assertIsNotNone(await database.scalar(sqlalchemy.select(schema.live.c.user_id)))
            self.assertIsNotNone(await database.scalar(sqlalchemy.select(main.LiveInProgress.id)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(schema.receipt.c.response)))
        settled = (await self.post("live/reward", self.reward_body()))["response_data"]
        self.assertEqual(settled["goal_accomp_info"]["achieved_ids"], [42])

    async def test_extra_chart_requires_ack_and_existing_live_keeps_issued_rule(self):
        await self.migrate()
        await self.seed_period()
        revisions, _ = event_charts.revisions([self.rule])
        configured = json.loads((RULE_FILE.parent / "event_extra_charts.json").read_bytes())["active"][self.rule["key"]]
        extra = revisions[configured]["songs"][0]
        self.difficulty_id = extra["id"]
        body = {
            "timeStamp": 100,
            "commandNum": "unit.play.1",
            "party_user_id": self.player_id,
            "is_training": False,
            "unit_deck_id": 1,
            "live_difficulty_id": self.difficulty_id,
            "lp_factor": 2,
        }
        self.assertNotIn(
            extra["id"],
            [row["live_difficulty_id"] for row in (await self.post("live/schedule"))["response_data"]["live_list"]],
        )

        async def play_base(context: session.SchoolIdolUserParams, request: game_live.LivePlayRequest):
            current = await user.get_current(context)
            info = await live.get_live_info_table(context, request.live_difficulty_id)
            assert info is not None
            self.assertEqual((info.capital_type, info.capital_value), (1, 0))
            deck = advanced.LiveDeckStats()
            await live.register_live_in_progress(
                context, current, current, request.lp_factor, request.unit_deck_id, b'{"unit_list":[]}'
            )
            return game_live.LivePlayResponse(
                rank_info=[],
                energy_full_time="",
                over_max_energy=0,
                live_list=[
                    game_live.LivePlayList(
                        live_info=live_model.LiveInfoWithNotes(
                            live_difficulty_id=request.live_difficulty_id, notes_list=[self.note]
                        ),
                        deck_info=advanced.LiveDeckInfo(
                            unit_deck_id=1,
                            total_smile=0,
                            total_cute=0,
                            total_cool=0,
                            total_hp=1,
                            total_status=deck,
                            center_bonus=deck,
                            si_bonus=deck,
                            unit_list=[],
                        ),
                    )
                ],
            )

        with (
            unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "event_client_resources", True),
            unittest.mock.patch.object(event_home, "extra_ready", return_value=True),
            unittest.mock.patch.object(game_live, "play_live", side_effect=play_base) as play,
        ):
            schedule = (await self.post("live/schedule"))["response_data"]
            self.assertIn(extra["id"], [row["live_difficulty_id"] for row in schedule["live_list"]])
            issued = (await self.post("live/play", body))["response_data"]
            self.assertEqual(issued["marathon_event_id"], self.rule["event_id"])
            self.assertEqual((await self.post("live/play", body))["response_data"], issued)
            play.assert_awaited_once()
        async with self.sessions() as database:
            self.assertEqual(await database.scalar(sqlalchemy.select(schema.live.c.cost)), extra["cost"] * 2)
            self.assertEqual(
                await database.scalar(sqlalchemy.select(schema.progress.c.tokens)), 10000 - extra["cost"] * 2
            )
        settled = (await self.post("live/reward", self.reward_body()))["response_data"]
        self.assertEqual(settled["event_info"]["event_point_info"]["base_event_point"], extra["base_point"] * 2)

    async def test_adopt_paid_goal_and_frozen_ranks_without_rewriting_published_rules(self):
        await self.migrate()
        period_id = await self.seed_period()
        async with self.sessions.begin() as database:
            await database.execute(schema.period.update().where(schema.period.c.id == period_id).values(frozen_at=10))
            for kind, score in (("point", 2500), ("score", 30000)):
                await database.execute(
                    schema.ranking_snapshot.insert().values(
                        period_id=period_id,
                        kind=kind,
                        rank=1,
                        user_id=self.player_id,
                        score=score,
                        profile=marathon.dump({"user_data": {"user_id": self.player_id, "name": "Archived"}}),
                    )
                )
            storage_id = -(period_id * 100000 + self.difficulty_id)
            database.add(main.LiveClear(user_id=self.player_id, live_difficulty_id=storage_id, difficulty=4))
            await database.execute(
                schema.song_goal.insert().values(
                    user_id=self.player_id,
                    period_id=period_id,
                    difficulty_id=self.difficulty_id,
                    storage_id=storage_id,
                    goal_id=41,
                    reward_json="{}",
                    created_at=10,
                )
            )
        await self.migrate("downgrade")
        await self.migrate()

        for action, score in (("eventPlayer", 2500), ("eventLive", 30000)):
            result = (await self.post(f"ranking/{action}", {"event_id": self.rule["event_id"]}))["response_data"]
            self.assertEqual(
                (result["items"][0]["score"], result["items"][0]["user_data"]["name"]), (score, "Archived")
            )
        async with session.BasicSchoolIdolContext() as context:
            clear = (await context.db.main.scalars(sqlalchemy.select(main.LiveClear))).one()
            self.assertEqual(await live.get_achieved_goal_id_list(context, clear), [41])

        published = marathon.dump(self.rule)
        self.rule["description"] = "Changed"
        result = await self.client.post("live/schedule", data={"request_data": "{}"})
        self.assertEqual(result.status_code, 409)
        async with self.sessions() as database:
            self.assertEqual(await database.scalar(sqlalchemy.select(schema.period.c.rule_json)), published)

    async def test_default_schedule_and_ordinary_start(self):
        await self.migrate()
        config.CONFIG_DATA.gameplay.marathon_policy = None
        self.policy.return_value = {"periods": []}
        self.assertEqual((await self.post("live/schedule"))["response_data"]["event_list"], [])
        self.assertEqual((await self.post("marathon/marathonInfo"))["response_data"], [])
        with unittest.mock.patch.object(game_live, "play_live", return_value=marathon.Wire({"ordinary": True})):
            body = {
                "party_user_id": self.player_id,
                "is_training": False,
                "unit_deck_id": 1,
                "live_difficulty_id": 1,
                "lp_factor": 1,
            }
            self.assertEqual((await self.post("live/play", body))["response_data"], {"ordinary": True})

    def test_shipped_policy_and_goal_catalog(self):
        raw = json.loads(RULE_FILE.read_bytes())
        unit_ids = {entry["unit_id"] for rule in raw["periods"] for entry in rule["cheer_units"]}
        unit_ids.update(
            entry["id"]
            for rule in raw["periods"]
            for kind in ("point_rewards", "score_rank_rewards", "point_rank_rewards")
            for condition in rule[kind]
            for entry in condition["items"]
            if entry["type"] == "UNIT"
        )
        with contextlib.closing(sqlite3.connect(self.path.parent / "unit")) as unit_database, unit_database:
            unit_database.execute("CREATE TABLE unit_m(unit_id INTEGER)")
            unit_database.executemany("INSERT INTO unit_m VALUES (?)", ((entry,) for entry in unit_ids))
        with contextlib.closing(sqlite3.connect(self.path.parent / "item")) as item_database, item_database:
            item_database.execute(
                "CREATE TABLE kg_item_m(item_id INTEGER, item_category_id INTEGER, item_sub_category_id INTEGER)"
            )
            item_database.execute("INSERT INTO kg_item_m VALUES (1,1,1)")
        with unittest.mock.patch.object(download, "get_db_path", side_effect=lambda name: self.path.parent / name):
            self.addCleanup(marathon.load_policy.cache_clear)
            policy = marathon.load_policy(RULE_FILE)
        definitions = event_song_goal.load_catalog(RULE_FILE.parent / "event_song_goals.json")
        for rule in policy["periods"]:
            for song in rule["songs"]:
                self.assertEqual(definitions[str(song["id"])]["setting_id"], song["setting_id"])
