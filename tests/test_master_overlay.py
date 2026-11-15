import concurrent.futures
import contextlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
import unittest.mock

import npps4.config.config as config
import npps4.download.download as download
import npps4.download.master_overlay as overlay


class MasterOverlayTest(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.directory / "live.db_"
        self.cache = self.directory / "cache"
        self.patch_path = pathlib.Path(config.ROOT_DIR) / "resources/master-overlays/live-59.4.json"
        self.patch = json.loads(self.patch_path.read_text("utf-8"))
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            db.executescript(
                "CREATE TABLE live_goal_reward_m (live_goal_reward_id INTEGER PRIMARY KEY, "
                "live_difficulty_id INTEGER, live_goal_type INTEGER, rank INTEGER, add_type INTEGER, "
                "item_id INTEGER, item_category_id INTEGER, amount INTEGER, item_option TEXT);"
                "CREATE TABLE special_live_rotation_m (rotation_group_id INTEGER, live_difficulty_id INTEGER, "
                "base_date TEXT, PRIMARY KEY(rotation_group_id,base_date));"
                "CREATE TABLE unrelated (value TEXT); INSERT INTO unrelated VALUES ('preserve');"
                "INSERT INTO live_goal_reward_m VALUES (1,1,1,1,3000,3,3,1,NULL);"
                "INSERT INTO special_live_rotation_m VALUES (9,1605,'kept');"
            )
            for row in self.patch["operations"]["special_live_rotation_m"]["remove_exact_rows"]:
                db.execute("INSERT INTO special_live_rotation_m VALUES (?,?,?)", tuple(row.values()))

    def prepare(self):
        return pathlib.Path(overlay.prepare_live_database(str(self.source), self.patch_path, self.cache))

    def test_restore_reuse_and_original_untouched(self):
        original = self.source.read_bytes()
        target = self.prepare()
        self.assertNotEqual(target, self.source)
        self.assertEqual(self.source.read_bytes(), original)
        with contextlib.closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM live_goal_reward_m").fetchone()[0], 2869)
            self.assertEqual(db.execute("SELECT * FROM special_live_rotation_m").fetchall(), [(9, 1605, "kept")])
            self.assertEqual(db.execute("SELECT * FROM unrelated").fetchall(), [("preserve",)])
            self.assertEqual(overlay.apply_overlay(db, self.patch), 0)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        before = target.stat().st_mtime_ns
        self.assertEqual(self.prepare(), target)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_conflict_rejected_without_publishing_or_mutating_source(self):
        row = self.patch["operations"]["live_goal_reward_m"]["insert_rows"][0].copy()
        row["amount"] += 1
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO live_goal_reward_m VALUES (?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "Conflicting reference row"):
            self.prepare()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_rotation_conflict_rolls_back_earlier_goal_inserts(self):
        row = self.patch["operations"]["special_live_rotation_m"]["remove_exact_rows"][0]
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            with db:
                db.execute(
                    "UPDATE special_live_rotation_m SET live_difficulty_id=-1 WHERE rotation_group_id=? AND base_date=?",
                    (row["rotation_group_id"], row["base_date"]),
                )
            with self.assertRaisesRegex(ValueError, "Conflicting reference row"), db:
                overlay.apply_overlay(db, self.patch)
            self.assertEqual(db.execute("SELECT count(*) FROM live_goal_reward_m").fetchone()[0], 1)

    def test_rebuild_corrupt_cache_and_changed_source(self):
        target = self.prepare()
        target.write_bytes(b"not sqlite")
        self.assertEqual(self.prepare(), target)
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO unrelated VALUES ('later')")
        replacement = self.prepare()
        self.assertNotEqual(replacement, target)
        with contextlib.closing(sqlite3.connect(replacement)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unrelated").fetchone()[0], 2)

    def test_concurrent_publication_and_already_patched_source(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(lambda _: self.prepare(), range(2)))
        self.assertEqual(paths[0], paths[1])
        new = overlay.prepare_live_database(str(paths[0]), self.patch_path, self.directory / "second")
        with contextlib.closing(sqlite3.connect(new)) as db:
            self.assertEqual(overlay.apply_overlay(db, self.patch), 0)

    def test_provider_facade_only_patches_live_59_4(self):
        backend = unittest.mock.Mock()
        backend.get_server_version.return_value = (59, 4)
        backend.get_db_path.return_value = str(self.source)
        with unittest.mock.patch.object(download, "CURRENT_BACKEND", backend), unittest.mock.patch.object(
            config, "get_data_directory", return_value=str(self.directory)
        ):
            self.assertNotEqual(download.get_db_path("live"), str(self.source))
            self.assertEqual(download.get_db_path("unit"), str(self.source))
            backend.get_server_version.return_value = (60, 0)
            self.assertEqual(download.get_db_path("live"), str(self.source))

    def test_unknown_patch_tables_rejected(self):
        patch = self.patch | {"operations": {"user": {}}}
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            with self.assertRaisesRegex(ValueError, "Unexpected live master overlay tables"):
                overlay.apply_overlay(db, patch)
