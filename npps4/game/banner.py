import hashlib

import pydantic

from .. import idol
from .. import util
from ..download import event_presentation
from ..system import event_home
from ..system import marathon
from ..system import user

DEFAULT_EXPIRY = 2147483647


class BannerInfo(pydantic.BaseModel):
    banner_type: int
    target_id: int
    asset_path: str
    webview_url: str | None = None
    is_registered: bool | None = None
    fixed_flag: bool
    back_side: bool
    banner_id: int
    start_date: str
    end_date: str


class BannerListResponse(pydantic.BaseModel):
    time_limit: str
    banner_list: list[BannerInfo]


@idol.register("banner", "bannerList", exclude_none=True)
async def banner_bannerlist(context: idol.SchoolIdolUserParams) -> BannerListResponse:
    current_user = await user.get_current(context)
    # TODO
    util.stub("banner", "bannerList", context.raw_request_data)

    response = BannerListResponse(
        time_limit=util.timestamp_to_datetime(DEFAULT_EXPIRY),
        banner_list=[
            # SIF2 transfer banner
            BannerInfo(
                banner_type=18,
                target_id=1,
                asset_path=(
                    "en/assets/image/handover/banner/banner_01.png"
                    if context.lang == idol.Language.en
                    else "assets/image/handover/banner/banner_01.png"
                ),
                is_registered=current_user.transfer_sha1 is not None,
                fixed_flag=False,
                back_side=False,
                banner_id=1800002,
                start_date=util.timestamp_to_datetime(1476522000),
                end_date=util.timestamp_to_datetime(2147483647),
            ),
            # TenFes banner
            BannerInfo(
                banner_type=2,
                target_id=1,
                asset_path=(
                    "en/assets/image/webview/wv_ba_01.png"
                    if context.lang == idol.Language.en
                    else "assets/image/webview/wv_ba_01.png"
                ),
                webview_url="/",
                fixed_flag=False,
                back_side=True,
                banner_id=200001,
                start_date=util.timestamp_to_datetime(1476522000),
                end_date=util.timestamp_to_datetime(2147483647),
            ),
        ],
    )
    if event_presentation.protocol_version() is None:
        return response

    now = marathon.now()
    expiry = min(DEFAULT_EXPIRY, now + 60)
    period = marathon.current_period()
    package = event_home.ready_package(context, period["key"]) if period else None
    if period is not None and package is not None:
        dates = [marathon.timestamp(period[phase]) for phase in ("open", "start", "end", "close")]
        opens, starts, ends, closes = dates
        event_id = period["event_id"]
        if not opens < starts < ends < closes or not 0 < event_id < DEFAULT_EXPIRY:
            raise ValueError("Invalid event home banner period")
        expiry = min(expiry, min((date for date in dates if date > now), default=expiry))
        if opens <= now < closes:
            banner_id = 1500000000 + int(hashlib.sha256(package.period_key.encode()).hexdigest()[:8], 16) % 500000000
            response.banner_list.insert(
                0,
                BannerInfo(
                    banner_type=0,
                    target_id=event_id,
                    asset_path=package.asset_path,
                    fixed_flag=False,
                    back_side=False,
                    banner_id=banner_id,
                    start_date=util.timestamp_to_datetime(opens),
                    end_date=util.timestamp_to_datetime(closes),
                ),
            )
    response.time_limit = util.timestamp_to_datetime(expiry)
    return response
