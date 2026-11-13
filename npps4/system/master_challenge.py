import hashlib
import json
import sqlite3

import pydantic
import sqlalchemy
import sqlalchemy.exc

from . import live
from .. import idol
from .. import util
from ..config import config
from ..db import main
from ..idol import database


async def validate_entry(context: idol.BasicSchoolIdolContext, difficulty_id: int, is_training: bool):
    setting = await live.get_live_setting_from_difficulty_id(context, difficulty_id)
    is_challenge = (
        setting is not None
        and setting.difficulty == 6
        and not setting.ac_flag
        and difficulty_id
        in await live.get_training_live_difficulty_id_from_live_track_id(context, setting.live_track_id)
    )
    if is_training != is_challenge:
        raise idol.error.IdolError(detail="Invalid MASTER Challenge training flag", http_code=409)
    return is_challenge


async def get_energy(context: idol.BasicSchoolIdolContext, player: main.User) -> tuple[int, int]:
    limit = config.CONFIG_DATA.gameplay.master_challenge_daily_limit
    if limit is None:
        return player.training_energy, player.training_energy_max

    state = await context.db.main.get(main.DestinyMasterChallengeDay, player.id)
    used = state.used if state is not None and state.game_day >= util.get_days_since_unix() else 0
    return max(0, limit - used), limit


def require_energy(remaining: int) -> None:
    if remaining <= 0:
        raise idol.error.by_code(idol.error.ERROR_CODE_LIVE_NOT_ENOUGH_TRAINING_ENERGY_CANT_RECOVERY)


def request_identity(context: idol.SchoolIdolUserParams, request: pydantic.BaseModel) -> tuple[str, str]:
    try:
        body = json.loads(context.raw_request_data)
    except (ValueError, TypeError) as exc:
        raise idol.error.IdolError(detail="Invalid MASTER start identity", http_code=409) from exc

    if isinstance(body, dict) and "timeStamp" in body and "commandNum" in body:
        key = f"body:{body['timeStamp']}:{body['commandNum']}"
    elif context.nonce > 0:
        key = f"header:{context.timestamp}:{context.nonce}"
    else:
        raise idol.error.IdolError(detail="Missing MASTER start request identity", http_code=409)
    if len(key) > 255:
        raise idol.error.IdolError(detail="Invalid MASTER start identity", http_code=409)

    payload = {"body": body, "request": request.model_dump(mode="json"), "index": None}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return key, digest


async def lock_player(context: idol.SchoolIdolUserParams) -> None:
    assert context.token is not None
    try:
        await database.lock_user(context.db.main, context.token.user_id)
    except sqlalchemy.exc.OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        raise idol.error.IdolError(detail="MASTER Challenge start is busy", http_code=409) from exc


async def reserve_attempt(context: idol.SchoolIdolUserParams, player: main.User, game_day: int, limit: int) -> None:
    session = context.db.main
    state = await session.get(main.DestinyMasterChallengeDay, player.id, populate_existing=True, with_for_update=True)
    if state is None:
        state = main.DestinyMasterChallengeDay(user_id=player.id, game_day=game_day)
        session.add(state)
    elif state.game_day < game_day:
        state.game_day = game_day
        state.used = 0
    require_energy(limit - state.used)
    state.used += 1
