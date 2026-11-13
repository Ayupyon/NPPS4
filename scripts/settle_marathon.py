"""Preview and apply one offline Marathon settlement; never starts a server."""

import argparse
import json
import pathlib

import npps4.idol
import npps4.scriptutils.settlement
import npps4.system.marathon


async def run_script(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan", help="Read-only preview against the configured database")
    plan.add_argument("--period-key", required=True)
    plan.add_argument("--target-policy", type=pathlib.Path, required=True)
    plan.add_argument("--output", type=pathlib.Path, required=True)
    apply = sub.add_parser("apply", help="Apply a reviewed plan after stopping all database writers")
    apply.add_argument("--plan", type=pathlib.Path, required=True)
    apply.add_argument("--server-stopped", action="store_true", required=True)
    apply.add_argument("--backup-confirmed", action="store_true", required=True)
    options = parser.parse_args(args)
    if options.action == "plan":
        target = json.loads(options.target_policy.read_text(encoding="utf-8"))
        npps4.system.marathon.validate_policy(target)
        async with npps4.idol.BasicSchoolIdolContext() as context:
            result = await npps4.scriptutils.settlement.preview(
                context, npps4.system.marathon.get_policy(), target, options.period_key
            )
        with options.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
        print(f"Preview written to {options.output}; no database changes.")
    else:
        document = json.loads(options.plan.read_text(encoding="utf-8"))
        async with npps4.idol.BasicSchoolIdolContext() as context:
            result = await npps4.scriptutils.settlement.apply(context, document)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    import npps4.scriptutils.boot

    npps4.scriptutils.boot.start(run_script)
