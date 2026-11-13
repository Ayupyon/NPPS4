import copy
import functools
import hashlib
import json
import pathlib
import re

import fastapi

from . import dltype
from .. import idoltype
from ..config import config

from typing import Any

type Manifest = dict[str, Any]
PROTOCOL_VERSION = 1
REVISION = re.compile(r"^[0-9a-f]{32}$")
PERIOD = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
ASSET = re.compile(r"^(?:en/)?assets/image/event/published/([0-9a-f]{32})/[a-z][a-z0-9_]*(?:\.png\.imag|\.texb)$")
REQUIRED = ("banner", "background", "navi", "info", "token")
MAX_RELEASE_BYTES = 64 * 1024 * 1024


def json_file(path: pathlib.Path, limit: int = 2 * 1024 * 1024) -> Manifest:
    if path.stat().st_size > limit:
        raise ValueError("Event presentation JSON too large")
    value = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(value, dict):
        raise TypeError("Expected event presentation JSON object")
    return value


def inside(root: pathlib.Path, relative: str) -> pathlib.Path:
    root = root.resolve()
    value = (root / relative).resolve()
    if not value.is_relative_to(root):
        raise ValueError("Event presentation path escapes root")
    return value


def validate_text(value: object, limit: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or any(ord(character) < 32 or character in "{}" for character in value)
    ):
        raise ValueError("Invalid event display text")


def validate_manifest(manifest: Manifest) -> None:
    if manifest.get("schema_version") != 1 or not REVISION.fullmatch(manifest.get("revision", "")):
        raise ValueError("Invalid release identity")
    validate_text(manifest.get("title"), 60)
    validate_text(manifest.get("token_name"), 32)
    files = manifest.get("files")
    assets = manifest.get("assets")
    if not isinstance(files, dict) or not 20 <= len(files) <= 80 or not isinstance(assets, dict):
        raise ValueError("Invalid release assets")
    total = 0
    for path, item in files.items():
        match = ASSET.fullmatch(path)
        if not match or match[1] != manifest["revision"] or not isinstance(item, dict):
            raise ValueError("Invalid release file namespace")
        if type(item.get("size")) is not int or not 0 < item["size"] <= 16 * 1024 * 1024:
            raise ValueError("Invalid release file size")
        if not re.fullmatch("[0-9a-f]{64}", item.get("sha256", "")) or not re.fullmatch(
            "[0-9a-f]{32}", item.get("md5", "")
        ):
            raise ValueError("Invalid release checksum")
        total += item["size"]
    if total > MAX_RELEASE_BYTES:
        raise ValueError("Release exceeds download limit")
    if set(assets) != set(REQUIRED):
        raise ValueError("Need banner/background/navi/info/token")
    tutorials = manifest.get("tutorials")
    if not isinstance(tutorials, list) or not 1 <= len(tutorials) <= 10:
        raise ValueError("Need 1-10 tutorial images")
    images = list(assets.values()) + tutorials
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("path"), str):
            raise TypeError("Invalid image descriptor")
        path = image["path"]
        if not path.startswith("assets/image/event/published/" + manifest["revision"] + "/") or not path.endswith(
            ".png"
        ):
            raise ValueError("Invalid display image path")
        if any(type(image.get(k)) is not int or not 1 <= image[k] <= 2048 for k in ("width", "height")):
            raise ValueError("Invalid image dimensions")
        name = path.rsplit("/", 1)[1][:-4]
        folder = path.rsplit("/", 1)[0]
        for prefix in ("", "en/"):
            if prefix + path + ".imag" not in files or prefix + folder + "/tx_" + name + ".texb" not in files:
                raise ValueError("Incomplete image/texture or locale pair")
    if manifest.get("total_bytes") != total:
        raise ValueError("Release byte total mismatch")


class Store:
    def __init__(self, root: pathlib.Path):
        self.catalog = root / "resources/events/event_presentation.json"
        self.static = root / "static"
        self.last_catalog: Manifest | None = None
        self.file_cache: set[tuple[pathlib.Path, int, int, int, str, str]] = set()

    def read_catalog(self) -> Manifest:
        try:
            catalog = json_file(self.catalog)
            if (
                catalog.get("schema_version") != 1
                or not isinstance(catalog.get("bindings"), dict)
                or not isinstance(catalog.get("releases"), dict)
            ):
                raise ValueError("Invalid event presentation catalog")
            if len(catalog["bindings"]) > 4096 or len(catalog["releases"]) > 4096:
                raise ValueError("Catalog too large")
            for revision, sha in catalog["releases"].items():
                if not REVISION.fullmatch(revision) or not re.fullmatch("[0-9a-f]{64}", sha):
                    raise ValueError("Invalid catalog release")
            for key, revision in catalog["bindings"].items():
                if not PERIOD.fullmatch(key) or revision not in catalog["releases"]:
                    raise ValueError("Invalid catalog binding")
            self.last_catalog = catalog
            return catalog
        except (OSError, ValueError, TypeError, KeyError):
            if self.last_catalog is not None:
                return self.last_catalog
            raise ValueError("Event presentation catalog rejected") from None

    def release(self, revision: str, catalog: Manifest) -> Manifest:
        if revision not in catalog["releases"]:
            raise ValueError("Unpublished release")
        base = inside(self.static, f"event-presentations/releases/{revision}")
        path = inside(base, "manifest.json")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Manifest too large")
        # Git may change manifest line endings on checkout
        content = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(content).hexdigest() != catalog["releases"][revision]:
            raise ValueError("Manifest checksum mismatch")
        manifest = json.loads(content)
        if not isinstance(manifest, dict):
            raise TypeError("Invalid release manifest")
        validate_manifest(manifest)
        if manifest["revision"] != revision:
            raise ValueError("Manifest revision mismatch")
        for name, info in manifest["files"].items():
            self.verify_file(base, name, info)
        return manifest

    def verify_file(self, base: pathlib.Path, name: str, info: Manifest) -> None:
        path = inside(base, f"files/{name}")
        stat = path.stat()
        key = (path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, info["sha256"], info["md5"])
        if stat.st_size != info["size"]:
            raise ValueError("Published resource size mismatch")
        if key not in self.file_cache:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != info["sha256"] or hashlib.md5(raw).hexdigest() != info["md5"]:
                raise ValueError("Published resource checksum mismatch")
            if len(self.file_cache) > 16384:
                self.file_cache.clear()
            self.file_cache.add(key)

    def for_period(self, period_key: str) -> Manifest | None:
        catalog = self.read_catalog()
        revision = catalog["bindings"].get(period_key)
        if revision is None:
            return None
        manifest = self.release(revision, catalog)
        return {
            key: copy.deepcopy(manifest[key])
            for key in ("schema_version", "revision", "title", "token_name", "assets", "tutorials", "total_bytes")
        } | {"download_files": sorted(manifest["files"])}

    def raw_file(self, request: fastapi.Request, platform: idoltype.PlatformType, name: str) -> dltype.BaseInfo | None:
        match = ASSET.fullmatch(name)
        if not match:
            return None
        revision = match[1]
        manifest = self.release(revision, self.read_catalog())
        info = manifest["files"].get(name)
        if info is None:
            return None
        return dltype.BaseInfo(
            url=asset_url(request, platform, f"event-presentations/releases/{revision}/files/{name}"),
            size=info["size"],
            checksums=dltype.Checksum(md5=info["md5"], sha256=info["sha256"]),
        )


@functools.lru_cache(maxsize=1)
def get_store(root: pathlib.Path) -> Store:
    return Store(root)


def protocol_version() -> int | None:
    return PROTOCOL_VERSION if config.CONFIG_DATA.gameplay.event_client_resources else None


def asset_url(request: fastapi.Request, platform: idoltype.PlatformType, path: str) -> str:
    url = str(request.url_for("static_file", path=path))
    # Android's native downloader requires HTTP for these public assets
    if platform == idoltype.PlatformType.Android and url.startswith("https://"):
        return "http" + url[5:]
    return url
