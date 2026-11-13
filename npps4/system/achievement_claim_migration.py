import hashlib
import json

import sqlalchemy
import sqlalchemy.ext.asyncio

from . import achievement_claim
from .. import idol
from .. import util
from ..db import main

BASELINE = "achievement-claim-baseline-v1"
STALE_DAILY_HOLDS = "achievement-stale-daily-hold-v1"
AMBIGUOUS_PERIOD = "Legacy period marker is absent or has no verifiable year"


async def apply(context: idol.BasicSchoolIdolContext) -> None:
    session = context.db.main
    now = util.time()
    baseline, created = await reserve_migration(session, BASELINE, now)
    if created:
        baseline.report_json = json.dumps(await backfill_claims(session, now), sort_keys=True)

    repair, created = await reserve_migration(session, STALE_DAILY_HOLDS, now)
    if created:
        report = await repair_stale_daily_holds(session, baseline.applied_at, now)
        repair.report_json = json.dumps(report, sort_keys=True)


async def reserve_migration(session: sqlalchemy.ext.asyncio.AsyncSession, key: str, now: int):
    marker = main.DestinyAchievementClaimMigration(migration_key=key, applied_at=now, report_json="{}")
    return await achievement_claim.reserve_record(session, marker, key)


async def backfill_claims(session: sqlalchemy.ext.asyncio.AsyncSession, now: int) -> dict[str, int]:
    report = {"recorded_claims": 0, "review_holds": 0, "already_recorded": 0, "rewards_granted": 0}
    achievement = main.Achievement
    query = (
        sqlalchemy.select(
            achievement.id,
            achievement.user_id,
            achievement.achievement_id,
            achievement.reset_type,
            achievement.reset_value,
            achievement.insert_date,
            achievement.update_date,
        )
        .where(achievement.is_reward_claimed.is_(True))
        .order_by(achievement.user_id, achievement.id)
    )
    for row in (await session.execute(query)).mappings():
        user_id, achievement_id = row["user_id"], row["achievement_id"]
        reset_type = row["reset_type"]
        current_cycle = achievement_claim.period_key(reset_type, now)
        current_key = (user_id, achievement_id, current_cycle)
        if await session.get(main.DestinyAchievementClaim, current_key) is not None:
            report["already_recorded"] += 1
            continue

        updated = row["update_date"] or row["insert_date"]
        cycle = "permanent" if reset_type == 0 else None
        if reset_type and updated and updated <= now:
            inferred = achievement_claim.period_key(reset_type, updated)
            if row["reset_value"] == int(inferred.partition(":")[2]):
                cycle = inferred
        if cycle is not None:
            if await session.get(main.DestinyAchievementClaim, (user_id, achievement_id, cycle)) is not None:
                report["already_recorded"] += 1
                continue
            session.add(
                main.DestinyAchievementClaim(
                    user_id=user_id,
                    achievement_id=achievement_id,
                    period_key=cycle,
                    owning_achievement_id=row["id"],
                    claimed_at=updated or 0,
                    request_key=f"migration:{BASELINE}",
                )
            )
            report["recorded_claims"] += 1
        elif await session.get(main.DestinyAchievementClaimReview, current_key) is None:
            session.add(
                main.DestinyAchievementClaimReview(
                    user_id=user_id,
                    achievement_id=achievement_id,
                    blocked_period=current_cycle,
                    owning_achievement_id=row["id"],
                    reason=AMBIGUOUS_PERIOD,
                    evidence_json=json.dumps(dict(row), sort_keys=True),
                    created_at=now,
                )
            )
            report["review_holds"] += 1
    return report


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stale_daily_evidence(hold: main.DestinyAchievementClaimReview, row: sqlalchemy.RowMapping) -> int | None:
    try:
        evidence = json.loads(hold.evidence_json)
    except ValueError:
        return None
    keys = {"id", "user_id", "achievement_id", "reset_type", "reset_value", "insert_date", "update_date"}
    if not isinstance(evidence, dict) or set(evidence) != keys or any(type(evidence[key]) is not int for key in keys):
        return None
    if (
        evidence["reset_type"] != 1
        or evidence["reset_value"] != 0
        or row["reset_type"] != 1
        or evidence["id"] != hold.owning_achievement_id
        or evidence["id"] != row["id"]
        or evidence["user_id"] != hold.user_id
        or evidence["achievement_id"] != hold.achievement_id
        or hold.blocked_period != achievement_claim.period_key(1, hold.created_at)
    ):
        return None
    updated = evidence["update_date"]
    if not (0 < evidence["insert_date"] <= updated <= hold.created_at):
        return None
    if util.get_days_since_unix(updated) >= util.get_days_since_unix(hold.created_at):
        return None
    return updated


async def repair_stale_daily_holds(
    session: sqlalchemy.ext.asyncio.AsyncSession, baseline_at: int, now: int
) -> dict[str, int]:
    report = {
        "resolved_stale_daily_holds": 0,
        "retained_holds": 0,
        "rewards_granted": 0,
        "progress_rows_changed": 0,
        "claim_ledger_rows_changed": 0,
    }
    review = main.DestinyAchievementClaimReview
    achievement = main.Achievement
    holds = (await session.scalars(sqlalchemy.select(review).where(review.resolved_at.is_(None)))).all()
    for hold in holds:
        report["retained_holds"] += 1
        if hold.reason != AMBIGUOUS_PERIOD or hold.created_at != baseline_at or not (0 < baseline_at <= now):
            continue
        key = (hold.user_id, hold.achievement_id, hold.blocked_period)
        if (
            await session.get(main.DestinyAchievementClaim, key) is not None
            or await session.get(main.DestinyAchievementClaimReviewDecision, key) is not None
        ):
            continue
        query = sqlalchemy.select(
            achievement.id,
            achievement.user_id,
            achievement.achievement_id,
            achievement.count,
            achievement.is_accomplished,
            achievement.is_reward_claimed,
            achievement.reset_type,
            achievement.reset_value,
            achievement.insert_date,
            achievement.update_date,
        ).where(achievement.user_id == hold.user_id, achievement.achievement_id == hold.achievement_id)
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            continue
        updated = stale_daily_evidence(hold, row)
        if updated is None:
            continue

        snapshot = {
            "hold": {column.key: getattr(hold, column.key) for column in review.__table__.columns},
            "achievement": dict(
                row, is_accomplished=int(row["is_accomplished"]), is_reward_claimed=int(row["is_reward_claimed"])
            ),
            "claim": None,
        }
        evidence = (
            "The frozen native achievement last-write timestamp predates the held game day. "
            "No claim ledger or explicit review overrides that evidence."
        )
        plan = {
            "version": 1,
            "migration": STALE_DAILY_HOLDS,
            "snapshot": snapshot,
            "snapshot_sha256": digest(snapshot),
            "decision": "allow_retry",
            "evidence": evidence,
            "source_update_date": updated,
            "rewards_granted": 0,
        }
        session.add(
            main.DestinyAchievementClaimReviewDecision(
                user_id=hold.user_id,
                achievement_id=hold.achievement_id,
                blocked_period=hold.blocked_period,
                plan_hash=digest(plan),
                decision="allow_retry",
                approved_by=f"system:{STALE_DAILY_HOLDS}",
                evidence=evidence,
                plan_json=json.dumps(plan, sort_keys=True),
                applied_at=now,
            )
        )
        hold.resolved_at = now
        report["retained_holds"] -= 1
        report["resolved_stale_daily_holds"] += 1
    return report
