import argparse
import contextlib
import hashlib
import json
import pathlib
import sqlite3
import time

from typing import Any


def _review_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _review_snapshot(
    database: sqlite3.Connection, user_id: int, achievement_id: int, blocked_period: str
) -> dict[str, Any]:
    key = (user_id, achievement_id, blocked_period)
    hold = database.execute(
        """SELECT * FROM destiny_achievement_claim_review
        WHERE user_id=? AND achievement_id=? AND blocked_period=?""",
        key,
    ).fetchone()
    if hold is None:
        raise ValueError("The requested legacy claim hold does not exist")
    row = database.execute(
        """SELECT id,user_id,achievement_id,count,is_accomplished,is_reward_claimed,
        reset_type,reset_value,insert_date,update_date FROM achievement
        WHERE user_id=? AND achievement_id=?""",
        key[:2],
    ).fetchone()
    paid = database.execute(
        """SELECT * FROM destiny_achievement_claim
        WHERE user_id=? AND achievement_id=? AND period_key=?""",
        key,
    ).fetchone()
    return {
        "hold": dict(hold),
        "achievement": None if row is None else dict(row),
        "claim": None if paid is None else dict(paid),
    }


def make_review_plan(path: pathlib.Path, user_id: int, achievement_id: int, blocked_period: str) -> dict[str, Any]:
    if (
        any(type(value) is not int or value <= 0 for value in (user_id, achievement_id))
        or not isinstance(blocked_period, str)
        or not blocked_period
    ):
        raise ValueError("A positive account/achievement ID and explicit blocked period are required")
    target = path.resolve()
    with contextlib.closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=25)) as database:
        database.execute("BEGIN")
        database.row_factory = sqlite3.Row
        snapshot = _review_snapshot(database, user_id, achievement_id, blocked_period)
    if snapshot["hold"]["resolved_at"] is not None:
        raise ValueError("The legacy claim hold was already reviewed")
    return {
        "version": 1,
        "database": str(target),
        "snapshot": snapshot,
        "snapshot_sha256": _review_digest(snapshot),
        "decision": "review_required",
        "approved": False,
        "approved_by": "",
        "evidence": "",
        "note": "confirm_paid preserves a proven old payout without issuing currency; "
        "allow_retry requires evidence that no payout happened in the held period. "
        "Normal eligibility still applies after review.",
    }


def apply_review_plan(path: pathlib.Path, plan: object) -> dict[str, Any]:
    target = path.resolve()
    if not isinstance(plan, dict):
        raise TypeError("The reviewed plan must be a JSON object")
    if (
        plan.get("version") != 1
        or not isinstance(plan.get("database"), str)
        or pathlib.Path(plan["database"]).resolve() != target
    ):
        raise ValueError("Review plan targets a different database or version")
    if plan.get("approved") is not True or any(
        not isinstance(plan.get(key), str) or not plan[key].strip() for key in ("approved_by", "evidence")
    ):
        raise ValueError("An explicit reviewer and period-specific payout evidence are required")
    decision = plan.get("decision")
    if decision not in ("confirm_paid", "allow_retry"):
        raise ValueError("Select a supported, evidenced review decision")
    snapshot = plan.get("snapshot", {})
    if not isinstance(snapshot, dict) or _review_digest(snapshot) != plan.get("snapshot_sha256"):
        raise ValueError("Review snapshot checksum mismatch")
    hold = snapshot.get("hold", {})
    if not isinstance(hold, dict):
        raise TypeError("Invalid review hold")
    user_id = hold.get("user_id")
    achievement_id = hold.get("achievement_id")
    blocked_period = hold.get("blocked_period")
    if (
        type(user_id) is not int
        or user_id <= 0
        or type(achievement_id) is not int
        or achievement_id <= 0
        or not isinstance(blocked_period, str)
        or not blocked_period
    ):
        raise ValueError("Invalid review account, achievement, or period")
    key = (user_id, achievement_id, blocked_period)
    plan_hash = _review_digest(plan)
    with contextlib.closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True, timeout=25)) as database, database:
        database.row_factory = sqlite3.Row
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            """SELECT plan_hash FROM destiny_achievement_claim_review_decision
            WHERE user_id=? AND achievement_id=? AND blocked_period=?""",
            key,
        ).fetchone()
        if existing is not None:
            if existing[0] != plan_hash:
                raise ValueError("This hold already has a different review decision")
            return {"already_applied": True, "decision": decision, "rewards_granted": 0}
        current = _review_snapshot(database, *key)
        if current["hold"]["resolved_at"] is not None or _review_digest(current) != plan["snapshot_sha256"]:
            raise ValueError("Account or hold changed after review; make a fresh plan")
        if current["claim"] is not None and decision == "allow_retry":
            raise ValueError("An existing claim ledger cannot be removed or overridden by review")
        now = int(time.time())
        if decision == "confirm_paid":
            database.execute(
                """INSERT INTO destiny_achievement_claim
                (user_id,achievement_id,period_key,owning_achievement_id,claimed_at,request_key,rewards_json)
                VALUES(?,?,?,?,?,?,'[]') ON CONFLICT(user_id,achievement_id,period_key) DO NOTHING""",
                (*key, hold["owning_achievement_id"], 0, "legacy-review:" + plan_hash),
            )
        database.execute(
            """INSERT INTO destiny_achievement_claim_review_decision
            (user_id,achievement_id,blocked_period,plan_hash,decision,approved_by,evidence,plan_json,applied_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (*key, plan_hash, decision, plan["approved_by"], plan["evidence"], json.dumps(plan, sort_keys=True), now),
        )
        database.execute(
            """UPDATE destiny_achievement_claim_review SET resolved_at=?
            WHERE user_id=? AND achievement_id=? AND blocked_period=?""",
            (now, *key),
        )
    return {"already_applied": False, "decision": decision, "rewards_granted": 0}


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Review a historical achievement claim in an offline SQLite database.")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="Write a review plan awaiting approval to stdout")
    plan.add_argument("database", type=pathlib.Path)
    plan.add_argument("user_id", type=int)
    plan.add_argument("achievement_id", type=int)
    plan.add_argument("blocked_period")

    apply = commands.add_parser("apply", help="Apply an approved review plan")
    apply.add_argument("database", type=pathlib.Path)
    apply.add_argument("plan", type=pathlib.Path)

    options = parser.parse_args(args)
    try:
        if options.command == "plan":
            result = make_review_plan(options.database, options.user_id, options.achievement_id, options.blocked_period)
        else:
            result = apply_review_plan(options.database, json.loads(options.plan.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))


async def run_script(args: list[str]) -> None:
    main(args)


if __name__ == "__main__":
    main()
