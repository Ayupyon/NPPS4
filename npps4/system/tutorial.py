from . import unit
from . import user
from .. import idol
from ..config import config
from ..db import main
from ..db import game_mater


def get_tutorial_gold():
    return game_mater.GAME_SETTING.initial_game_coin + 600


async def grant_starter_gifts(context: idol.BasicSchoolIdolContext, account: main.User):
    settings = config.CONFIG_DATA.gameplay
    if settings.starter_post_tutorial_gold is not None:
        initial_gold = settings.starter_post_tutorial_gold - get_tutorial_gold()
        if initial_gold < 0:
            raise ValueError("Starter gold must cover the normal tutorial reward")
        account.game_coin = initial_gold

    for unit_number in settings.starter_unit_numbers:
        unit_info = await unit.get_unit_info_from_unit_number(context, unit_number)
        if unit_info is None or unit_info.unit_number != unit_number or unit_info.disable_rank_up:
            raise ValueError(f"Invalid starter unit number {unit_number}")

        rarity = await unit.get_unit_rarity(context, unit_info.rarity)
        if rarity is None:
            raise ValueError(f"Missing rarity for starter unit number {unit_number}")

        starter = await unit.create_unit(context, account, unit_info.unit_id, True, level=rarity.after_level_max)
        if starter is None:
            raise ValueError(f"Cannot create starter unit number {unit_number}")
        starter.rank = unit_info.rank_max
        starter.display_rank = unit_info.rank_max
        starter.max_level = rarity.after_level_max
        await unit.add_unit_by_object(context, account, starter)


async def phase1(context: idol.BasicSchoolIdolContext, u: main.User):
    u.tutorial_state = 1


async def phase2(context: idol.BasicSchoolIdolContext, u: main.User):
    u.tutorial_state = 2


async def phase3(context: idol.BasicSchoolIdolContext, u: main.User):
    u.game_coin += get_tutorial_gold()
    # Friend Points +5
    u.social_point = u.social_point + game_mater.GAME_SETTING.live_social_point_for_others
    # Add EXP
    await user.add_exp(context, u, 11)
    # Reine Saeki
    await unit.add_unit_simple(context, u, 13, True)
    # Akemi Kikuchi
    await unit.add_unit_simple(context, u, 9, True)
    # Bond calculation
    await unit.add_love_by_deck(context, u, u.active_deck_index, 34)
    u.tutorial_state = 3


async def finalize(context: idol.BasicSchoolIdolContext, u: main.User):
    u.tutorial_state = -1
