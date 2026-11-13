import alembic.op
import sqlalchemy

revision = "e091c292a3da"
down_revision = "5f1b8a4c3d72"
branch_labels = None
depends_on = None

ID_TYPE = sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")
QUOTA_TABLE = "daily_friend_scout"
LEGACY_QUOTA_TABLE = "destiny_daily_friend_scout"
ACCOUNT_TABLE = "destiny_scout_account"
LOG_TABLE = "destiny_scout_log"
LOG_INDEX = "destiny_scout_log_account_time"


def upgrade() -> None:
    op = alembic.op
    connection = op.get_bind()
    inspector = None if op.get_context().as_sql else sqlalchemy.inspect(connection)
    if inspector is None or not inspector.has_table(QUOTA_TABLE):
        op.create_table(
            QUOTA_TABLE,
            sqlalchemy.Column(
                "user_id", ID_TYPE, sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
            ),
            sqlalchemy.Column("game_day", sqlalchemy.Integer(), primary_key=True),
            sqlalchemy.Column("claimed_at_utc", ID_TYPE, nullable=False),
        )

    if inspector is not None and inspector.has_table(LEGACY_QUOTA_TABLE):
        metadata = sqlalchemy.MetaData()
        quota = sqlalchemy.Table(QUOTA_TABLE, metadata, autoload_with=connection)
        legacy = sqlalchemy.Table(LEGACY_QUOTA_TABLE, metadata, autoload_with=connection)
        accounts = sqlalchemy.Table(ACCOUNT_TABLE, metadata, autoload_with=connection)
        players = sqlalchemy.Table("user", metadata, autoload_with=connection)
        active_claims = legacy.join(accounts, legacy.c.account_uid == accounts.c.uid).join(
            players, accounts.c.user_id == players.c.id
        )
        claims = (
            sqlalchemy.select(accounts.c.user_id, legacy.c.day_jst, legacy.c.claimed_at_utc)
            .select_from(
                active_claims.outerjoin(
                    quota, sqlalchemy.and_(quota.c.user_id == accounts.c.user_id, quota.c.game_day == legacy.c.day_jst)
                )
            )
            .where(quota.c.user_id.is_(None))
        )
        connection.execute(quota.insert().from_select(["user_id", "game_day", "claimed_at_utc"], claims))

    if inspector is None or not inspector.has_table(ACCOUNT_TABLE):
        op.create_table(
            ACCOUNT_TABLE,
            sqlalchemy.Column("user_id", ID_TYPE, primary_key=True, autoincrement=False),
            sqlalchemy.Column("uid", sqlalchemy.String(32), nullable=False, unique=True),
            sqlalchemy.CheckConstraint("length(uid) = 32"),
        )

    log_exists = inspector is not None and inspector.has_table(LOG_TABLE)
    if not log_exists:
        op.create_table(
            LOG_TABLE,
            sqlalchemy.Column("draw_uid", sqlalchemy.String(32), primary_key=True),
            sqlalchemy.Column("account_uid", sqlalchemy.String(32), nullable=False),
            sqlalchemy.Column("user_id", ID_TYPE, nullable=False),
            sqlalchemy.Column("nickname", sqlalchemy.Text(), nullable=False),
            sqlalchemy.Column("drawn_at_utc", sqlalchemy.String(32), nullable=False),
            sqlalchemy.Column("draw_type", sqlalchemy.String(32), nullable=False),
            sqlalchemy.Column("pool_id", ID_TYPE, nullable=False),
            sqlalchemy.Column("draw_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("cost_type", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("cost_item_id", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("n_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("r_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("sr_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("ssr_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.Column("ur_count", sqlalchemy.Integer(), nullable=False),
            sqlalchemy.CheckConstraint("draw_count > 0"),
            sqlalchemy.CheckConstraint(
                "n_count >= 0 AND r_count >= 0 AND sr_count >= 0 AND ssr_count >= 0 AND ur_count >= 0"
            ),
            sqlalchemy.CheckConstraint("draw_count = n_count + r_count + sr_count + ssr_count + ur_count"),
        )

    if (
        inspector is None
        or not log_exists
        or LOG_INDEX not in {index["name"] for index in inspector.get_indexes(LOG_TABLE)}
    ):
        op.create_index(LOG_INDEX, LOG_TABLE, ["account_uid", "drawn_at_utc"])


def downgrade() -> None:
    # Keep redemptions and audit history available on re-upgrade
    pass
