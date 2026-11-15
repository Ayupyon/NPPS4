import asyncio
import pathlib
import tempfile
import types
import unittest
import unittest.mock

import sqlalchemy
import sqlalchemy.ext.asyncio

import npps4.db.main as main
import npps4.game.ranking as ranking_api
import npps4.game.tutorial as tutorial_api
import npps4.idol.error as error
import npps4.idol.session as session
import npps4.system.ranking as ranking
import npps4.system.reward as reward
import npps4.system.tutorial as tutorial
import npps4.system.unit as unit
import npps4.util as util


class Context(session.BasicSchoolIdolContext):
    def __init__(self, user_id: int):
        super().__init__()
        self.token = session.TokenData(user_id=user_id, client_key=b"", server_key=b"")

    def add_task(self, *args, **kwargs):
        pass


class RankingTutorialSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.engine = sqlalchemy.ext.asyncio.create_async_engine(
            f"sqlite+aiosqlite:///{self.directory / 'main.sqlite3'}", connect_args={"autocommit": False, "timeout": 10}
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.maker = sqlalchemy.ext.asyncio.async_sessionmaker(self.engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", self.maker))
        async with self.engine.begin() as connection:
            await connection.run_sync(
                main.User.metadata.create_all,
                tables=[main.User.__table__, main.PlayerRanking.__table__, main.LiveClear.__table__],
            )
        async with self.maker.begin() as db:
            db.add_all([main.User(name=f"Synthetic {i}", tutorial_state=2) for i in range(1, 46)])
        self.enterContext(unittest.mock.patch.object(util, "time", return_value=1794254400))
        self.enterContext(unittest.mock.patch.object(reward, "count_presentbox", return_value=0))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit_center", return_value=1))
        card = types.SimpleNamespace(
            unit_id=1, rank=2, love=0, display_rank=2, skill_exp=0, unit_removable_skill_capacity=0
        )
        full = types.SimpleNamespace(level=1, is_love_max=False, is_level_max=False, is_rank_max=True)
        stats = types.SimpleNamespace(smile=1, pure=1, cool=1)
        self.enterContext(unittest.mock.patch.object(unit, "get_unit", return_value=card))
        self.enterContext(unittest.mock.patch.object(unit, "validate_unit"))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit_info", return_value=object()))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit_removable_skills", return_value=[]))
        self.enterContext(unittest.mock.patch.object(unit, "get_unit_data_full_info", return_value=(full, stats)))

    async def seed_rankings(self):
        async with self.maker.begin() as db:
            for uid in range(45, 0, -1):
                db.add(main.PlayerRanking(user_id=uid, day=util.get_days_since_unix(), score=1000))
            db.add(main.PlayerRanking(user_id=23, day=util.get_days_since_unix() - 1, score=999))

    async def test_daily_page_jump_own_position_and_ties(self):
        await self.seed_rankings()
        async with Context(23) as c:
            for request in (
                ranking_api.RankingPlayerRequest(page=1),
                ranking_api.RankingPlayerRequest(rank=21),
                ranking_api.RankingPlayerRequest(id=23),
            ):
                response = await ranking_api.ranking_player(c, request)
                self.assertEqual((response.page, response.rank, response.total_cnt), (1, 23, 45))
                self.assertEqual([x.rank for x in response.items], list(range(21, 41)))
                self.assertEqual([x.user_data.user_id for x in response.items], list(range(21, 41)))
            yesterday = await ranking_api.ranking_player(c, ranking_api.RankingPlayerRequest(daily_index=2))
            self.assertEqual((yesterday.total_cnt, yesterday.items[0].score, yesterday.rank), (1, 999, 1))

    async def test_empty_and_invalid_rank_selectors(self):
        async with Context(1) as c:
            response = await ranking_api.ranking_player(c, ranking_api.RankingPlayerRequest())
            self.assertEqual((response.page, response.items, response.total_cnt), (0, [], 0))
            for data in ({"page": -1}, {"rank": 0}, {"term": 2}, {"daily_index": 3}, {"id": -1}):
                with self.subTest(data=data), self.assertRaises(error.IdolError):
                    await ranking_api.ranking_player(c, ranking_api.RankingPlayerRequest(**data))
            with self.assertRaises(error.IdolError):
                await ranking_api.ranking_player(c, ranking_api.RankingPlayerRequest(id=1))
        await self.seed_rankings()
        async with Context(1) as c:
            with self.assertRaises(error.IdolError):
                await ranking_api.ranking_player(c, ranking_api.RankingPlayerRequest(rank=61))

    async def test_song_ranking_page_labels_and_tie_order(self):
        async with self.maker.begin() as db:
            for uid in range(45, 0, -1):
                db.add(main.LiveClear(user_id=uid, live_difficulty_id=1, difficulty=4, clear_cnt=1, hi_score=100))
        async with Context(1) as c:
            response = await ranking_api.ranking_live(c, ranking_api.RankingLiveRequest(live_difficulty_id=1, page=1))
            self.assertEqual([x.rank for x in response.items], list(range(21, 41)))
            self.assertEqual([x.user_data.user_id for x in response.items], list(range(21, 41)))
            empty = await ranking_api.ranking_live(c, ranking_api.RankingLiveRequest(live_difficulty_id=2))
            self.assertEqual(empty.items, [])
            for page in (-1, 3):
                with self.assertRaises(error.IdolError):
                    await ranking_api.ranking_live(c, ranking_api.RankingLiveRequest(live_difficulty_id=1, page=page))

    async def test_concurrent_daily_score_increments(self):
        async def increment():
            async with Context(1) as c:
                player = main.User()
                player.id = 1
                await ranking.increment_daily_score(c, player, 7)

        await asyncio.gather(increment(), increment(), increment())
        async with Context(1) as c:
            rows = await ranking.get_daily_ranking(c, 0, False)
            self.assertEqual([(x.user_id, x.score) for x in rows], [(1, 21)])

    async def award_phase(self, c: Context, current: main.User):
        current.game_coin += 600
        c.db.main.add(main.PlayerRanking(user_id=current.id, day=util.get_days_since_unix(), score=1))
        current.tutorial_state = 3
        await c.db.main.flush()

    async def test_tutorial_retry_concurrency_and_completed_noops(self):
        with unittest.mock.patch.object(tutorial, "phase3", side_effect=self.award_phase) as phase:

            async def progress():
                async with Context(1) as c:
                    await tutorial_api.tutorial_progress(c, tutorial_api.TutorialProgressRequest(tutorial_state=3))

            async def skip():
                async with Context(1) as c:
                    await tutorial_api.tutorial_skip(c)

            await asyncio.gather(progress(), skip(), progress())
            await skip()
            self.assertEqual(phase.await_count, 1)
        async with Context(1) as c:
            current = await c.db.main.get(main.User, 1)
            self.assertEqual((current.tutorial_state, current.game_coin), (-1, 600))
            for target in (1, 2, 3, -1):
                await tutorial_api.tutorial_progress(c, tutorial_api.TutorialProgressRequest(tutorial_state=target))
            self.assertEqual(current.game_coin, 600)
            with self.assertRaises(error.IdolError):
                await tutorial_api.tutorial_progress(c, tutorial_api.TutorialProgressRequest(tutorial_state=0))

    async def test_tutorial_phase_failure_rollback_then_retry(self):
        async def broken(c: Context, current: main.User):
            await self.award_phase(c, current)
            raise RuntimeError("injected award failure")

        async with Context(1) as c:
            with unittest.mock.patch.object(tutorial, "phase3", side_effect=broken):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    await tutorial_api.tutorial_skip(c)
            # Deliberately commit the outer transaction after catching failure.
        async with Context(1) as c:
            current = await c.db.main.get(main.User, 1)
            self.assertEqual((current.tutorial_state, current.game_coin), (2, 0))
            self.assertEqual(
                await c.db.main.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(main.PlayerRanking)), 0
            )
            with unittest.mock.patch.object(tutorial, "phase3", side_effect=self.award_phase):
                await tutorial_api.tutorial_skip(c)
            self.assertEqual((current.tutorial_state, current.game_coin), (-1, 600))

    async def test_invalid_future_phase_and_other_account_are_untouched(self):
        async with Context(1) as c:
            with self.assertRaises(error.IdolError):
                await tutorial_api.tutorial_progress(c, tutorial_api.TutorialProgressRequest(tutorial_state=-1))
            current = await c.db.main.get(main.User, 1)
            other = await c.db.main.get(main.User, 2)
            self.assertEqual(
                (current.tutorial_state, other.tutorial_state, current.game_coin, other.game_coin), (2, 2, 0, 0)
            )
