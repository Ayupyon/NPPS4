Scripts
=====

Scripts are code that performs automated tasks to NPPS4 itself.

To run the scripts directly like ordinary Python scripts, run this once in virtual environment:
```
pip install -e .
```

Then you can run them like ordinary scripts (e.g. `python script.py ...`)

If you don't want to do `pip install -e .`, then to run script, you must run it like this:
```
python -m npps4.script script.py ...
```

Will run `script.py` in the working directory.

## Review a historical achievement claim

With the server stopped, export a review plan for an unresolved hold in the main SQLite database. Use the hold's exact period key.

```sh
python scripts/review_achievement_claim.py plan MAIN_DB USER_ID ACHIEVEMENT_ID BLOCKED_PERIOD > review.json
```

Set `approved` to `true` and fill in `approved_by` and `evidence`. Set `decision` to `confirm_paid` to record a proven payout or `allow_retry` to release the hold when evidence shows the reward is still due. Leave the snapshot and its checksum intact.

```sh
python scripts/review_achievement_claim.py apply MAIN_DB review.json
```
