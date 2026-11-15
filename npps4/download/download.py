import pathlib

import fastapi

from . import dltype
from . import event_presentation
from . import master_overlay
from . import none as none_backend
from .. import idoltype
from ..config import config, cfgtype

from typing import Callable


def n4dlapi_factory():
    from . import n4dlapi as n4dlapi_backend

    return n4dlapi_backend


def internal_factory():
    from . import internal as internal_backend

    return internal_backend


BACKEND_FACTORY: dict[str, Callable[[], cfgtype.DownloadBackendProtocol]] = {
    "none": lambda: none_backend,
    "n4dlapi": n4dlapi_factory,
    "internal": internal_factory,
    "custom": config.get_custom_download_protocol,
}
assert config.CONFIG_DATA.download.backend is not None
_used_backend = BACKEND_FACTORY.get(config.CONFIG_DATA.download.backend)
if _used_backend is None:
    raise Exception(f"Missing or unknown backend '{config.CONFIG_DATA.download.backend}'")

CURRENT_BACKEND = _used_backend()


def get_server_version():
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    return CURRENT_BACKEND.get_server_version()


def get_db_path(name: str):
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    source = CURRENT_BACKEND.get_db_path(name)
    if name == "live" and get_server_version() == (59, 4):
        return master_overlay.prepare_live_database(
            source,
            pathlib.Path(config.ROOT_DIR) / "resources/master-overlays/live-59.4.json",
            pathlib.Path(config.get_data_directory()) / "master-overlays",
        )
    return source


async def get_update_files(
    request: fastapi.Request, platform: idoltype.PlatformType, from_client_version: tuple[int, int]
):
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    return await CURRENT_BACKEND.get_update_files(request, platform, from_client_version)


async def get_batch_files(
    request: fastapi.Request, platform: idoltype.PlatformType, package_type: int, exclude: list[int]
):
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    return await CURRENT_BACKEND.get_batch_files(request, platform, package_type, exclude)


async def get_single_package(
    request: fastapi.Request, platform: idoltype.PlatformType, package_type: int, package_id: int
):
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    return await CURRENT_BACKEND.get_single_package(request, platform, package_type, package_id)


async def get_raw_files(request: fastapi.Request, platform: idoltype.PlatformType, files: list[str]):
    global CURRENT_BACKEND
    assert CURRENT_BACKEND is not None
    if event_presentation.protocol_version() is None:
        return await CURRENT_BACKEND.get_raw_files(request, platform, files)

    published: dict[int, dltype.BaseInfo] = {}
    delegated: list[str] = []
    store = event_presentation.get_store(pathlib.Path(config.ROOT_DIR))
    for index, filename in enumerate(files):
        name = filename.removeprefix("external/")
        if not name.startswith(("assets/image/event/published/", "en/assets/image/event/published/")):
            delegated.append(filename)
            continue
        try:
            asset = store.raw_file(request, platform, name)
        except (OSError, ValueError, TypeError, KeyError):
            asset = None
        published[index] = asset or dltype.BaseInfo(url="", size=0, checksums=dltype.Checksum(md5="", sha256=""))

    backend = await CURRENT_BACKEND.get_raw_files(request, platform, delegated) if delegated else []
    if len(backend) != len(delegated):
        raise ValueError("Native resource response length mismatch")
    delegated_assets = iter(backend)
    return [published[index] if index in published else next(delegated_assets) for index in range(len(files))]


CURRENT_BACKEND.initialize()
