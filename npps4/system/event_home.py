import dataclasses
import hashlib
import pathlib
import time

import pydantic

from .. import idol
from ..config import config
from ..download import event_presentation

from typing import Annotated

OFFER_LIFETIME = 1800
READY_LIFETIME = 21600
SESSION_LIMIT = 4096


class HomePackage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    period_key: str
    revision: Annotated[str, pydantic.Field(pattern=r"^[0-9a-f]{32}$")]
    asset_path: str
    zip_path: str
    zip_size: Annotated[int, pydantic.Field(strict=True, gt=0)]
    zip_sha256: Annotated[str, pydantic.Field(pattern=r"^[0-9a-f]{64}$")]
    package_id: Annotated[int, pydantic.Field(strict=True, ge=900000000, lt=1000000000)]
    extra_chart_revision: str | None = None


@dataclasses.dataclass(frozen=True)
class SessionPackage:
    package: HomePackage
    recorded_at: float


_verified: set[tuple[pathlib.Path, int, int, int, str]] = set()
_pending: dict[bytes, SessionPackage] = {}
_acknowledged: dict[bytes, SessionPackage] = {}


def binding(period_key: str | None) -> HomePackage | None:
    if not config.CONFIG_DATA.gameplay.event_client_resources or period_key is None:
        return None
    root = pathlib.Path(config.ROOT_DIR)
    try:
        catalog = event_presentation.json_file(root / "resources/events/event_home.json")
        if catalog.get("schema_version") != 1 or catalog.get("enabled") is not True:
            return None
        data = catalog["bindings"].get(period_key)
        if data is None:
            return None
        package = HomePackage.model_validate(data)
        if (
            package.period_key != period_key
            or package.asset_path != f"assets/image/event/published/{package.revision}/home_banner.png"
            or package.zip_path != f"event-home/releases/{package.revision}/package.zip"
        ):
            return None
        path = event_presentation.inside(root / "static", package.zip_path)
        stat = path.stat()
        cache_key = (path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, package.zip_sha256)
        if stat.st_size != package.zip_size:
            return None
        if cache_key not in _verified:
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != package.zip_sha256:
                    return None
            if len(_verified) >= 100:
                _verified.clear()
            _verified.add(cache_key)
        return package
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _session_key(context: idol.BasicSchoolIdolContext) -> bytes | None:
    if isinstance(context, idol.SchoolIdolParams) and context.token_text:
        return hashlib.sha256(context.token_text.encode()).digest()
    return None


def _record(states: dict[bytes, SessionPackage], key: bytes, package: HomePackage) -> None:
    states.pop(key, None)
    if len(states) >= SESSION_LIMIT:
        del states[next(iter(states))]
    states[key] = SessionPackage(package, time.monotonic())


def _matches(state: SessionPackage, package: HomePackage, lifetime: int) -> bool:
    return state.package == package and time.monotonic() - state.recorded_at < lifetime


def offer_package(
    context: idol.SchoolIdolAuthParams, period_key: str | None, excluded_package_ids: list[int]
) -> HomePackage | None:
    key = _session_key(context)
    package = binding(period_key)
    if package is None:
        if key is not None:
            _pending.pop(key, None)
        return None
    if package.package_id in excluded_package_ids:
        if key is not None:
            _record(_acknowledged, key, package)
            _pending.pop(key, None)
        return None
    if key is not None:
        _acknowledged.pop(key, None)
        _record(_pending, key, package)
    return package


def pending_offer(context: idol.SchoolIdolUserParams) -> SessionPackage | None:
    key = _session_key(context)
    return _pending.get(key) if key is not None else None


def acknowledge_offer(context: idol.SchoolIdolUserParams, period_key: str | None, offer: SessionPackage) -> None:
    key = _session_key(context)
    if key is None or _pending.get(key) is not offer:
        return
    package = binding(period_key)
    if package is not None and _matches(offer, package, OFFER_LIFETIME):
        _record(_acknowledged, key, package)
    _pending.pop(key, None)


def ready_package(context: idol.BasicSchoolIdolContext, period_key: str | None) -> HomePackage | None:
    key = _session_key(context)
    acknowledged = _acknowledged.get(key) if key is not None else None
    if acknowledged is None:
        return None
    package = binding(period_key)
    return package if package is not None and _matches(acknowledged, package, READY_LIFETIME) else None


def extra_ready(context: idol.BasicSchoolIdolContext, period_key: str, revision: str) -> bool:
    package = ready_package(context, period_key)
    return package is not None and package.extra_chart_revision == revision
