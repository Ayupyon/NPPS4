import datetime
import hashlib
import json
import sqlite3

import pydantic
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.ext.asyncio

from . import achievement
from .. import idol
from .. import util
from ..config import config
from ..db import common
from ..db import main
from ..idol import database


class ClaimRejected(idol.error.IdolError):
    def __init__(self, detail: str):
        super().__init__(detail=detail, http_code=409)


def period_key(reset_type: int, timestamp: int) -> str:
    if reset_type == 0:
        return "permanent"
    if reset_type == 1:
        return f"day:{util.get_days_since_unix(timestamp)}"
    if reset_type == 2:
        return f"week:{util.get_weeks_since_unix(timestamp)}"
    if reset_type == 3:
        day = util.get_days_since_unix(timestamp)
        date = datetime.datetime.fromtimestamp(day * 86400, datetime.timezone.utc)
        return f"month:{date.year:04d}{date.month:02d}"
    raise ValueError(f"Unsupported achievement reset type {reset_type}")


def request_identity(
    context: idol.SchoolIdolUserParams, action: str, request: pydantic.BaseModel | None, accomplish_id: int | None
) -> tuple[str, str]:
    try:
        body = json.loads(context.raw_request_data)
    except (ValueError, TypeError) as exc:
        raise ClaimRejected("Invalid request identity") from exc

    entry = body
    batch_index = None
    if isinstance(body, list):
        matches = [
            index
            for index, entry in enumerate(body)
            if isinstance(entry, dict)
            and entry.get("module") == "achievement"
            and entry.get("action") == action
            and (accomplish_id is None or str(entry.get("accomplish_id")) == str(accomplish_id))
        ]
        position_key = (action, accomplish_id)
        position = context.get_cache("achievement_claim_batch_positions", position_key) or 0
        if position >= len(matches):
            raise ClaimRejected("Cannot identify achievement request within batch")
        batch_index = matches[position]
        context.set_cache("achievement_claim_batch_positions", position_key, position + 1)
        entry = body[batch_index]

    if not isinstance(entry, dict):
        raise ClaimRejected("Missing achievement request identity")
    if "timeStamp" in entry and "commandNum" in entry:
        key = f"body:{entry['timeStamp']}:{entry['commandNum']}"
    elif context.nonce > 0:
        key = f"header:{context.timestamp}:{context.nonce}"
    else:
        raise ClaimRejected("Missing timeStamp/commandNum or authenticated request nonce")
    if batch_index is not None:
        key += f":batch:{batch_index}"

    session = context.db.main
    if session.bind is not None and session.bind.dialect.name in ("mysql", "mariadb") and len(key) > 255:
        raise ClaimRejected("Invalid request identity")

    payload = {
        "body": body,
        "action": action,
        "arguments": request.model_dump(mode="json") if request is not None else None,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return key, digest


async def lock_account(context: idol.SchoolIdolUserParams) -> None:
    assert context.token is not None
    try:
        found = await database.lock_user(context.db.main, context.token.user_id)
    except sqlalchemy.exc.OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        raise ClaimRejected("Another account update is in progress; retry the request") from exc
    if not found:
        raise ClaimRejected("Invalid account")


async def reserve_receipt(context: idol.SchoolIdolUserParams, action: str, key: str, digest: str):
    assert context.token is not None
    session = context.db.main
    user_id = context.token.user_id
    receipt = main.DestinyAchievementClaimReceipt(
        user_id=user_id, action=action, request_key=key, payload_hash=digest, response_json=""
    )
    return await reserve_record(session, receipt, (user_id, action, key))


async def reserve_record[Record: common.Base](
    session: sqlalchemy.ext.asyncio.AsyncSession, record: Record, key: tuple[int | str, ...] | str
) -> tuple[Record, bool]:
    # InnoDB locking reads of missing keys can deadlock with inserts by other accounts
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except sqlalchemy.exc.IntegrityError:
        existing = await session.get(type(record), key, with_for_update=True)
        if existing is None:
            raise
        return existing, False
    return record, True


async def qualify(
    context: idol.BasicSchoolIdolContext, current: main.User, entry: main.Achievement, timestamp: int
) -> str:
    if entry.user_id != current.id or not entry.is_accomplished or entry.is_reward_claimed:
        raise ClaimRejected("Achievement is not completed and unclaimed for this account")
    info = await achievement.get_achievement_reward_info(context, entry, timestamp)
    if info is None:
        raise ClaimRejected("Achievement is not eligible for reward")

    try:
        cycle = period_key(entry.reset_type, timestamp)
    except ValueError as exc:
        raise ClaimRejected("Unsupported achievement reset period") from exc
    if entry.reset_type:
        updated = entry.update_date or entry.insert_date
        if updated > timestamp or period_key(entry.reset_type, updated) != cycle:
            raise ClaimRejected("Achievement completion belongs to an earlier period")
        expected = int(cycle.partition(":")[2])
        allowed = {0, expected}
        if entry.reset_type == 3:
            allowed.add(expected % 100)
        if entry.reset_value not in allowed:
            raise ClaimRejected("Achievement period marker is stale")
    if (
        config.CONFIG_DATA.gameplay.block_fragment_achievement_claims
        and info.achievement_type == 55
        and info.params1 == 1000
    ):
        raise ClaimRejected("Fragment achievement claims are disabled")

    session = context.db.main
    prerequisites = set(await achievement.get_prerequisite_achievement_ids(context, entry.achievement_id))
    if prerequisites:
        query = (
            sqlalchemy.select(main.Achievement)
            .where(
                main.Achievement.user_id == current.id,
                main.Achievement.achievement_id.in_(prerequisites),
                main.Achievement.is_accomplished.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = (await session.scalars(query)).all()
        if {row.achievement_id for row in rows} != prerequisites:
            raise ClaimRejected("Achievement prerequisites are not completed")
        for row in rows:
            if (
                entry.reset_type
                and row.reset_type == entry.reset_type
                and period_key(row.reset_type, row.update_date or row.insert_date) != cycle
            ):
                raise ClaimRejected("Achievement prerequisite belongs to an earlier period")

    checkers = [
        group[info.achievement_type]
        for group in achievement.ACHIEVEMENT_CHECKER.values()
        if info.achievement_type in group
    ]
    if checkers and not any([await checker.is_accomplished(context, entry.count, info) for checker in checkers]):
        raise ClaimRejected("Achievement progress does not meet its completion condition")

    claim_key = (current.id, entry.achievement_id, cycle)
    hold = await session.get(main.DestinyAchievementClaimReview, claim_key, with_for_update=True)
    if hold is not None and hold.resolved_at is None:
        raise ClaimRejected("Legacy claim period requires review before this period can be paid")
    return cycle
