import dataclasses
import random

import pydantic
import sqlalchemy

from . import advanced
from . import common
from . import item
from . import item_model
from . import reward_egg
from .. import const
from .. import data
from .. import idol
from ..config import config
from ..db import effort
from ..db import main

from typing import Any

_DROP_RANDOM = random.SystemRandom()


@dataclasses.dataclass
class ProcessResult:
    new_live_effort_point_box_spec_id: int
    offer_limited_effort_event_id: int
    rewards: list[tuple[int, int, int, dict[str, Any] | None]]


async def process_effort_box(
    context: idol.BasicSchoolIdolContext,
    current_live_effort_point_box_spec_id: int,
    current_limited_effort_event_id: int,
    score: int,
) -> ProcessResult:
    query = sqlalchemy.select(effort.LiveEffortPointBoxSpec).order_by(effort.LiveEffortPointBoxSpec.capacity.desc())
    specs = (await context.db.effort.execute(query)).scalars().all()
    next_box_id = next((spec.live_effort_point_box_spec_id for spec in specs if score * 4 >= spec.capacity), 1)
    current_box = next(
        (spec for spec in specs if spec.live_effort_point_box_spec_id == current_live_effort_point_box_spec_id), None
    )

    rewards: list[tuple[int, int, int, dict[str, Any] | None]] = []
    if current_box is not None:
        drops = [
            drop
            for drop in data.get().live_effort_drops.get(current_live_effort_point_box_spec_id, [])
            if drop.weight > 0
        ]
        if drops:
            rewards = [
                (drop.add_type, drop.item_id, drop.amount, drop.extra_data)
                for drop in _DROP_RANDOM.choices(
                    drops, weights=[drop.weight for drop in drops], k=current_box.num_rewards
                )
            ]

    return ProcessResult(next_box_id, 0, rewards)


class EffortPointInfo(common.BeforeAfter[int]):
    live_effort_point_box_spec_id: int
    capacity: int
    rewards: list[pydantic.SerializeAsAny[common.AnyItem]]


class EggProgress(EffortPointInfo):
    limited_effort_event_id: int | None = None


@common.context_cacheable("effort_point_box_spec")
async def get_effort_spec(context: idol.BasicSchoolIdolContext, live_effort_point_box_spec_id: int):
    effort_spec = await context.db.effort.get(effort.LiveEffortPointBoxSpec, live_effort_point_box_spec_id)
    if effort_spec is None:
        raise ValueError(f"invalid live_effort_point_box_spec_id {live_effort_point_box_spec_id}")

    return effort_spec


async def add_effort(
    context: idol.BasicSchoolIdolContext,
    user: main.User,
    amount: int,
    *,
    offer_after_live: bool = False,
    song_category: int | None = None,
) -> tuple[list[EffortPointInfo], int]:
    policy = reward_egg.get_policy()
    state = await reward_egg.get_state(context, user.id) if policy is not None or user.limited_effort_event_id else None
    began_limited = bool(user.limited_effort_event_id)
    if began_limited and state is not None and state.saved_spec and policy is None:
        raise idol.error.IdolError(detail="Configure the reward egg policy to continue the active egg", http_code=409)
    if amount < 0:
        raise ValueError("Invalid effort amount")

    result: list[EffortPointInfo] = []
    current_amount = min(amount, const.MAX_EFFORT_SCORE)
    offer_limited_box_id = 0

    while True:
        event = user.limited_effort_event_id
        tier = user.live_effort_point_box_spec_id
        effort_spec = await get_effort_spec(context, tier)
        capacity = effort_spec.limited_capacity if event else effort_spec.capacity
        before = user.current_limited_effort_point if event else user.current_live_effort_point
        if not 0 <= before < capacity:
            raise ValueError("Invalid persisted effort box progress")
        after = min(before + current_amount, capacity)
        current_amount -= after - before
        if event:
            user.current_limited_effort_point = after
        else:
            user.current_live_effort_point = after

        reward_list: list[common.AnyItem] = []
        if after == capacity:
            if event and policy is not None and state is not None and state.saved_spec:
                reward_list.append(await reward_egg.draw_reward(context, user, policy, event, tier))
                user.live_effort_point_box_spec_id = state.saved_spec
                state.saved_spec = 0
                state.openings += 1
            else:
                drop = await config.get_live_box_drop_protocol().process_effort_box(context, tier, event, amount)
                for add_type, item_id, item_count, additional_data in drop.rewards:
                    reward_data = await advanced.deserialize_item_data(
                        context,
                        item_model.BaseItem(
                            add_type=const.ADD_TYPE(add_type),
                            item_id=item_id,
                            amount=item_count,
                            extra_data=additional_data,
                        ),
                    )
                    await item.update_item_category_id(context, reward_data)
                    reward_list.append(reward_data)
                if not event:
                    user.live_effort_point_box_spec_id = drop.new_live_effort_point_box_spec_id
                if policy is None and drop.offer_limited_effort_event_id > 0:
                    offer_limited_box_id = drop.offer_limited_effort_event_id

            if event:
                user.limited_effort_event_id = 0
                user.current_limited_effort_point = 0
            else:
                user.current_live_effort_point = 0

        if policy is not None:
            result.append(
                EggProgress(
                    live_effort_point_box_spec_id=tier,
                    limited_effort_event_id=event or None,
                    capacity=capacity,
                    before=before,
                    after=after,
                    rewards=reward_list,
                )
            )
            if after < capacity:
                break
        else:
            if after == capacity:
                result.append(
                    EffortPointInfo(
                        live_effort_point_box_spec_id=tier,
                        capacity=effort_spec.capacity,
                        before=before,
                        after=after,
                        rewards=reward_list,
                    )
                )
            if current_amount <= 0:
                result.append(
                    EffortPointInfo(
                        live_effort_point_box_spec_id=user.live_effort_point_box_spec_id,
                        capacity=effort_spec.capacity,
                        before=before,
                        after=user.current_live_effort_point,
                        rewards=[],
                    )
                )
                break

    if policy is not None:
        if offer_after_live and amount > 0 and not began_limited and not (state and state.pending_event):
            offer = await reward_egg.choose_offer(context, user, policy, song_category)
            if offer:
                if state is None:
                    state = main.DestinyRewardEgg(user_id=user.id)
                    context.db.main.add(state)
                tier = (
                    policy.fixed_tier if policy.mode == "fixed_test" else None
                ) or user.live_effort_point_box_spec_id
                if tier not in policy.eggs[offer].rewards:
                    raise ValueError("Missing reward egg asset tier")
                state.pending_event = offer
                state.pending_spec = tier
                state.generation += 1
                state.last_choice = None
        if state is not None:
            offer_limited_box_id = state.pending_event

    await context.db.main.flush()
    return result, offer_limited_box_id
