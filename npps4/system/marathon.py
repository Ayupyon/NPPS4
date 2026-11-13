from __future__ import annotations

import collections.abc
import contextlib
import copy
import datetime
import functools
import hashlib
import json
import pathlib
import sqlite3

import fastapi
import pydantic
import sqlalchemy

from . import advanced
from . import event_charts
from . import event_song_goal
from . import friend
from . import item
from . import item_model
from . import live
from . import reward
from . import reward_egg
from . import unit
from . import user
from .. import const
from .. import idol
from .. import util
from ..config import config
from ..db import main
from ..db import marathon as schema
from ..download import download
from ..download import event_presentation

from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from ..game import live as game_live


type PlayHandler = collections.abc.Callable[
    [idol.SchoolIdolUserParams, game_live.LivePlayRequest], collections.abc.Awaitable[game_live.LivePlayResponse]
]
type RewardHandler = collections.abc.Callable[
    [idol.SchoolIdolUserParams, game_live.LiveRewardRequest], collections.abc.Awaitable[game_live.LiveRewardResponse]
]
type Data = dict[str, Any]
type Row = sqlalchemy.RowMapping
type RankKind = Literal["point", "score"]


def now() -> int:
    return util.time()


def client_database(name: str) -> sqlite3.Connection:
    path = pathlib.Path(download.get_db_path(name)).resolve()
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


@functools.lru_cache(maxsize=1)
def load_policy(path: pathlib.Path) -> Data:
    policy = json.loads(path.read_text(encoding="utf-8"))
    validate_policy(policy)
    return policy


def get_policy() -> Data:
    path = config.CONFIG_DATA.gameplay.marathon_policy
    return load_policy(pathlib.Path(config.ROOT_DIR, path)) if path is not None else {"periods": []}


def enabled() -> bool:
    return config.CONFIG_DATA.gameplay.marathon_policy is not None


def authenticated_id(context: idol.SchoolIdolUserParams) -> int:
    assert context.token is not None
    return context.token.user_id


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def timestamp(value: str) -> int:
    return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def reject(message: str, code: int = 409) -> NoReturn:
    raise idol.error.IdolError(detail="SIF005: " + message, http_code=code)


async def sql(context: idol.BasicSchoolIdolContext, text: str, **params: Any):
    return await context.db.main.execute(sqlalchemy.text(text), params)


async def lock(context: idol.BasicSchoolIdolContext) -> None:
    await friend.lock(context)


class Wire(pydantic.RootModel[Data]):
    pass


class InfoWire(pydantic.RootModel[list[Data]]):
    pass


class Request(pydantic.BaseModel):
    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any):
        try:
            return super().model_validate(obj, **kwargs)
        except pydantic.ValidationError:
            raise fastapi.HTTPException(422, "Invalid event request") from None


class EventRequest(Request):
    event_id: pydantic.StrictInt = pydantic.Field(gt=0, le=2147483647)


class Slot(Request):
    unit_owning_user_id: pydantic.StrictInt = pydantic.Field(gt=0, le=9223372036854775807)
    position: pydantic.StrictInt = pydantic.Field(ge=1, le=5)


class CheerRequest(EventRequest):
    unit_deck_detail: list[Slot] = pydantic.Field(max_length=5)


class RankRequest(EventRequest):
    page: pydantic.StrictInt | None = pydantic.Field(default=None, ge=0, le=100000)
    id: pydantic.StrictInt | None = pydantic.Field(default=None, ge=0, le=9223372036854775807)
    rank: pydantic.StrictInt | None = pydantic.Field(default=None, ge=1, le=2000000)
    buff: pydantic.StrictInt = pydantic.Field(default=0, ge=-20, le=20)


class DetailRequest(EventRequest):
    user_id: pydantic.StrictInt = pydantic.Field(gt=0, le=9223372036854775807)


def identity(context: idol.SchoolIdolUserParams, action: str) -> tuple[str, str]:
    raw = context.raw_request_data
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        reject("native request identity required", 422)
    if isinstance(data, list) and action == "reward":
        matches = [
            entry
            for entry in data
            if isinstance(entry, dict) and entry.get("module") == "live" and entry.get("action") == "reward"
        ]
        if len(matches) == 1:
            data = matches[0]
    if (
        not isinstance(data, dict)
        or type(data.get("timeStamp")) is not int
        or not -(2**63) <= data["timeStamp"] < 2**63
    ):
        reject("native request identity required", 422)
    command = data.get("commandNum")
    # Original mobile clients send UUID.timestamp.sequence strings. Keep the
    # complete value in the receipt key; never coerce it to an integer.
    if not (
        type(command) is int
        and -(2**63) <= command < 2**63
        or (
            type(command) is str
            and 1 <= len(command) <= 160
            and command.isascii()
            and all(33 <= ord(x) <= 126 for x in command)
        )
    ):
        reject("native request identity required", 422)
    return f"{data['timeStamp']}:{data['commandNum']}", hashlib.sha256(dump(data).encode()).hexdigest()


async def cached(context: idol.SchoolIdolUserParams, action: str, key: str, digest: str) -> Data | None:
    receipt = schema.receipt.c
    query = (
        sqlalchemy.select(receipt.payload_hash, receipt.response)
        .where(receipt.user_id == authenticated_id(context), receipt.action == action, receipt.request_key == key)
        .with_for_update()
    )
    row = (await context.db.main.execute(query)).mappings().first()
    if row:
        if row["payload_hash"] != digest:
            reject("request identity reused with changed payload")
        return json.loads(row["response"])


async def remember(context: idol.SchoolIdolUserParams, action: str, key: str, digest: str, response: Data):
    await sql(
        context,
        "INSERT INTO sif005_receipt VALUES(:u,:a,:k,:h,:r)",
        u=authenticated_id(context),
        a=action,
        k=key,
        h=digest,
        r=dump(response),
    )


def validate_event_points(rule: Data):
    if (
        not isinstance(rule, dict)
        or rule.get("revision") != "late-token-pt-20261111-v1"
        or rule.get("rounding") != "half-up-per-play-before-multiplier"
    ):
        raise ValueError("Invalid event point revision")
    rows = rule.get("difficulties")
    if not isinstance(rows, dict) or set(rows) != {"1", "2", "3", "4", "6"}:
        raise ValueError("Missing event difficulty points")
    for row in rows.values():
        if not isinstance(row, dict) or type(row.get("base_point")) is not int or not 1 <= row["base_point"] <= 99999:
            raise ValueError("Invalid event base points")
        for field in ("score_percent", "combo_percent"):
            values = row.get(field)
            if (
                not isinstance(values, list)
                or len(values) != 5
                or any(type(v) is not int or not 100 <= v <= 200 for v in values)
                or values[-1] != 100
                or values != sorted(values, reverse=True)
            ):
                raise ValueError("Invalid event rank multiplier")


def event_point_result(rule: Data, difficulty: int, ranks: tuple[int, int, int], factor: int, cheer_percent: int):
    # Integer half-up avoids Python banker's rounding and floating-point edges.
    # Rank order in the actual packed runtime is S/A/B/C/none = 1/2/3/4/5.
    row = rule["difficulties"][str(difficulty)]
    sr = row["score_percent"][ranks[0] - 1]
    cr = row["combo_percent"][ranks[1] - 1]
    points = (row["base_point"] * sr * cr * cheer_percent + 500000) // 1000000
    return row["base_point"] * factor, sr, cr, points * factor


def validate_policy(policy: Data) -> None:
    if policy["schema_version"] != 1 or not policy["periods"]:
        raise ValueError("Invalid SIF005 policy")
    if policy.get("event_live_rewards") is not None:
        validate_event_points(policy["event_live_rewards"])
    normal = policy.get("normal_live_rewards")
    if normal is not None and (
        normal.get("revision") != "historical-base-lp-v1"
        or normal.get("tokens_by_base_lp") != {"5": 5, "10": 10, "15": 16, "25": 27}
        or normal.get("include_master_challenge") is not True
    ):
        raise ValueError("Invalid normal live reward revision")
    keys = set()
    windows = []
    selected = policy.get("selected_period_key")
    if selected is not None and (not isinstance(selected, str) or not selected):
        raise ValueError("Invalid selected period key")
    with contextlib.closing(client_database("unit")) as db:
        valid_units = {record[0] for record in db.execute("SELECT unit_id FROM unit_m")}
    with contextlib.closing(client_database("item")) as db:
        # This extension currently supports the historical single scouting ticket.
        # Verify the packed master's identity, not just that an arbitrary ID exists.
        ticket = db.execute("SELECT item_category_id,item_sub_category_id FROM kg_item_m WHERE item_id=1").fetchone()
        if ticket != (1, 1):
            raise ValueError("Historical scouting ticket master mismatch")
    for rule in policy["periods"]:
        if rule["key"] in keys:
            raise ValueError("Duplicate period key")
        keys.add(rule["key"])
        times = [timestamp(rule[k]) for k in ("open", "start", "end", "close")]
        if type(rule["event_id"]) is not int or rule["event_id"] <= 0 or not rule["version"] or not rule["key"]:
            raise ValueError("Invalid event identity")
        if type(rule["normal_point_per_token"]) is not int or not 0 <= rule["normal_point_per_token"] <= 100000:
            raise ValueError("Invalid point value")
        if any(
            type(rule[k]) is not int or rule[k] < 0
            for k in ("minimum_reward_points", "minimum_reward_clears", "settlement_grace_seconds")
        ):
            raise ValueError("Invalid participation rules")
        if not rule["songs"] or len({s["id"] for s in rule["songs"]}) != len(rule["songs"]):
            raise ValueError("Ambiguous event songs")
        for s in rule["songs"]:
            if any(type(s[k]) is not int or not 1 <= s[k] <= 99999 for k in ("id", "setting_id", "cost", "base_point")):
                raise ValueError("Invalid event song")
        if set(rule["normal_token_count"]) != {"1", "2", "3", "4", "5"}:
            raise ValueError("Need token quota for five difficulties")
        if (
            not times[0] <= times[1] < times[2] < times[3]
            or not 0 <= rule["settlement_grace_seconds"] < times[3] - times[2]
        ):
            raise ValueError("Invalid event dates")
        if selected is None and any(max(a, times[0]) < min(b, times[3]) for a, b in windows):
            raise ValueError("Only one visible token period at a time")
        windows.append((times[0], times[3]))
        if not rule["allowed_factors"] or any(type(i) is not int or not 1 <= i <= 4 for i in rule["allowed_factors"]):
            raise ValueError("Unsupported LP factor")
        if len({record["unit_id"] for record in rule["cheer_units"]}) != len(rule["cheer_units"]):
            raise ValueError("Ambiguous cheer rule")
        for record in rule["cheer_units"]:
            if record["unit_id"] not in valid_units or any(
                type(record[k]) is not int or not 0 <= record[k] <= 10000 for k in ("amount", "rank_max_amount")
            ):
                raise ValueError("Invalid cheer unit/percent")
        ids = set()
        for group in ("point_rewards", "point_rank_rewards", "score_rank_rewards"):
            previous = 0
            for record in rule[group]:
                if record["id"] in ids:
                    raise ValueError("Duplicate reward condition ID")
                ids.add(record["id"])
                if group == "point_rewards":
                    if type(record["points"]) is not int or record["points"] <= 0:
                        raise ValueError("Invalid threshold")
                    if record.get("delivery") not in ("direct", "present"):
                        raise ValueError("Invalid delivery")
                else:
                    if not previous < record["from"] <= record["to"]:
                        raise ValueError("Overlapping rank tiers")
                    previous = record["to"]
                if not record["items"]:
                    raise ValueError("Empty reward condition")
                for item_definition in record["items"]:
                    if (
                        item_definition["type"] not in ("UNIT", "ITEM", "GAME_COIN", "SOCIAL_POINT", "LOVECA")
                        or type(item_definition["amount"]) is not int
                        or not 1 <= item_definition["amount"] <= 1000000
                        or type(item_definition["id"]) is not int
                    ):
                        raise ValueError("Unsupported reward")
                    if item_definition["type"] == "UNIT" and item_definition["id"] not in valid_units:
                        raise ValueError("Unknown internal unit ID")
                    if item_definition["type"] == "ITEM" and item_definition["id"] != 1:
                        raise ValueError("Unsupported historical item ID")
                    if (
                        item_definition["type"] not in ("UNIT", "ITEM")
                        and item_definition["id"]
                        != {"GAME_COIN": 3, "SOCIAL_POINT": 2, "LOVECA": 0}[item_definition["type"]]
                    ):
                        raise ValueError("Invalid currency ID")
        rule["point_rewards"].sort(key=lambda record: (record["points"], record["id"]))
        if set(rule["cheer_sources"]) - {"normal", "event"}:
            raise ValueError("Invalid cheer scope")
        if any(type(n) is not int or not 0 <= n <= 100 for n in rule["normal_token_count"].values()):
            raise ValueError("Invalid token count")
        if any(
            type(n) is not int or not 0 < n <= 1000
            for n in rule["score_percent_by_rank"] + rule["combo_percent_by_rank"]
        ):
            raise ValueError("Invalid rank multiplier")
        if len(rule["score_percent_by_rank"]) != 5 or len(rule["combo_percent_by_rank"]) != 5:
            raise ValueError("Need five ranks")

    if selected is not None and selected not in keys:
        raise ValueError("Selected period does not exist")


def selected_periods() -> list[Data]:
    # Selection only controls visibility, so other published periods can still settle
    selected = get_policy().get("selected_period_key")
    return [rule for rule in get_policy()["periods"] if selected is None or rule["key"] == selected]


async def register_period(context: idol.BasicSchoolIdolContext, rule: Data) -> Row:
    text = dump(rule)
    digest = hashlib.sha256(text.encode()).hexdigest()
    query = sqlalchemy.select(schema.period).where(schema.period.c.period_key == rule["key"])
    row = (await context.db.main.execute(query)).mappings().first()
    if row is None:
        await context.db.main.execute(
            schema.period.insert().values(
                period_key=rule["key"],
                event_id=rule["event_id"],
                version=rule["version"],
                rule_hash=digest,
                rule_json=text,
                end_at=timestamp(rule["end"]),
                freeze_at=timestamp(rule["end"]) + rule["settlement_grace_seconds"],
            )
        )
        row = (await context.db.main.execute(query)).mappings().one()
    if row["rule_hash"] != digest:
        reject("published rules changed; create a new period key instead")
    return row


async def period(
    context: idol.BasicSchoolIdolContext, event_id: int | None = None, active: bool = False
) -> tuple[Row | None, Data | None]:
    rule = current_period(event_id, active)
    if rule is None:
        return None, None
    await lock(context)
    recorded = await register_period(context, rule)
    return recorded, event_charts.effective_period(context, rule, get_policy()["periods"])


async def require_period(context: idol.BasicSchoolIdolContext, event_id: int, active: bool = False) -> tuple[Row, Data]:
    record, rule = await period(context, event_id, active)
    if record is None or rule is None:
        reject("event not available", 403)
    return record, rule


async def progress(context: idol.BasicSchoolIdolContext, period_id: int, user_id: int) -> Row:
    query = sqlalchemy.select(schema.progress).where(
        schema.progress.c.period_id == period_id, schema.progress.c.user_id == user_id
    )
    row = (await context.db.main.execute(query)).mappings().first()
    if row is None:
        await context.db.main.execute(schema.progress.insert().values(period_id=period_id, user_id=user_id))
        row = (await context.db.main.execute(query)).mappings().one()
    return row


async def cheer(context: idol.BasicSchoolIdolContext, record: Row, rule: Data, user_id: int):
    rules = {x["unit_id"]: x for x in rule["cheer_units"]}
    total = 100
    valid = []
    snapshot = []
    rows = (
        (
            await sql(
                context,
                "SELECT * FROM sif005_cheer WHERE period_id=:p AND user_id=:u ORDER BY position",
                p=record["id"],
                u=user_id,
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        card = await context.db.main.get(main.Unit, row["owning_id"])
        unit_rule = rules.get(card.unit_id) if card is not None and card.user_id == user_id else None
        if card is None or unit_rule is None:
            await sql(
                context,
                "DELETE FROM sif005_cheer WHERE period_id=:p AND user_id=:u AND position=:s",
                p=record["id"],
                u=user_id,
                s=row["position"],
            )
            continue
        master = await unit.get_unit_info(context, card.unit_id)
        if master is None:
            reject("event cheer card missing from client database")
        value = unit_rule["rank_max_amount"] if card.rank >= master.rank_max else unit_rule["amount"]
        total += value
        valid.append({"unit_owning_user_id": card.id, "position": row["position"]})
        snapshot.append(
            {"owning_id": card.id, "unit_id": card.unit_id, "rank": card.rank, "percent": value, "active": card.active}
        )
    return total, valid, snapshot


async def save_cheer(context: idol.SchoolIdolUserParams, request: CheerRequest) -> None:
    await lock(context)
    record, rule = await require_period(context, request.event_id, True)
    user_id = (await user.get_current(context)).id
    slots = request.unit_deck_detail
    if len({s.position for s in slots}) != len(slots) or len({s.unit_owning_user_id for s in slots}) != len(slots):
        reject("duplicate cheer slot/instance", 422)
    rules = {x["unit_id"] for x in rule["cheer_units"]}
    for s in slots:
        card = await context.db.main.get(main.Unit, s.unit_owning_user_id)
        if card is None or card.user_id != user_id or card.unit_id not in rules:
            reject("unowned or ineligible cheer card", 403)
    await sql(context, "DELETE FROM sif005_cheer WHERE period_id=:p AND user_id=:u", p=record["id"], u=user_id)
    for s in slots:
        await sql(
            context,
            "INSERT INTO sif005_cheer VALUES(:p,:u,:s,:i)",
            p=record["id"],
            u=user_id,
            s=s.position,
            i=s.unit_owning_user_id,
        )


async def make_item(context: idol.BasicSchoolIdolContext, item_definition: Data):
    if item_definition["type"] == "UNIT":
        return await unit.create_unit_item(context, item_definition["id"], item_definition["amount"])
    result = item_model.Item(
        add_type=getattr(const.ADD_TYPE, item_definition["type"]),
        item_id=item_definition["id"],
        amount=item_definition["amount"],
    )
    await item.update_item_category_id(context, result)
    return result


async def preview_items(context: idol.BasicSchoolIdolContext, items: list[Data]):
    out = []
    for item_definition in items:
        v = (await make_item(context, item_definition)).model_dump(exclude_none=True)
        v["image_display_flag"] = 1 if item_definition["type"] == "UNIT" else 0
        # ItemOption.isRankMax parses JSON text, not a nested Lua table.
        v["item_option"] = dump({"rank": v["rank"]} if "rank" in v else {})
        out.append(v)
    return out


async def grant(
    context: idol.BasicSchoolIdolContext, record: Row, rule: Data, user_id: int, condition: Data, delivery: str
):
    exists = (
        await sql(
            context,
            "SELECT 1 FROM sif005_reward_ledger WHERE period_id=:p AND user_id=:u AND condition_id=:k",
            p=record["id"],
            u=user_id,
            k=condition["id"],
        )
    ).first()
    if exists:
        return []
    target = await user.get(context, user_id)
    if target is None:
        return []
    out = []
    for item_definition in condition["items"]:
        obj = await make_item(context, item_definition)
        boxed = delivery == "present"
        if not boxed:
            boxed = not (await advanced.add_item(context, target, obj)).success
        if boxed:
            await reward.add_item(
                context, target, obj, rule["name"] + " " + condition["id"], rule["name"] + " " + condition["id"]
            )
        obj.reward_box_flag = boxed
        entry = obj.model_dump(exclude_none=True)
        if "points" in condition:
            entry["required_event_point"] = condition["points"]
        out.append(entry)
    await sql(
        context,
        "INSERT INTO sif005_reward_ledger VALUES(:p,:u,:k,:v,:t,:r)",
        p=record["id"],
        u=user_id,
        k=condition["id"],
        v=rule["version"],
        t=now(),
        r=dump(out),
    )
    return out


async def rank_profile(context: idol.BasicSchoolIdolContext, user_id: int):
    u = await user.get(context, user_id)
    if u is None:
        return None
    center = await context.db.main.get(main.Unit, u.center_unit_owning_user_id)
    if center is None or center.user_id != u.id:
        return None
    info = await advanced.get_user_guest_party_info(context, u)
    return {
        "user_data": {"user_id": u.id, "name": u.name, "level": u.level},
        "center_unit_info": info.center_unit_info.model_dump(),
        "setting_award_id": u.active_award,
    }


async def rank_rows(context: idol.BasicSchoolIdolContext, record: Row, kind: RankKind) -> list[Data]:
    if record["frozen_at"] is not None:
        return [
            {"user_id": x["user_id"], "score": x["score"], "rank": x["rank"], "profile": json.loads(x["profile"])}
            for x in (
                await context.db.main.execute(
                    sqlalchemy.select(schema.ranking_snapshot)
                    .where(schema.ranking_snapshot.c.period_id == record["id"], schema.ranking_snapshot.c.kind == kind)
                    .order_by(schema.ranking_snapshot.c.rank)
                )
            )
            .mappings()
            .all()
        ]
    col = "points" if kind == "point" else "score"
    tm = "point_time" if kind == "point" else "score_time"
    rows = (
        (
            await sql(
                context,
                "SELECT * FROM sif005_progress WHERE period_id=:p AND "
                + col
                + ">0 ORDER BY "
                + col
                + " DESC,"
                + tm
                + ",user_id",
                p=record["id"],
            )
        )
        .mappings()
        .all()
    )
    return [{"user_id": x["user_id"], "score": x[col], "rank": i + 1, "profile": None} for i, x in enumerate(rows)]


RANK_REWARD_GROUPS: tuple[tuple[RankKind, str], ...] = (
    ("point", "point_rank_rewards"),
    ("score", "score_rank_rewards"),
)


async def finalize_due(context: idol.BasicSchoolIdolContext, period_key: str | None = None):
    rules = [rule for rule in get_policy()["periods"] if period_key is None or rule["key"] == period_key]
    if period_key is not None and not rules:
        raise ValueError("Unknown settlement period")
    for rule in rules:
        await register_period(context, rule)
    query = sqlalchemy.select(schema.period).where(
        schema.period.c.frozen_at.is_(None), schema.period.c.freeze_at <= now()
    )
    if period_key is not None:
        query = query.where(schema.period.c.period_key == period_key)
    periods = (await context.db.main.execute(query)).mappings().all()
    for record in periods:
        rule = json.loads(record["rule_json"])
        for kind, group in RANK_REWARD_GROUPS:
            for row in await rank_rows(context, record, kind):
                profile = await rank_profile(context, row["user_id"])
                if profile is None:
                    continue
                await sql(
                    context,
                    "INSERT INTO sif005_ranking_snapshot VALUES(:p,:k,:rank,:u,:s,:profile)",
                    p=record["id"],
                    k=kind,
                    rank=row["rank"],
                    u=row["user_id"],
                    s=row["score"],
                    profile=dump(profile),
                )
                player_progress = await progress(context, record["id"], row["user_id"])
                if (
                    player_progress["points"] < rule["minimum_reward_points"]
                    or player_progress["clears"] < rule["minimum_reward_clears"]
                ):
                    continue
                if kind == "score" and not player_progress["event_clears"]:
                    continue
                for condition in rule[group]:
                    if condition["from"] <= row["rank"] <= condition["to"]:
                        await grant(context, record, rule, row["user_id"], condition, "present")
        await sql(context, "UPDATE sif005_period SET frozen_at=:t WHERE id=:p", t=now(), p=record["id"])


async def top(context: idol.SchoolIdolUserParams, request: EventRequest) -> Wire:
    await lock(context)
    await finalize_due(context)
    record, rule = await require_period(context, request.event_id)
    user_id = (await user.get_current(context)).id
    player_progress = await progress(context, record["id"], user_id)
    percent, slots, _ = await cheer(context, record, rule, user_id)
    ranks = await rank_rows(context, record, "point")
    rank = next((x["rank"] for x in ranks if x["user_id"] == user_id), 0)
    rewards = {}
    for group, key in [
        ("point_rewards", "event_point"),
        ("point_rank_rewards", "event_ranking"),
        ("score_rank_rewards", "live_ranking"),
    ]:
        values = []
        for v in rule[group]:
            item_list = await preview_items(context, v["items"])
            values.append(
                {"point_count": v["points"], "reward_list": item_list}
                if group == "point_rewards"
                else {"rank_from": v["from"], "rank_to": v["to"], "reward_list": item_list}
            )
        rewards[key] = values
    return Wire(
        {
            "event_id": rule["event_id"],
            "event_status": {
                "total_event_point": player_progress["points"],
                "event_point": player_progress["tokens"],
                "event_rank": rank,
            },
            "event_reward_info": rewards,
            "event_yell_unit_list": [dict(x, cheer_type=1) for x in rule["cheer_units"]],
            "event_yell_unit_deck_detail": slots,
            "event_yell_percent": percent,
            "notice_list": [],
            "event_scenario": {"progress": 0, "event_scenario_status": []},
            "rule_version": rule["version"],
            "test_configuration": True,
            "ranking_frozen": record["frozen_at"] is not None,
        }
    )


async def marathon_info(context: idol.SchoolIdolUserParams) -> InfoWire:
    if not enabled():
        return InfoWire([])
    await lock(context)
    await finalize_due(context)
    record, rule = await period(context)
    if record is None or rule is None:
        return InfoWire([])
    player_progress = await progress(context, record["id"], (await user.get_current(context)).id)
    return InfoWire(
        [
            {
                "event_id": rule["event_id"],
                "point_name": rule["point_name"],
                "point_icon_asset": rule["point_icon_asset"],
                "event_point": player_progress["tokens"],
                "total_event_point": player_progress["points"],
                "event_scenario": {"progress": 0, "event_scenario_status": []},
            }
        ]
    )


async def ranking_response(
    context: idol.SchoolIdolUserParams, request: RankRequest, kind: RankKind, friends: bool = False
):
    await lock(context)
    await finalize_due(context)
    record, _rule = await require_period(context, request.event_id)
    current = await user.get_current(context)
    rows = await rank_rows(context, record, kind)
    if friends:
        allowed = (
            {
                x[0]
                for x in (
                    await sql(
                        context,
                        """SELECT CASE WHEN user_a=:u THEN user_b ELSE user_a END
            FROM destiny_friend_pair WHERE accepted=1 AND (user_a=:u OR user_b=:u)""",
                        u=current.id,
                    )
                ).all()
            }
            | {current.id}
        )
        rows = [x for x in rows if x["user_id"] in allowed]
    # The client's friend rankings combine the initial page=0 hint with a rank or player anchor
    has_id = request.id not in (None, 0)
    has_rank = request.rank is not None
    if (has_id and has_rank) or (request.page not in (None, 0) and (has_id or has_rank)):
        reject("ambiguous rank cursor", 422)
    page = request.page or 0
    if has_id:
        idx = next((i for i, x in enumerate(rows) if x["user_id"] == request.id), None)
        if idx is None:
            reject("player not ranked", 404)
        page = idx // 20
    if request.rank is not None:
        page = (request.rank - 1) // 20
    start = max(0, page * 20 + min(request.buff, 0))
    stop = page * 20 + 20 + max(request.buff, 0)
    items = []
    for x in rows[start:stop]:
        profile = x["profile"] or await rank_profile(context, x["user_id"])
        if profile:
            items.append(dict(rank=x["rank"], score=x["score"], **profile))
    return Wire(
        {
            "items": items,
            "total_cnt": len(rows),
            "page": page,
            "rank": next((x["rank"] for x in rows if x["user_id"] == current.id), 0),
            "present_cnt": await reward.count_presentbox(context, current),
        }
    )


async def rank_point(context: idol.SchoolIdolUserParams, request: RankRequest) -> Wire:
    return await ranking_response(context, request, "point")


async def rank_score(context: idol.SchoolIdolUserParams, request: RankRequest) -> Wire:
    return await ranking_response(context, request, "score")


async def rank_friend_point(context: idol.SchoolIdolUserParams, request: RankRequest) -> Wire:
    return await ranking_response(context, request, "point", True)


async def rank_friend_score(context: idol.SchoolIdolUserParams, request: RankRequest) -> Wire:
    return await ranking_response(context, request, "score", True)


def ranking_detail_wire(detail: Data) -> Data:
    """Restore the original client deck contract from the saved live snapshot.

    Older SIF005 records kept the difficulty ID only in live_info. The native
    score-detail page copies deck_info.live_difficulty_id to the team list;
    opening Team Details then feeds that ID to LiveTitle. Repair the response
    without rewriting historical scores, decks, or player rows.
    """
    result = copy.deepcopy(detail)
    for entry in result["live_list"]:
        difficulty_id = entry["live_info"]["live_difficulty_id"]
        if type(difficulty_id) is not int or difficulty_id <= 0:
            reject("invalid saved live difficulty", 409)
        deck = entry["deck_info"]
        if deck.get("live_difficulty_id") is None:
            deck["live_difficulty_id"] = difficulty_id
        elif type(deck["live_difficulty_id"]) is not int or deck["live_difficulty_id"] != difficulty_id:
            reject("inconsistent saved live difficulty", 409)
    return result


async def ranking_detail_with_members(context: idol.BasicSchoolIdolContext, detail: Data) -> Data:
    """Adapt stored play-time attributes to the native other-deck view.

    The frozen calculator saves each member's final smile/cute/cool flat.
    OtherDeck.getUnitStatus expects a nested total_status, including HP.
    Reconstruct missing legacy HP from the recorded level and master patterns,
    never from the user's current card/deck (which may have changed).
    """
    result = ranking_detail_wire(detail)
    hp_cache = {}
    for entry in result["live_list"]:
        for member in entry["deck_info"]["unit_list"]:
            if isinstance(member.get("total_status"), dict):
                if any(type(member["total_status"].get(k)) is not int for k in ("hp", "smile", "cute", "cool")):
                    reject("invalid saved member status", 409)
                continue
            if any(type(member.get(k)) is not int for k in ("unit_id", "level", "smile", "cute", "cool")):
                reject("invalid saved member attributes", 409)
            key = (member["unit_id"], member["level"])
            if key not in hp_cache:
                info = await unit.get_unit_info(context, key[0])
                if info is None:
                    reject("unknown saved member", 409)
                patterns = await unit.get_unit_level_up_pattern(context, info.unit_level_up_pattern_id)
                matches = {rule.hp_diff for rule in patterns if rule.unit_level == key[1]}
                if not matches and patterns and key[1] > max(rule.unit_level for rule in patterns):
                    # The snapshot omits level_limit_id. Recovery is allowed only
                    # when every master pattern at that recorded level agrees.
                    matches = set(
                        (
                            await context.db.unit.execute(
                                sqlalchemy.text(
                                    "SELECT DISTINCT hp_diff FROM unit_level_limit_pattern_m WHERE unit_level=:level"
                                ),
                                {"level": key[1]},
                            )
                        ).scalars()
                    )
                if len(matches) != 1:
                    reject("ambiguous saved member HP", 409)
                hp_cache[key] = info.hp_max - next(iter(matches))
                if hp_cache[key] <= 0:
                    reject("invalid saved member HP", 409)
            member["total_status"] = {k: member[k] for k in ("smile", "cute", "cool")}
            member["total_status"]["hp"] = hp_cache[key]
    return result


async def rank_detail(context: idol.SchoolIdolUserParams, request: DetailRequest) -> Wire:
    await lock(context)
    record, _rule = await require_period(context, request.event_id)
    row = (
        await sql(
            context,
            "SELECT best_detail FROM sif005_progress WHERE period_id=:p AND user_id=:u",
            p=record["id"],
            u=request.user_id,
        )
    ).first()
    if row is None or not row[0]:
        reject("no event song record", 404)
    return Wire(await ranking_detail_with_members(context, json.loads(row[0])))


def event_descriptor(rule: Data) -> Data:
    descriptor = dict(
        event_id=rule["event_id"],
        event_category_id=1,
        event_category=1,
        name=rule["name"],
        **{k + "_date": util.timestamp_to_datetime(timestamp(rule[k])) for k in ("open", "start", "end", "close")},
        banner_asset_name=rule["banner_asset"],
        result_banner_asset_name=rule["banner_asset"],
        banner_se_asset_name="",
        description=rule["description"],
        member_category=1,
        sif005_period_key=rule["key"],
        sif005_rule_version=rule["version"],
    )
    protocol = event_presentation.protocol_version()
    if protocol is not None:
        descriptor["event_presentation_protocol"] = protocol
    return descriptor


async def friend_eligible(context: idol.BasicSchoolIdolContext, current: main.User, helper: int):
    other = await user.get(context, helper)
    if other is None or other.id == current.id or other.locked:
        return False
    status = await friend.get_friend_status(context, current, other)
    return (
        status in (const.FRIEND_STATUS.OTHER, const.FRIEND_STATUS.APPROVAL_WAIT)
        and await friend.count(context, current.id) < current.friend_max
        and await friend.count(context, helper) < other.friend_max
    )


def song_for(rule: Data | None, difficulty_id: int) -> Data | None:
    return next((s for s in rule["songs"] if s["id"] == difficulty_id), None) if rule else None


async def play(context: idol.SchoolIdolUserParams, request: game_live.LivePlayRequest, function: PlayHandler) -> Wire:
    await lock(context)
    key, digest = identity(context, "play")
    old = await cached(context, "play", key, digest)
    if old is not None:
        return Wire(old)
    current = await user.get_current(context)
    if await live.get_live_in_progress(context, current) is not None:
        reject("a live is already in progress; resume or abandon it")
    record, rule = await period(context, active=True)
    song = song_for(rule, request.live_difficulty_id) if rule else None
    reserved = reserved_song(request.live_difficulty_id)
    if reserved and (song is None or request.is_training):
        reject("event song is unavailable", 403)
    if type(request.lp_factor) is not int or request.lp_factor not in (
        rule["allowed_factors"] if rule else [1, 2, 3, 4]
    ):
        reject("unsupported play multiplier", 422)
    # Only MASTER Challenge training contributes collection tokens
    normal = get_policy().get("normal_live_rewards")
    if request.is_training:
        setting = await live.get_live_setting_from_difficulty_id(context, request.live_difficulty_id)
        challenge = bool(
            normal
            and setting
            and setting.difficulty == 6
            and request.live_difficulty_id
            in await live.get_training_live_difficulty_id_from_live_track_id(context, setting.live_track_id)
        )
        if not challenge:
            record, rule = None, None
    if record and not song:
        setting = await live.get_live_setting_from_difficulty_id(context, request.live_difficulty_id)
        # Original AC collaboration charts do not produce collection tokens/pt.
        if setting is not None and setting.ac_flag:
            record, rule = None, None
    if rule is not None and not song and normal:
        rule = dict(rule, sif005_normal_live_rewards=normal)
    if rule is not None and song and get_policy().get("event_live_rewards"):
        rule = dict(rule, sif005_event_live_rewards=get_policy()["event_live_rewards"])
    if song and rule is not None:
        rule = dict(rule, sif005_song_goal_definition=event_song_goal.definition(song))
    set_live_context(context, record, rule, free_token=bool(song))
    await discard_live(context, current.id)
    cost = 0
    cheer_percent = 100
    cheer_snapshot = []
    if record is not None and rule is not None:
        player_progress = await progress(context, record["id"], current.id)
        cheer_percent, _, cheer_snapshot = await cheer(context, record, rule, current.id)
        if song:
            cost = song["cost"] * request.lp_factor
            if player_progress["tokens"] < cost:
                raise idol.error.by_code(idol.error.ERROR_CODE_LIVE_NOT_ENOUGH_EVENT_POINT)
            await sql(
                context,
                "UPDATE sif005_progress SET tokens=tokens-:n WHERE period_id=:p AND user_id=:u",
                n=cost,
                p=record["id"],
                u=current.id,
            )
    response = await function(context, request)
    wire = response.model_dump(exclude_none=True)
    info = wire["live_list"][0]["live_info"]
    notes = info["notes_list"]
    indices = []
    # Archived charts can already contain event tap visuals from a different period
    for n in notes:
        if n["effect"] == 2:
            n["effect"] = 1
        elif n["effect"] == 12:
            n["effect"] = 11
    if record is not None and rule is not None and not song:
        setting = await live.get_live_setting_from_difficulty_id(context, request.live_difficulty_id)
        if setting is None:
            reject("event Live setting unavailable", 422)
        # Restrict event icons to plain taps. Hold/slide semantics stay untouched.
        if any(n.get("speed", 1) != 1 for n in notes):
            reject("variable-speed chart token mapping not supported", 422)
        notes.sort(key=lambda n: (n["timing_sec"], n["position"]))
        eligible = [i for i, n in enumerate(notes) if n["effect"] == 1]
        normal = rule.get("sif005_normal_live_rewards")
        if normal:
            entry = await live.get_live_info_table(context, request.live_difficulty_id)
            quota = (
                normal["tokens_by_base_lp"].get(str(entry.capital_value))
                if entry is not None and entry.capital_type == 1
                else None
            )
            if quota is None or len(eligible) < quota:
                reject("unsupported normal live token quota", 422)
            count = quota
        else:
            count = min(rule["normal_token_count"].get(str(setting.difficulty), 0), len(eligible))
        indices = [eligible[(i * len(eligible)) // count] for i in range(count)] if count else []
        for i in indices:
            notes[i]["effect"] = 2
    wire["is_marathon_event"] = bool(record and not song)
    wire["marathon_event_id"] = rule["event_id"] if record is not None and rule is not None else None
    await context.db.main.execute(
        schema.live.insert().values(
            user_id=current.id,
            period_id=record["id"] if record is not None else None,
            difficulty_id=request.live_difficulty_id,
            party_user_id=request.party_user_id,
            training=int(request.is_training),
            is_event_song=int(bool(song)),
            factor=request.lp_factor,
            cost=cost,
            started_at=now(),
            rule_json=dump(rule) if rule else None,
            cheer_percent=cheer_percent,
            cheer_snapshot=dump(cheer_snapshot),
            notes_json=dump(notes),
            token_indices=dump([i + 1 for i in indices]),
            play_key=key,
            play_hash=digest,
        )
    )
    await remember(context, "play", key, digest, wire)
    return Wire(wire)


def valid_token_count(request: game_live.LiveRewardRequest, run: Row) -> int:
    notes = json.loads(run["notes_json"])
    indices = set(json.loads(run["token_indices"]))
    logs = request.precise_score_log.get("precise_list")
    if not isinstance(logs, list) or len(logs) != len(notes):
        reject("complete native precise_list required for token verification", 422)
    seen = set()
    hits = 0
    for row in logs:
        if not isinstance(row, dict):
            reject("invalid note judgment", 422)
        number = row.get("note_number")
        accuracy = row.get("accuracy")
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or number != int(number)
            or not 1 <= number <= len(notes)
        ):
            reject("invalid note number", 422)
        number = int(number)
        if number in seen:
            reject("duplicate note judgment", 422)
        seen.add(number)
        n = notes[number - 1]
        if (
            type(accuracy) is not int
            or accuracy not in (0, 1, 2, 3, 4, 9)
            or row.get("position") != n["position"]
            or row.get("effect") != n["effect"]
        ):
            reject("judgment does not match issued chart", 422)
        # Native event notes count on tap (including BAD), but not MISS.
        if number in indices and accuracy in (1, 2, 3, 4):
            hits += 1
    counts = [request.perfect_cnt, request.great_cnt, request.good_cnt, request.bad_cnt, request.miss_cnt]
    if any(v < 0 for v in counts) or sum(counts) != len(notes):
        reject("judgment total does not match chart", 422)
    return hits


async def settle(
    context: idol.SchoolIdolUserParams, request: game_live.LiveRewardRequest, function: RewardHandler
) -> Wire | game_live.LiveRewardResponse:
    await lock(context)
    async with context.db.main.begin_nested():
        return await settle_live(context, request, function)


async def settle_live(
    context: idol.SchoolIdolUserParams, request: game_live.LiveRewardRequest, function: RewardHandler
) -> Wire | game_live.LiveRewardResponse:
    current = await user.get_current(context)
    run = await live_run(context, current.id, for_update=True)
    if run is None and not await has_reward_receipt(context, for_update=True):
        if reserved_song(request.live_difficulty_id):
            reject("event live session required", 403)
        return await function(context, request)
    key, digest = identity(context, "reward")
    previous = await cached(context, "reward", key, digest)
    if previous is not None:
        return Wire(previous)
    if run is None:
        reject("event live session required", 403)
    if run["difficulty_id"] != request.live_difficulty_id or bool(run["training"]) != request.is_training:
        reject("settlement does not match started live")
    ongoing = await live.get_live_in_progress(context, current)
    if ongoing is None or ongoing.party_user_id != run["party_user_id"] or ongoing.lp_factor != run["factor"]:
        reject("live session mismatch")
    rule: Data | None = json.loads(run["rule_json"]) if run["rule_json"] else None
    record = (
        (await sql(context, "SELECT * FROM sif005_period WHERE id=:p", p=run["period_id"])).mappings().first()
        if rule
        else None
    )
    if rule is not None and record is None:
        reject("saved Marathon period is missing")
    issued_notes = json.loads(run["notes_json"])
    set_live_context(context, record, rule, issued=(run["difficulty_id"], issued_notes))
    eligible = not run["training"] and await friend_eligible(context, current, run["party_user_id"])
    tokens = base = total = 0
    score_rate = combo_rate = 100
    percent = 100
    qualifies = bool(
        record and now() < record["freeze_at"] and record["frozen_at"] is None and run["started_at"] < record["end_at"]
    )
    before = await progress(context, record["id"], current.id) if record else None
    score = request.score_smile + request.score_cute + request.score_cool
    if (
        any(v < 0 for v in (request.score_smile, request.score_cute, request.score_cool, request.max_combo))
        or score > 2147483647
    ):
        reject("invalid score", 422)
    if request.remain_hp <= 0:
        reject("failed live must use gameover", 422)
    if rule and (
        any(
            v < 0 for v in (request.perfect_cnt, request.great_cnt, request.good_cnt, request.bad_cnt, request.miss_cnt)
        )
        or sum((request.perfect_cnt, request.great_cnt, request.good_cnt, request.bad_cnt, request.miss_cnt))
        != len(issued_notes)
    ):
        reject("judgment total does not match chart", 422)
    song = song_for(rule, request.live_difficulty_id) if rule else None
    detail = None
    if qualifies and rule is not None:
        if song:
            setting = await live.get_live_setting_from_difficulty_id(context, request.live_difficulty_id)
            info = await live.get_live_info_table(context, request.live_difficulty_id)
            if setting is None or info is None:
                reject("saved Marathon chart is missing")
            ranks = live.get_live_ranks(info, setting, score, request.max_combo, 1)
            score_rate = rule["score_percent_by_rank"][ranks[0] - 1]
            combo_rate = rule["combo_percent_by_rank"][ranks[1] - 1]
            base = song["base_point"] * run["factor"]
            percent = run["cheer_percent"] if "event" in rule["cheer_sources"] else 100
            point_rule = rule.get("sif005_event_live_rewards")
            if point_rule:
                base, score_rate, combo_rate, total = event_point_result(
                    point_rule, setting.difficulty, ranks, run["factor"], percent
                )
            else:
                total = base * score_rate * combo_rate * percent // 1000000
            detail = {
                "live_list": [
                    {
                        "live_info": {
                            "live_difficulty_id": request.live_difficulty_id,
                            "is_random": False,
                            "ac_flag": 0,
                            "swing_flag": 0,
                        },
                        "deck_info": json.loads(ongoing.deck_data),
                    }
                ],
                "score_info": {
                    "score": score,
                    "max_combo": request.max_combo,
                    "perfect_cnt": request.perfect_cnt,
                    "great_cnt": request.great_cnt,
                    "good_cnt": request.good_cnt,
                    "bad_cnt": request.bad_cnt,
                    "miss_cnt": request.miss_cnt,
                },
            }
            detail = await ranking_detail_with_members(context, detail)
        else:
            tokens = valid_token_count(request, run) * run["factor"]
            base = tokens * rule["normal_point_per_token"]
            percent = run["cheer_percent"] if "normal" in rule["cheer_sources"] else 100
            total = base * score_rate * combo_rate * percent // 1000000
    original_request = request
    if song and request.precise_score_log.get("live_setting", {}).get("precise_score_auto_update_flag"):
        original_request = request.model_copy(deep=True)
        original_request.precise_score_log["live_setting"]["precise_score_auto_update_flag"] = False
    response = await function(context, original_request)
    wire = response.model_dump(exclude_none=True)
    wire["can_send_friend_request"] = eligible
    if run["is_event_song"]:
        await event_song_goal.settle(context, current, run, wire)
    if record is not None and rule is not None and before is not None:
        gained = []
        if qualifies:
            await sql(
                context,
                """UPDATE sif005_progress SET tokens=tokens+:tokens,points=points+:points,clears=clears+1,
                event_clears=event_clears+:event,point_time=CASE WHEN :points>0 THEN :t ELSE point_time END WHERE period_id=:p AND user_id=:u""",
                tokens=tokens,
                points=total,
                event=int(bool(song)),
                t=now(),
                p=record["id"],
                u=current.id,
            )
            if song and score > before["score"]:
                await sql(
                    context,
                    "UPDATE sif005_progress SET score=:s,score_time=:t,best_detail=:d WHERE period_id=:p AND user_id=:u",
                    s=score,
                    t=now(),
                    d=dump(detail),
                    p=record["id"],
                    u=current.id,
                )
            for condition in rule["point_rewards"]:
                if condition["points"] <= before["points"] + total:
                    gained.extend(await grant(context, record, rule, current.id, condition, condition["delivery"]))
        after = await progress(context, record["id"], current.id)
        nxt = next((x for x in rule["point_rewards"] if x["points"] > after["points"]), None)
        wire["event_info"] = {
            "event_id": rule["event_id"],
            "point_icon_asset": rule["point_icon_asset"],
            "event_point_info": {
                "before_event_point": before["tokens"],
                "after_event_point": after["tokens"],
                "before_total_event_point": before["points"],
                "after_total_event_point": after["points"],
                "base_event_point": base,
                "added_event_point": tokens,
                "event_yell_rate": percent / 100,
                "score_rank_rate": score_rate / 100,
                "combo_rank_rate": combo_rate / 100,
            },
            "event_reward_info": gained,
            "next_event_reward_info": {
                "event_point": nxt["points"] if nxt else 0,
                "rewards": await preview_items(context, nxt["items"]) if nxt else [],
            },
        }
        wire["present_cnt"] = await reward.count_presentbox(context, current)
        wire["after_user_info"] = (await user.get_user_info(context, current)).model_dump()
    await discard_live(context, current.id)
    await remember(context, "reward", key, digest, wire)
    return Wire(wire)


def current_period(event_id: int | None = None, active: bool = False) -> Data | None:
    start, end = ("start", "end") if active else ("open", "close")
    timestamp_now = now()
    return next(
        (
            period
            for period in selected_periods()
            if (event_id is None or period["event_id"] == event_id)
            and timestamp(period[start]) <= timestamp_now < timestamp(period[end])
        ),
        None,
    )


def reserved_song(difficulty_id: int) -> bool:
    periods = get_policy()["periods"]
    return any(song_for(period, difficulty_id) for period in periods) or (
        90000 <= difficulty_id <= 99999
        and any(song_id == difficulty_id for _, song_id in event_charts.reserved_songs(periods))
    )


async def live_run(context: idol.BasicSchoolIdolContext, user_id: int, *, for_update: bool = False) -> Row | None:
    query = sqlalchemy.select(schema.live).where(schema.live.c.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    return (await context.db.main.execute(query)).mappings().first()


async def discard_live(context: idol.BasicSchoolIdolContext, user_id: int) -> None:
    await context.db.main.execute(schema.live.delete().where(schema.live.c.user_id == user_id))


def set_live_context(
    context: idol.BasicSchoolIdolContext,
    row: Row | None,
    rule: Data | None,
    *,
    free_token: bool = False,
    issued: tuple[int, list[Data]] | None = None,
) -> None:
    context.set_cache("marathon", "period", (row, rule))
    context.set_cache("marathon", "free_token", free_token)
    context.set_cache("marathon", "issued_notes", issued)


@contextlib.contextmanager
def live_scope(context: idol.BasicSchoolIdolContext):
    names = ("marathon", "live_setting_from_difficulty", "live_capital_value", "adjacent_live_difficulty_id")
    for name in names:
        context.cache.pop(name, None)
    try:
        yield
    finally:
        for name in names:
            context.cache.pop(name, None)


async def prepare_party(context: idol.SchoolIdolUserParams, request: game_live.LivePartyListRequest) -> None:
    row, rule = await period(context, active=True)
    song = song_for(rule, request.live_difficulty_id)
    if song is None:
        if reserved_song(request.live_difficulty_id):
            reject("event song is unavailable", 403)
        return
    assert row is not None and rule is not None
    if request.is_training or request.lp_factor not in rule["allowed_factors"]:
        reject("invalid event play settings", 422)
    balance = (
        await context.db.main.scalar(
            sqlalchemy.select(schema.progress.c.tokens).where(
                schema.progress.c.period_id == row["id"], schema.progress.c.user_id == authenticated_id(context)
            )
        )
        or 0
    )
    if balance < song["cost"] * request.lp_factor:
        raise idol.error.by_code(idol.error.ERROR_CODE_LIVE_NOT_ENOUGH_EVENT_POINT)
    set_live_context(context, row, rule, free_token=True)


async def has_reward_receipt(context: idol.SchoolIdolUserParams, *, for_update: bool = False) -> bool:
    try:
        key, _ = identity(context, "reward")
    except idol.error.IdolError:
        return False
    query = sqlalchemy.select(schema.receipt.c.request_key).where(
        schema.receipt.c.user_id == authenticated_id(context),
        schema.receipt.c.action == "reward",
        schema.receipt.c.request_key == key,
    )
    if for_update:
        query = query.with_for_update()
    return await context.db.main.scalar(query) is not None


def active_period(period: Data) -> bool:
    return timestamp(period["start"]) <= now() < timestamp(period["end"])


async def visible_period(context: idol.BasicSchoolIdolContext) -> tuple[Row | None, Data | None]:
    if not enabled():
        return None, None
    await lock(context)
    await finalize_due(context)
    return await period(context)


async def lock_for_settlement(context: idol.SchoolIdolUserParams) -> bool:
    # The event mutex must precede the player lock used by reward eggs and account deletion
    if enabled():
        await lock(context)
        return True
    bind = context.db.main.bind
    has_sqlite_egg_lock = bind is not None and bind.dialect.name == "sqlite" and reward_egg.get_policy() is not None
    if has_sqlite_egg_lock:
        await lock(context)
    recorded = await live_run(context, authenticated_id(context)) is not None or await has_reward_receipt(context)
    if recorded and not has_sqlite_egg_lock:
        await lock(context)
    return recorded


async def legacy_egg_event_reward(context: idol.SchoolIdolUserParams) -> reward_egg.WireResponse | None:
    if reward_egg.get_policy() is not None:
        return None
    try:
        key, digest = reward_egg.request_identity(context, "live")
    except idol.error.IdolError:
        return None
    player_id = authenticated_id(context)
    receipt = await context.db.main.get(main.DestinyRewardEggReceipt, (player_id, "live", key))
    if receipt is None:
        return None
    response = reward_egg.WireResponse.model_validate_json(receipt.response)
    if "event_info" not in response.root:
        return None
    await user.get_current(context)
    if receipt.payload_hash != digest:
        reject("request identity reused with changed payload")
    return response
