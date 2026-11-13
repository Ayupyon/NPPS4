import functools
import json
import pathlib

import sqlalchemy

from . import advanced
from . import item_model
from . import live
from .. import const
from .. import idol
from .. import util
from ..config import config
from ..db import main
from ..db import marathon as schema

from typing import Any

type Data = dict[str, Any]

EVENT_CLEAR_COUNTS = {"c": 1, "b": 2, "a": 3, "s": 5}


@functools.lru_cache(maxsize=1)
def load_catalog(path: pathlib.Path) -> dict[str, Data]:
    catalog = json.loads(path.read_bytes())
    if catalog.get("schema_version") != 1:
        raise ValueError("Invalid event Song Goal schema")
    songs = catalog["songs"]
    seen = set()
    for key, data in songs.items():
        # Normalize even a future catalog imported from an older native master.
        data["clear_counts"] = dict(EVENT_CLEAR_COUNTS)
        if str(int(key)) != key or not 0 < int(key) < 100000:
            raise ValueError("Invalid Song Goal difficulty ID")
        counts = [data["clear_counts"][x] for x in ("c", "b", "a", "s")]
        if any(type(x) is not int or x < 1 for x in counts) or counts != sorted(set(counts)):
            raise ValueError("Invalid clear thresholds")
        if type(data["setting_id"]) is not int or data["setting_id"] < 1:
            raise ValueError("Invalid Song Goal setting")
        goals = data["goals"]
        if len(goals) != 12 or {(g["live_goal_type"], g["rank"]) for g in goals} != {
            (t, r) for t in (1, 2, 3) for r in (1, 2, 3, 4)
        }:
            raise ValueError("Incomplete native goals")
        for g in goals:
            ident = g["live_goal_reward_id"]
            if type(ident) is not int or not 0 < ident <= 2147483647 or ident in seen:
                raise ValueError("Duplicate native goal ID")
            seen.add(ident)
            if type(g["amount"]) is not int or g["amount"] <= 0:
                raise ValueError("Invalid native reward")
            const.ADD_TYPE(g["add_type"])
    return songs


def definition(song: Data) -> Data:
    data = load_catalog(pathlib.Path(config.ROOT_DIR, "resources/events/event_song_goals.json")).get(str(song["id"]))
    if data is None or data["setting_id"] != song["setting_id"]:
        raise ValueError("Event Song Goal definition missing or mismatched")
    return data


async def achieved(context: idol.BasicSchoolIdolContext, clear: main.LiveClear) -> list[int]:
    ledger = schema.song_goal.c
    query = (
        sqlalchemy.select(ledger.goal_id)
        .where(ledger.user_id == clear.user_id, ledger.storage_id == clear.live_difficulty_id)
        .order_by(ledger.goal_id)
    )
    return list(await context.db.main.scalars(query))


async def settle(
    context: idol.BasicSchoolIdolContext, current: main.User, run: sqlalchemy.RowMapping, wire: Data
) -> None:
    period = json.loads(run["rule_json"])
    difficulty_id = run["difficulty_id"]
    song = next(song for song in period["songs"] if song["id"] == difficulty_id)
    data = period.get("sif005_song_goal_definition") or definition(song)
    storage = -(run["period_id"] * 100000 + difficulty_id)
    clear = await live.get_live_clear_data(context, current, difficulty_id)
    if clear is None or clear.live_difficulty_id != storage:
        raise ValueError("Event Song Goal progress mismatch")
    setting = await live.get_live_setting(context, data["setting_id"])
    entry = await live.get_live_info_table(context, difficulty_id)
    if setting is None or entry is None:
        raise ValueError("Event Song Goal chart missing")
    ranks = live.get_live_ranks(entry, setting, clear.hi_score, clear.hi_combo_cnt, clear.clear_cnt)
    gained = []
    ids = []
    paid = set(await achieved(context, clear))
    # Highest metrics also qualify any goals missed by an earlier version
    for goal in data["goals"]:
        if ranks[goal["live_goal_type"] - 1] > goal["rank"] or goal["live_goal_reward_id"] in paid:
            continue
        reward = item_model.Item(
            add_type=const.ADD_TYPE(goal["add_type"]),
            item_id=goal["item_id"],
            amount=goal["amount"],
            item_category_id=goal["item_category_id"] or 0,
            reward_box_flag=False,
        )
        payload = reward.model_dump(exclude_none=True)
        await context.db.main.execute(
            schema.song_goal.insert().values(
                user_id=current.id,
                period_id=run["period_id"],
                difficulty_id=difficulty_id,
                storage_id=storage,
                goal_id=goal["live_goal_reward_id"],
                reward_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                created_at=util.time(),
            )
        )
        if not (await advanced.add_item(context, current, reward)).success:
            raise ValueError("Unable to grant Event Song Goal reward")
        ids.append(goal["live_goal_reward_id"])
        gained.append(payload)

    existing = wire["goal_accomp_info"]
    if existing["achieved_ids"] or existing["rewards"]:
        raise ValueError("Unexpected native event goals")
    wire["goal_accomp_info"] = {"achieved_ids": ids, "rewards": gained}
