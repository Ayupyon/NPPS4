import pydantic
import sqlalchemy

from . import models
from .. import const
from .. import idol
from ..db import main
from ..system import common
from ..system import friend
from ..system import unit
from ..system import unit_model
from ..system import user


class FriendListRequest(pydantic.BaseModel):
    type: int
    sort: int
    page: int


class FriendUserRequest(pydantic.BaseModel):
    user_id: int = pydantic.Field(gt=0, le=2**63 - 1)


class FriendRespondRequest(FriendUserRequest):
    status: int


class FriendFlagResponse(pydantic.BaseModel):
    is_friend: bool = False


class FriendEmptyResponse(pydantic.RootModel[list[object]]):
    root: list[object] = pydantic.Field(default_factory=list)


class FriendListFriendUserData(models.UserData):
    comment: str
    elapsed_time_from_login: str
    elapsed_time_from_applied: str


class FriendListFriend(pydantic.BaseModel):
    is_new: bool = False
    user_data: FriendListFriendUserData
    center_unit_info: common.CenterUnitInfo
    setting_award_id: int


class FriendListResponse(common.TimestampMixin):
    item_count: int
    friend_list: list[FriendListFriend]
    new_friend_list: list


class FriendSearchRequest(pydantic.BaseModel):
    invite_code: str


class FriendSearchUserInfo(pydantic.BaseModel):
    user_id: int
    name: str
    level: int
    cost_max: int
    unit_max: int
    energy_max: int
    friend_max: int
    unit_cnt: int
    elapsed_time_from_login: str
    comment: str


class FriendSearchUnitInfo(unit_model.UnitInfoData):
    attribute: int
    smile: int
    cute: int
    cool: int
    setting_award_id: int
    removable_skill_ids: list[int]


class FriendSearchResponse(common.TimestampMixin):
    user_info: FriendSearchUserInfo
    center_unit_info: FriendSearchUnitInfo
    setting_award_id: int
    is_alliance: bool
    friend_status: const.FRIEND_STATUS


@idol.register("friend", "list")
async def friend_list(context: idol.SchoolIdolUserParams, request: FriendListRequest) -> FriendListResponse:
    if request.type not in (0, 1, 2) or request.page < 0:
        raise idol.error.by_code(idol.error.ERROR_CODE_GAME_LOGIC_ERROR)
    if request.type == 0:
        await friend.lock(context)
    current = await user.get_current(context)
    pair = main.DestinyFriendPair
    other_id = sqlalchemy.case((pair.user_a == current.id, pair.user_b), else_=pair.user_a)
    order = {
        1: main.User.level.asc(),
        2: main.User.level.desc(),
        9: main.User.update_date.asc(),
        10: main.User.update_date.desc(),
        11: pair.created_at.asc(),
    }.get(request.sort, pair.created_at.desc())
    item_count = await friend.count(context, current.id, request.type)
    if request.page * 20 >= item_count:
        return FriendListResponse(item_count=item_count, friend_list=[], new_friend_list=[])
    query = (
        sqlalchemy.select(pair, main.User)
        .join(main.User, main.User.id == other_id)
        .where(friend.list_condition(current.id, request.type))
        .order_by(order, main.User.id)
        .limit(20)
        .offset(request.page * 20)
    )
    entries: list[FriendListFriend] = []
    read_pairs: list[main.DestinyFriendPair] = []
    for relationship, other in await context.db.main.execute(query):
        center_id = await unit.get_unit_center(context, other)
        if not center_id:
            continue
        card = await context.db.main.get(main.Unit, center_id)
        if card is None or card.user_id != other.id:
            continue
        full, stats = await unit.get_unit_data_full_info(context, card)
        is_new = bool(relationship.new_a if relationship.user_a == current.id else relationship.new_b)
        entries.append(
            FriendListFriend(
                is_new=is_new,
                user_data=FriendListFriendUserData(
                    user_id=other.id,
                    name=other.name,
                    level=other.level,
                    comment=other.bio,
                    elapsed_time_from_login=friend.elapsed(other.update_date),
                    elapsed_time_from_applied=friend.elapsed(relationship.updated_at),
                ),
                center_unit_info=common.CenterUnitInfo(
                    unit_id=card.unit_id,
                    level=full.level,
                    rank=card.rank,
                    love=card.love,
                    display_rank=card.display_rank,
                    unit_skill_exp=card.skill_exp,
                    unit_removable_skill_capacity=card.unit_removable_skill_capacity,
                    smile=stats.smile,
                    cute=stats.pure,
                    cool=stats.cool,
                    is_love_max=full.is_love_max,
                    is_level_max=full.is_level_max,
                    is_rank_max=full.is_rank_max,
                    removable_skill_ids=await unit.get_unit_removable_skills(context, card),
                ),
                setting_award_id=other.active_award,
            )
        )
        if is_new:
            read_pairs.append(relationship)
    for relationship in read_pairs:
        if relationship.user_a == current.id:
            relationship.new_a = 0
        else:
            relationship.new_b = 0
    return FriendListResponse(item_count=item_count, friend_list=entries, new_friend_list=[])


@idol.register("friend", "request", batchable=False)
async def friend_request(context: idol.SchoolIdolUserParams, request: FriendUserRequest) -> FriendFlagResponse:
    return FriendFlagResponse(is_friend=await friend.request_friend(context, request.user_id))


@idol.register("friend", "response", batchable=False)
async def friend_response(context: idol.SchoolIdolUserParams, request: FriendRespondRequest) -> FriendEmptyResponse:
    await friend.respond_friend(context, request.user_id, request.status)
    return FriendEmptyResponse()


@idol.register("friend", "requestCancel", batchable=False)
async def friend_cancel(context: idol.SchoolIdolUserParams, request: FriendUserRequest) -> FriendFlagResponse:
    await friend.remove_friend(
        context,
        request.user_id,
        const.FRIEND_STATUS.PENDING,
        idol.error.ERROR_CODE_FRIEND_NOT_REQUESTING_TO_SPECIFIED_USER,
    )
    return FriendFlagResponse()


@idol.register("friend", "expel", batchable=False)
async def friend_expel(context: idol.SchoolIdolUserParams, request: FriendUserRequest) -> FriendEmptyResponse:
    await friend.remove_friend(
        context,
        request.user_id,
        const.FRIEND_STATUS.FRIEND,
        idol.error.ERROR_CODE_FRIEND_SPECIFIED_USER_IS_NOT_A_FRIEND,
    )
    return FriendEmptyResponse()


@idol.register("friend", "search")
async def friend_search(context: idol.SchoolIdolUserParams, request: FriendSearchRequest) -> FriendSearchResponse:
    current_user = await user.get_current(context)

    try:
        invite_code_int = int(request.invite_code)
    except ValueError:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_USER_NOT_EXISTS) from None

    target_user = await user.find_by_invite_code(context, invite_code_int)

    if target_user is None or target_user.center_unit_owning_user_id == 0:
        raise idol.error.by_code(idol.error.ERROR_CODE_FRIEND_USER_NOT_EXISTS)

    unit_data = await unit.get_unit(context, target_user.center_unit_owning_user_id)
    unit_info = await unit.get_unit_info(context, unit_data.unit_id)
    assert unit_info is not None
    unit_data_full_info, unit_stats = await unit.get_unit_data_full_info(context, unit_data)

    return FriendSearchResponse(
        user_info=FriendSearchUserInfo(
            user_id=target_user.id,
            name=target_user.name,
            level=target_user.level,
            cost_max=((target_user.energy_max + target_user.over_max_energy) // 25)
            * 25,  # TODO get from game variables
            unit_max=target_user.unit_max,
            energy_max=target_user.energy_max,
            friend_max=target_user.friend_max,
            unit_cnt=await unit.count_units(context, target_user, True),
            elapsed_time_from_login=friend.elapsed(target_user.update_date),
            comment=target_user.bio,
        ),
        center_unit_info=FriendSearchUnitInfo(
            unit_owning_user_id=unit_data_full_info.unit_owning_user_id,
            unit_id=unit_data_full_info.unit_id,
            unit_rarity_id=unit_data_full_info.unit_rarity_id,
            exp=unit_data_full_info.exp,
            next_exp=unit_data_full_info.next_exp,
            level=unit_data_full_info.level,
            level_limit_id=unit_data_full_info.level_limit_id,
            max_level=unit_data_full_info.max_level,
            rank=unit_data_full_info.rank,
            max_rank=unit_data_full_info.max_rank,
            love=unit_data_full_info.love,
            max_love=unit_data_full_info.max_love,
            unit_skill_level=unit_data_full_info.unit_skill_level,
            max_hp=unit_data_full_info.max_hp,
            favorite_flag=unit_data_full_info.favorite_flag,
            display_rank=unit_data_full_info.display_rank,
            unit_skill_exp=unit_data_full_info.unit_skill_exp,
            unit_removable_skill_capacity=unit_data_full_info.unit_removable_skill_capacity,
            is_removable_skill_capacity_max=unit_data_full_info.is_removable_skill_capacity_max,
            is_love_max=unit_data_full_info.is_love_max,
            is_level_max=unit_data_full_info.is_level_max,
            is_rank_max=unit_data_full_info.is_rank_max,
            is_signed=unit_data_full_info.is_signed,
            is_skill_level_max=unit_data_full_info.is_skill_level_max,
            attribute=unit_info.attribute_id,
            smile=unit_stats.smile,
            cute=unit_stats.pure,
            cool=unit_stats.cool,
            setting_award_id=target_user.active_award,
            removable_skill_ids=await unit.get_unit_removable_skills(context, unit_data),
        ),
        setting_award_id=target_user.active_award,
        is_alliance=False,  # TODO
        friend_status=await friend.get_friend_status(context, current_user, target_user),
    )
