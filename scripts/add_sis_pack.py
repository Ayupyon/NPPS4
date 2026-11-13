"""Preview or add nine normal SIS each, plus two extra of each standard Fortune."""

import argparse
import json

import sqlalchemy

import npps4.db.main
import npps4.idol
import npps4.idol.database
import npps4.util

FORTUNE = {61: "Pure Fortune", 91: "Smile Fortune", 92: "Cool Fortune"}


async def run_script(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", "-u", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args(args)
    if options.user_id <= 0:
        raise ValueError("Require a positive user ID")
    async with npps4.idol.BasicSchoolIdolContext() as context:
        if options.apply:
            await npps4.idol.database.lock_user(context.db.main, options.user_id)
        target = await context.db.main.get(npps4.db.main.User, options.user_id, with_for_update=options.apply)
        if target is None:
            raise ValueError("User not found")
        skills = dict(
            (
                await context.db.unit.execute(
                    sqlalchemy.text(
                        "SELECT unit_removable_skill_id, name_en FROM unit_removable_skill_m "
                        "WHERE skill_type = 1 ORDER BY unit_removable_skill_id"
                    )
                )
            ).all()
        )
        if not skills or any(skills.get(key) != value for key, value in FORTUNE.items()):
            raise ValueError("SIS database differs from the legacy pack definition")
        rows = (
            await context.db.main.scalars(
                sqlalchemy.select(npps4.db.main.RemovableSkillInfo).where(
                    npps4.db.main.RemovableSkillInfo.user_id == target.id
                )
            )
        ).all()
        owned = {row.unit_removable_skill_id: row for row in rows}
        total = 0
        for skill_id in skills:
            amount = 11 if skill_id in FORTUNE else 9
            total += amount
            if options.apply:
                row = owned.get(skill_id)
                if row is None:
                    row = npps4.db.main.RemovableSkillInfo(
                        user_id=target.id, unit_removable_skill_id=skill_id, amount=0, insert_date=npps4.util.time()
                    )
                    context.db.main.add(row)
                row.amount += amount
    print(json.dumps(dict(applied=options.apply, user_id=options.user_id, types=len(skills), gems_added=total)))
    print("Each --apply adds another full pack.")


if __name__ == "__main__":
    import npps4.scriptutils.boot

    npps4.scriptutils.boot.start(run_script)
