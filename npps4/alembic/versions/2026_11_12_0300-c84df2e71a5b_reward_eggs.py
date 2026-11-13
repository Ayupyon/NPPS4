import alembic.op
import sqlalchemy
import sqlalchemy.dialects.mysql

revision = "c84df2e71a5b"
down_revision = "e091c292a3da"
branch_labels = None
depends_on = None

ID_TYPE = sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")
KEY_TYPE = sqlalchemy.Text().with_variant(
    sqlalchemy.dialects.mysql.VARCHAR(255, collation="utf8mb4_bin"), "mysql", "mariadb"
)
JSON_TYPE = sqlalchemy.Text().with_variant(sqlalchemy.dialects.mysql.LONGTEXT(), "mysql", "mariadb")
EGG_TABLE = "destiny_reward_egg"
RECEIPT_TABLE = "destiny_reward_egg_receipt"
LIVE_TABLE = "destiny_reward_egg_live"


def user_id_column() -> sqlalchemy.Column[int]:
    return sqlalchemy.Column("user_id", ID_TYPE, sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)


def upgrade() -> None:
    op = alembic.op
    inspector = None if op.get_context().as_sql else sqlalchemy.inspect(op.get_bind())
    tables = {
        EGG_TABLE: (
            user_id_column(),
            sqlalchemy.Column("pending_event", sqlalchemy.Integer(), nullable=False, server_default="0"),
            sqlalchemy.Column("pending_spec", sqlalchemy.Integer(), nullable=False, server_default="0"),
            sqlalchemy.Column("saved_spec", sqlalchemy.Integer(), nullable=False, server_default="0"),
            sqlalchemy.Column("generation", sqlalchemy.Integer(), nullable=False, server_default="0"),
            sqlalchemy.Column("last_choice", sqlalchemy.Integer()),
            sqlalchemy.Column("openings", sqlalchemy.Integer(), nullable=False, server_default="0"),
        ),
        RECEIPT_TABLE: (
            user_id_column(),
            sqlalchemy.Column("action", sqlalchemy.String(32), primary_key=True),
            sqlalchemy.Column("request_key", KEY_TYPE, primary_key=True),
            sqlalchemy.Column("payload_hash", sqlalchemy.String(64), nullable=False),
            sqlalchemy.Column("response", JSON_TYPE, nullable=False),
        ),
        LIVE_TABLE: (
            user_id_column(),
            sqlalchemy.Column("live_in_progress_id", ID_TYPE, nullable=False),
            sqlalchemy.Column("live_difficulty_id", ID_TYPE, nullable=False),
        ),
    }

    for name, columns in tables.items():
        if inspector is None or not inspector.has_table(name):
            op.create_table(name, *columns, mysql_charset="utf8mb4", mariadb_charset="utf8mb4")
        elif not {column.name for column in columns}.issubset(
            column["name"] for column in inspector.get_columns(name)
        ) or inspector.get_pk_constraint(name)["constrained_columns"] != [
            column.name for column in columns if column.primary_key
        ]:
            raise RuntimeError(f"Unexpected reward egg schema in {name}")


def downgrade() -> None:
    # The extension reads these tables when resuming Lives or replaying receipts
    pass
