import hashlib
import json
import pathlib
import re

from . import event_home
from .. import idol
from ..config import config

from typing import Any

type Data = dict[str, Any]
_cached_revisions: dict[pathlib.Path, tuple[int, int, int, Data]] = {}


def revisions(periods: list[Data]) -> tuple[dict[str, Data], dict[tuple[str, int], Data]]:
    available: dict[str, Data] = {}
    reserved: dict[tuple[str, int], Data] = {}
    base_ids = {song["id"] for period in periods for song in period["songs"]}
    root = pathlib.Path(config.ROOT_DIR) / "resources/events/event-extra-charts/revisions"
    for path in sorted(root.glob("*.json")):
        if not re.fullmatch("[0-9a-f]{32}", path.stem):
            continue
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        cached = _cached_revisions.get(path)
        if cached is None or cached[:3] != stamp:
            if stat.st_size > 32768:
                raise ValueError("Invalid extra chart revision")
            revision = json.loads(path.read_bytes())
            canonical = json.dumps(revision, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(canonical).hexdigest()[:32] != path.stem:
                raise ValueError("Invalid extra chart revision")
            if revision.get("schema_version") != 1 or revision.get("rule_origin") != "private-server-extension":
                raise ValueError("Invalid extra chart origin")
            if not re.fullmatch("[A-Za-z0-9_-]{1,160}", revision.get("period_key", "")):
                raise ValueError("Invalid extra chart period")
            if not isinstance(revision.get("songs"), list) or not 1 <= len(revision["songs"]) <= 10:
                raise ValueError("Invalid extra chart count")
            seen: set[int] = set()
            for song in revision["songs"]:
                if set(song) != {"id", "setting_id", "cost", "base_point"} or any(
                    type(value) is not int or not 1 <= value <= 99999 for value in song.values()
                ):
                    raise ValueError("Invalid extra chart rule")
                if not 90000 <= song["id"] <= 99999 or song["id"] in seen:
                    raise ValueError("Extra chart ID collision")
                seen.add(song["id"])
            _cached_revisions[path] = (*stamp, revision)
        else:
            revision = cached[3]

        for song in revision["songs"]:
            identity = (revision["period_key"], song["id"])
            previous = reserved.get(identity)
            if previous is not None and previous["setting_id"] != song["setting_id"]:
                raise ValueError("Extra chart mapping reused")
            if song["id"] in base_ids:
                raise ValueError("Extra chart overlaps base song")
            reserved[identity] = song
        available[path.stem] = revision
    return available, reserved


def reserved_songs(periods: list[Data]) -> dict[tuple[str, int], Data]:
    try:
        return revisions(periods)[1]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def effective_period(context: idol.BasicSchoolIdolContext, period: Data, periods: list[Data]) -> Data:
    effective = dict(period, sif005_extra_chart_revision=None)
    if not config.CONFIG_DATA.gameplay.event_client_resources:
        return effective
    try:
        bindings = json.loads(pathlib.Path(config.ROOT_DIR, "resources/events/event_extra_charts.json").read_bytes())
        if bindings["schema_version"] != 1:
            return effective
        key = bindings["active"].get(period["key"])
        available, _ = revisions(periods)
        revision = available.get(key)
        if (
            revision is not None
            and revision["period_key"] == period["key"]
            and revision["event_id"] == period["event_id"]
            and event_home.extra_ready(context, period["key"], key)
        ):
            effective["songs"] = [*period["songs"], *revision["songs"]]
            effective["sif005_extra_chart_revision"] = key
            effective["sif005_extra_chart_rule_origin"] = "private-server-extension"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return effective
