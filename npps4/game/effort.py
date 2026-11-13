import pydantic

from .. import idol
from ..system import reward_egg


class SelectLimitedBoxRequest(pydantic.BaseModel):
    no_agree: pydantic.StrictInt = pydantic.Field(ge=0, le=1)


@idol.register("effortPoint", "selectLimitedBox", batchable=False)
async def select_limited_box(context: idol.SchoolIdolUserParams, request: SelectLimitedBoxRequest) -> None:
    await reward_egg.select(context, request.no_agree)
