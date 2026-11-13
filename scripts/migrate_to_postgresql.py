import argparse
import collections.abc
import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import urllib.parse

import alembic.command
import alembic.config
import alembic.script
import sqlalchemy
import sqlalchemy.dialects.postgresql
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.pool

from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
BATCH_SIZE = 1000
POSTGRESQL_DIALECT = sqlalchemy.dialects.postgresql.dialect()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def migration_config() -> alembic.config.Config:
    settings = alembic.config.Config()
    settings.set_main_option("script_location", str(ROOT / "npps4" / "alembic"))
    return settings


def model_metadata() -> sqlalchemy.MetaData:
    import npps4.db.main
    import npps4.db.marathon

    return npps4.db.main.common.Base.metadata


def target_url(value: str) -> sqlalchemy.engine.URL:
    url = sqlalchemy.engine.make_url(value)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise ValueError("Target must be a postgresql+psycopg:// URL with a database name")
    return url


def validate_schema(source: sqlite3.Connection, metadata: sqlalchemy.MetaData, head: str) -> None:
    names = {
        row[0]
        for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    expected = set(metadata.tables) | {"alembic_version"}
    if names != expected:
        raise ValueError(
            f"Source tables differ: unknown={sorted(names - expected)}, missing={sorted(expected - names)}"
        )
    if source.execute("SELECT version_num FROM alembic_version").fetchall() != [(head,)]:
        raise ValueError("Source Alembic revision must match the current head; upgrade an offline copy first")
    if source.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
        raise ValueError("Source contains views that are not covered by the main database models")

    for table in metadata.sorted_tables:
        columns = source.execute(f"PRAGMA table_xinfo({quote(table.name)})").fetchall()
        if {row[1] for row in columns} != set(table.c.keys()) or any(row[6] for row in columns):
            raise ValueError(f"Source columns differ in {table.name}")
        primary_key = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if primary_key != [column.name for column in table.primary_key]:
            raise ValueError(f"Source primary key differs in {table.name}")

    if source.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Source SQLite integrity check failed")
    if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Source contains foreign key violations")


def convert_row(table: sqlalchemy.Table, values: tuple[Any, ...]) -> dict[str, Any]:
    result = {}
    identity = tuple(values[index] for index, column in enumerate(table.c) if column.primary_key)
    for column, value in zip(table.c, values, strict=True):
        error = None
        column_type = column.type.dialect_impl(POSTGRESQL_DIALECT)
        if value is None:
            if not column.nullable:
                error = "NULL in a required column"
        elif isinstance(column_type, sqlalchemy.Boolean):
            if type(value) is not int or value not in (0, 1):
                error = "expected boolean 0 or 1"
            else:
                value = bool(value)
        elif isinstance(column_type, sqlalchemy.Integer):
            bits = 64 if isinstance(column_type, sqlalchemy.BigInteger) else 32
            if isinstance(column_type, sqlalchemy.SmallInteger):
                bits = 16
            if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                error = f"expected a signed {bits}-bit integer"
        elif isinstance(column_type, sqlalchemy.String):
            if not isinstance(value, str) or "\x00" in value:
                error = "expected text without NUL characters"
            elif column_type.length is not None and len(value) > column_type.length:
                error = f"text exceeds {column_type.length} characters"
        elif isinstance(column_type, sqlalchemy.LargeBinary):
            if not isinstance(value, bytes):
                error = "expected binary data"
        else:
            error = f"unsupported type {column_type}"
        if error:
            raise ValueError(f"{table.name} primary key {identity!r}, column {column.name}: {error}")
        result[column.name] = value
    return result


def source_rows(source: sqlite3.Connection, table: sqlalchemy.Table) -> collections.abc.Iterator[list[dict[str, Any]]]:
    columns = ", ".join(quote(column.name) for column in table.c)
    order = ", ".join(
        quote(column.name) + (" COLLATE BINARY" if isinstance(column.type, sqlalchemy.String) else "")
        for column in table.primary_key
    )
    cursor = source.execute(f"SELECT {columns} FROM {quote(table.name)} ORDER BY {order}")
    while batch := cursor.fetchmany(BATCH_SIZE):
        yield [convert_row(table, row) for row in batch]


def row_bytes(values: list[Any]) -> bytes:
    encoded = [{"bytes": value.hex()} if isinstance(value, bytes) else value for value in values]
    return (json.dumps(encoded, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def source_summary(source: sqlite3.Connection, table: sqlalchemy.Table) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for batch in source_rows(source, table):
        for row in batch:
            digest.update(row_bytes(list(row.values())))
        count += len(batch)
    return count, digest.hexdigest()


def target_summary(target: sqlalchemy.Connection, table: sqlalchemy.Table) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    order = [
        column.collate("C") if isinstance(column.type, sqlalchemy.String) else column for column in table.primary_key
    ]
    query = sqlalchemy.select(table).order_by(*order)
    with target.execute(query.execution_options(yield_per=BATCH_SIZE)) as rows:
        for row in rows:
            digest.update(row_bytes(list(row)))
            count += 1
    return count, digest.hexdigest()


def require_empty_target(target: sqlalchemy.Connection) -> None:
    if target.scalar(sqlalchemy.text("SELECT current_schema()")) != "public":
        raise ValueError("Target must use the public schema")
    inspector = sqlalchemy.inspect(target)
    objects = (
        inspector.get_table_names()
        + inspector.get_view_names()
        + inspector.get_materialized_view_names()
        + inspector.get_sequence_names()
    )
    if objects:
        raise ValueError(f"Target schema must be empty; found {sorted(set(objects))}")


def reset_sequences(source: sqlite3.Connection, target: sqlalchemy.Connection, tables: list[sqlalchemy.Table]) -> None:
    high_water = {}
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        high_water = dict(source.execute("SELECT name, seq FROM sqlite_sequence"))
    preparer = target.dialect.identifier_preparer
    for table in tables:
        for column in table.primary_key:
            sequence = target.scalar(
                sqlalchemy.text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": preparer.format_table(table), "column_name": column.name},
            )
            if sequence is None:
                continue
            maximum = target.scalar(sqlalchemy.select(sqlalchemy.func.max(column)))
            maximum = max(maximum or 0, high_water.get(table.name, 0))
            if maximum >= 2**63 - 1:
                raise ValueError(f"Sequence exhausted in {table.name}.{column.name}")
            target.execute(
                sqlalchemy.text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                {"sequence": sequence, "value": max(1, maximum), "called": maximum >= 1},
            )


def migrate(source_path: pathlib.Path, destination: str, check_only: bool = False) -> dict[str, Any]:
    source_path = source_path.resolve(strict=True)
    url = target_url(destination)
    metadata = model_metadata()
    settings = migration_config()
    head = alembic.script.ScriptDirectory.from_config(settings).get_current_head()
    if head is None:
        raise ValueError("No Alembic head found")
    tables = list(metadata.sorted_tables)
    with contextlib.ExitStack() as resources:
        source = resources.enter_context(
            contextlib.closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True))
        )
        source.execute("BEGIN")
        validate_schema(source, metadata, head)
        summaries = {table.name: source_summary(source, table) for table in tables}
        engine = sqlalchemy.create_engine(url, poolclass=sqlalchemy.pool.NullPool, hide_parameters=True)
        resources.callback(engine.dispose)
        with engine.begin() as target:
            if check_only:
                target.exec_driver_sql("SET TRANSACTION READ ONLY")
            else:
                target.execute(sqlalchemy.text("SELECT pg_advisory_xact_lock(51376, 4)"))
            require_empty_target(target)
            if not check_only:
                target.exec_driver_sql("SET LOCAL search_path TO public")
                settings.attributes["connection"] = target
                alembic.command.upgrade(settings, "head")
                actual = sqlalchemy.MetaData()
                actual.reflect(target)
                if set(actual.tables) != set(metadata.tables) | {"alembic_version"}:
                    raise ValueError("Alembic tables differ from the current models")
                for table in tables:
                    if set(actual.tables[table.name].c.keys()) != set(table.c.keys()):
                        raise ValueError(f"Alembic columns differ in {table.name}")

                # Fresh migrations seed mutex rows; the source snapshot owns their state.
                for table in reversed(tables):
                    target.execute(table.delete())
                for table in tables:
                    for batch in source_rows(source, table):
                        target.execute(table.insert(), batch)
                    if target_summary(target, table) != summaries[table.name]:
                        raise ValueError(f"Row count or checksum mismatch in {table.name}")
                    print(f"Verified {table.name}: {summaries[table.name][0]} rows")
                reset_sequences(source, target, tables)
        return {
            "check_only": check_only,
            "source": str(source_path),
            "target": url.set(query={}).render_as_string(hide_password=True),
            "revision": head,
            "tables": {name: {"rows": count, "sha256": digest} for name, (count, digest) in summaries.items()},
        }


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Migrate an offline NPPS4 SQLite main database into empty PostgreSQL.")
    parser.add_argument("--source", type=pathlib.Path, default=ROOT / "data" / "main.sqlite3")
    parser.add_argument("--target-url", default=os.environ.get("NPPS4_MIGRATION_TARGET_URL"))
    parser.add_argument(
        "--check-only", action="store_true", help="Validate source data and target emptiness without writes"
    )
    options = parser.parse_args(args)
    if not options.target_url:
        parser.error("Specify --target-url or NPPS4_MIGRATION_TARGET_URL")
    try:
        report = migrate(options.source, options.target_url, options.check_only)
    except sqlalchemy.exc.SQLAlchemyError as exc:
        original = getattr(exc, "orig", None)
        state = getattr(original, "sqlstate", None)
        diagnostic = getattr(original, "diag", None)
        location = ", ".join(
            f"{name}={value}"
            for name in ("table_name", "column_name", "constraint_name")
            if (value := getattr(diagnostic, name, None))
        )
        details = [type(exc).__name__, f"SQLSTATE={state}"]
        if location:
            details.append(location)
        message = f"Migration failed ({', '.join(details)})\n"
        if isinstance(exc, sqlalchemy.exc.OperationalError) and state is None:
            reason = str(original)
            url = sqlalchemy.engine.make_url(options.target_url)
            secrets = [options.target_url, url.password]
            for key, value in url.query.items():
                if "password" in key.lower():
                    secrets.extend((value,) if isinstance(value, str) else value)
            for secret in secrets:
                if isinstance(secret, str) and secret:
                    for encoded in (secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)):
                        reason = reason.replace(encoded, "***")
            message += reason + "\n"
        parser.exit(1, message)
    except (OSError, ValueError, sqlite3.Error, ImportError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))


async def run_script(args: list[str]) -> None:
    main(args)


if __name__ == "__main__":
    main()
