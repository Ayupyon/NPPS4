import sqlalchemy
import sqlalchemy.dialects.mysql

from . import common

metadata = common.Base.metadata
ID = common.IDInteger
JSON = sqlalchemy.Text().with_variant(sqlalchemy.dialects.mysql.LONGTEXT(), "mysql", "mariadb")


def user_id(primary_key: bool = False) -> sqlalchemy.Column[int]:
    return sqlalchemy.Column(
        "user_id", ID, sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"), primary_key=primary_key, nullable=False
    )


def key(length: int) -> sqlalchemy.String:
    return sqlalchemy.String(length).with_variant(
        sqlalchemy.dialects.mysql.VARCHAR(length, collation="utf8mb4_bin"), "mysql", "mariadb"
    )


def counter(name: str) -> sqlalchemy.Column[int]:
    return sqlalchemy.Column(name, ID, nullable=False, server_default="0")


period = sqlalchemy.Table(
    "sif005_period",
    metadata,
    sqlalchemy.Column("id", ID, primary_key=True),
    sqlalchemy.Column("period_key", key(160), nullable=False, unique=True),
    sqlalchemy.Column("event_id", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("version", key(160), nullable=False),
    sqlalchemy.Column("rule_hash", sqlalchemy.String(64), nullable=False),
    sqlalchemy.Column("rule_json", JSON, nullable=False),
    sqlalchemy.Column("end_at", ID, nullable=False),
    sqlalchemy.Column("freeze_at", ID, nullable=False),
    sqlalchemy.Column("frozen_at", ID),
    sqlite_autoincrement=True,
    mysql_charset="utf8mb4",
    mariadb_charset="utf8mb4",
)
progress = sqlalchemy.Table(
    "sif005_progress",
    metadata,
    sqlalchemy.Column("period_id", ID, sqlalchemy.ForeignKey(period.c.id), primary_key=True),
    user_id(True),
    counter("tokens"),
    counter("points"),
    counter("clears"),
    counter("event_clears"),
    counter("point_time"),
    counter("score"),
    counter("score_time"),
    sqlalchemy.Column("best_detail", JSON),
    sqlalchemy.CheckConstraint("tokens >= 0"),
    sqlalchemy.CheckConstraint("points >= 0"),
)
sqlalchemy.Index(
    "sif005_points", progress.c.period_id, progress.c.points.desc(), progress.c.point_time, progress.c.user_id
)
sqlalchemy.Index(
    "sif005_scores", progress.c.period_id, progress.c.score.desc(), progress.c.score_time, progress.c.user_id
)

cheer = sqlalchemy.Table(
    "sif005_cheer",
    metadata,
    sqlalchemy.Column("period_id", ID, primary_key=True),
    user_id(True),
    sqlalchemy.Column("position", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owning_id", ID, nullable=False),
    sqlalchemy.CheckConstraint("position BETWEEN 1 AND 5"),
    sqlalchemy.UniqueConstraint("period_id", "user_id", "owning_id"),
)
live = sqlalchemy.Table(
    "sif005_live",
    metadata,
    user_id(True),
    sqlalchemy.Column("period_id", ID),
    sqlalchemy.Column("difficulty_id", ID, nullable=False),
    sqlalchemy.Column("party_user_id", ID, nullable=False),
    sqlalchemy.Column("training", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("is_event_song", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("factor", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("cost", ID, nullable=False),
    sqlalchemy.Column("started_at", ID, nullable=False),
    sqlalchemy.Column("rule_json", JSON),
    sqlalchemy.Column("cheer_percent", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("cheer_snapshot", JSON, nullable=False),
    sqlalchemy.Column("notes_json", JSON, nullable=False),
    sqlalchemy.Column("token_indices", JSON, nullable=False),
    sqlalchemy.Column("play_key", key(192), nullable=False),
    sqlalchemy.Column("play_hash", sqlalchemy.String(64), nullable=False),
    mysql_charset="utf8mb4",
    mariadb_charset="utf8mb4",
)
receipt = sqlalchemy.Table(
    "sif005_receipt",
    metadata,
    user_id(True),
    sqlalchemy.Column("action", key(32), primary_key=True),
    sqlalchemy.Column("request_key", key(192), primary_key=True),
    sqlalchemy.Column("payload_hash", sqlalchemy.String(64), nullable=False),
    sqlalchemy.Column("response", JSON, nullable=False),
    mysql_charset="utf8mb4",
    mariadb_charset="utf8mb4",
)
reward_ledger = sqlalchemy.Table(
    "sif005_reward_ledger",
    metadata,
    sqlalchemy.Column("period_id", ID, primary_key=True),
    user_id(True),
    sqlalchemy.Column("condition_id", key(160), primary_key=True),
    sqlalchemy.Column("rule_version", key(160), nullable=False),
    sqlalchemy.Column("created_at", ID, nullable=False),
    sqlalchemy.Column("rewards", JSON, nullable=False),
    mysql_charset="utf8mb4",
    mariadb_charset="utf8mb4",
)
ranking_snapshot = sqlalchemy.Table(
    "sif005_ranking_snapshot",
    metadata,
    sqlalchemy.Column("period_id", ID, primary_key=True),
    sqlalchemy.Column("kind", key(16), primary_key=True),
    sqlalchemy.Column("rank", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("user_id", ID, nullable=False),
    sqlalchemy.Column("score", ID, nullable=False),
    sqlalchemy.Column("profile", JSON, nullable=False),
    sqlalchemy.UniqueConstraint("period_id", "kind", "user_id"),
    mysql_charset="utf8mb4",
    mariadb_charset="utf8mb4",
)
song_goal = sqlalchemy.Table(
    "sif005_song_goal_ledger",
    metadata,
    user_id(True),
    sqlalchemy.Column("period_id", ID, sqlalchemy.ForeignKey(period.c.id), primary_key=True),
    sqlalchemy.Column("difficulty_id", ID, primary_key=True),
    sqlalchemy.Column("storage_id", ID, nullable=False),
    sqlalchemy.Column("goal_id", ID, primary_key=True),
    sqlalchemy.Column("reward_json", JSON, nullable=False),
    sqlalchemy.Column("created_at", ID, nullable=False),
    sqlalchemy.CheckConstraint("storage_id < 0"),
)
sqlalchemy.Index("sif005_song_goal_storage", song_goal.c.user_id, song_goal.c.storage_id)

tables = (period, progress, cheer, live, receipt, reward_ledger, ranking_snapshot, song_goal)
