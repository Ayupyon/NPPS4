import sqlite3

import sqlalchemy
import sqlalchemy.exc

from . import user
from .. import const
from .. import idol
from .. import util
from ..db import main


async def lock(context: idol.BasicSchoolIdolContext) -> None:
    # Serialize capacity checks, relationship changes, and friend-only mail
    try:
        await context.db.main.execute(
            sqlalchemy.update(main.DestinyFriendMutex)
            .where(main.DestinyFriendMutex.id == 1)
            .values(revision=main.DestinyFriendMutex.revision)
        )
    except sqlalchemy.exc.OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        raise idol.error.IdolError(detail="Friend update is busy", http_code=409) from exc


def pair_condition(user_id: int):
    return sqlalchemy.or_(main.DestinyFriendPair.user_a == user_id, main.DestinyFriendPair.user_b == user_id)


def list_condition(user_id: int, mode: int):
    pair = main.DestinyFriendPair
    if mode == 0:
        condition = pair.accepted == 1
    elif mode == 1:
        condition = sqlalchemy.and_(pair.accepted == 0, pair.requester == user_id)
    else:
        condition = sqlalchemy.and_(pair.accepted == 0, pair.requester != user_id)
    return sqlalchemy.and_(pair_condition(user_id), condition)


def status(pair: main.DestinyFriendPair | None, user_id: int) -> const.FRIEND_STATUS:
    if pair is None:
        return const.FRIEND_STATUS.OTHER
    if pair.accepted:
        return const.FRIEND_STATUS.FRIEND
    return const.FRIEND_STATUS.PENDING if pair.requester == user_id else const.FRIEND_STATUS.APPROVAL_WAIT


async def get_pair(context: idol.BasicSchoolIdolContext, first: int, second: int):
    return await context.db.main.get(main.DestinyFriendPair, tuple(sorted((first, second))))


async def get_friend_status(
    context: idol.BasicSchoolIdolContext, /, current_user: main.User, target_user: main.User
) -> const.FRIEND_STATUS:
    if current_user.id == target_user.id:
        return const.FRIEND_STATUS.FRIEND
    return status(await get_pair(context, current_user.id, target_user.id), current_user.id)


async def count(context: idol.BasicSchoolIdolContext, user_id: int, mode: int = 0) -> int:
    query = sqlalchemy.select(sqlalchemy.func.count()).where(list_condition(user_id, mode))
    return (await context.db.main.execute(query)).scalar_one()


async def count_new(context: idol.BasicSchoolIdolContext, user_id: int) -> int:
    pair = main.DestinyFriendPair
    query = sqlalchemy.select(sqlalchemy.func.count()).where(
        pair.accepted == 1,
        sqlalchemy.or_(
            sqlalchemy.and_(pair.user_a == user_id, pair.new_a == 1),
            sqlalchemy.and_(pair.user_b == user_id, pair.new_b == 1),
        ),
    )
    return (await context.db.main.execute(query)).scalar_one()


async def get_target(context: idol.BasicSchoolIdolContext, user_id: int) -> main.User:
    target = await user.get(context, user_id)
    if target is None:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_USER_NOT_EXISTS)
    return target


async def check_capacity(context: idol.BasicSchoolIdolContext, current: main.User, other: main.User) -> None:
    await context.db.main.refresh(current, with_for_update=True)
    await context.db.main.refresh(other, with_for_update=True)
    if await count(context, current.id) >= current.friend_max:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_COUNT_OVER_LIMIT)
    if await count(context, other.id) >= other.friend_max:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_COUNT_OVER_LIMIT_OF_APPLICANT)


def accept(pair: main.DestinyFriendPair) -> None:
    pair.accepted = pair.new_a = pair.new_b = 1
    pair.updated_at = util.time()


async def request_friend(context: idol.SchoolIdolUserParams, target_id: int) -> bool:
    await lock(context)
    current = await user.get_current(context)
    other = await get_target(context, target_id)
    if current.id == other.id:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_USER_NOT_EXISTS)
    pair = await get_pair(context, current.id, other.id)
    if pair is not None and pair.accepted:
        return True
    if pair is not None and pair.requester == current.id:
        return False

    await check_capacity(context, current, other)
    if pair is not None:
        accept(pair)
        return True

    user_a, user_b = sorted((current.id, other.id))
    now = util.time()
    context.db.main.add(
        main.DestinyFriendPair(user_a=user_a, user_b=user_b, requester=current.id, created_at=now, updated_at=now)
    )
    return False


async def respond_friend(context: idol.SchoolIdolUserParams, target_id: int, response: int) -> None:
    if response not in (0, 2):
        raise idol.error.by_code(idol.error.ERROR_CODE_GAME_LOGIC_ERROR)
    await lock(context)
    current = await user.get_current(context)
    other = await get_target(context, target_id)
    pair = await get_pair(context, current.id, other.id)
    if (pair is None and response == 0) or (pair is not None and pair.accepted and response == 2):
        return
    if pair is None or status(pair, current.id) != const.FRIEND_STATUS.APPROVAL_WAIT:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_NOT_REQUESTED_FROM_SPECIFIED_USER)
    if response == 0:
        await context.db.main.delete(pair)
    else:
        await check_capacity(context, current, other)
        accept(pair)


async def remove_friend(
    context: idol.SchoolIdolUserParams, target_id: int, expected: const.FRIEND_STATUS, error_code: int
) -> None:
    await lock(context)
    current = await user.get_current(context)
    await get_target(context, target_id)
    pair = await get_pair(context, current.id, target_id)
    if pair is None:
        return
    if status(pair, current.id) != expected:
        raise idol.error.by_code(error_code)
    await context.db.main.delete(pair)


def elapsed(timestamp: int) -> str:
    seconds = max(0, util.time() - timestamp)
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    return f"{seconds // 86400} days ago"
