Null-Pointer Private Server: Revisited
=====

[![Python: 3.14](https://img.shields.io/badge/Python-3.14-blue)](https://www.python.org/)
[![Static Type Checker: Pyright](https://img.shields.io/badge/Static%20Type%20Checker-Pyright-CEB98F)](https://github.com/microsoft/pyright)
[![Code Style: Black](https://img.shields.io/badge/Code%20Style-Black-000000.svg)](https://github.com/psf/black)
[![Build](https://github.com/DarkEnergyProcessor/NPPS4/actions/workflows/main.yml/badge.svg)](https://github.com/DarkEnergyProcessor/NPPS4/actions/workflows/main.yml)

WIP SIF1 v9.11 private server written in Python 3.12+ using FastAPI.

Notes
-----

* This project is currently in need of funding.

* Database schema changes constantly. Expect frequent database wipes.

Requirements
-----

NPPS4 supports Python 3.12 and later. Python 3.11 and earlier is not supported.

Install Manually
----

1. Install Python 3.12 or later.
2. Create virtual environment.
3. Activate it.
4. `pip install -r requirements.txt -r requirements-perf.txt`

Note: If you're using Termux, make sure to have Rust toolchain and remove `-r requirements-perf.txt`.

Development Environment
-----

From a local checkout, use Python 3.12 or later to create and activate a virtual environment:

```sh
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate it with `.venv\Scripts\Activate.ps1` instead.

Install the runtime dependencies and Black:

```sh
python -m pip install -r requirements-dev.txt
```

For the optional performance dependencies, also install `requirements-perf.txt` as described above.
To run the server, complete the configuration and client database setup in [Starting Up](#starting-up).

Follow [CONTRIBUTE.md](CONTRIBUTE.md) when making changes. Format Python files and check their formatting with:

```sh
python -m black -l 120 .
python -m black -l 120 --check .
```

CI also checks types with Pyright. Install Node.js and npm, then use the same Pyright version constraint as CI:

```sh
npm install -g 'pyright@>1.1.376||<1.1.376'
pyright
```

Starting Up
-----

Before starting the server, you need 3 things:
1. Create configuration file.
2. Get private key.
3. Get client game database.

### Create configuration file.

Copy out `config.sample.toml` to `config.toml` and adjust as needed. The file has extensive comments on each
configuration values.

### Get private key.

> [!WARNING]
> A private key is required due to internal request-response verification done in the game.

#### Using provided private key

NPPS4 provides default private key which is used to develop other private servers by the community
and compatible with community-patched APK.

There's nothing to do to use this private key. This private key is used by default as per the sample configuration
key `main.server_private_key`.

The public key of the default server key is:
```
-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDE0RNd6047aeBirzVb61DolatY
YWpaEUIPugOIkobHDc9qVR5iliMLyC0ErXO1siLBwN+U3zaDVOa5uhXbiS7uYq5c
cpxComxTnZtcn/b+mKDpYWLaC0Gv7UoiT8rpNqN3Vko645usz9OFc4VciijsHGRP
XmmmoP6qykfI/vba8wIDAQAB
-----END PUBLIC KEY-----
```

#### Using your own private key

> [!IMPORTANT]
> The private key **must** be 1024-bit RSA stored in PEM format!

If you already have your existing private key, simply put them as `server_key.pem`.

To get the public key, run:

```
python make_server_key.py -p
```

#### Generating your own private key

To generate a new private key, run:

```sh
python make_server_key.py
```

The public key, for your game client will be printed to stdout in PEM format. Patch your game to use the
newly-provided public key.

### Get client game database.

There's 2 simple methods: Having local copy of SIF client files or loading it directly through internet.

#### Create local copy

First, follow the instruction at https://gist.github.com/MikuAuahDark/ece4eb73b3396403c6a2f11610a783b8.

Then set the `download.backend` to `internal` and configure the `download.internal.archive_root` directory in the
configuration file to your local copy of SIF client files.

#### Loading directly from internet

Look at the URL in https://gist.github.com/MikuAuahDark/ece4eb73b3396403c6a2f11610a783b8.

Set the `download.backend` to `n4dlapi` and configure `download.n4dlapi.server` to the URL writen in above gist (not
the gist itself).

Database
-----

Please consult [SQLAlchemy supported database backends](https://docs.sqlalchemy.org/en/20/dialects/index.html) for
more information. If in doubt, SQLite3 is a safe choice if you don't need performance as it's already a part of NPPS4
required dependencies.

Otherwise, install the additional dependencies depending on which backend you want to use. Ensure to install the 
"async" version of the database packages!

* SQLite3: `aiosqlite` (already installed)
* PostgreSQL: `asyncpg`
* MySQL/MariaDB: `asyncmy`

Running
-----

From the project directory, run the server. The command upgrades the database and applies data fixes before serving requests.
```sh
python -m npps4 run --host 0.0.0.0 --port 51376
```

For a multi-worker deployment, run the migrations and data fixes before launching Gunicorn. Event home ZIP
acknowledgments are held in worker memory. Use a single worker or keep each client session on the same worker
when event client resources are enabled.
```sh
alembic upgrade head
python -m npps4 script apply_fixes
pip install gunicorn uvicorn-worker # only needs to be done once

gunicorn --preload npps4.run.app:main -w 4 -k uvicorn_worker.UvicornWorker -b <your lan IP or 0.0.0.0>:51376
```

Private-server gameplay
-----

To carry over the extension launcher's gameplay settings, merge these entries into your existing `[gameplay]` section.
The event settings enable the bundled Marathon rules and client resources. Omit them for deployments that previously
had activity disabled.

```toml
[gameplay]
game_day_offset_minutes = 240
random_live_rotation_anchor = 1794254400
master_challenge_daily_limit = 10
daily_friend_scout = true
scout_audit = true
friend_mail = true
daily_clear_required_groups = { "20010015" = 13, "20010016" = 113 }
daily_clear_bonus_loveca = { "20010015" = 1, "20010016" = 1, "20010017" = 1 }
block_fragment_achievement_claims = true
starter_unit_numbers = [937, 938, 939, 940, 941, 942, 943, 944, 945, 946, 947, 948, 949, 950, 951, 952, 953, 954]
starter_post_tutorial_gold = 1000000
reward_egg_policy = "resources/events/reward_egg_policy.json"
marathon_policy = "resources/events/marathon_rules.json"
event_client_resources = true
```

For retro scouting artwork, set `backend = "custom"` under `[download]` and
`file = "external/retro_scouting_download.py"` under `[download.custom]`. The provider uses your
`[download.n4dlapi]` settings for upstream files and client databases.

Before switching the running service, back up the deployment database and rehearse startup on a copy.
Point a separate config file at the copy, then run
`NPPS4_CONFIG=rehearsal.toml python -m npps4 run --host 127.0.0.1 --port 51400`.
After verifying the rehearsal, configure the service to use the normal run command with its existing host and port.

Updating
-----

Before updating, please ensure if database breakage is not happening. See [DBBREAKAGE.md](DBBREAKAGE.md) for
information.

If there's no database breakage or you already know a way to handle the breakage (either by doing stuff manually or
destroying the database), run:

```
git pull --ff-only
alembic upgrade head
```

Update existing configuration entries that point to the bundled JSON files to use these paths.

| Setting | Bundled file |
| --- | --- |
| `server_data` | `resources/server_data.json` |
| `marathon_policy` | `resources/events/marathon_rules.json` |
| `reward_egg_policy` | `resources/events/reward_egg_policy.json` |

If you customized the bundled server data, carry those edits into its new location before restarting.
Keep pointing to your deployment's own data files if you configured separate paths such as `data/server_data.json`.

Most changes requires a server restart. However, changes that only touch `server_data.json` does not require server
restart as that particular file is hot-reloaded by the server.

Client App
-----

To get compatible client, please look at https://ethanaobrien.github.io/sif-patcher/. Both iOS and Android are
supported.

Contribute
-----

See [CONTRIBUTE.md](CONTRIBUTE.md)


License
-----

zLib license, excluding any files in `external/`.

Public domain/unlicense, any files in `external/`.
