"""Inspect reward eggs or cancel pending/active eggs before reverting the egg feature."""

import argparse
import json

import sqlalchemy

import npps4.db.main
import npps4.idol


async def inspect_eggs(context: npps4.idol.BasicSchoolIdolContext) -> dict[str, int]:
    users, eggs = npps4.db.main.User, npps4.db.main.DestinyRewardEgg
    active = await context.db.main.scalar(
        sqlalchemy.select(sqlalchemy.func.count()).select_from(users).where(users.limited_effort_event_id > 0)
    )
    pending = await context.db.main.scalar(
        sqlalchemy.select(sqlalchemy.func.count()).select_from(eggs).where(eggs.pending_event > 0)
    )
    invalid = await context.db.main.scalar(
        sqlalchemy.select(sqlalchemy.func.count())
        .select_from(users)
        .outerjoin(eggs, eggs.user_id == users.id)
        .where(
            users.limited_effort_event_id > 0,
            sqlalchemy.or_(eggs.saved_spec.is_(None), eggs.saved_spec < 1, eggs.saved_spec > 5),
        )
    )
    return dict(active=active or 0, pending=pending or 0, invalid_restore_points=invalid or 0)


async def drain(context: npps4.idol.BasicSchoolIdolContext) -> dict[str, int]:
    # Caller must stop all writers. Retain receipt ledgers and awarded inventory.
    if await context.db.main.scalar(sqlalchemy.select(npps4.db.main.LiveInProgress.id).limit(1)):
        raise ValueError("Unfinished songs must finish or be abandoned before draining eggs")
    report = await inspect_eggs(context)
    if report["invalid_restore_points"]:
        raise ValueError("Active eggs have missing or invalid normal-box restore points")
    users, eggs = npps4.db.main.User, npps4.db.main.DestinyRewardEgg
    rows = (
        await context.db.main.scalars(
            sqlalchemy.select(users).where(users.limited_effort_event_id > 0).with_for_update()
        )
    ).all()
    for player in rows:
        state = await context.db.main.get(eggs, player.id, with_for_update=True)
        if state is None or not 1 <= state.saved_spec <= 5:
            raise ValueError("Restore state changed")
        player.live_effort_point_box_spec_id = state.saved_spec
        player.limited_effort_event_id = 0
        player.current_limited_effort_point = 0
    await context.db.main.execute(sqlalchemy.update(eggs).values(pending_event=0, pending_spec=0, saved_spec=0))
    await context.db.main.flush()
    return await inspect_eggs(context)


async def run_script(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check")
    change = sub.add_parser("drain")
    change.add_argument("--server-stopped", action="store_true", required=True)
    change.add_argument("--backup-confirmed", action="store_true", required=True)
    options = parser.parse_args(args)
    async with npps4.idol.BasicSchoolIdolContext() as context:
        result = await drain(context) if options.action == "drain" else await inspect_eggs(context)
    print(json.dumps(dict(action=options.action, **result)))


if __name__ == "__main__":
    import npps4.scriptutils.boot

    npps4.scriptutils.boot.start(run_script)
