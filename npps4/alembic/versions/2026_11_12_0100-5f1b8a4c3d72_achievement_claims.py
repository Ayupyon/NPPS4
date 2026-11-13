import alembic.op
import sqlalchemy
import sqlalchemy.dialects.mysql

revision = "5f1b8a4c3d72"
down_revision = "88cda5c24a01"
branch_labels = None
depends_on = None

ID_TYPE = sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")
KEY_TYPE = sqlalchemy.Text().with_variant(
    sqlalchemy.dialects.mysql.VARCHAR(255, collation="utf8mb4_bin"), "mysql", "mariadb"
)
JSON_TYPE = sqlalchemy.Text().with_variant(sqlalchemy.dialects.mysql.LONGTEXT(), "mysql", "mariadb")
CLAIM_TABLE = "destiny_achievement_claim"


def user_id_column() -> sqlalchemy.Column[int]:
    return sqlalchemy.Column("user_id", ID_TYPE, sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)


def upgrade() -> None:
    op = alembic.op
    connection = op.get_bind()
    inspector = None if op.get_context().as_sql else sqlalchemy.inspect(connection)
    rewards_default = "[]" if connection.dialect.name == "sqlite" else None
    tables = (
        (
            CLAIM_TABLE,
            (
                user_id_column(),
                sqlalchemy.Column("achievement_id", ID_TYPE, primary_key=True),
                sqlalchemy.Column("period_key", KEY_TYPE, primary_key=True),
                sqlalchemy.Column("owning_achievement_id", ID_TYPE, nullable=False),
                sqlalchemy.Column("claimed_at", ID_TYPE, nullable=False),
                sqlalchemy.Column("request_key", KEY_TYPE, nullable=False),
                sqlalchemy.Column("rewards_json", JSON_TYPE, nullable=False, server_default=rewards_default),
            ),
            (),
        ),
        (
            f"{CLAIM_TABLE}_receipt",
            (
                user_id_column(),
                sqlalchemy.Column("action", sqlalchemy.String(32), primary_key=True),
                sqlalchemy.Column("request_key", KEY_TYPE, primary_key=True),
                sqlalchemy.Column("payload_hash", sqlalchemy.String(64), nullable=False),
                sqlalchemy.Column("response_json", JSON_TYPE, nullable=False),
            ),
            (),
        ),
        (
            f"{CLAIM_TABLE}_review",
            (
                user_id_column(),
                sqlalchemy.Column("achievement_id", ID_TYPE, primary_key=True),
                sqlalchemy.Column("blocked_period", KEY_TYPE, primary_key=True),
                sqlalchemy.Column("owning_achievement_id", ID_TYPE, nullable=False),
                sqlalchemy.Column("reason", sqlalchemy.Text(), nullable=False),
                sqlalchemy.Column("evidence_json", JSON_TYPE, nullable=False),
                sqlalchemy.Column("created_at", ID_TYPE, nullable=False),
                sqlalchemy.Column("resolved_at", ID_TYPE),
            ),
            (),
        ),
        (
            f"{CLAIM_TABLE}_migration",
            (
                sqlalchemy.Column("migration_key", KEY_TYPE, primary_key=True),
                sqlalchemy.Column("applied_at", ID_TYPE, nullable=False),
                sqlalchemy.Column("report_json", JSON_TYPE, nullable=False),
            ),
            (),
        ),
        (
            f"{CLAIM_TABLE}_review_decision",
            (
                user_id_column(),
                sqlalchemy.Column("achievement_id", ID_TYPE, primary_key=True),
                sqlalchemy.Column("blocked_period", KEY_TYPE, primary_key=True),
                sqlalchemy.Column("plan_hash", sqlalchemy.String(64), nullable=False),
                sqlalchemy.Column("decision", sqlalchemy.String(16), nullable=False),
                sqlalchemy.Column("approved_by", sqlalchemy.Text(), nullable=False),
                sqlalchemy.Column("evidence", sqlalchemy.Text(), nullable=False),
                sqlalchemy.Column("plan_json", JSON_TYPE, nullable=False),
                sqlalchemy.Column("applied_at", ID_TYPE, nullable=False),
            ),
            (sqlalchemy.CheckConstraint("decision IN ('confirm_paid', 'allow_retry')"),),
        ),
    )
    for name, columns, constraints in tables:
        if inspector is None or not inspector.has_table(name):
            op.create_table(name, *columns, *constraints, mysql_charset="utf8mb4", mariadb_charset="utf8mb4")
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(name)}
        primary_key = [column.name for column in columns if column.primary_key]
        if (
            not {column.name for column in columns}.issubset(existing_columns)
            or inspector.get_pk_constraint(name)["constrained_columns"] != primary_key
        ):
            raise RuntimeError(f"Unexpected achievement claim schema in {name}")


def downgrade() -> None:
    # The previous launcher still reads these tables when replaying receipts
    pass
