"""Preview or add G to an explicitly selected account."""

import argparse
import json

import npps4.db.main
import npps4.idol
import npps4.idol.database


async def run_script(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", "-u", type=int, required=True)
    parser.add_argument("amount", type=int)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args(args)
    if options.user_id <= 0 or not 1 <= options.amount <= 100000000:
        raise ValueError("Require a positive user ID and an amount between 1 and 100000000")
    async with npps4.idol.BasicSchoolIdolContext() as context:
        if options.apply:
            await npps4.idol.database.lock_user(context.db.main, options.user_id)
        target = await context.db.main.get(npps4.db.main.User, options.user_id, with_for_update=options.apply)
        if target is None:
            raise ValueError("User not found")
        before, after = target.game_coin, target.game_coin + options.amount
        if after > 999999999:
            raise ValueError("Result exceeds the legacy tool's maximum; no change made")
        if options.apply:
            target.game_coin = after
    print(json.dumps(dict(applied=options.apply, user_id=options.user_id, before=before, after=after)))


if __name__ == "__main__":
    import npps4.scriptutils.boot

    npps4.scriptutils.boot.start(run_script)
