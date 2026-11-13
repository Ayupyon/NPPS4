import html
import json

import fastapi
import pydantic

from .. import idol
from ..app import app
from ..system import marathon


class MarathonInfoEventScenarioStatus(pydantic.BaseModel):
    title: str
    chapter: int
    is_reward: bool
    open_date: str
    title_font: str | None = None
    asset_bgm_id: int
    status_origin: int
    title_call_asset: str | None = None
    event_scenario_id: int
    open_total_event_point: int


class MarathonInfoEventScenario(pydantic.BaseModel):
    progress: int
    event_scenario_status: list[MarathonInfoEventScenarioStatus]


class MarathonInfoData(pydantic.BaseModel):
    event_id: int
    point_name: str
    event_point: int
    event_scenario: MarathonInfoEventScenario
    point_icon_asset: str
    total_event_point: int


class MarathonInfoResponse(pydantic.RootModel):
    root: list[MarathonInfoData]


@idol.register("marathon", "marathonInfo")
async def marathon_marathoninfo(context: idol.SchoolIdolUserParams) -> MarathonInfoResponse:
    return MarathonInfoResponse.model_validate((await marathon.marathon_info(context)).root)


@idol.register("marathon", "top", batchable=False)
async def marathon_top(context: idol.SchoolIdolUserParams, request: marathon.EventRequest) -> marathon.Wire:
    return await marathon.top(context, request)


@app.core.get("/webview.php/sif005/rules/{event_id}", response_class=fastapi.responses.HTMLResponse)
async def marathon_rules(event_id: int) -> str:
    period = marathon.current_period(event_id)
    if period is None:
        raise fastapi.HTTPException(404)
    return (
        '<meta charset="utf-8"><title>SIF005 TEST</title><h1>'
        + html.escape(period["name"])
        + "</h1><p>实验配置，非官方或正式奖励。应援仅加活动pt；失败/退出不返道具、不获活动pt。所有倍率合并后向下取整。结束后"
        + str(period["settlement_grace_seconds"])
        + "秒宽限，之后冻结排名。相同分数先达到者优先，其后按玩家ID。</p><pre>"
        + html.escape(json.dumps(period, ensure_ascii=False, indent=2))
        + "</pre>"
    )
