import contextlib
import io
import logging.config
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

import alembic.script
import fastapi
import sqlalchemy.ext.asyncio
import sqlalchemy.pool
import uvicorn

import npps4.__main__ as entrypoint
import npps4.config.config as config
import npps4.config.data as config_data
import npps4.db.achievement as achievement
import npps4.db.main as main
import npps4.system.achievement_claim_migration as claim_migration
import npps4.system.marathon as marathon
import npps4.system.reward_egg as reward_egg

ROOT = pathlib.Path(config.ROOT_DIR)


class CutoverTest(unittest.TestCase):
    def test_native_run_upgrades_and_repairs_before_serving(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        main_path = directory / "main.sqlite3"
        achievement_path = directory / "achievement.sqlite3"
        with contextlib.closing(sqlite3.connect(achievement_path)) as database:
            database.execute("CREATE TABLE achievement_m (achievement_id INTEGER PRIMARY KEY, reset_type INTEGER)")

        profile = re.search(r"```toml\n(\[gameplay\]\n.*?)\n```", (ROOT / "README.md").read_text(), re.DOTALL)
        assert profile is not None
        fixture_config = directory / "config.toml"
        fixture_config.write_text(
            f'{profile[1]}\n[database]\nurl = "sqlite+aiosqlite:///{main_path.as_posix()}"\n'
            '[download]\nbackend = "none"\n',
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(config_data.ConfigData.model_config, {"toml_file": fixture_config}):
            settings = config_data.ConfigData.model_validate({})
        self.assertEqual(
            settings.gameplay.model_dump(exclude_unset=True),
            {
                "game_day_offset_minutes": 240,
                "random_live_rotation_anchor": 1794254400,
                "master_challenge_daily_limit": 10,
                "daily_friend_scout": True,
                "scout_audit": True,
                "friend_mail": True,
                "daily_clear_required_groups": {20010015: 13, 20010016: 113},
                "daily_clear_bonus_loveca": {20010015: 1, 20010016: 1, 20010017: 1},
                "block_fragment_achievement_claims": True,
                "starter_unit_numbers": list(range(937, 955)),
                "starter_post_tutorial_gold": 1000000,
                "reward_egg_policy": "resources/events/reward_egg_policy.json",
                "marathon_policy": "resources/events/marathon_rules.json",
                "event_client_resources": True,
            },
        )
        self.enterContext(unittest.mock.patch.object(config, "CONFIG_DATA", settings))
        self.enterContext(
            unittest.mock.patch.object(sys, "argv", ["npps4", "run", "--host", "127.0.0.1", "--port", "51400"])
        )
        if "npps4.script_dummy" not in sys.modules:
            self.addCleanup(sys.modules.pop, "npps4.script_dummy", None)
        for module, path in ((main, main_path), (achievement, achievement_path)):
            engine = sqlalchemy.ext.asyncio.create_async_engine(
                f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=sqlalchemy.pool.NullPool
            )
            self.enterContext(unittest.mock.patch.object(module, "engine", engine))
            self.enterContext(
                unittest.mock.patch.object(module, "sessionmaker", sqlalchemy.ext.asyncio.async_sessionmaker(engine))
            )

        marathon_policy = self.enterContext(unittest.mock.patch.object(marathon, "load_policy", return_value={}))
        egg_policy = self.enterContext(unittest.mock.patch.object(reward_egg, "_load_policy"))
        expected_head = alembic.script.ScriptDirectory.from_config(entrypoint._get_alembic_config()).get_current_head()

        def serve(application: fastapi.FastAPI, *, host: str, port: int, loop: str) -> None:
            self.assertEqual((host, port, loop), ("127.0.0.1", 51400, "npps4.evloop:new_event_loop"))
            with contextlib.closing(sqlite3.connect(main_path)) as database:
                self.assertEqual(
                    database.execute("SELECT version_num FROM alembic_version").fetchone(), (expected_head,)
                )
                self.assertEqual(
                    database.execute("SELECT revision FROM migration_fixes").fetchone(), ("9_achievement_fix_2",)
                )
                self.assertCountEqual(
                    database.execute("SELECT migration_key FROM destiny_achievement_claim_migration").fetchall(),
                    [(claim_migration.BASELINE,), (claim_migration.STALE_DAILY_HOLDS,)],
                )
            expected_paths = {
                "achievement/rewardOpen",
                "achievement/rewardOpenAll",
                "banner/bannerList",
                "common/liveResume",
                "download/event",
                "download/getUrl",
                "effortPoint/selectLimitedBox",
                "event/eventList",
                "event/eventYellDeck",
                "event/presentation",
                "friend/request",
                "friend/search",
                "greet/user",
                "handover/exec",
                "item/list",
                "lbonus/execute",
                "live/play",
                "live/reward",
                "live/schedule",
                "login/startUp",
                "marathon/top",
                "notice/noticeFriendGreeting",
                "profile/profileInfo",
                "ranking/eventLive",
                "ranking/player",
                "secretbox/all",
                "secretbox/multi",
            }
            paths = application.openapi()["paths"]
            self.assertEqual({path for path in expected_paths if "/main.php/" + path not in paths}, set())

        serving = self.enterContext(unittest.mock.patch.object(uvicorn, "run", side_effect=serve))
        with (
            unittest.mock.patch.object(logging.config, "fileConfig"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            entrypoint.main()
        serving.assert_called_once()
        marathon_policy.assert_called_once_with(ROOT / "resources/events/marathon_rules.json")
        egg_policy.assert_called_once_with(ROOT / "resources/events/reward_egg_policy.json")
