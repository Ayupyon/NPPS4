import collections.abc
import pathlib

import pydantic

from . import live_model
from .. import idol
from ..config import cfgtype
from ..config import config

_NOTES = pydantic.TypeAdapter(list[live_model.LiveNote])


async def get_beatmap_data(livejson: str, context: idol.BasicSchoolIdolContext) -> list[live_model.LiveNote] | None:
    try:
        root = (pathlib.Path(config.ROOT_DIR) / "resources" / "beatmaps").resolve()
        path = (root / livejson).resolve()
        if not path.is_relative_to(root):
            return None
        return _NOTES.validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None


async def randomize_beatmaps(
    beatmap: collections.abc.Iterable[cfgtype.BeatmapData], seed: bytes, context: idol.BasicSchoolIdolContext
) -> collections.abc.Iterable[cfgtype.BeatmapData]:
    return beatmap
