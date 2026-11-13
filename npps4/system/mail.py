import json
import unicodedata

import sqlalchemy

from . import advanced
from . import friend
from . import user
from .. import const
from .. import idol
from .. import util
from ..config import config
from ..db import main

from typing import Any, NoReturn

PAGE_SIZE = 40
SEND_INTERVAL = 10
HOURLY_LIMIT = 30


def reject(detail: str, error_code: int = idol.error.ERROR_CODE_FRIEND_SPECIFIED_USER_IS_NOT_A_FRIEND) -> NoReturn:
    raise idol.error.IdolError(error_code=error_code, status_code=600, detail=detail)


async def validate_message(context: idol.BasicSchoolIdolContext, message: str) -> str:
    message = message.replace("\r\n", "\n").replace("\r", "\n")
    invalid = idol.error.ERROR_CODE_UNAVAILABLE_WORDS
    if len(message) > 1024 or len(message.encode("utf8", errors="replace")) > 4096:
        reject("Message too long", invalid)
    if any(unicodedata.category(character) in ("Cc", "Cs") and character != "\n" for character in message):
        reject("Invalid message character", invalid)
    meaningful = [character for character in message if character not in ("\n", " ", "\u3000")]
    if not any(
        not character.isspace() and unicodedata.category(character) not in ("Cf", "Mn", "Me")
        for character in meaningful
    ):
        reject("Empty message", invalid)
    if len(meaningful) > 69:
        reject("Message exceeds 69 characters excluding line breaks and spaces", invalid)
    if await config.contains_badwords(message, context):
        raise idol.error.by_code(idol.error.ERROR_CODE_NG_WORDS)
    return message


async def snapshot_profile(context: idol.BasicSchoolIdolContext, person: main.User) -> str:
    card = await context.db.main.get(main.Unit, person.center_unit_owning_user_id)
    if card is None or card.user_id != person.id:
        reject("mail:recipient_profile_unavailable")
    info = await advanced.get_user_guest_party_info(context, person)
    return json.dumps(
        {
            "user_data": {
                "user_id": person.id,
                "name": person.name,
                "level": person.level,
                "comment": person.bio,
                "elapsed_time_from_login": friend.elapsed(person.update_date),
            },
            "center_unit_info": info.center_unit_info.model_dump(),
            "setting_award_id": person.active_award,
        },
        ensure_ascii=False,
    )


async def send(
    context: idol.SchoolIdolUserParams,
    recipient_id: int,
    message: str,
    replied_notice_id: int,
    request_key: str,
    payload_hash: str,
) -> None:
    await friend.lock(context)
    current = await user.get_current(context)
    query = sqlalchemy.select(main.DestinyMail.payload_hash).where(
        main.DestinyMail.sender_id == current.id, main.DestinyMail.request_key == request_key
    )
    previous_hash = await context.db.main.scalar(query)
    if previous_hash is not None:
        if previous_hash != payload_hash:
            reject("Send identity reused for different message")
        return

    target = await user.get(context, recipient_id)
    if target is None or target.locked or target.id == current.id:
        reject("Invalid recipient")
    if await friend.get_friend_status(context, current, target) != const.FRIEND_STATUS.FRIEND:
        reject("Private-server mail policy: friends only")
    message = await validate_message(context, message)

    original = None
    if replied_notice_id:
        original = await context.db.main.get(main.DestinyMail, replied_notice_id)
        if (
            original is None
            or original.receiver_id != current.id
            or original.sender_id != target.id
            or original.receiver_deleted
        ):
            reject("Reply does not belong to this inbox and recipient")

    now = util.time()
    query = sqlalchemy.select(sqlalchemy.func.max(main.DestinyMail.created_at), sqlalchemy.func.count()).where(
        main.DestinyMail.sender_id == current.id, main.DestinyMail.created_at > now - 3600
    )
    latest, count = (await context.db.main.execute(query)).one()
    if (latest is not None and now - latest < SEND_INTERVAL) or count >= HOURLY_LIMIT:
        reject("mail:rate_limit:10_seconds:30_per_hour", idol.error.ERROR_CODE_FRIEND_OVER_REQUEST_COUNT)

    context.db.main.add(
        main.DestinyMail(
            sender_id=current.id,
            receiver_id=target.id,
            message=message,
            created_at=now,
            replied_notice_id=replied_notice_id,
            sender_profile=await snapshot_profile(context, current),
            receiver_profile=await snapshot_profile(context, target),
            request_key=request_key,
            payload_hash=payload_hash,
        )
    )
    if original is not None:
        original.reply_flag = 1
        if original.read_at is None:
            original.read_at = now


def to_response(row: main.DestinyMail, sent: bool) -> dict[str, Any]:
    response = {
        "notice_id": row.notice_id,
        "message": row.message,
        "insert_date": util.timestamp_to_datetime(row.created_at),
        "readed": sent or row.read_at is not None,
        "reply_flag": bool(row.reply_flag),
    }
    response["receiver" if sent else "affector"] = json.loads(row.receiver_profile if sent else row.sender_profile)
    return response


async def inbox(context: idol.SchoolIdolUserParams, next_id: int) -> tuple[list[dict[str, Any]], int, bool]:
    await friend.lock(context)
    current = await user.get_current(context)
    mail = main.DestinyMail
    query = sqlalchemy.select(mail).where(mail.receiver_id == current.id, mail.receiver_deleted == 0)
    if next_id:
        query = query.where(mail.notice_id < next_id)
    rows = (await context.db.main.scalars(query.order_by(mail.notice_id.desc()).limit(PAGE_SIZE + 1))).all()
    page = rows[:PAGE_SIZE]
    items = [to_response(row, False) for row in page]
    now = util.time()
    for row in page:
        if row.read_at is None:
            row.read_at = now
    more = len(rows) > PAGE_SIZE
    return items, page[-1].notice_id if more else 0, more


async def history(
    context: idol.SchoolIdolUserParams, notice_id: int, is_previous: bool
) -> tuple[list[dict[str, Any]], bool]:
    current = await user.get_current(context)
    mail = main.DestinyMail
    older = is_previous or notice_id == 0
    query = sqlalchemy.select(mail).where(mail.sender_id == current.id, mail.sender_deleted == 0)
    if notice_id:
        query = query.where(mail.notice_id < notice_id if older else mail.notice_id > notice_id)
    order = mail.notice_id.desc() if older else mail.notice_id.asc()
    rows = (await context.db.main.scalars(query.order_by(order).limit(PAGE_SIZE + 1))).all()
    page = rows[:PAGE_SIZE]
    if not older:
        page = list(reversed(page))
    return [to_response(row, True) for row in page], len(rows) > PAGE_SIZE


async def delete(context: idol.SchoolIdolUserParams, notice_id: int, sent: bool) -> None:
    await friend.lock(context)
    current = await user.get_current(context)
    row = await context.db.main.get(main.DestinyMail, notice_id)
    if row is None or (row.sender_id if sent else row.receiver_id) != current.id:
        reject("Mail not owned by this account")
    if sent:
        row.sender_deleted = 1
    else:
        row.receiver_deleted = 1


async def summary(context: idol.BasicSchoolIdolContext, user_id: int) -> tuple[int, int]:
    if not config.CONFIG_DATA.gameplay.friend_mail:
        return 0, 86400
    mail = main.DestinyMail
    query = sqlalchemy.select(
        sqlalchemy.func.count(sqlalchemy.case((mail.read_at.is_(None), 1))),
        sqlalchemy.func.max(mail.created_at),
    ).where(mail.receiver_id == user_id, mail.receiver_deleted == 0)
    unread, latest = (await context.db.main.execute(query)).one()
    return unread, latest if latest is not None else 86400
