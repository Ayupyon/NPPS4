"""Reproduce deployed 59.4 reference data without modifying download originals.

Only the two enumerated live reference tables can be patched. Player databases
are never opened. A conflicting reference row stops loading instead of silently
overwriting a local customization.
"""

import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile

from typing import Any

TABLE_KEYS = {
    "live_goal_reward_m": ("live_goal_reward_id",),
    "special_live_rotation_m": ("rotation_group_id", "base_date"),
}


def _rows(database: sqlite3.Connection, table: str) -> tuple[list[str], dict[Any, tuple[Any, ...]]]:
    key = TABLE_KEYS[table]
    cursor = database.execute(f'SELECT * FROM "{table}"')
    columns = [field[0] for field in cursor.description]
    indices = [columns.index(column) for column in key]
    return columns, {tuple(row[index] for index in indices): tuple(row) for row in cursor}


def apply_overlay(database: sqlite3.Connection, patch: dict[str, Any], *, verify_only: bool = False) -> int:
    """Caller owns the transaction. Exact old/new rows make retries harmless."""
    if patch.get("schema_version") != 1 or patch.get("client_master_version") != "59.4":
        raise ValueError("Unsupported live master overlay")
    operations = patch["operations"]
    if set(operations) != set(TABLE_KEYS):
        raise ValueError("Unexpected live master overlay tables")
    changes = 0
    for table, operation in operations.items():
        key = TABLE_KEYS[table]
        columns, actual = _rows(database, table)
        for action in ("remove_exact_rows", "insert_rows"):
            for row in operation[action]:
                if set(row) != set(columns):
                    raise ValueError(f"Unexpected columns in {table}")
                expected = tuple(row[column] for column in columns)
                identity = tuple(row[column] for column in key)
                previous = actual.get(identity)
                if previous is not None and previous != expected:
                    raise ValueError(f"Conflicting reference row in {table}: {identity}")
                remove = action == "remove_exact_rows"
                needed = previous is not None if remove else previous is None
                if not needed:
                    continue
                if verify_only:
                    raise ValueError(f"Unapplied reference overlay in {table}")
                if remove:
                    where = " AND ".join(f'"{column}"=?' for column in key)
                    database.execute(f'DELETE FROM "{table}" WHERE {where}', identity)
                    actual.pop(identity)
                else:
                    names = ",".join(f'"{column}"' for column in columns)
                    params = ",".join("?" for _ in columns)
                    database.execute(f'INSERT INTO "{table}" ({names}) VALUES ({params})', expected)
                    actual[identity] = expected
                changes += 1
    return changes


def _verify(path: pathlib.Path, patch: dict[str, Any]) -> bool:
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                return False
            apply_overlay(database, patch, verify_only=True)
        return True
    except (sqlite3.Error, ValueError):
        return False


def _fingerprint(path: pathlib.Path) -> str:
    hashes = []
    for part in (path, pathlib.Path(str(path) + "-wal")):
        if part.is_file():
            with part.open("rb") as stream:
                hashes.append(hashlib.file_digest(stream, "sha256").hexdigest())
    return ":".join(hashes)


def prepare_live_database(source: str, patch_path: pathlib.Path, cache_dir: pathlib.Path) -> str:
    """Publish a fully verified copy; source and cache readers remain untouched."""
    source_path = pathlib.Path(source).resolve()
    content = patch_path.read_bytes()
    patch = json.loads(content)
    source_sha = _fingerprint(source_path)
    identity = hashlib.sha256((source_sha + hashlib.sha256(content).hexdigest()).encode()).hexdigest()
    cache_dir = cache_dir.resolve()
    target = cache_dir / f"live-59.4-{identity}.db_"
    if target.is_file() and _verify(target, patch):
        return str(target)
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="live-59.4-", suffix=".tmp", dir=cache_dir)
    os.close(fd)
    temporary = pathlib.Path(name)
    try:
        with (
            contextlib.closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as original,
            contextlib.closing(sqlite3.connect(temporary)) as database,
        ):
            # SQLite backup includes a WAL if a downloader or prior tool used one.
            original.backup(database)
            with database:
                apply_overlay(database, patch)
        if not _verify(temporary, patch):
            raise ValueError("Patched live reference database failed verification")
        if _fingerprint(source_path) != source_sha:
            raise ValueError("Live reference source changed during overlay preparation; retry loading")
        try:
            os.replace(temporary, target)
        except OSError:
            # A concurrent process may have published and opened the same copy.
            if not _verify(target, patch):
                raise
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)
