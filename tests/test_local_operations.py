import asyncio
import copy
import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock

import sqlalchemy
import sqlalchemy.ext.asyncio

import npps4.config.config as config
import npps4.db.main as main
import npps4.db.marathon as schema
import npps4.db.unit as unit_db
import npps4.idol.session as session
import npps4.scriptutils.settlement as settlement
import npps4.system.marathon as marathon
import scripts.add_gold as add_gold
import scripts.add_sis_pack as add_sis_pack
import scripts.reward_egg_maintenance as egg_admin


class LocalOperationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.engine = sqlalchemy.ext.asyncio.create_async_engine(
            f"sqlite+aiosqlite:///{(directory / 'test.sqlite3').as_posix()}",
            connect_args={"autocommit": False, "timeout": 25},
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = sqlalchemy.ext.asyncio.async_sessionmaker(self.engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", self.sessions))
        async with self.engine.begin() as connection:
            await connection.run_sync(main.User.metadata.create_all)
        async with self.sessions.begin() as database:
            people = [main.User(name=f"Player {index}", game_coin=100) for index in range(3)]
            database.add_all(people)
            database.add(main.DestinyFriendMutex(id=1, revision=0))
            await database.flush()
            self.users = [person.id for person in people]
        policy = json.loads(
            (pathlib.Path(config.ROOT_DIR) / "resources/events/marathon_rules.json").read_text(encoding="utf-8")
        )
        rule = copy.deepcopy(settlement.rule_for(policy, policy["selected_period_key"]))
        rule["minimum_reward_points"] = rule["minimum_reward_clears"] = 1
        rule["point_rank_rewards"] = [
            {"id": "point-test", "from": 1, "to": 3, "items": [{"type": "GAME_COIN", "id": 3, "amount": 10}]}
        ]
        rule["score_rank_rewards"] = [
            {"id": "score-test", "from": 1, "to": 3, "items": [{"type": "GAME_COIN", "id": 3, "amount": 5}]}
        ]
        self.key = rule["key"]
        other = copy.deepcopy(rule)
        other.update(key="other-due-period", event_id=999, end="2026-11-10T00:00:00Z", settlement_grace_seconds=0)
        self.source = dict(periods=[rule, other], selected_period_key=self.key)
        self.target = copy.deepcopy(self.source)
        settlement.rule_for(self.target, self.key).update(end="2026-11-13T07:20:29Z", settlement_grace_seconds=0)
        self.enterContext(unittest.mock.patch.object(marathon, "now", return_value=1794555000))
        self.policy = self.enterContext(unittest.mock.patch.object(marathon, "get_policy", return_value=self.source))
        self.enterContext(
            unittest.mock.patch.object(marathon, "rank_profile", return_value={"user_data": {"name": "Player"}})
        )
        async with session.BasicSchoolIdolContext() as context:
            record = await marathon.register_period(context, rule)
            self.period_id = record["id"]
            other_record = await marathon.register_period(context, other)
            self.other_id = other_record["id"]
            for index, user_id in enumerate(self.users):
                await context.db.main.execute(
                    schema.progress.insert().values(
                        period_id=self.period_id,
                        user_id=user_id,
                        points=10,
                        tokens=99,
                        clears=1,
                        score=10 if index < 2 else 0,
                        event_clears=1 if index < 2 else 0,
                        point_time=100,
                        score_time=100,
                    )
                )

    async def plan(self):
        async with session.BasicSchoolIdolContext() as context:
            return await settlement.preview(context, self.source, self.target, self.key)

    async def apply(self, plan):
        self.policy.return_value = self.target
        async with session.BasicSchoolIdolContext() as context:
            return await settlement.apply(context, plan)

    async def count(self, table) -> int:
        async with self.sessions() as database:
            return await database.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(table))

    async def test_preview_is_read_only_and_apply_is_targeted_and_idempotent(self):
        plan = await self.plan()
        self.assertEqual([row["user_id"] for row in plan["boards"]["point"]], self.users)
        self.assertEqual(len(plan["awards"]), 5)
        self.assertEqual(await self.count(main.Incentive), 0)
        first = await self.apply(plan)
        self.assertEqual(first["present_entries"], 5)
        self.assertTrue((await self.apply(plan))["already_settled"])
        self.assertEqual(await self.count(main.Incentive), 5)
        async with self.sessions() as database:
            other = (
                (await database.execute(sqlalchemy.select(schema.period).where(schema.period.c.id == self.other_id)))
                .mappings()
                .one()
            )
            self.assertIsNone(other["frozen_at"])
            rows = (await database.execute(sqlalchemy.select(schema.progress))).mappings().all()
            self.assertTrue(all(row["tokens"] == 99 and row["points"] == 10 for row in rows))

    async def test_concurrent_apply_does_not_duplicate_rewards(self):
        plan = await self.plan()
        results = await asyncio.gather(self.apply(plan), self.apply(plan))
        self.assertEqual(sorted(result["already_settled"] for result in results), [False, True])
        self.assertEqual(await self.count(main.Incentive), 5)

    async def test_partial_grant_failure_rolls_back_rules_rankings_and_rewards(self):
        plan = await self.plan()
        original = marathon.grant
        calls = 0

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")
            return await original(*args, **kwargs)

        with unittest.mock.patch.object(marathon, "grant", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                await self.apply(plan)
        self.assertEqual(await self.count(main.Incentive), 0)
        self.assertEqual(await self.count(schema.ranking_snapshot), 0)
        async with session.BasicSchoolIdolContext() as context:
            row = await settlement.get_record(context, self.key)
            self.assertEqual(row["rule_hash"], settlement.digest(plan["original_rule"]))
        self.assertFalse((await self.apply(plan))["already_settled"])

    async def test_changed_standings_refuse_apply(self):
        plan = await self.plan()
        async with self.sessions.begin() as database:
            await database.execute(schema.progress.update().values(points=11))
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            await self.apply(plan)
        self.assertEqual(await self.count(main.Incentive), 0)

    async def test_unfinished_song_refuses_preview(self):
        async with self.sessions.begin() as database:
            await database.execute(
                schema.live.insert().values(
                    user_id=self.users[0],
                    period_id=self.period_id,
                    difficulty_id=1,
                    party_user_id=1,
                    training=0,
                    is_event_song=1,
                    factor=1,
                    cost=1,
                    started_at=1,
                    rule_json="{}",
                    cheer_percent=100,
                    cheer_snapshot="[]",
                    notes_json="[]",
                    token_indices="[]",
                    play_key="test",
                    play_hash="a" * 64,
                )
            )
        with self.assertRaisesRegex(ValueError, "Unfinished"):
            await self.plan()

    async def test_reward_changes_and_other_period_changes_are_rejected(self):
        for field, value in [("name", "Changed"), ("point_rank_rewards", [])]:
            target = copy.deepcopy(self.target)
            target["periods"][0][field] = value
            with self.assertRaisesRegex(ValueError, "Only the target"):
                settlement.validate_transition(self.source, target, self.key)
        target = copy.deepcopy(self.target)
        target["periods"][1]["end"] = "2026-11-11T00:00:00Z"
        with self.assertRaises(ValueError):
            settlement.validate_transition(self.source, target, self.key)

    async def test_target_config_plan_integrity_and_database_binding(self):
        plan = await self.plan()
        async with session.BasicSchoolIdolContext() as context:
            with self.assertRaisesRegex(ValueError, "Configure the reviewed"):
                await settlement.apply(context, plan)
        edited = copy.deepcopy(plan)
        edited["awards"] = []
        with self.assertRaisesRegex(ValueError, "edited"):
            await self.apply(edited)
        with unittest.mock.patch.object(settlement, "database_identity", return_value="different"):
            with self.assertRaisesRegex(ValueError, "another database"):
                await self.apply(plan)

    async def test_gold_preview_and_apply_preserve_other_accounts(self):
        args = ["--user-id", str(self.users[0]), "20"]
        with unittest.mock.patch("sys.stdout", new=io.StringIO()):
            await add_gold.run_script(args)
            async with self.sessions() as database:
                self.assertEqual((await database.get(main.User, self.users[0])).game_coin, 100)
            await add_gold.run_script(args + ["--apply"])
        async with self.sessions() as database:
            self.assertEqual((await database.get(main.User, self.users[0])).game_coin, 120)
            self.assertEqual((await database.get(main.User, self.users[1])).game_coin, 100)

    async def test_sis_pack_preview_apply_and_definition_guard(self):
        engine = sqlalchemy.ext.asyncio.create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(engine.dispose)
        client_sessions = sqlalchemy.ext.asyncio.async_sessionmaker(engine)
        self.enterContext(unittest.mock.patch.object(unit_db, "sessionmaker", client_sessions))
        async with engine.begin() as connection:
            await connection.execute(
                sqlalchemy.text(
                    "CREATE TABLE unit_removable_skill_m(unit_removable_skill_id INTEGER, name_en TEXT, skill_type INTEGER)"
                )
            )
            for skill_id, name in (add_sis_pack.FORTUNE | {1: "Test normal SIS"}).items():
                await connection.execute(
                    sqlalchemy.text("INSERT INTO unit_removable_skill_m VALUES(:id,:name,1)"),
                    dict(id=skill_id, name=name),
                )
        args = ["--user-id", str(self.users[0])]
        with unittest.mock.patch("sys.stdout", new=io.StringIO()):
            await add_sis_pack.run_script(args)
            self.assertEqual(await self.count(main.RemovableSkillInfo), 0)
            await add_sis_pack.run_script(args + ["--apply"])
        async with self.sessions() as database:
            rows = (await database.scalars(sqlalchemy.select(main.RemovableSkillInfo))).all()
            self.assertEqual({row.unit_removable_skill_id: row.amount for row in rows}, {1: 9, 61: 11, 91: 11, 92: 11})
            self.assertTrue(all(row.user_id == self.users[0] for row in rows))
        async with engine.begin() as connection:
            await connection.execute(
                sqlalchemy.text("UPDATE unit_removable_skill_m SET name_en='Mismatch' WHERE unit_removable_skill_id=61")
            )
        with self.assertRaisesRegex(ValueError, "differs"):
            await add_sis_pack.run_script(args + ["--apply"])
        async with self.sessions() as database:
            self.assertEqual(
                await database.scalar(sqlalchemy.select(sqlalchemy.func.sum(main.RemovableSkillInfo.amount))), 42
            )

    async def test_egg_drain_restores_normal_box_and_preserves_rewards(self):
        async with self.sessions.begin() as database:
            player = await database.get(main.User, self.users[0])
            player.limited_effort_event_id = 3
            player.current_limited_effort_point = 123
            player.current_live_effort_point = 73
            database.add(
                main.DestinyRewardEgg(user_id=player.id, saved_spec=4, pending_event=1, pending_spec=2, openings=7)
            )
        async with session.BasicSchoolIdolContext() as context:
            self.assertEqual((await egg_admin.inspect_eggs(context))["active"], 1)
            self.assertEqual((await egg_admin.drain(context))["active"], 0)
        async with self.sessions() as database:
            player = await database.get(main.User, self.users[0])
            egg = await database.get(main.DestinyRewardEgg, player.id)
            self.assertEqual(player.live_effort_point_box_spec_id, 4)
            self.assertEqual(player.game_coin, 100)
            self.assertEqual(player.current_live_effort_point, 73)
            self.assertEqual(egg.openings, 7)
            self.assertEqual(egg.pending_event, 0)

    async def test_egg_missing_restore_point_leaves_state_unchanged(self):
        async with self.sessions.begin() as database:
            player = await database.get(main.User, self.users[0])
            player.limited_effort_event_id = 3
        with self.assertRaisesRegex(ValueError, "restore"):
            async with session.BasicSchoolIdolContext() as context:
                await egg_admin.drain(context)
        async with self.sessions() as database:
            self.assertEqual((await database.get(main.User, self.users[0])).limited_effort_event_id, 3)


if __name__ == "__main__":
    unittest.main()
