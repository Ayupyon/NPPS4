import pathlib
import tempfile
import types
import unittest
import unittest.mock

import npps4.config.config as config
import npps4.data as data
import npps4.system.badwords as badwords
import npps4.system.beatmap as beatmap
import npps4.system.effort as effort
import npps4.system.lbonus as lbonus
import npps4.system.live_unit_drop as live_unit_drop


class BuiltinGameProvidersTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_custom_provider_selection(self):
        self.enterContext(unittest.mock.patch.object(config, "_badwords_check_module", None))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.game, "badwords", ""))
        with unittest.mock.patch.object(data, "get", return_value=types.SimpleNamespace(badwords=["badword"])):
            self.assertTrue(await config.contains_badwords("Bad Word", None))
            self.assertFalse(await config.contains_badwords("Good Word", None))
        self.assertIs(config._badwords_check_module, badwords)

        providers = [
            (
                "beatmaps",
                "_beatmap_provider_module",
                config.get_beatmap_provider_protocol,
                beatmap,
            ),
            (
                "login_bonus",
                "_login_bonus_module",
                config.get_login_bonus_protocol,
                lbonus,
            ),
            (
                "live_unit_drop",
                "_live_unit_drop_module",
                config.get_live_unit_drop_protocol,
                live_unit_drop,
            ),
            (
                "live_box_drop",
                "_live_box_drop_module",
                config.get_live_box_drop_protocol,
                effort,
            ),
        ]
        for field, cache, getter, module in providers:
            with self.subTest(provider=field):
                with unittest.mock.patch.object(config, cache, None), unittest.mock.patch.object(
                    config.CONFIG_DATA.game, field, ""
                ):
                    self.assertIs(getter(), module)

        directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = directory / "custom.py"
        script.write_text("async def get_rewards(*args): return (3001, 4, 5, None)\n", encoding="utf-8")
        with unittest.mock.patch.object(config, "_login_bonus_module", None), unittest.mock.patch.object(
            config.CONFIG_DATA.game, "login_bonus", str(script)
        ):
            self.assertEqual(await config.get_login_bonus_protocol().get_rewards(1, 1, 1970, None), (3001, 4, 5, None))

    async def test_beatmap_validation_and_login_bonus_rotation(self):
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        chart_directory = root / "resources" / "beatmaps"
        chart_directory.mkdir(parents=True)
        chart = '[{"timing_sec":1,"notes_attribute":1,"notes_level":1,"effect":1,"effect_value":0,"position":1}]'
        (chart_directory / "chart.json").write_text(chart, encoding="utf-8")
        (chart_directory / "invalid.json").write_text("{}", encoding="utf-8")
        (root / "outside.json").write_text(chart, encoding="utf-8")
        with unittest.mock.patch.object(config, "ROOT_DIR", str(root)):
            notes = await beatmap.get_beatmap_data("chart.json", None)
            assert notes is not None
            self.assertEqual((notes[0].speed, notes[0].vanish), (1.0, 0))
            self.assertIsNone(await beatmap.get_beatmap_data("invalid.json", None))
            self.assertIsNone(await beatmap.get_beatmap_data("../../outside.json", None))

        rewards = [await lbonus.get_rewards(day, 1, 1970, None) for day in range(1, 5)]
        self.assertEqual(
            rewards, [(3000, 3, 20000, None), (3002, 2, 2500, None), (3001, 4, 1, None), (3000, 3, 20000, None)]
        )

    async def test_unit_drop_with_missing_song_pool(self):
        server_data = types.SimpleNamespace(
            live_unit_drop_chance=types.SimpleNamespace(common=1, live_specific=5),
            common_live_unit_drops=[types.SimpleNamespace(unit_id=12, weight=1)],
            live_specific_live_unit_drops={},
        )
        with unittest.mock.patch.object(data, "get", return_value=server_data):
            self.assertEqual(await live_unit_drop.get_live_drop_unit(9, None), 12)
            server_data.live_unit_drop_chance.common = 0
            with self.assertRaisesRegex(ValueError, "No eligible unit drops"):
                await live_unit_drop.get_live_drop_unit(9, None)

    async def test_effort_drop_respects_current_reward_table(self):
        specs = [
            types.SimpleNamespace(live_effort_point_box_spec_id=2, capacity=400, num_rewards=1),
            types.SimpleNamespace(live_effort_point_box_spec_id=1, capacity=100, num_rewards=2),
        ]
        result = types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: specs))
        context = types.SimpleNamespace(
            db=types.SimpleNamespace(effort=types.SimpleNamespace(execute=unittest.mock.AsyncMock(return_value=result)))
        )
        extra_data = {"level": 2}
        reward = types.SimpleNamespace(add_type=1001, item_id=3, amount=1, extra_data=extra_data, weight=2)
        server_data = types.SimpleNamespace(live_effort_drops={1: [reward]})
        with unittest.mock.patch.object(data, "get", return_value=server_data):
            drop = await effort.process_effort_box(context, 1, 0, 100)
            self.assertEqual(drop.new_live_effort_point_box_spec_id, 2)
            self.assertEqual(drop.rewards, [(1001, 3, 1, extra_data)] * 2)
            server_data.live_effort_drops.clear()
            drop = await effort.process_effort_box(context, 1, 0, 100)
            self.assertEqual(drop.rewards, [])
