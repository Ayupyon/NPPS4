import hashlib
import importlib
import io
import json
import pathlib
import tempfile
import types
import unittest
import unittest.mock
import zipfile

import fastapi
import fastapi.staticfiles
import httpx
import sqlalchemy.ext.asyncio

import npps4.app.app as app
import npps4.config.config as config
import npps4.db.main as main
import npps4.download.dltype as dltype
import npps4.download.download as download
import npps4.download.event_presentation as presentation
import npps4.game.lbonus as game_lbonus
import npps4.idol.core as core
import npps4.idol.session as session
import npps4.release_key as release_key
import npps4.system.event_charts as event_charts
import npps4.system.event_home as event_home
import npps4.system.live as live
import npps4.system.marathon as marathon
import npps4.system.reward_egg as reward_egg
import npps4.util as util

from typing import Any

ROOT = pathlib.Path(config.ROOT_DIR)


class EventDownloadsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        engine = sqlalchemy.ext.asyncio.create_async_engine(f"sqlite+aiosqlite:///{directory / 'main.sqlite3'}")
        self.addAsyncCleanup(engine.dispose)
        sessions = sqlalchemy.ext.asyncio.async_sessionmaker(engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", sessions))
        async with engine.begin() as connection:
            await connection.run_sync(main.User.metadata.create_all)
        async with sessions.begin() as database:
            player = main.User(name="Player")
            database.add(player)
            await database.flush()
            player_id = player.id

        self.policy = json.loads((ROOT / "resources/events/marathon_rules.json").read_bytes())
        self.period = next(rule for rule in self.policy["periods"] if rule["key"] == self.policy["selected_period_key"])
        self.timestamp = marathon.timestamp(self.period["start"]) + 1
        self.monotonic = 0.0
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "marathon_policy", "rules.json"))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "event_client_resources", True))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "random_live_rotation_anchor", None))
        self.enterContext(unittest.mock.patch.object(marathon, "get_policy", return_value=self.policy))
        self.enterContext(unittest.mock.patch.object(util, "time", return_value=self.timestamp))
        self.enterContext(
            unittest.mock.patch.object(event_home, "time", types.SimpleNamespace(monotonic=lambda: self.monotonic))
        )
        self.enterContext(unittest.mock.patch.object(event_home, "_pending", {}))
        self.enterContext(unittest.mock.patch.object(event_home, "_acknowledged", {}))
        self.enterContext(unittest.mock.patch.object(reward_egg, "get_policy", return_value=object()))
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(unittest.mock.patch.object(session, "try_cleanup_tokens"))
        self.enterContext(unittest.mock.patch.object(live, "get_special_live_rotation_difficulty_id", return_value={}))
        self.login_bonus = self.enterContext(
            unittest.mock.patch.object(game_lbonus, "execute_login_bonus", return_value=reward_egg.WireResponse({}))
        )

        async def finalize(context: session.SchoolIdolAuthParams) -> None:
            context.token = session.TokenData(user_id=player_id, client_key=b"", server_key=b"")

        self.enterContext(unittest.mock.patch.object(session.SchoolIdolAuthParams, "finalize", finalize))
        application = fastapi.FastAPI()
        application.mount("/static", fastapi.staticfiles.StaticFiles(directory=ROOT / "static"), "static_file")
        application.include_router(app.main)
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="https://test/main.php/",
                headers={"Client-Version": "59.4", "LANG": "jp", "Platform-Type": "2", "X-Message-Code": "test"},
            )
        )
        package = event_home.binding(self.period["key"])
        assert package is not None
        self.package = package
        revisions, _ = event_charts.revisions(self.policy["periods"])
        assert package.extra_chart_revision is not None
        self.extra_id = revisions[package.extra_chart_revision]["songs"][0]["id"]

    async def post(
        self, route: str, body: dict[str, Any] | None = None, *, token: str = "test-session", status: int = 200
    ) -> Any:
        response = await self.client.post(
            route,
            data={"request_data": json.dumps(body or {})},
            headers={"Authorize": f"consumerKey=lovelive_test&token={token}"},
        )
        self.assertEqual(response.status_code, status, response.text)
        return response.json()["response_data"]

    async def event_zip(self, excluded: list[int] | None = None, *, package_type: int = 5) -> list[dict[str, str]]:
        return await self.post(
            "download/event",
            {
                "client_version": "59.4",
                "os": "Android",
                "package_type": package_type,
                "excluded_package_ids": excluded or [],
            },
        )

    async def assert_ready(self, ready: bool, *, token: str = "test-session") -> None:
        banners = (await self.post("banner/bannerList", token=token))["banner_list"]
        self.assertEqual(any(entry["banner_type"] == 0 for entry in banners), ready)
        schedule = await self.post("live/schedule", token=token)
        self.assertEqual(self.extra_id in {entry["live_difficulty_id"] for entry in schedule["live_list"]}, ready)

    async def test_home_zip_acknowledgment_and_login_receipt_replay(self):
        await self.assert_ready(False)
        self.assertEqual(await self.event_zip(package_type=1), [])
        [offered] = await self.event_zip()
        self.assertEqual(offered["url"], f"http://test/static/{self.package.zip_path}")
        downloaded = await self.client.get(offered["url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.content), int(offered["size"]))
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), self.package.zip_sha256)
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            self.assertIn(self.package.asset_path + ".imag", archive.namelist())
            self.assertIn(f"config/pkg_005_{self.package.package_id}_info.json", archive.namelist())

        await self.assert_ready(False)
        login = {"timeStamp": self.timestamp, "commandNum": "login.1"}
        self.assertEqual(await self.post("lbonus/execute", login), {})
        await self.assert_ready(True)
        banner = (await self.post("banner/bannerList"))["banner_list"][0]
        self.assertEqual(
            (banner["target_id"], banner["asset_path"]), (self.period["event_id"], self.package.asset_path)
        )
        await self.assert_ready(False, token="new-session")

        self.assertEqual(len(await self.event_zip([self.package.package_id + 1])), 1)
        self.assertEqual(await self.post("lbonus/execute", login), {})
        self.login_bonus.assert_awaited_once()
        await self.assert_ready(False)
        self.assertEqual(await self.event_zip([self.package.package_id]), [])
        await self.assert_ready(True)

    async def test_pending_expiry_rollback_and_revision_checks(self):
        await self.event_zip()
        self.monotonic += event_home.OFFER_LIFETIME
        await self.post("lbonus/execute", {"timeStamp": self.timestamp, "commandNum": "login.expired"})
        await self.assert_ready(False)
        await self.event_zip()

        with (
            unittest.mock.patch.object(reward_egg, "remember", side_effect=RuntimeError("receipt failed")),
            self.assertRaisesRegex(RuntimeError, "receipt failed"),
        ):
            await self.post("lbonus/execute", {"timeStamp": self.timestamp, "commandNum": "login.failed"})
        await self.assert_ready(False)

        await self.event_zip([self.package.package_id])
        await self.assert_ready(True)
        for fields in (
            {"revision": "a" * 32},
            {"period_key": "different-period"},
            {"extra_chart_revision": "b" * 32},
        ):
            with unittest.mock.patch.object(event_home, "binding", return_value=self.package.model_copy(update=fields)):
                await self.assert_ready(False)
        self.monotonic += event_home.READY_LIFETIME
        await self.assert_ready(False)

    async def test_presentation_and_configured_scout_provider_preserve_asset_order(self):
        schedule = await self.post("live/schedule")
        self.assertEqual(schedule["event_presentation_protocol"], 1)
        event = schedule["event_list"][0]
        self.assertEqual(event["event_presentation_protocol"], 1)
        descriptor = await self.post("event/presentation", {"event_id": self.period["event_id"]})
        self.assertEqual(descriptor["event"], event)
        self.assertFalse(descriptor["resource_error"])
        release = descriptor["presentation"]
        [art, *other_art] = release["download_files"]
        self.assertTrue(other_art)
        manifest = json.loads((ROOT / "static/retro-scouting/manifest.json").read_bytes())
        scout = next(iter(manifest))
        upstream = dltype.BaseInfo(url="https://upstream/game.png", size=9, checksums=dltype.Checksum())
        upstream_config = config.CONFIG_DATA.download.n4dlapi
        with unittest.mock.patch.object(
            upstream_config, "server", upstream_config.server or "https://upstream.invalid/"
        ):
            n4dlapi = importlib.import_module("npps4.download.n4dlapi")
        with (
            unittest.mock.patch.object(
                config, "CUSTOM_DOWNLOAD_FILE", str(ROOT / "external/retro_scouting_download.py")
            ),
            unittest.mock.patch.object(config, "_custom_download_backend_module", None),
            unittest.mock.patch.object(n4dlapi, "initialize"),
            unittest.mock.patch.object(n4dlapi, "get_raw_files", return_value=[upstream]) as delegated,
        ):
            provider = config.get_custom_download_protocol()
            provider.initialize()
            with unittest.mock.patch.object(download, "CURRENT_BACKEND", provider):
                paths = [
                    "external/" + art,
                    scout,
                    "game.png",
                    f"assets/image/event/published/{release['revision']}/missing.png.imag",
                ]
                result = await self.post("download/getUrl", {"os": "Android", "path_list": paths})
                ios = await self.post("download/getUrl", {"os": "iOS", "path_list": paths})
        self.assertEqual([call.args[2] for call in delegated.await_args_list], [["game.png"], ["game.png"]])
        published_url, scout_url, upstream_url, missing = result["url_list"]
        self.assertEqual(
            published_url, f"http://test/static/event-presentations/releases/{release['revision']}/files/{art}"
        )
        self.assertEqual(scout_url, f"http://test/static/{manifest[scout]['path']}")
        self.assertEqual((upstream_url, missing), (upstream.url, ""))
        self.assertEqual(ios["url_list"], ["https" + published_url[4:], "https" + scout_url[4:], upstream_url, ""])
        for url, expected in (
            (published_url, ROOT / "static" / published_url.split("/static/", 1)[1]),
            (scout_url, ROOT / "static" / manifest[scout]["path"]),
        ):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, expected.read_bytes())

    async def test_resource_opt_out_and_presentation_errors(self):
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "event_client_resources", False):
            self.assertEqual(await self.event_zip(), [])
            banner = await self.post("banner/bannerList")
            self.assertEqual(banner["time_limit"], util.timestamp_to_datetime(2147483647))
            self.assertEqual([entry["banner_type"] for entry in banner["banner_list"]], [18, 2])
            self.assertNotIn("event_presentation_protocol", await self.post("live/schedule"))
            await self.post("event/presentation", status=404)
            ordinary = dltype.BaseInfo(url="https://upstream/ordinary.png", size=1, checksums=dltype.Checksum())
            with unittest.mock.patch.object(download.CURRENT_BACKEND, "get_raw_files", return_value=[ordinary]):
                result = await self.post("download/getUrl", {"os": "iOS", "path_list": ["ordinary.png"]})
            self.assertEqual(result["url_list"], [ordinary.url])

        for invalid in ({"event_id": True}, {"protocol_version": "1"}):
            await self.post("event/presentation", invalid, status=422)
        self.assertEqual(
            await self.post("event/presentation", {"event_id": 2147483647}),
            {"protocol_version": 1, "available": False, "event": None, "presentation": None},
        )
        with unittest.mock.patch.object(presentation.Store, "for_period", side_effect=ValueError("Bad manifest")):
            rejected = await self.post("event/presentation")
        self.assertTrue(rejected["resource_error"])
        self.assertIsNone(rejected["presentation"])
