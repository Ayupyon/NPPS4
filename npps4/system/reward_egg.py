import collections.abc
import contextlib
import functools
import hashlib
import json
import math
import pathlib
import random
import sqlite3

import pydantic
import sqlalchemy
import sqlalchemy.exc

from . import advanced
from . import award
from . import background
from . import common
from . import effort
from . import item
from . import item_model
from . import unit
from . import user
from .. import const
from .. import idol
from ..config import config
from ..db import main
from ..download import download
from ..idol import database

from typing import Annotated, Any, Literal

_RANDOM = random.SystemRandom()
type Weight = Annotated[float, pydantic.Field(gt=0, allow_inf_nan=False)]
type Action = Literal["live", "lbonus", "select"]


class EggReward(pydantic.BaseModel):
    type: Literal["AWARD", "BACKGROUND", "ITEM", "SCHOOL_IDOL_SKILL", "SOCIAL_POINT", "UNIT"]
    id: pydantic.PositiveInt
    amount: Annotated[int, pydantic.Field(gt=0, strict=True)]
    category: str | None = None
    requires_award: pydantic.PositiveInt | None = None


class WeightedReward(EggReward):
    weight: Weight


class Egg(pydantic.BaseModel):
    member_type_id: pydantic.PositiveInt | None
    weight: Weight
    rewards: dict[int, list[WeightedReward]]


class OfferSelection(pydantic.BaseModel):
    partner_weight: Literal[40]
    center_weight: Literal[40]
    group_weight: Literal[20]
    song_category_group_events: dict[int, int]
    fallback_group_events: tuple[Literal[1], Literal[2]]


class Policy(pydantic.BaseModel):
    mode: Literal["disabled", "fixed_test", "custom_random"]
    eggs: dict[int, Egg]
    offer_selection: OfferSelection
    custom_offer_probability: Annotated[float, pydantic.Field(ge=0, le=1)]
    fixed_event: pydantic.PositiveInt
    fixed_tier: pydantic.PositiveInt | None = None
    test_user_ids: frozenset[pydantic.PositiveInt] = frozenset()
    all_isolated_test_users: bool = False

    @pydantic.model_validator(mode="after")
    def validate_eggs(self):
        if self.eggs.keys() != set(range(1, 21)):
            raise ValueError("Expected 20 reward egg types")
        if self.offer_selection.song_category_group_events != {1: 1, 2: 2}:
            raise ValueError("Invalid reward egg song groups")
        members = [egg.member_type_id for egg in self.eggs.values() if egg.member_type_id is not None]
        if len(set(members)) != 18 or len(members) != 18 or any(self.eggs[event].member_type_id for event in (1, 2)):
            raise ValueError("Expected two group eggs and eighteen distinct personal eggs")
        if self.fixed_event not in self.eggs or self.fixed_tier not in (None, 1, 2, 3, 4, 5):
            raise ValueError("Invalid fixed reward egg")
        for egg in self.eggs.values():
            if egg.rewards.keys() != set(range(1, 6)):
                raise ValueError("Expected five tiers per reward egg")
            for rewards in egg.rewards.values():
                if not any(entry.type in ("SOCIAL_POINT", "ITEM", "UNIT", "SCHOOL_IDOL_SKILL") for entry in rewards):
                    raise ValueError("Repeatable reward egg fallback required")
                if not math.isclose(sum(entry.weight for entry in rewards), 100, abs_tol=1e-8):
                    raise ValueError("Reward egg weights must total 100")
        return self


def _client_database(name: str) -> sqlite3.Connection:
    path = pathlib.Path(download.get_db_path(name)).resolve()
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _validate_resources(policy: Policy) -> None:
    with contextlib.closing(_client_database("effort")) as db:
        assets = set(
            db.execute("SELECT limited_effort_event_id,live_effort_point_box_spec_id FROM limited_effort_box_asset_m")
        )
        if not {(event, tier) for event, egg in policy.eggs.items() for tier in egg.rewards} <= assets:
            raise ValueError("Missing reward egg assets in configured client database")

    with contextlib.closing(_client_database("item")) as db:
        awards = {row[0] for row in db.execute("SELECT award_id FROM award_m")}
        backgrounds = {row[0] for row in db.execute("SELECT background_id FROM background_m")}
        items = {row[0] for row in db.execute("SELECT item_id FROM kg_item_m")}

    with contextlib.closing(_client_database("unit")) as db:
        skills = {row[0] for row in db.execute("SELECT unit_removable_skill_id FROM unit_removable_skill_m")}
        supporters = {row[0] for row in db.execute("SELECT unit_id FROM unit_m WHERE disable_rank_up<>0")}
        personal_skills = set(
            db.execute(
                "SELECT unit_removable_skill_id,target_type FROM unit_removable_skill_m "
                "WHERE target_reference_type=2 AND size=4 AND effect_value=29"
            )
        )

    for event, egg in policy.eggs.items():
        first_group = event == 1 or 3 <= event <= 11
        index = event - 3
        if egg.member_type_id is not None:
            title_ids = {95 + index, 113 + index, 137 + index}
            required_title = {113 + index: 5 + index if first_group else 30 + event - 12}
        else:
            title_ids = {131, 132, 133} if first_group else {134, 135, 136}
            required_title = {132: 131} if first_group else {135: 134}

        for entries in egg.rewards.values():
            for entry in entries:
                if entry.requires_award is not None and (entry.type != "AWARD" or entry.requires_award not in awards):
                    raise ValueError("Invalid reward egg award prerequisite")
                match entry.type:
                    case "AWARD":
                        valid = (
                            entry.id in awards
                            and entry.id in title_ids
                            and entry.amount == 1
                            and entry.requires_award == required_title.get(entry.id)
                        )
                    case "BACKGROUND":
                        room = 65 + event - 3 if 3 <= event <= 11 else 75 + event - 12 if 12 <= event <= 20 else None
                        valid = entry.id == room and entry.id in backgrounds and entry.amount == 1
                    case "SCHOOL_IDOL_SKILL":
                        valid = (
                            entry.id in skills
                            and entry.amount == 1
                            and (entry.category != "personal_gem" or (entry.id, egg.member_type_id) in personal_skills)
                        )
                    case "UNIT":
                        allowed = {632, 383, 384, 385, 386} if first_group else {1142, 1354, 1355, 1356, 1050}
                        valid = entry.id in supporters and entry.id in allowed and entry.amount == 1
                    case "ITEM":
                        valid = entry.id in items and (
                            (entry.id == 15 and entry.amount == 1) or (entry.id == 1000 and 2 <= entry.amount <= 10)
                        )
                    case "SOCIAL_POINT":
                        valid = entry.id == 2 and entry.amount == 1000
                if not valid:
                    raise ValueError(f"Invalid reward egg entry {event}/{entry.type}/{entry.id}")


@functools.lru_cache(maxsize=1)
def _load_policy(path: pathlib.Path) -> Policy:
    policy = Policy.model_validate_json(path.read_bytes())
    if policy.mode == "fixed_test" and "reward-egg-implementation" not in pathlib.Path(config.ROOT_DIR).parts:
        raise ValueError("Fixed reward egg test policy requires an isolated runtime")
    _validate_resources(policy)
    return policy


def get_policy() -> Policy | None:
    path = config.CONFIG_DATA.gameplay.reward_egg_policy
    if path is None:
        return None
    return _load_policy(pathlib.Path(config.ROOT_DIR, path))


class WireResponse(pydantic.RootModel[dict[str, Any]]):
    pass


async def lock_player(context: idol.SchoolIdolUserParams) -> None:
    assert context.token is not None
    try:
        if not await database.lock_user(context.db.main, context.token.user_id):
            raise ValueError("logic error, user is None")
    except sqlalchemy.exc.OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        raise idol.error.IdolError(detail="Reward egg update is busy", http_code=409) from exc


def request_identity(context: idol.SchoolIdolUserParams, action: Action) -> tuple[str, str]:
    try:
        body = json.loads(context.raw_request_data)
    except (TypeError, ValueError) as exc:
        raise idol.error.IdolError(detail="Invalid reward egg request identity", http_code=409) from exc
    if isinstance(body, list) and action == "live":
        rewards = [
            entry
            for entry in body
            if isinstance(entry, dict) and entry.get("module") == "live" and entry.get("action") == "reward"
        ]
        if len(rewards) == 1:
            body = rewards[0]
    if not isinstance(body, dict) or "timeStamp" not in body or "commandNum" not in body:
        raise idol.error.IdolError(detail="Missing native timeStamp/commandNum", http_code=409)
    key = f"{body['timeStamp']}:{body['commandNum']}"
    if len(key) > 255:
        raise idol.error.IdolError(detail="Invalid reward egg request identity", http_code=409)
    # Keep the legacy hash format so retries can replay pre-upgrade responses
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return key, digest


async def lookup_receipt(
    context: idol.SchoolIdolUserParams, user_id: int, action: Action, key: str, digest: str
) -> main.DestinyRewardEggReceipt | None:
    receipt = await context.db.main.get(main.DestinyRewardEggReceipt, (user_id, action, key), with_for_update=True)
    if receipt is not None and receipt.payload_hash != digest:
        raise idol.error.IdolError(detail="Request identity reused with different payload", http_code=409)
    return receipt


def remember(
    context: idol.SchoolIdolUserParams, user_id: int, action: Action, key: str, digest: str, response: str
) -> None:
    context.db.main.add(
        main.DestinyRewardEggReceipt(
            user_id=user_id, action=action, request_key=key, payload_hash=digest, response=response
        )
    )


async def settle[T: pydantic.BaseModel](
    context: idol.SchoolIdolUserParams,
    action: Literal["live", "lbonus"],
    operation: collections.abc.Callable[[], collections.abc.Awaitable[T]],
) -> T | WireResponse:
    if get_policy() is None:
        return await operation()

    key, digest = request_identity(context, action)
    await lock_player(context)
    async with context.db.main.begin_nested():
        current = await user.get_current(context)
        receipt = await lookup_receipt(context, current.id, action, key, digest)
        if receipt is not None:
            return WireResponse.model_validate_json(receipt.response)
        response = await operation()
        wire = WireResponse(response.model_dump(mode="json", exclude_none=True))
        remember(context, current.id, action, key, digest, wire.model_dump_json())
        return wire


async def get_state(context: idol.BasicSchoolIdolContext, user_id: int) -> main.DestinyRewardEgg | None:
    return await context.db.main.get(main.DestinyRewardEgg, user_id, with_for_update=True)


async def offer_info(context: idol.BasicSchoolIdolContext, user_id: int) -> dict[str, int] | list[object]:
    if get_policy() is None:
        return []
    state = await get_state(context, user_id)
    if state is None or not state.pending_event:
        # Lua treats a zero event ID as an offer
        return {}
    spec = await effort.get_effort_spec(context, state.pending_spec)
    return {
        "limited_effort_event_id": state.pending_event,
        "live_effort_point_box_spec_id": state.pending_spec,
        "capacity": spec.limited_capacity,
    }


async def select(context: idol.SchoolIdolUserParams, no_agree: int) -> None:
    if get_policy() is None:
        raise idol.error.IdolError(detail="Reward egg policy is disabled", http_code=409)

    key, digest = request_identity(context, "select")
    await lock_player(context)
    current = await user.get_current(context)
    if await lookup_receipt(context, current.id, "select", key, digest) is not None:
        return
    state = await get_state(context, current.id)
    if state is None or not state.pending_event:
        raise idol.error.IdolError(detail="No pending reward egg", http_code=409)
    if current.limited_effort_event_id:
        raise idol.error.IdolError(detail="Reward egg already active", http_code=409)

    if no_agree == 0:
        state.saved_spec = current.live_effort_point_box_spec_id
        current.live_effort_point_box_spec_id = state.pending_spec
        current.limited_effort_event_id = state.pending_event
        current.current_limited_effort_point = 0

    state.pending_event = 0
    state.pending_spec = 0
    state.last_choice = no_agree
    remember(context, current.id, "select", key, digest, "[]")


async def draw_reward(
    context: idol.BasicSchoolIdolContext, current: main.User, policy: Policy, event: int, tier: int
) -> common.AnyItem:
    eligible: list[WeightedReward] = []
    for entry in policy.eggs[event].rewards[tier]:
        if entry.requires_award is not None and not await award.has_award(context, current, entry.requires_award):
            continue
        if entry.type == "AWARD" and await award.has_award(context, current, entry.id):
            continue
        if entry.type == "BACKGROUND" and await background.has_background(context, current, entry.id):
            continue
        eligible.append(entry)
    chosen = _RANDOM.choices(eligible, weights=[entry.weight for entry in eligible], k=1)[0]
    reward = await advanced.deserialize_item_data(
        context, item_model.BaseItem(add_type=const.ADD_TYPE[chosen.type], item_id=chosen.id, amount=chosen.amount)
    )
    return await item.update_item_category_id(context, reward)


async def _member_of_owned_card(
    context: idol.BasicSchoolIdolContext, current: main.User, owning_id: int | None
) -> int | None:
    if not owning_id:
        return None
    card = await context.db.main.get(main.Unit, owning_id)
    if card is None or card.user_id != current.id:
        return None
    info = await unit.get_unit_info(context, card.unit_id)
    return info.unit_type_id if info is not None else None


def offer_weights(
    policy: Policy, partner_member: int | None, center_member: int | None, song_category: int | None
) -> dict[int, int]:
    rule = policy.offer_selection
    member_events = {egg.member_type_id: event for event, egg in policy.eggs.items() if egg.member_type_id is not None}
    weights: dict[int, int] = {}
    group_weight = rule.group_weight
    for member, weight in ((partner_member, rule.partner_weight), (center_member, rule.center_weight)):
        event = member_events.get(member) if member is not None else None
        if event is None:
            group_weight += weight
        else:
            weights[event] = weights.get(event, 0) + weight
    group_event = rule.song_category_group_events.get(song_category) if song_category is not None else None
    if group_event is not None:
        weights[group_event] = group_weight
    else:
        for event in rule.fallback_group_events:
            weights[event] = group_weight // 2
    return weights


async def choose_offer(
    context: idol.BasicSchoolIdolContext, current: main.User, policy: Policy, song_category: int | None
) -> int:
    if policy.mode == "disabled":
        return 0
    if policy.mode == "fixed_test":
        return policy.fixed_event if policy.all_isolated_test_users or current.id in policy.test_user_ids else 0
    if _RANDOM.random() >= policy.custom_offer_probability:
        return 0

    partner = await _member_of_owned_card(context, current, current.center_unit_owning_user_id)
    center_id = await context.db.main.scalar(
        sqlalchemy.select(main.UnitDeck.unit_owning_user_id_5).where(
            main.UnitDeck.user_id == current.id, main.UnitDeck.deck_number == current.active_deck_index
        )
    )
    center = await _member_of_owned_card(context, current, center_id)
    weights = offer_weights(policy, partner, center, song_category)
    return _RANDOM.choices(list(weights), weights=list(weights.values()), k=1)[0]


async def bind_live(
    context: idol.BasicSchoolIdolContext, playing: main.LiveInProgress, live_difficulty_id: int
) -> None:
    binding = await context.db.main.get(main.DestinyRewardEggLive, playing.user_id)
    if binding is None:
        context.db.main.add(
            main.DestinyRewardEggLive(
                user_id=playing.user_id, live_in_progress_id=playing.id, live_difficulty_id=live_difficulty_id
            )
        )
    else:
        binding.live_in_progress_id = playing.id
        binding.live_difficulty_id = live_difficulty_id


async def verify_live_binding(
    context: idol.BasicSchoolIdolContext, playing: main.LiveInProgress, live_difficulty_id: int
) -> bool:
    binding = await context.db.main.get(main.DestinyRewardEggLive, playing.user_id)
    if binding is None or binding.live_in_progress_id != playing.id:
        return False
    if binding.live_difficulty_id != live_difficulty_id:
        raise idol.error.IdolError(detail="Reward song does not match the validated Live start", http_code=409)
    return True
