Scripts
=====

Scripts are code that performs automated tasks to NPPS4 itself.

## Local deployment operations

The legacy Test tools have native equivalents that use the configured database,
including PostgreSQL. They never start or stop an HTTP server. Historical
account-specific jobs and the old extension launchers are not required by the
native server and are not replayed during startup.

### Preview and settle a single Marathon period

The snapshot `resources/events/marathon_rules.local-test-20261113.json` records
the old Test server's early closure of its second period. It contains rules only,
not player progress, ranking receipts or already granted gifts. The default
policy retains the original dates because existing databases may have registered
that policy. Do not overwrite a published policy without reconciling its database
record. See `docs/local-progress-sync-20261113.md` for the cutover boundary.

First preview against the original configured policy:

```sh
python -m npps4 script settle_marathon plan --period-key sif005-love-heart-history-20261109-r1 --target-policy resources/events/marathon_rules.local-test-20261113.json --output settlement-plan.json
```

Review the plan's rankings and rewards. Stop every server or script writing to
this database and make a database backup using the database's native tools.
Back up `config.toml`, then point `gameplay.marathon_policy` at the reviewed target
file. Do not start the server between changing the configuration and applying:

```sh
python -m npps4 script settle_marathon apply --plan settlement-plan.json --server-stopped --backup-confirmed
```

These flags attest to operator actions; they do not stop services or create a
backup. The plan is bound to the configured database URL and target policy. A
changed standing, unfinished song, previous inconsistent grant or changed rule
rejects the operation. Only the selected period is frozen and awarded. An exact
retry after success returns `already_settled=true`; failures roll back all
database changes. If apply fails, restore the original configuration before
restarting. Keep the target configuration after success. No player database is
copied from the old deployment, and previous local grants are not imported by
this tool. Reconcile those separately if this is a migration of the same users.
Plans contain player IDs; keep plans and backups private and outside Git.

### Reward egg maintenance

```sh
python -m npps4 script reward_egg_maintenance check
```

For an intentional rollback of the egg feature, stop all database writers and
back up first, then use `drain --server-stopped --backup-confirmed`. Drain cancels
pending/active eggs, restores the saved normal box, and retains normal box
progress, inventory and reward receipt ledgers. Unfinished songs or missing
restore points reject the operation. This is not a routine startup command.

### Account grants

These commands preview by default. Choose the exact internal user ID and append
`--apply` only to grant the displayed amount. Repeating an apply adds the amount
again. They do not target a default account.

```sh
python -m npps4 script add_gold --user-id 123 1000
python -m npps4 script add_sis_pack --user-id 123
```

The SIS pack preserves the legacy definition: nine of each normal SIS, eleven
of each standard Smile/Pure/Cool Fortune. It validates the client SIS definitions
before writing. Test-only rank/account grants remain historical operations,
not automatic setup defaults.

To run the scripts directly like ordinary Python scripts, run this once in virtual environment:
```
pip install -e .
```

Then you can run them like ordinary scripts (e.g. `python script.py ...`)

If you don't want to do `pip install -e .`, then to run script, you must run it like this:
```
python -m npps4.script script.py ...
```

Will run `script.py` in the working directory.

## Review a historical achievement claim

With the server stopped, export a review plan for an unresolved hold in the main SQLite database. Use the hold's exact period key.

```sh
python scripts/review_achievement_claim.py plan MAIN_DB USER_ID ACHIEVEMENT_ID BLOCKED_PERIOD > review.json
```

Set `approved` to `true` and fill in `approved_by` and `evidence`. Set `decision` to `confirm_paid` to record a proven payout or `allow_retry` to release the hold when evidence shows the reward is still due. Leave the snapshot and its checksum intact.

```sh
python scripts/review_achievement_claim.py apply MAIN_DB review.json
```

## Migrate the main SQLite database to PostgreSQL

Stop the server and all scripts that write to its database. Keep an offline backup made with SQLite's backup API,
including any changes in the WAL. Keep the existing private key, secret key, gameplay settings and client resources.
The migration copies only the server-owned main database; client resource databases under `data/db/` remain SQLite.

Use an existing PostgreSQL database whose `public` schema has no tables, views or sequences. The migration account
needs permission to create tables and sequences. Install the Psycopg 3 driver in the server's Python environment:

```sh
python -m pip install 'psycopg[binary]'
```

From the project root, provide the destination through `NPPS4_MIGRATION_TARGET_URL` or `--target-url`. A plain
`postgresql://` URL is also accepted and uses Psycopg 3. First run the read-only checks:

```sh
export NPPS4_MIGRATION_TARGET_URL='postgresql+psycopg://user:password@localhost:5432/npps4'
python -m npps4 script migrate_to_postgresql --check-only
python -m npps4 script migrate_to_postgresql
```

The source defaults to `<project root>/data/main.sqlite3`; use `--source /absolute/path/main.sqlite3` to override it.
The standalone module entry point is `python -m scripts.migrate_to_postgresql`. The source revision must match the
current Alembic head. Upgrade and rehearse an offline copy first if it is older. Unknown tables, extra columns,
views, incompatible primary keys or invalid values stop the migration. Review legacy or extension tables before
migrating; the script never silently drops their data.

`--check-only` validates source structure, SQLite integrity and foreign keys, field types and ranges, and destination
emptiness. It does not create PostgreSQL objects. Target constraints and creation permissions are exercised during
the actual migration. The script builds tables through Alembic, replaces migration seed rows with the source state,
copies data in batches, verifies each table's row count and SHA-256 checksum, and resets generated-ID sequences.
SQLite AUTOINCREMENT high-water marks are preserved even when the corresponding rows have been deleted.
All PostgreSQL DDL and imported data share one transaction; a failure rolls them back so the empty destination can
be retried. The source and configuration are never changed. Successful output includes per-table counts and checksums.

After success, set the same explicit driver URL in `config.toml`:

```toml
[database]
url = "postgresql+psycopg://user:password@localhost:5432/npps4"
```

Update any `NPPS4_CONFIG_DATABASE_URL` deployment override as well, then start normally:

```sh
python -m npps4 run --host 127.0.0.1 --port 51376
```

Startup runs the normal migrations and data fixes. Before opening access, verify an existing account, inventory,
new-account creation, reward claims and retries, Live settlement, and enabled events. Returning to the old SQLite
database after PostgreSQL accepts new writes requires reconciling those changes.

Run migration tests with:

```sh
python -m unittest discover -s tests -p test_postgresql_migration.py -v
```

To include the PostgreSQL integration tests, set `NPPS4_TEST_POSTGRESQL_URL` to a separate disposable database with an
empty `public` schema. The integration tests create and remove their tables there; never use the migration destination
or a live database for this variable. Without it, the PostgreSQL tests are skipped.
