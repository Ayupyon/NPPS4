"""Offline, previewed Marathon settlement. The caller owns the transaction."""

import copy
import hashlib
import json

import sqlalchemy

from .. import idol
from ..config import config
from ..db import marathon as schema
from ..system import marathon

from typing import Any

type Data = dict[str, Any]


def digest(value: Any) -> str:
    return hashlib.sha256(marathon.dump(value).encode()).hexdigest()


def database_identity() -> str:
    # Bind a plan to this database without writing credentials into the plan.
    return hashlib.sha256(config.get_database_url().encode()).hexdigest()


def rule_for(policy: Data, key: str) -> Data:
    matches = [rule for rule in policy["periods"] if rule["key"] == key]
    if len(matches) != 1:
        raise ValueError("Expected one matching period")
    return matches[0]


def validate_transition(source: Data, target: Data, key: str) -> tuple[Data, Data]:
    before, after = rule_for(source, key), rule_for(target, key)
    expected = copy.deepcopy(source)
    expected_rule = rule_for(expected, key)
    for field in ("end", "close", "settlement_grace_seconds"):
        expected_rule[field] = after[field]
    if expected != target:
        raise ValueError("Only the target period's end, close and grace may change")
    if (
        after["settlement_grace_seconds"] != 0
        or not marathon.timestamp(after["start"])
        < marathon.timestamp(after["end"])
        < marathon.timestamp(after["close"])
        or marathon.timestamp(after["end"]) > marathon.now()
        or marathon.timestamp(after["end"]) > marathon.timestamp(before["end"])
    ):
        raise ValueError("Expected an ended period with zero grace; extending or reopening is not supported")
    return before, after


async def get_record(context: idol.BasicSchoolIdolContext, key: str):
    row = (
        (await context.db.main.execute(sqlalchemy.select(schema.period).where(schema.period.c.period_key == key)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("Period is not registered in this database")
    return row


async def progress_rows(context: idol.BasicSchoolIdolContext, period_id: int) -> list[Data]:
    query = (
        sqlalchemy.select(schema.progress)
        .where(schema.progress.c.period_id == period_id)
        .order_by(schema.progress.c.user_id)
    )
    return [dict(row) for row in (await context.db.main.execute(query)).mappings()]


async def preview(context: idol.BasicSchoolIdolContext, source: Data, target: Data, key: str) -> Data:
    before, after = validate_transition(source, target, key)
    record = await get_record(context, key)
    if record["rule_hash"] != digest(before) or digest(json.loads(record["rule_json"])) != digest(before):
        raise ValueError("Published rules do not match the source configuration")
    if record["frozen_at"] is not None:
        raise ValueError("Period is already settled")
    if await context.db.main.scalar(
        sqlalchemy.select(schema.live.c.user_id).where(schema.live.c.period_id == record["id"]).limit(1)
    ):
        raise ValueError("Unfinished songs must finish or be abandoned before settlement")
    if await context.db.main.scalar(
        sqlalchemy.select(schema.ranking_snapshot.c.user_id)
        .where(schema.ranking_snapshot.c.period_id == record["id"])
        .limit(1)
    ):
        raise ValueError("Unfrozen period already has ranking snapshots")
    progress = await progress_rows(context, record["id"])
    by_user = {row["user_id"]: row for row in progress}
    boards, awards = {}, []
    conditions = [condition["id"] for _, group in marathon.RANK_REWARD_GROUPS for condition in before[group]]
    if await context.db.main.scalar(
        sqlalchemy.select(schema.reward_ledger.c.user_id)
        .where(schema.reward_ledger.c.period_id == record["id"], schema.reward_ledger.c.condition_id.in_(conditions))
        .limit(1)
    ):
        raise ValueError("Unfrozen period already has ranking grants")
    for kind, group in marathon.RANK_REWARD_GROUPS:
        rows = await marathon.rank_rows(context, record, kind)
        boards[kind] = [{field: row[field] for field in ("user_id", "rank", "score")} for row in rows]
        for row in rows:
            if await marathon.rank_profile(context, row["user_id"]) is None:
                raise ValueError("Ranking member has no usable profile")
            state = by_user[row["user_id"]]
            if state["points"] < before["minimum_reward_points"] or state["clears"] < before["minimum_reward_clears"]:
                continue
            if kind == "score" and not state["event_clears"]:
                continue
            for condition in before[group]:
                if condition["from"] <= row["rank"] <= condition["to"]:
                    await marathon.preview_items(context, condition["items"])
                    awards.append(
                        dict(
                            user_id=row["user_id"],
                            kind=kind,
                            rank=row["rank"],
                            condition_id=condition["id"],
                            items=condition["items"],
                        )
                    )
    plan = dict(
        version=1,
        database_identity=database_identity(),
        period_id=record["id"],
        period_key=key,
        source_policy_hash=digest(source),
        target_policy_hash=digest(target),
        original_rule=copy.deepcopy(before),
        target_rule=copy.deepcopy(after),
        progress_digest=digest(progress),
        boards=boards,
        awards=awards,
    )
    plan["checksum"] = digest(plan)
    return plan


async def apply(context: idol.BasicSchoolIdolContext, plan: Data) -> Data:
    if plan.get("version") != 1 or digest({k: v for k, v in plan.items() if k != "checksum"}) != plan.get("checksum"):
        raise ValueError("Invalid or edited settlement plan")
    if plan["database_identity"] != database_identity():
        raise ValueError("Plan belongs to another database configuration")
    target = marathon.get_policy()
    key = plan["period_key"]
    if digest(target) != plan["target_policy_hash"] or rule_for(target, key) != plan["target_rule"]:
        raise ValueError("Configure the reviewed target policy before applying this plan, with the server stopped")
    source = copy.deepcopy(target)
    source["periods"] = [plan["original_rule"] if rule["key"] == key else rule for rule in source["periods"]]
    validate_transition(source, target, key)
    if digest(source) != plan["source_policy_hash"]:
        raise ValueError("Source policy does not match the preview")
    await marathon.lock(context)
    record = await get_record(context, key)
    if record["id"] != plan["period_id"]:
        raise ValueError("Period identity changed")
    if record["frozen_at"] is not None:
        if record["rule_hash"] != digest(plan["target_rule"]):
            raise ValueError("Settled rules differ from this plan")
        return dict(already_settled=True, period_id=record["id"], frozen_at=record["frozen_at"])
    refreshed = await preview(context, source, target, key)
    if refreshed != plan:
        raise ValueError("Standings or rewards changed since preview; generate a new plan")
    rule = plan["target_rule"]
    await context.db.main.execute(
        schema.period.update()
        .where(schema.period.c.id == record["id"])
        .values(
            rule_json=marathon.dump(rule),
            rule_hash=digest(rule),
            end_at=marathon.timestamp(rule["end"]),
            freeze_at=marathon.timestamp(rule["end"]),
        )
    )
    await marathon.finalize_due(context, period_key=key)
    snapshots = [
        dict(row)
        for row in (
            await context.db.main.execute(
                sqlalchemy.select(
                    schema.ranking_snapshot.c.kind,
                    schema.ranking_snapshot.c.user_id,
                    schema.ranking_snapshot.c.rank,
                    schema.ranking_snapshot.c.score,
                )
                .where(schema.ranking_snapshot.c.period_id == record["id"])
                .order_by(schema.ranking_snapshot.c.kind, schema.ranking_snapshot.c.rank)
            )
        ).mappings()
    ]
    expected = [dict(kind=kind, **row) for kind in ("point", "score") for row in plan["boards"][kind]]
    if snapshots != expected:
        raise ValueError("Frozen rankings differ from preview")
    ids = [condition["id"] for _, group in marathon.RANK_REWARD_GROUPS for condition in rule[group]]
    receipts = (
        (
            await context.db.main.execute(
                sqlalchemy.select(schema.reward_ledger).where(
                    schema.reward_ledger.c.period_id == record["id"], schema.reward_ledger.c.condition_id.in_(ids)
                )
            )
        )
        .mappings()
        .all()
    )
    if {(row["user_id"], row["condition_id"]) for row in receipts} != {
        (row["user_id"], row["condition_id"]) for row in plan["awards"]
    }:
        raise ValueError("Granted ranking rewards differ from preview")
    if digest(await progress_rows(context, record["id"])) != plan["progress_digest"]:
        raise ValueError("Settlement changed player points or tokens")
    return dict(
        already_settled=False,
        period_id=record["id"],
        award_groups=len(receipts),
        present_entries=sum(len(json.loads(row["rewards"])) for row in receipts),
    )
