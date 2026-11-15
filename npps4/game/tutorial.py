import pydantic

from .. import idol
from ..idol import database
from ..idol import error
from ..system import tutorial
from ..system import user


class TutorialProgressRequest(pydantic.BaseModel):
    tutorial_state: int


async def _lock_current_user(context: idol.SchoolIdolUserParams):
    assert context.token is not None
    if not await database.lock_user(context.db.main, context.token.user_id):
        raise error.IdolError(detail="Unknown tutorial account")
    current = await user.get_current(context)
    # Authentication or another operation may have loaded an older ORM object.
    await context.db.main.refresh(current)
    if current.tutorial_state not in (-1, 0, 1, 2, 3):
        raise error.IdolError(detail=f"Invalid tutorial state: {current.tutorial_state}")
    return current


@idol.register("tutorial", "progress", batchable=False)
async def tutorial_progress(context: idol.SchoolIdolUserParams, request: TutorialProgressRequest) -> None:
    target = request.tutorial_state
    if target not in (-1, 1, 2, 3):
        raise error.IdolError(detail="Invalid tutorial target")
    current = await _lock_current_user(context)
    if current.tutorial_state == -1 or (target != -1 and target <= current.tutorial_state):
        return
    # A failed phase must not leave partial rewards if an internal caller catches
    # the error and subsequently commits its outer context.
    async with context.db.main.begin_nested():
        if current.tutorial_state == 0 and target == 1:
            await tutorial.phase1(context, current)
        elif current.tutorial_state == 1 and target == 2:
            await tutorial.phase2(context, current)
        elif current.tutorial_state == 2 and target == 3:
            await tutorial.phase3(context, current)
        elif current.tutorial_state == 3 and target == -1:
            await tutorial.finalize(context, current)
        else:
            raise error.IdolError(detail=f"Unknown state, u {current.tutorial_state} r {target}")


@idol.register("tutorial", "skip", batchable=False)
async def tutorial_skip(context: idol.SchoolIdolUserParams) -> None:
    current = await _lock_current_user(context)
    if current.tutorial_state == -1:
        return
    async with context.db.main.begin_nested():
        if current.tutorial_state == 0:
            await tutorial.phase1(context, current)
        if current.tutorial_state == 1:
            await tutorial.phase2(context, current)
        if current.tutorial_state == 2:
            await tutorial.phase3(context, current)
        await tutorial.finalize(context, current)
