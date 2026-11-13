"""Serve manifest-listed custom scouting assets; delegate everything else to n4dlapi."""

import hashlib
import json
from pathlib import Path

from npps4.config import config
from npps4.download import dltype, n4dlapi

MANIFEST = Path(config.ROOT_DIR) / "static/retro-scouting/manifest.json"
_assets = {}


def initialize():
    global _assets
    _assets = json.loads(MANIFEST.read_text(encoding="utf-8"))
    static = (Path(config.ROOT_DIR) / "static").resolve()
    for item in _assets.values():
        target = (static / item["path"]).resolve()
        if not target.is_relative_to(static):
            raise ValueError("Invalid scouting asset path")
        content = target.read_bytes()
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Scouting resource checksum mismatch")
    n4dlapi.initialize()


get_server_version = n4dlapi.get_server_version
get_db_path = n4dlapi.get_db_path
get_update_files = n4dlapi.get_update_files
get_batch_files = n4dlapi.get_batch_files
get_single_package = n4dlapi.get_single_package


async def get_raw_files(request, platform, files):
    result = [None] * len(files)
    delegated = []
    indices = []
    for index, path in enumerate(files):
        key = path.removeprefix("external/").lstrip("/")
        item = _assets.get(key)
        if item is None:
            indices.append(index)
            delegated.append(path)
        else:
            url = str(request.url_for("static_file", path=item["path"]))
            # Match n4dlapi's Android resource-download compatibility handling.
            # This applies only to public static assets, never login/gameplay APIs.
            if int(platform) == 2 and url.startswith("https://"):
                url = "http" + url[5:]
            result[index] = dltype.BaseInfo(
                url=url, size=item["size"], checksums=dltype.Checksum(md5=item["md5"], sha256=item["sha256"])
            )
    if delegated:
        external = await n4dlapi.get_raw_files(request, platform, delegated)
        if len(external) != len(indices):
            raise ValueError("Upstream resource response length mismatch")
        for i, item in zip(indices, external):
            result[i] = item
    return result
