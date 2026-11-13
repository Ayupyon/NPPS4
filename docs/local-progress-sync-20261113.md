# Local deployment progress synchronized on 2026-11-13

This change carries reusable operations and an exact rules snapshot from the
old local Test deployment into the native NPPS4 repository. Existing native
gameplay integrations remain the implementation baseline. This is a repository
change, not a database migration or a running-service deployment.

## Configurations and data

| Area | Local deployment | Native repository |
| --- | --- | --- |
| Main database | Separate formal and Test SQLite databases | Existing local configuration connects to PostgreSQL; connection settings retained |
| Activity | Formal disabled; Test enabled | Current development configuration enables events |
| Second period | Closed at 2026-11-13 07:20:29 UTC, zero grace | Exact closed snapshot added separately; original published default policy retained |
| Server data | Test-specific scouting, drops and serial codes | Existing bundled data is semantically equal to old Test; formal serial codes differ |
| Reward eggs | Confirmed 20-egg policy | Same bytes already present |
| Event goals and extra charts | Current Test configuration | Same JSON values already present |
| Client resources | Current Test presentation and home bindings | Same current bindings and referenced resources; obsolete packages are not required |
| Beatmaps | 1,910 files | Matching bytes at relocated `resources/beatmaps` paths |

The old Test's closed second-period rule digest is
`5705486d1d07b12863be3bf8fff7bb1a2863c33ee8af2038410a35af18008239`.
The development PostgreSQL database was read-only checked before this change:
it still held the original digest
`0fc4437af4466f58e8b6dc384e8b608adf5ad524cdb016ddbe203ae44cee284f`
with no frozen ranking timestamp. Changing the live file alone would violate
the server's published-rule check. Therefore `config.toml` and its active policy
are deliberately not switched by this repository sync.

The snapshot contains no player database or settlement receipts. The old Test's
rankings, gifts, account grants and LP corrections are data changes; copying code
does not migrate them. A database cutover requires a separate reconciliation of
existing users and rewards. The settlement tool recomputes awards from the target
database and does not claim to reproduce old player progress.

## Completed functionality retained

Native implementations already cover the 04:00 Beijing game-day boundary,
anchored daily-song rotation, ten daily MASTER attempts, starter cards and gold,
daily achievements and claim protection, daily free friend scouting and audit,
reward eggs, friend-only mail and native command identifiers, player ranking
signatures, inventory contracts, Marathon gameplay, event downloads and song goals.
The native event party-list check already rejects unavailable event songs.

## Operations brought across

- Targeted offline Marathon preview and settlement: one explicit period,
  unchanged rewards, unfinished-live protection, fresh standings check,
  atomic ranking/gift writes and retry protection.
- Reward egg inspection and rollback drain using the native database models.
- Gold and SIS grants with an explicit account and read-only previews.
- Exact closed Test rules in `resources/events/marathon_rules.local-test-20261113.json`.

The old packed-executable bootstrap, cloning probes and hard-coded personal
account jobs are deployment-specific history. They are not native startup hooks.
The historical operations for changing a particular user's level or LP must not
be replayed against different users in PostgreSQL.

No main database table or column is added by this change. The commands operate
on existing tables through the existing transaction context. See the scripts
README for preview, backup, configuration-switch and apply steps. Repository
tests use disposable SQLite databases; live PostgreSQL settlement and phone
acceptance remain deployment checks.

## Verification of this repository change

- The complete existing and added unittest suite ran against an isolated config:
  67 tests, 63 passed and 4 optional PostgreSQL integration tests skipped.
- Eleven added cases cover targeted settlement, concurrent retry, rollback after
  a partial grant, changed standings, unfinished songs, edited plans, database
  binding, gold/SIS previews and egg restoration guards.
- Pyright reported zero errors and warnings with the repository virtual environment;
  Black and Git whitespace checks passed.
- A PostgreSQL preview ran with `default_transaction_read_only=on`: four point
  ranking entries, three score ranking entries and seven proposed reward groups.
  No live settlement, schema migration, inventory write or service restart ran.
- The closed Test snapshot matches its local source byte for byte.

Live PostgreSQL write-path rehearsal and phone acceptance remain unperformed.
