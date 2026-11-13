import asyncio
import contextlib
import importlib
import json
import pathlib
import runpy
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

from npps4 import const
from npps4 import idol
from npps4 import release_key
from npps4 import util
from npps4.app import app
from npps4.config import config
from npps4.db import main
from npps4.idol import core
from npps4.idol import session
from npps4.system import achievement
from npps4.system import achievement_claim
from npps4.system import achievement_claim_migration
from npps4.system import advanced
from npps4.system import item
from npps4.system import item_model
from npps4.system import reward
from npps4.system import unit
from npps4.system import user

REVISION = importlib.import_module("npps4.alembic.versions.2026_11_12_0100-5f1b8a4c3d72_achievement_claims")
importlib.import_module("npps4.game.achievement")
NOW = 1794254400


class AchievementClaimTest(unittest.IsolatedAsyncioTestCase):
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
            tables = [table for table in metadata.sorted_tables if not table.name.startswith(REVISION.CLAIM_TABLE)]
            await connection.run_sync(metadata.create_all, tables=tables)
        async with self.sessions.begin() as database:
            player = main.User(unit_max=0)
            database.add(player)
            await database.flush()
            self.player_id = player.id

        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "game_day_offset_minutes", 240))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "master_challenge_daily_limit", None))
        self.enterContext(unittest.mock.patch.object(util, "time", return_value=NOW))
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(
            unittest.mock.patch.object(session, "try_cleanup_tokens", new_callable=unittest.mock.AsyncMock)
        )
        self.enterContext(
            unittest.mock.patch.object(reward, "try_cleanup_incentive", new_callable=unittest.mock.AsyncMock)
        )
        self.enterContext(unittest.mock.patch.object(unit, "is_support_member", return_value=False))
        self.enterContext(
            unittest.mock.patch.object(achievement, "get_achievement_info", side_effect=self.achievement_definition)
        )
        self.enterContext(unittest.mock.patch.object(achievement, "get_prerequisite_achievement_ids", return_value=[]))
        self.enterContext(unittest.mock.patch.object(achievement, "get_achievement_rewards", side_effect=self.rewards))
        self.definitions: dict[int, types.SimpleNamespace] = {}

        async def finalize(context: session.SchoolIdolUserParams):
            context.token = session.TokenData(user_id=self.player_id, client_key=b"", server_key=b"")

        self.enterContext(unittest.mock.patch.object(session.SchoolIdolUserParams, "finalize", finalize))
        application = fastapi.FastAPI()
        application.include_router(app.main)
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://test",
                headers={
                    "Authorize": "consumerKey=lovelive_test",
                    "Client-Version": "59.4",
                    "LANG": "jp",
                    "Platform-Type": "2",
                    "X-Message-Code": "test",
                },
            )
        )

    async def migrate(self, *, downgrade: bool = False):
        def run(connection: sqlalchemy.Connection):
            context = alembic.migration.MigrationContext.configure(connection)
            with alembic.operations.Operations.context(context):
                (REVISION.downgrade if downgrade else REVISION.upgrade)()

        async with self.engine.begin() as connection:
            await connection.run_sync(run)

    async def add_achievement(
        self, achievement_id: int, *, reset_type: int = 0, marker: int = 0, updated: int = NOW, paid: bool = False
    ):
        self.definitions[achievement_id] = types.SimpleNamespace(
            achievement_type=999,
            params1=None,
            title="Test",
            title_en="Test",
            auto_reward_flag=False,
            default_open_flag=True,
            reset_type=reset_type,
            start_date="2020-01-01 00:00:00",
            end_date=None,
        )
        async with self.sessions.begin() as database:
            entry = main.Achievement(
                achievement_id=achievement_id,
                user_id=self.player_id,
                achievement_type=999,
                achievement_filter_category_id=1,
                is_accomplished=True,
                is_reward_claimed=paid,
                reset_type=reset_type,
                reset_value=marker,
                insert_date=updated,
                update_date=updated,
            )
            database.add(entry)
            await database.flush()
            return entry.id

    async def achievement_definition(self, context: idol.BasicSchoolIdolContext, achievement_id: int):
        return self.definitions[achievement_id]

    async def rewards(self, context: idol.BasicSchoolIdolContext, entry: main.Achievement):
        return [
            item.loveca(1),
            item_model.Item(add_type=const.ADD_TYPE.ITEM, item_id=12345, amount=2),
            item_model.Item(add_type=const.ADD_TYPE.UNIT, item_id=123, amount=1),
        ]

    async def post(self, path: str, body: dict | list[dict], *, authorize: str | None = None):
        headers = {"Authorize": authorize} if authorize is not None else None
        return await self.client.post(f"/main.php/{path}", data={"request_data": json.dumps(body)}, headers=headers)

    def single(self, accomplish_id: int, command: int):
        return {
            "module": "achievement",
            "action": "rewardOpen",
            "timeStamp": NOW,
            "commandNum": command,
            "accomplish_id": accomplish_id,
        }

    def bulk(self, command: int):
        return {"module": "achievement", "action": "rewardOpenAll", "timeStamp": NOW, "commandNum": command}

    async def payout(self):
        async with self.sessions() as database:
            player = await database.get_one(main.User, self.player_id)
            inventory = await database.scalar(sqlalchemy.select(main.Item.amount))
            presents = await database.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(main.Incentive))
            claims = await database.scalar(
                sqlalchemy.select(sqlalchemy.func.count()).select_from(main.DestinyAchievementClaim)
            )
            return player.free_sns_coin, inventory or 0, presents, claims

    async def apply_history(self):
        async with idol.BasicSchoolIdolContext() as context:
            await achievement_claim_migration.apply(context)

    async def test_legacy_replay_upgrade_and_downgrade(self):
        first = await self.add_achievement(100)
        await self.add_achievement(101)
        fixtures = pathlib.Path(__file__).with_name("fixtures")
        legacy_schema = (fixtures / "legacy_achievement_claims.sql").read_text(encoding="utf-8")
        review = runpy.run_path(str(pathlib.Path(config.ROOT_DIR) / "scripts/review_achievement_claim.py"))
        with contextlib.closing(sqlite3.connect(self.path)) as database, database:
            database.executescript(legacy_schema)
            database.execute(
                "INSERT INTO destiny_achievement_claim_review VALUES (?, 100, 'permanent', ?, 'legacy', '{}', ?, NULL)",
                (self.player_id, first, NOW),
            )
            old_response = [
                json.loads(row[0])
                for row in database.execute(
                    "SELECT response_json FROM destiny_achievement_claim_receipt ORDER BY request_key"
                )
            ]
        plan = review["make_review_plan"](self.path, self.player_id, 100, "permanent")
        with self.assertRaisesRegex(ValueError, "explicit reviewer"):
            review["apply_review_plan"](self.path, plan)
        plan.update(
            approved=True, approved_by="test reviewer", evidence="Prior payout recorded", decision="confirm_paid"
        )
        self.assertFalse(review["apply_review_plan"](self.path, plan)["already_applied"])
        self.assertTrue(review["apply_review_plan"](self.path, plan)["already_applied"])

        await self.migrate()
        await self.apply_history()
        async with self.sessions() as database:
            decision = await database.get_one(
                main.DestinyAchievementClaimReviewDecision, (self.player_id, 100, "permanent")
            )
            self.assertEqual(json.loads(decision.plan_json), plan)
        batch = [self.single(first, 1), self.bulk(2), self.bulk(2)]
        replay = (await self.post("api", batch)).json()["response_data"]
        self.assertEqual([entry["status"] for entry in replay], [200, 200, 200])
        self.assertEqual([entry["result"] for entry in replay], old_response)
        self.assertEqual(await self.payout(), (0, 0, 0, 2))
        conflicting = [*batch, self.bulk(3)]
        mismatch = (await self.post("api", conflicting)).json()["response_data"]
        self.assertIn("reused with different data", mismatch[0]["result"]["detail"])

        last = await self.add_achievement(102)
        native_batch = [self.single(last, 4)]
        new_response = (await self.post("api", native_batch)).json()["response_data"]
        self.assertEqual(new_response[0]["status"], 200)
        await self.migrate(downgrade=True)
        with contextlib.closing(sqlite3.connect(self.path)) as database:
            digest, response = database.execute(
                "SELECT payload_hash, response_json FROM destiny_achievement_claim_receipt "
                "WHERE user_id=? AND action='rewardOpen' AND request_key='body:1794254400:4:batch:0'",
                (self.player_id,),
            ).fetchone()
        self.assertEqual(digest, "9ee6518754320d3c262ce5bbc00314e3573b4f9989393438efd7fff7c51f6344")
        self.assertEqual(json.loads(response), new_response[0]["result"])
        self.assertEqual(await self.payout(), (1, 2, 1, 3))

        async with idol.BasicSchoolIdolContext() as context:
            await user.delete_user(context, self.player_id)
        async with self.sessions() as database:
            for table in (
                main.DestinyAchievementClaim,
                main.DestinyAchievementClaimReceipt,
                main.DestinyAchievementClaimReview,
                main.DestinyAchievementClaimReviewDecision,
            ):
                self.assertIsNone(await database.scalar(sqlalchemy.select(table)))
            self.assertEqual(
                len((await database.scalars(sqlalchemy.select(main.DestinyAchievementClaimMigration))).all()), 2
            )

    async def test_concurrent_direct_replay_and_identity(self):
        await self.migrate()
        first = await self.add_achievement(100)
        body = {"accomplish_id": first}
        authorize = f"consumerKey=lovelive_test&timeStamp={NOW}&nonce=7"
        responses = await asyncio.gather(
            self.post("achievement/rewardOpen", body, authorize=authorize),
            self.post("achievement/rewardOpen", body, authorize=authorize),
        )
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(responses[0].json()["response_data"], responses[1].json()["response_data"])
        mismatch = await self.post("achievement/rewardOpen", {"accomplish_id": first + 1}, authorize=authorize)
        self.assertEqual(mismatch.status_code, 409)
        missing = await self.post("achievement/rewardOpen", body)
        self.assertEqual(missing.status_code, 409)
        async with self.sessions.begin() as database:
            entry = await database.get_one(main.Achievement, first)
            entry.is_reward_claimed = False
        paid = await self.post("achievement/rewardOpen", self.single(first, 9))
        self.assertEqual(paid.status_code, 409)
        self.assertIn("already recorded for this period", paid.json()["response_data"]["detail"])
        self.assertEqual(await self.payout(), (1, 2, 1, 1))

    async def test_review_blocks_one_period_and_preserves_saved_result(self):
        await self.migrate()
        first = await self.add_achievement(100, reset_type=1, marker=util.get_days_since_unix(NOW))
        await self.add_achievement(101)
        async with self.sessions.begin() as database:
            database.add(
                main.DestinyAchievementClaimReview(
                    user_id=self.player_id,
                    achievement_id=100,
                    blocked_period=achievement_claim.period_key(1, NOW),
                    owning_achievement_id=first,
                    reason="review",
                    evidence_json="{}",
                    created_at=NOW,
                )
            )
        rejected = await self.post("achievement/rewardOpen", self.single(first, 1))
        self.assertEqual(rejected.status_code, 409)
        bulk = await self.post("achievement/rewardOpenAll", self.bulk(2))
        self.assertEqual(bulk.json()["response_data"]["opened_num"], 1)

        tomorrow = NOW + 86400
        async with self.sessions.begin() as database:
            entry = await database.get_one(main.Achievement, first)
            entry.reset_value = util.get_days_since_unix(tomorrow)
            entry.update_date = tomorrow
        with unittest.mock.patch.object(util, "time", return_value=tomorrow):
            response = await self.post("achievement/rewardOpen", self.single(first, 3))
            replay = await self.post("achievement/rewardOpenAll", self.bulk(2))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(replay.json()["response_data"], bulk.json()["response_data"])
        self.assertEqual(await self.payout(), (2, 4, 2, 2))

    async def test_failed_grant_rolls_back_only_its_batch_entry(self):
        await self.migrate()
        first = await self.add_achievement(100)
        second = await self.add_achievement(101)
        add_item = advanced.add_item
        calls = 0

        async def fail_once(context: idol.BasicSchoolIdolContext, player: main.User, entry: item_model.Item):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("grant failed")
            return await add_item(context, player, entry)

        batch = [self.single(first, 1), self.single(second, 2)]
        with unittest.mock.patch.object(advanced, "add_item", side_effect=fail_once):
            response = (await self.post("api", batch)).json()["response_data"]
        self.assertEqual([entry["status"] for entry in response], [500, 200])
        self.assertEqual(await self.payout(), (1, 2, 1, 1))
        retry = (await self.post("api", batch)).json()["response_data"]
        self.assertEqual([entry["status"] for entry in retry], [200, 200])
        self.assertEqual(retry[1]["result"], response[1]["result"])
        self.assertEqual(await self.payout(), (2, 4, 2, 2))

    async def test_weekly_prerequisite_and_fragment_configuration(self):
        await self.migrate()
        last_week = NOW - 7 * 86400
        prerequisite = await self.add_achievement(
            100, reset_type=2, marker=util.get_weeks_since_unix(last_week), updated=last_week
        )
        target = await self.add_achievement(101, reset_type=2, marker=util.get_weeks_since_unix(NOW))
        fragment = await self.add_achievement(102)
        self.definitions[102].achievement_type = 55
        self.definitions[102].params1 = 1000
        self.definitions[102].params3 = 1
        with unittest.mock.patch.object(achievement, "get_prerequisite_achievement_ids", return_value=[100]):
            body = self.single(target, 1)
            rejected = await self.post("achievement/rewardOpen", body)
            self.assertEqual(rejected.status_code, 409)
            self.assertIn("prerequisite belongs to an earlier period", rejected.json()["response_data"]["detail"])
            async with self.sessions.begin() as database:
                entry = await database.get_one(main.Achievement, prerequisite)
                entry.reset_value = util.get_weeks_since_unix(NOW)
                entry.update_date = NOW
            self.assertEqual((await self.post("achievement/rewardOpen", body)).status_code, 200)

        body = self.single(fragment, 2)
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "block_fragment_achievement_claims", True):
            rejected = await self.post("achievement/rewardOpen", body)
            self.assertIn("Fragment achievement claims are disabled", rejected.json()["response_data"]["detail"])
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "block_fragment_achievement_claims", False):
            rejected = await self.post("achievement/rewardOpen", body)
            self.assertIn("progress does not meet", rejected.json()["response_data"]["detail"])
            async with self.sessions.begin() as database:
                entry = await database.get_one(main.Achievement, fragment)
                entry.count = 1
            self.assertEqual((await self.post("achievement/rewardOpen", body)).status_code, 200)

    async def test_missing_history_markers_are_backfilled_once(self):
        await self.add_achievement(100, paid=True)
        await self.add_achievement(101, reset_type=1, paid=True)
        await self.add_achievement(102, reset_type=1, updated=NOW - 86400, paid=True)
        await self.migrate()
        await self.apply_history()
        await self.apply_history()
        async with self.sessions() as database:
            claims = (await database.scalars(sqlalchemy.select(main.DestinyAchievementClaim))).all()
            self.assertEqual([(entry.achievement_id, entry.period_key) for entry in claims], [(100, "permanent")])
            holds = (await database.scalars(sqlalchemy.select(main.DestinyAchievementClaimReview))).all()
            self.assertEqual({entry.achievement_id: entry.resolved_at for entry in holds}, {101: None, 102: NOW})
            decision = await database.get_one(
                main.DestinyAchievementClaimReviewDecision, (self.player_id, 102, achievement_claim.period_key(1, NOW))
            )
            self.assertEqual(decision.decision, "allow_retry")
        self.assertEqual(await self.payout(), (0, 0, 0, 1))
