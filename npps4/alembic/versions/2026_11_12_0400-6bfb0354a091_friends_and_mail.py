import alembic.op
import sqlalchemy
import sqlalchemy.dialects.mysql

revision = "6bfb0354a091"
down_revision = "c84df2e71a5b"
branch_labels = None
depends_on = None

ID = sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")
MUTEX = "destiny_friend_mutex"
PAIR = "destiny_friend_pair"
MAIL = "destiny_mail"


def user_column(name: str, primary_key: bool = False) -> sqlalchemy.Column[int]:
    return sqlalchemy.Column(
        name, ID, sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"), primary_key=primary_key, nullable=False
    )


def upgrade() -> None:
    op = alembic.op
    connection = op.get_bind()
    inspector = None if op.get_context().as_sql else sqlalchemy.inspect(connection)
    if inspector is None or not inspector.has_table(MUTEX):
        op.create_table(
            MUTEX,
            sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True, autoincrement=False),
            sqlalchemy.Column("revision", sqlalchemy.Integer, nullable=False),
            sqlalchemy.CheckConstraint("id = 1"),
        )
    mutex = sqlalchemy.table(MUTEX, sqlalchemy.column("id", sqlalchemy.Integer), sqlalchemy.column("revision"))
    if inspector is None or connection.scalar(sqlalchemy.select(mutex.c.id).where(mutex.c.id == 1)) is None:
        op.bulk_insert(mutex, [{"id": 1, "revision": 0}])

    if inspector is None or not inspector.has_table(PAIR):
        op.create_table(
            PAIR,
            user_column("user_a", primary_key=True),
            user_column("user_b", primary_key=True),
            sqlalchemy.Column("requester", ID, nullable=False),
            sqlalchemy.Column("accepted", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.Column("created_at", ID, nullable=False),
            sqlalchemy.Column("updated_at", ID, nullable=False),
            sqlalchemy.Column("new_a", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.Column("new_b", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.CheckConstraint("user_a < user_b"),
            sqlalchemy.CheckConstraint("requester IN (user_a, user_b)"),
            sqlalchemy.CheckConstraint("accepted IN (0, 1)"),
            sqlalchemy.CheckConstraint("new_a IN (0, 1)"),
            sqlalchemy.CheckConstraint("new_b IN (0, 1)"),
        )
        op.create_index("destiny_friend_b", PAIR, ["user_b", "accepted"])

    if inspector is None or not inspector.has_table(MAIL):
        op.create_table(
            MAIL,
            sqlalchemy.Column("notice_id", ID, primary_key=True),
            user_column("sender_id"),
            user_column("receiver_id"),
            sqlalchemy.Column("message", sqlalchemy.Text, nullable=False),
            sqlalchemy.Column("created_at", ID, nullable=False),
            sqlalchemy.Column("replied_notice_id", ID, nullable=False, server_default="0"),
            sqlalchemy.Column("read_at", ID),
            sqlalchemy.Column("reply_flag", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.Column("sender_deleted", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.Column("receiver_deleted", sqlalchemy.Integer, nullable=False, server_default="0"),
            sqlalchemy.Column("sender_profile", sqlalchemy.Text, nullable=False),
            sqlalchemy.Column("receiver_profile", sqlalchemy.Text, nullable=False),
            sqlalchemy.Column(
                "request_key",
                sqlalchemy.Text().with_variant(
                    sqlalchemy.dialects.mysql.VARCHAR(532, collation="utf8mb4_bin"), "mysql", "mariadb"
                ),
                nullable=False,
            ),
            sqlalchemy.Column("payload_hash", sqlalchemy.String(64), nullable=False),
            sqlalchemy.UniqueConstraint("sender_id", "request_key"),
            sqlite_autoincrement=True,
            mysql_charset="utf8mb4",
            mariadb_charset="utf8mb4",
        )
        op.create_index("destiny_mail_inbox", MAIL, ["receiver_id", "receiver_deleted", "notice_id"])
        op.create_index("destiny_mail_sent", MAIL, ["sender_id", "sender_deleted", "notice_id"])
        op.create_index("destiny_mail_rate", MAIL, ["sender_id", "created_at"])


def downgrade() -> None:
    # Keep relationships, mailbox history and retry IDs available to the legacy launcher
    pass
