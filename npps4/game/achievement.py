import json

import pydantic
import sqlalchemy

from .. import idol
from .. import util
from ..db import achievement as achievement_db
from ..db import main
from ..system import achievement
from ..system import achievement_claim
from ..system import advanced
from ..system import common
from ..system import reward
from ..system import unit
from ..system import unit_model
from ..system import user

from typing import Any


class AchievementUnaccomplishedFilter(pydantic.BaseModel):
    filter_category_id: int
    achievement_list: list[achievement.AchievementData]
    count: int
    is_last: bool = True


class AchievementUnaccomplishedResponse(pydantic.RootModel[list[AchievementUnaccomplishedFilter]]):
    pass


class AchievementRewardOpenRequest(pydantic.BaseModel):
    accomplish_id: int


class AchievementRewardOpenResponse(
    achievement.AchievementMixin, common.TimestampMixin, user.UserDiffMixin, unit_model.SupporterListInfoResponse
):
    reward_item_list: list[pydantic.SerializeAsAny[common.AnyItem]]
    unit_support_list: list[unit_model.SupporterInfoResponse]
    present_cnt: int


class AchievementRewardOpenAllResponse(AchievementRewardOpenResponse):
    is_last: bool
    opened_num: int


class AchievementClaimResponse(pydantic.RootModel[dict[str, Any]]):
    pass


@idol.register("achievement", "unaccomplishList")
async def achievement_unaccomplishlist(context: idol.SchoolIdolUserParams) -> AchievementUnaccomplishedResponse:
    current_user = await user.get_current(context)
    filter_ids = await achievement.get_achievement_filter_ids(context)

    result: list[AchievementUnaccomplishedFilter] = []
    for fcat in filter_ids:
        unaccomplished = await achievement.get_unclaimed_achievements_by_filter_id(context, current_user, fcat)
        unaccomplished_rewards = [await achievement.get_achievement_rewards(context, ach) for ach in unaccomplished]
        ach_list = await achievement.to_game_representation(context, unaccomplished, unaccomplished_rewards)
        result.append(
            AchievementUnaccomplishedFilter(filter_category_id=fcat, achievement_list=ach_list, count=len(ach_list))
        )

    return AchievementUnaccomplishedResponse.model_validate(result)


@idol.register("achievement", "initialAccomplishedList")
async def achievement_initialaccomplishlist(context: idol.SchoolIdolUserParams) -> AchievementUnaccomplishedResponse:
    current_user = await user.get_current(context)
    filter_ids = await achievement.get_achievement_filter_ids(context)

    result: list[AchievementUnaccomplishedFilter] = []
    for fcat in filter_ids:
        accomplished = await achievement.get_accomplished_achievements_by_filter_id(context, current_user, fcat)
        accomplished_rewards = [await achievement.get_achievement_rewards(context, ach) for ach in accomplished]
        accomplished_rewards = await advanced.fixup_achievement_reward(context, current_user, accomplished_rewards)
        ach_list = await achievement.to_game_representation(context, accomplished, accomplished_rewards)
        result.append(
            AchievementUnaccomplishedFilter(filter_category_id=fcat, achievement_list=ach_list, count=len(ach_list))
        )

    return AchievementUnaccomplishedResponse.model_validate(result)


async def grant_reward(
    context: idol.SchoolIdolUserParams, current_user: main.User, ach: main.Achievement, info: achievement_db.Achievement
):
    rewards = await achievement.get_achievement_rewards(context, ach)
    new_rewards = await advanced.fixup_achievement_reward(context, current_user, [rewards])
    rewards = new_rewards[0]

    for reward_item in rewards:
        success = await advanced.add_item(context, current_user, reward_item)
        if not bool(success):
            reward_item.reward_box_flag = True
            fallback_name = f"Achievement #{ach.achievement_id}"
            await reward.add_item(
                context,
                current_user,
                reward_item,
                info.title or fallback_name,
                info.title_en or info.title or fallback_name,
            )

    return rewards


async def build_reward_response(
    context: idol.SchoolIdolUserParams,
    current_user: main.User,
    before_user: user.UserInfoData,
    reward_item_list: list[common.AnyItem],
):
    achievement_count = await achievement.get_achievement_count(context, current_user, False)
    return AchievementRewardOpenResponse(
        accomplished_achievement_list=[],
        unaccomplished_achievement_cnt=achievement_count,
        added_achievement_list=[],
        new_achievement_cnt=achievement_count,
        before_user_info=before_user,
        after_user_info=await user.get_user_info(context, current_user),
        reward_item_list=reward_item_list,
        unit_support_list=await unit.get_unit_support_list_response(context, current_user),
        present_cnt=await reward.count_presentbox(context, current_user),
    )


@idol.register("achievement", "rewardOpen")
async def achievement_rewardopen(
    context: idol.SchoolIdolUserParams, request: AchievementRewardOpenRequest
) -> AchievementClaimResponse:
    return await open_rewards(context, "rewardOpen", request)


@idol.register("achievement", "rewardOpenAll")
async def achievement_rewardopenall(context: idol.SchoolIdolUserParams) -> AchievementClaimResponse:
    return await open_rewards(context, "rewardOpenAll")


async def open_rewards(
    context: idol.SchoolIdolUserParams, action: str, request: AchievementRewardOpenRequest | None = None
) -> AchievementClaimResponse:
    await achievement_claim.lock_account(context)
    accomplish_id = request.accomplish_id if request is not None else None
    key, digest = achievement_claim.request_identity(context, action, request, accomplish_id)

    session = context.db.main
    async with session.begin_nested():
        receipt, created = await achievement_claim.reserve_receipt(context, action, key, digest)
        if not created:
            if receipt.payload_hash != digest:
                raise achievement_claim.ClaimRejected("Achievement request identity was reused with different data")
            return AchievementClaimResponse(json.loads(receipt.response_json))

        current_user = await user.get_current(context)
        await session.refresh(current_user, with_for_update=True)
        timestamp = util.time()
        await achievement.prepare_daily_achievements(context, current_user, timestamp)

        query = sqlalchemy.select(main.Achievement).where(main.Achievement.user_id == current_user.id)
        if accomplish_id is None:
            query = query.where(
                main.Achievement.is_accomplished.is_(True), main.Achievement.is_reward_claimed.is_(False)
            )
        else:
            query = query.where(main.Achievement.id == accomplish_id)
        query = query.order_by(main.Achievement.id).with_for_update().execution_options(populate_existing=True)
        entries = (await session.scalars(query)).all()
        if accomplish_id is not None and not entries:
            raise achievement_claim.ClaimRejected("Achievement does not belong to this account")

        eligible: list[tuple[main.Achievement, str]] = []
        for entry in entries:
            try:
                cycle = await achievement_claim.qualify(context, current_user, entry, timestamp)
            except idol.error.IdolError:
                if accomplish_id is not None:
                    raise
                continue
            eligible.append((entry, cycle))

        before_user = await user.get_user_info(context, current_user)
        reserved: list[tuple[main.Achievement, achievement_db.Achievement, main.DestinyAchievementClaim]] = []
        for entry, cycle in eligible:
            claim = main.DestinyAchievementClaim(
                user_id=current_user.id,
                achievement_id=entry.achievement_id,
                period_key=cycle,
                owning_achievement_id=entry.id,
                claimed_at=timestamp,
                request_key=key,
            )
            _, created = await achievement_claim.reserve_record(
                session, claim, (current_user.id, entry.achievement_id, cycle)
            )
            if not created:
                if accomplish_id is not None:
                    raise achievement_claim.ClaimRejected("Achievement reward already recorded for this period")
                continue
            info = await achievement.reserve_achievement_reward(context, current_user, entry, timestamp)
            if info is None:
                raise achievement_claim.ClaimRejected("Achievement reward already reserved")
            reserved.append((entry, info, claim))

        reward_item_list: list[common.AnyItem] = []
        for entry, info, claim in reserved:
            rewards = await grant_reward(context, current_user, entry, info)
            claim.rewards_json = json.dumps([item.model_dump(mode="json") for item in rewards])
            reward_item_list.extend(rewards)

        await session.flush()
        response = await build_reward_response(context, current_user, before_user, reward_item_list)
        if accomplish_id is None:
            response = AchievementRewardOpenAllResponse(**dict(response), is_last=True, opened_num=len(reserved))
        wire = response.model_dump(mode="json", exclude_none=True)
        receipt.response_json = json.dumps(wire)
        return AchievementClaimResponse(wire)
