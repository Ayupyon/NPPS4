import random

from .. import data
from .. import idol
from ..data import schema

_DROP_RANDOM = random.SystemRandom()


async def get_live_drop_unit(live_setting_id: int, context: idol.BasicSchoolIdolContext) -> int:
    server_data = data.get()
    chance = server_data.live_unit_drop_chance
    pools = [
        (chance.common, server_data.common_live_unit_drops),
        (chance.live_specific, server_data.live_specific_live_unit_drops.get(live_setting_id, [])),
    ]
    available_pools: list[list[schema.LiveUnitDrop]] = []
    pool_weights: list[int] = []
    for chance_weight, drops in pools:
        eligible_drops = [drop for drop in drops if drop.weight > 0 and drop.unit_id > 0]
        if chance_weight > 0 and eligible_drops:
            available_pools.append(eligible_drops)
            pool_weights.append(chance_weight)
    if not available_pools:
        raise ValueError(f"No eligible unit drops for live setting {live_setting_id}")

    drops = _DROP_RANDOM.choices(available_pools, weights=pool_weights)[0]
    return _DROP_RANDOM.choices(drops, weights=[drop.weight for drop in drops])[0].unit_id
