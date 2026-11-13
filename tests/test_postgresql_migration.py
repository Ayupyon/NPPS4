import contextlib
import hashlib
import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
import unittest.mock

import alembic.command
import alembic.script
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import scripts.migrate_to_postgresql as migration
import scripts.migrate_to_postgresql_without_legacy as temporary_migration


def create_source(path: pathlib.Path) -> None:
    import npps4.db.main as main

    migration.model_metadata()
    engine = sqlalchemy.create_engine(f"sqlite:///{path}", connect_args={"autocommit": False})
    try:
        settings = migration.migration_config()
        with engine.begin() as connection:
            settings.attributes["connection"] = connection
            alembic.command.upgrade(settings, "head")
        with sqlalchemy.orm.Session(engine) as session:
            player = main.User(name="海未", locked=True)
            player.id = 7
            session.add(player)
            session.flush()
            session.add(main.Session(user_id=7, token="preserved-token", client_key=b"\x00\xff", server_key=b"\x80"))
            session.commit()
        with engine.begin() as connection:
            connection.execute(sqlalchemy.update(main.DestinyFriendMutex).values(revision=19))
            connection.exec_driver_sql("INSERT INTO sqlite_sequence(name, seq) VALUES ('destiny_mail', 91)")
    finally:
        engine.dispose()


class MigrationValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = self.enterContext(contextlib.closing(sqlite3.connect(":memory:")))
        self.metadata = sqlalchemy.MetaData()
        self.table = sqlalchemy.Table(
            "sample",
            self.metadata,
            sqlalchemy.Column("id", sqlalchemy.BigInteger, primary_key=True),
            sqlalchemy.Column("enabled", sqlalchemy.Boolean, nullable=False),
            sqlalchemy.Column("text", sqlalchemy.String(4)),
            sqlalchemy.Column("data", sqlalchemy.LargeBinary),
            sqlalchemy.Column("count", sqlalchemy.Integer),
        )
        self.source.executescript(
            "CREATE TABLE sample(id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL, text TEXT, data BLOB, count INTEGER);"
            "CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);"
            "INSERT INTO alembic_version VALUES ('test-head');"
        )

    def test_binary_boolean_null_and_unicode_are_preserved(self) -> None:
        row = migration.convert_row(self.table, (2**40, 1, "海未", b"\x00\xff", None))
        self.assertEqual(row, {"id": 2**40, "enabled": True, "text": "海未", "data": b"\x00\xff", "count": None})

    def test_invalid_values_name_the_column(self) -> None:
        for column, invalid in (
            ("id", 2**63),
            ("enabled", 2),
            ("enabled", None),
            ("text", "abcde"),
            ("text", "a\x00"),
            ("data", "text"),
            ("count", 2**31),
        ):
            with self.subTest(column=column, value=invalid):
                values = {"id": 1, "enabled": 0, "text": "a", "data": b"", "count": 0}
                values[column] = invalid
                with self.assertRaisesRegex(ValueError, f"column {column}:"):
                    migration.convert_row(self.table, tuple(values.values()))

    def test_unknown_tables_and_columns_are_rejected(self) -> None:
        migration.validate_schema(self.source, self.metadata, "test-head")
        self.source.execute("CREATE TABLE legacy_receipt(id INTEGER)")
        with self.assertRaisesRegex(ValueError, "unknown=.*legacy_receipt"):
            migration.validate_schema(self.source, self.metadata, "test-head")
        self.source.execute("DROP TABLE legacy_receipt")
        self.source.execute("ALTER TABLE sample ADD COLUMN extra TEXT")
        with self.assertRaisesRegex(ValueError, "columns differ in sample"):
            migration.validate_schema(self.source, self.metadata, "test-head")

    def test_wrong_revision_and_primary_key_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "revision"):
            migration.validate_schema(self.source, self.metadata, "other-head")
        self.source.execute("DROP TABLE sample")
        self.source.execute(
            "CREATE TABLE sample(id INTEGER, enabled BOOLEAN, text TEXT PRIMARY KEY, data BLOB, count INT)"
        )
        with self.assertRaisesRegex(ValueError, "primary key"):
            migration.validate_schema(self.source, self.metadata, "test-head")

    def test_checksum_spans_batches_and_detects_changed_data(self) -> None:
        self.source.executemany(
            "INSERT INTO sample VALUES (?, ?, ?, ?, ?)", [(3, 1, "海未", b"\x00", None), (1, 0, "", b"", 5)]
        )
        digest = hashlib.sha256()
        for values in ([1, False, "", b"", 5], [3, True, "海未", b"\x00", None]):
            digest.update(migration.row_bytes(values))
        with unittest.mock.patch.object(migration, "BATCH_SIZE", 1):
            self.assertEqual(migration.source_summary(self.source, self.table), (2, digest.hexdigest()))
        self.source.execute("UPDATE sample SET count = 7 WHERE id = 1")
        self.assertNotEqual(migration.source_summary(self.source, self.table)[1], digest.hexdigest())

    def test_postgresql_driver_is_explicit(self) -> None:
        self.assertEqual(
            migration.target_url("postgresql://user:secret@localhost/test").drivername, "postgresql+psycopg"
        )
        for url in ("sqlite:///test", "postgresql+psycopg2://user@localhost/test", "postgresql+psycopg://localhost"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                migration.target_url(url)

    def test_missing_source_is_not_created(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        missing = directory / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            migration.migrate(missing, "postgresql://user@localhost/test")
        self.assertFalse(missing.exists())

    def test_database_errors_do_not_print_parameters_or_credentials(self) -> None:
        output = io.StringIO()
        failure = sqlalchemy.exc.IntegrityError("INSERT", {"password": "private-value"}, Exception("private-value"))
        with (
            unittest.mock.patch.object(migration, "migrate", side_effect=failure),
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit) as raised,
        ):
            migration.main(["--target-url", "postgresql://user:private-value@localhost/test"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("IntegrityError", output.getvalue())
        self.assertNotIn("private-value", output.getvalue())

    def test_connection_failure_explains_the_reason_without_passwords(self) -> None:
        output = io.StringIO()
        failure = sqlalchemy.exc.OperationalError(
            None, None, Exception("connection refused on localhost:5432; p@ss p%40ss tls-secret")
        )
        with (
            unittest.mock.patch.object(migration, "migrate", side_effect=failure),
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit),
        ):
            migration.main(["--target-url", "postgresql://user:p%40ss@localhost/test?sslpassword=tls-secret"])
        self.assertIn("connection refused on localhost:5432", output.getvalue())
        for secret in ("p@ss", "p%40ss", "tls-secret"):
            self.assertNotIn(secret, output.getvalue())


class MigrationSchemaTest(unittest.TestCase):
    def test_real_alembic_schema_and_seed_data_match_models(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "main.sqlite3"
        create_source(path)
        metadata = migration.model_metadata()
        head = alembic.script.ScriptDirectory.from_config(migration.migration_config()).get_current_head()
        assert head is not None
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            migration.validate_schema(source, metadata, head)
            for table in metadata.sorted_tables:
                migration.source_summary(source, table)
            self.assertEqual(source.execute("SELECT revision FROM destiny_friend_mutex").fetchone(), (19,))
            self.assertEqual(
                source.execute("SELECT seq FROM sqlite_sequence WHERE name='destiny_mail'").fetchone(), (91,)
            )


class TemporaryMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source_path = directory / "source.sqlite3"
        self.snapshot_path = directory / "snapshot.sqlite3"
        create_source(self.source_path)
        self.source = self.enterContext(contextlib.closing(sqlite3.connect(self.source_path)))
        self.source.executescript(
            "CREATE TABLE codex_test_inventory_grant(grant_key TEXT PRIMARY KEY, user_id INTEGER REFERENCES user(id));"
            "INSERT INTO codex_test_inventory_grant VALUES ('grant', 7);"
            "CREATE TABLE maintenance_fix_receipt(fix_key TEXT PRIMARY KEY);"
            "INSERT INTO maintenance_fix_receipt VALUES ('fix');"
            "CREATE TABLE destiny_daily_friend_scout(account_uid TEXT, day_jst INTEGER, claimed_at_utc INTEGER);"
            "INSERT INTO destiny_scout_account VALUES (7, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');"
            "INSERT INTO daily_friend_scout VALUES (7, 1, 99);"
            "INSERT INTO destiny_daily_friend_scout VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, 99);"
        )
        self.metadata = migration.model_metadata()
        head = alembic.script.ScriptDirectory.from_config(migration.migration_config()).get_current_head()
        assert head is not None
        self.head = head

    def test_only_the_snapshot_loses_excluded_tables_and_original_validation_stays_strict(self) -> None:
        original = self.source_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unknown="):
            migration.validate_schema(self.source, self.metadata, self.head)
        temporary_migration.prepare_snapshot(self.source_path, self.snapshot_path)
        self.assertEqual(self.source_path.read_bytes(), original)
        with contextlib.closing(sqlite3.connect(self.snapshot_path)) as snapshot:
            migration.validate_schema(snapshot, self.metadata, self.head)
            for table in self.metadata.sorted_tables:
                self.assertEqual(
                    migration.source_summary(snapshot, table), migration.source_summary(self.source, table)
                )

    def test_other_unknown_tables_are_still_rejected(self) -> None:
        self.source.execute("CREATE TABLE unexpected_table(id INTEGER PRIMARY KEY)")
        self.source.commit()
        temporary_migration.prepare_snapshot(self.source_path, self.snapshot_path)
        with (
            contextlib.closing(sqlite3.connect(self.snapshot_path)) as snapshot,
            self.assertRaisesRegex(ValueError, "unknown=.*unexpected_table"),
        ):
            migration.validate_schema(snapshot, self.metadata, self.head)

    def test_uncovered_legacy_quotas_are_rejected(self) -> None:
        self.source.execute("UPDATE daily_friend_scout SET claimed_at_utc = 100")
        self.source.commit()
        with self.assertRaisesRegex(ValueError, "1 records lack matching quotas"):
            temporary_migration.prepare_snapshot(self.source_path, self.snapshot_path)

    def test_retained_foreign_keys_to_excluded_tables_are_rejected(self) -> None:
        self.source.execute(
            "CREATE TABLE retained(id INTEGER PRIMARY KEY, grant_key TEXT REFERENCES codex_test_inventory_grant(grant_key))"
        )
        self.source.commit()
        with self.assertRaisesRegex(ValueError, "referenced by retained"):
            temporary_migration.prepare_snapshot(self.source_path, self.snapshot_path)


@unittest.skipUnless(
    os.environ.get("NPPS4_TEST_POSTGRESQL_URL"), "Set NPPS4_TEST_POSTGRESQL_URL to an empty test database"
)
class PostgreSQLMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.destination = os.environ["NPPS4_TEST_POSTGRESQL_URL"]
        self.engine = sqlalchemy.create_engine(migration.target_url(self.destination), hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        with self.engine.connect() as connection:
            migration.require_empty_target(connection)
        self.addCleanup(self.clear_target)

        self.metadata = migration.model_metadata()
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source_path = directory / "main.sqlite3"
        create_source(self.source_path)

    def clear_target(self) -> None:
        with self.engine.begin() as connection:
            metadata = sqlalchemy.MetaData()
            metadata.reflect(connection)
            metadata.drop_all(connection)

    def test_round_trip_checksums_sequences_and_new_writes(self) -> None:
        original = self.source_path.read_bytes()
        report = migration.migrate(self.source_path, self.destination)
        self.assertEqual(self.source_path.read_bytes(), original)
        self.assertEqual(report["tables"]["user"]["rows"], 1)
        import npps4.db.main as main

        with sqlalchemy.orm.Session(self.engine) as session:
            player = session.get(main.User, 7)
            self.assertIsNotNone(player)
            assert player is not None
            self.assertEqual(player.name, "海未")
            self.assertTrue(player.locked)
            self.assertEqual(session.scalar(sqlalchemy.select(main.Session.client_key)), b"\x00\xff")
            self.assertEqual(session.scalar(sqlalchemy.select(main.DestinyFriendMutex.revision)), 19)
            created = main.User(name="new account")
            session.add(created)
            session.flush()
            self.assertEqual(created.id, 8)
            sequence = session.scalar(sqlalchemy.text("SELECT pg_get_serial_sequence('destiny_mail', 'notice_id')"))
            self.assertEqual(
                session.scalar(sqlalchemy.text("SELECT nextval(CAST(:s AS regclass))"), {"s": sequence}), 92
            )
            session.rollback()
        with self.assertRaisesRegex(ValueError, "must be empty"):
            migration.migrate(self.source_path, self.destination)

    def test_check_only_does_not_create_tables(self) -> None:
        report = migration.migrate(self.source_path, self.destination, check_only=True)
        self.assertTrue(report["check_only"])
        with self.engine.connect() as connection:
            migration.require_empty_target(connection)

    def test_failure_rolls_back_ddl_and_data_and_can_retry(self) -> None:
        with (
            unittest.mock.patch.object(migration, "reset_sequences", side_effect=ValueError("injected failure")),
            self.assertRaisesRegex(ValueError, "injected failure"),
        ):
            migration.migrate(self.source_path, self.destination)
        with self.engine.connect() as connection:
            migration.require_empty_target(connection)
        migration.migrate(self.source_path, self.destination)

    def test_postgresql_constraint_failure_rolls_back(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source_path)) as source, source:
            source.execute("DROP TABLE background")
            source.execute(
                "CREATE TABLE background(id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, "
                "background_id INTEGER NOT NULL, insert_date BIGINT NOT NULL)"
            )
            source.execute("INSERT INTO background(user_id, background_id, insert_date) VALUES (999, 1, 0)")
        with self.assertRaises(sqlalchemy.exc.IntegrityError):
            migration.migrate(self.source_path, self.destination)
        with self.engine.connect() as connection:
            migration.require_empty_target(connection)


if __name__ == "__main__":
    unittest.main()
