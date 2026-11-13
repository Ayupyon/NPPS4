import json

import alembic.op
import sqlalchemy
import sqlalchemy.dialects.mysql

revision = "88cda5c24a01"
down_revision = "bf639a420bd4"
branch_labels = None
depends_on = None

DAY_TABLE = "destiny_master_challenge_day"
START_TABLE = "destiny_master_challenge_start"
ACTIVE_COLUMN = "master_challenge_request_key"
ID_TYPE = sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")


def backfill_active_requests(connection: sqlalchemy.Connection):
    egg_table_name = "destiny_reward_egg_live"
    if not sqlalchemy.inspect(connection).has_table(egg_table_name):
        return

    metadata = sqlalchemy.MetaData()
    active = sqlalchemy.Table("live_in_progress", metadata, autoload_with=connection)
    receipt = sqlalchemy.Table(START_TABLE, metadata, autoload_with=connection)
    egg = sqlalchemy.Table(egg_table_name, metadata, autoload_with=connection)
    active_players = (
        connection.execute(sqlalchemy.select(active.c.id, active.c.user_id, active.c.deck_data)).mappings().all()
    )
    for playing in active_players:
        query = (
            sqlalchemy.select(receipt)
            .join(
                egg,
                (egg.c.user_id == receipt.c.user_id)
                & (egg.c.live_in_progress_id == receipt.c.live_in_progress_id)
                & (egg.c.live_difficulty_id == receipt.c.live_difficulty_id),
            )
            .where(receipt.c.user_id == playing["user_id"], receipt.c.live_in_progress_id == playing["id"])
        )
        starts = connection.execute(query.order_by(receipt.c.started_at.desc()).limit(2)).mappings().all()
        if not starts or (len(starts) == 2 and starts[0]["started_at"] == starts[1]["started_at"]):
            continue
        start = starts[0]
        try:
            deck = json.loads(playing["deck_data"])
            saved_deck = json.loads(start["response_json"])["live_list"][0]["deck_info"]
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if deck == saved_deck:
            connection.execute(
                active.update().where(active.c.id == playing["id"]).values({ACTIVE_COLUMN: start["request_key"]})
            )


def upgrade() -> None:
    op = alembic.op
    connection = op.get_bind()
    inspector = None if op.get_context().as_sql else sqlalchemy.inspect(connection)
    has_day = inspector is not None and inspector.has_table(DAY_TABLE)
    has_start = inspector is not None and inspector.has_table(START_TABLE)

    if not has_day:
        op.create_table(
            DAY_TABLE,
            sqlalchemy.Column("user_id", ID_TYPE, primary_key=True, autoincrement=False),
            sqlalchemy.Column("game_day", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("used", sqlalchemy.Integer(), nullable=False, server_default=sqlalchemy.text("0")),
        )
    elif connection.dialect.name == "sqlite":
        # Rebuilding drops the legacy unnamed CHECK that capped used attempts at ten.
        with op.batch_alter_table(DAY_TABLE, recreate="always") as batch:
            batch.alter_column("user_id", existing_type=sqlalchemy.Integer(), nullable=False)

    if not has_start:
        op.create_table(
            START_TABLE,
            sqlalchemy.Column("user_id", ID_TYPE, primary_key=True),
            sqlalchemy.Column(
                "request_key", sqlalchemy.String(255).with_variant(sqlalchemy.Text(), "sqlite"), primary_key=True
            ),
            sqlalchemy.Column("payload_hash", sqlalchemy.Text(), nullable=False),
            sqlalchemy.Column("game_day", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("live_difficulty_id", ID_TYPE, nullable=False),
            sqlalchemy.Column("live_in_progress_id", ID_TYPE, nullable=False),
            sqlalchemy.Column("started_at", ID_TYPE, nullable=False),
            sqlalchemy.Column(
                "response_json",
                sqlalchemy.Text().with_variant(sqlalchemy.dialects.mysql.LONGTEXT(), "mysql", "mariadb"),
                nullable=False,
            ),
        )
        op.create_index("destiny_master_challenge_start_day", START_TABLE, ["user_id", "game_day"])

    op.add_column("live_in_progress", sqlalchemy.Column(ACTIVE_COLUMN, sqlalchemy.Text(), nullable=True))
    if has_start:
        backfill_active_requests(connection)


def downgrade() -> None:
    # Retain the quota and receipts for deployments that return to the external launcher.
    alembic.op.drop_column("live_in_progress", ACTIVE_COLUMN)
