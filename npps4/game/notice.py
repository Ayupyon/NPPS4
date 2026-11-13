import pydantic

from . import greet
from .. import const
from .. import idol
from .. import util
from ..system import common
from ..system import mail

from typing import Any


class NoticeMarquee(pydantic.BaseModel):
    # https://github.com/DarkEnergyProcessor/NPPS/blob/v3.1.x/modules/notice/noticeMarquee.php
    marquee_id: int
    text: str
    text_color: int
    display_place: int
    start_date: str
    end_date: str


class NoticeMarqueeResponse(pydantic.BaseModel):
    item_count: int
    marquee_list: list[NoticeMarquee]


class NoticeFriendVarietyRequest(pydantic.BaseModel):
    filter_id: int
    readed: bool
    page: int


class NoticeFriendVariety(pydantic.BaseModel):
    notice_id: int
    new_flag: bool
    reference_table: int
    filter_id: const.NOTICE_FILTER_ID
    notice_template_id: int
    message: str
    readed: bool
    insert_date: str
    affector: Any | None  # This is FriendSearchResponse


class NoticeFriendVarietyResponse(common.TimestampMixin):
    item_count: int
    notice_list: list[NoticeFriendVariety]


class InboxRequest(greet.MailRequest):
    next_id: pydantic.StrictInt = pydantic.Field(default=0, ge=0, le=greet.MAX_ID)


class HistoryRequest(greet.MailRequest):
    is_previous: bool = True
    notice_id: pydantic.StrictInt = pydantic.Field(default=0, ge=0, le=greet.MAX_ID)


class MailResponse(pydantic.BaseModel):
    notice_list: list[dict[str, Any]]
    next_id: int = 0
    has_next: bool = False


@idol.register("notice", "noticeMarquee")
async def notice_noticemarquee(context: idol.SchoolIdolUserParams) -> NoticeMarqueeResponse:
    # TODO
    util.stub("notice", "noticeMarquee", context.raw_request_data)
    return NoticeMarqueeResponse(item_count=0, marquee_list=[])


@idol.register("notice", "noticeFriendVariety")
async def notice_noticefriendvariety(
    context: idol.SchoolIdolUserParams, request: NoticeFriendVarietyRequest
) -> NoticeFriendVarietyResponse:
    # TODO
    util.stub("notice", "noticeFriendVariety", request)
    return NoticeFriendVarietyResponse(item_count=0, notice_list=[])


@idol.register("notice", "noticeFriendGreeting", batchable=False)
async def notice_friend_greeting(context: idol.SchoolIdolUserParams, request: InboxRequest) -> MailResponse:
    request.check()
    notices, next_id, has_next = await mail.inbox(context, request.next_id)
    return MailResponse(notice_list=notices, next_id=next_id, has_next=has_next)


@idol.register("notice", "noticeUserGreetingHistory", batchable=False)
async def notice_greeting_history(context: idol.SchoolIdolUserParams, request: HistoryRequest) -> MailResponse:
    request.check()
    notices, has_next = await mail.history(context, request.notice_id, request.is_previous)
    return MailResponse(notice_list=notices, has_next=has_next)
