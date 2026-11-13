import enum
import pathlib

import pydantic

from .. import idol
from .. import util
from ..config import config
from ..download import event_presentation
from ..system import common
from ..system import marathon

from typing import Any


class EventBonus(pydantic.BaseModel):
    limited_bonus_type: int
    limited_bonus_value: int


class EventCampaignList(pydantic.BaseModel):
    fixed_message: str = ""
    live_limited_bonuses: list[EventBonus]


class EventBannerType(enum.IntEnum):
    EVENT = 0
    DUEL = 7
    ARENA = 14
    CONCERT = 15
    CLASS_COMPETITION = 16
    CLASS = 17


class Event(pydantic.BaseModel):
    banner_type: EventBannerType
    asset_path: str
    start_date: str
    end_date: str
    is_locked: bool
    is_new: bool = True
    description: str = ""
    target_id: int | None = None
    # campaign_list: EventCampaignList | None = None


class EventTargetList(pydantic.BaseModel):
    position: int
    is_displayable: bool = True
    event_list: list[Event]


class EventListResponse(common.TimestampMixin):
    target_list: list[EventTargetList]


class PresentationRequest(pydantic.BaseModel):
    protocol_version: Any = event_presentation.PROTOCOL_VERSION
    event_id: Any = 0


class PresentationResponse(pydantic.RootModel[dict[str, Any]]):
    pass


@idol.register("event", "eventList")
async def event_eventlist(context: idol.SchoolIdolUserParams) -> marathon.Wire:
    if not marathon.enabled():
        raise idol.error.by_code(idol.error.ERROR_CODE_EVENT_NO_EVENT_DATA)
    _, period = await marathon.visible_period(context)
    entries = (
        [
            Event(
                banner_type=EventBannerType.EVENT,
                asset_path=period["banner_asset"],
                start_date=util.timestamp_to_datetime(marathon.timestamp(period["open"])),
                end_date=util.timestamp_to_datetime(marathon.timestamp(period["close"])),
                is_locked=False,
                description=period["name"],
                target_id=period["event_id"],
            )
        ]
        if period
        else []
    )
    return marathon.Wire(
        {
            "target_list": [
                EventTargetList(
                    position=position,
                    is_displayable=position == 1 and bool(entries),
                    event_list=entries if position == 1 else [],
                ).model_dump()
                for position in range(1, 7)
            ]
        }
    )


@idol.register("event", "eventYellDeck", batchable=False)
async def event_yell_deck(context: idol.SchoolIdolUserParams, request: marathon.CheerRequest) -> None:
    await marathon.save_cheer(context, request)


@idol.register("event", "presentation", batchable=False)
async def event_presentation_info(
    context: idol.SchoolIdolAuthParams, request: PresentationRequest
) -> PresentationResponse:
    if event_presentation.protocol_version() is None:
        raise idol.error.IdolError(detail="Event presentation is disabled", http_code=404)
    if (
        type(request.protocol_version) is not int
        or request.protocol_version != event_presentation.PROTOCOL_VERSION
        or type(request.event_id) is not int
        or not 0 <= request.event_id <= 2147483647
    ):
        raise idol.error.IdolError(detail="Invalid event presentation request", http_code=422)
    protocol = event_presentation.PROTOCOL_VERSION
    _, period = await marathon.period(context, event_id=request.event_id or None)
    if period is None:
        return PresentationResponse(
            {"protocol_version": protocol, "available": False, "event": None, "presentation": None}
        )

    descriptor = marathon.event_descriptor(period)
    store = event_presentation.get_store(pathlib.Path(config.ROOT_DIR))
    resource_error = False
    try:
        release = store.for_period(period["key"])
    except (OSError, ValueError, TypeError, KeyError):
        release = None
        resource_error = True
    return PresentationResponse(
        {
            "protocol_version": protocol,
            "available": True,
            "event": descriptor,
            "presentation": release,
            "resource_error": resource_error,
        }
    )
