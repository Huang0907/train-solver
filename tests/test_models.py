"""多模型端点与「更换模型」（离线）。"""

import unittest

from trainsolve.config import Config, ModelEndpoint, redacted
from trainsolve.solver.failover import FailoverSolver
from trainsolve.solver.factory import build_solver


def make_config() -> Config:
    config = Config()
    config.deepseek.api_key = "wbk-primary"
    config.deepseek.base_url = "http://127.0.0.1:7864/v1"
    config.deepseek.model = "cn:deepseek-v4.1-flash"
    config.deepseek.fallbacks = [
        ModelEndpoint(api_key="sk-official", base_url="https://api.deepseek.com",
                      model="deepseek-flash", label="官方flash"),
    ]
    return config


class DefaultModelTest(unittest.TestCase):
    """默认就是 DeepSeek 官方 flash：配置默认值与界面下拉框的第一项必须是它。"""

    def test_config_defaults_to_official_flash(self):
        config = Config()
        self.assertEqual(config.deepseek.model, "deepseek-flash")
        self.assertEqual(config.deepseek.base_url.rstrip("/"), "https://api.deepseek.com")

    def test_model_picker_lists_official_first(self):
        from trainsolve.gui import MODELS

        self.assertEqual(MODELS[0], "deepseek-flash")
        self.assertIn("deepseek-v4-pro", MODELS)
        self.assertFalse([m for m in MODELS if m.startswith("cn:")],
                         "旧网关的 cn: id 不该再出现在默认列表里")

    def test_shipped_configs_default_to_official_flash(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("config.public.toml", "config.example.toml"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("https://api.deepseek.com", text, name)
            self.assertIn('model = "deepseek-flash"', text, name)


class EndpointsTest(unittest.TestCase):
    def test_default_order(self):
        config = make_config()
        endpoints = config.deepseek.endpoints()
        self.assertEqual([e.model for e in endpoints], ["cn:deepseek-v4.1-flash", "deepseek-flash"])
        self.assertEqual(config.deepseek.current().model, "cn:deepseek-v4.1-flash")

    def test_select_promotes_fallback(self):
        config = make_config()
        chosen = config.deepseek.select(1)
        self.assertEqual(chosen.model, "deepseek-flash")
        self.assertEqual(config.deepseek.preferred, 1)
        self.assertEqual(config.deepseek.current().model, "deepseek-flash")
        # 原来的主模型变成“备用”，仍留在列表里
        self.assertEqual([e.model for e in config.deepseek.endpoints()],
                         ["deepseek-flash", "cn:deepseek-v4.1-flash"])

    def test_select_out_of_range_falls_back_to_primary(self):
        config = make_config()
        config.deepseek.select(99)
        self.assertEqual(config.deepseek.preferred, 0)
        self.assertEqual(config.deepseek.current().model, "cn:deepseek-v4.1-flash")

    def test_incomplete_endpoint_is_ignored(self):
        config = make_config()
        config.deepseek.fallbacks.append(ModelEndpoint(api_key="", base_url="https://x", model="m"))
        self.assertEqual(len(config.deepseek.endpoints()), 2)

    def test_redacted_marks_current(self):
        config = make_config()
        config.deepseek.select(1)
        text = redacted(config)
        self.assertIn("← 当前", text)
        self.assertNotIn("sk-official", text)  # 打码

    def test_preferred_survives_roundtrip(self):
        import tempfile
        from pathlib import Path

        from trainsolve.config import load_config
        from trainsolve.gui import save_client_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config = make_config()
            config.deepseek.select(1)
            import trainsolve.gui as gui_mod

            original = gui_mod.settings_path
            try:
                gui_mod.settings_path = lambda: path  # type: ignore[assignment]
                save_client_config(config)
            finally:
                gui_mod.settings_path = original  # type: ignore[assignment]
            loaded = load_config(str(path))
            self.assertEqual(loaded.deepseek.preferred, 1)
            self.assertEqual(len(loaded.deepseek.all_endpoints()), 2)
            self.assertEqual(loaded.deepseek.current().model, "deepseek-flash")


class BuildSolverTest(unittest.TestCase):
    def test_two_endpoints_become_failover(self):
        solver = build_solver(make_config(), log=lambda *_: None)
        self.assertIsInstance(solver, FailoverSolver)
        self.assertEqual(len(solver.solvers), 2)

    def test_preferred_endpoint_is_first(self):
        config = make_config()
        config.deepseek.select(1)
        solver = build_solver(config, log=lambda *_: None)
        self.assertIn("api.deepseek.com", solver.solvers[0].label)
        self.assertIn("官方flash", solver.solvers[0].label)

    def test_single_endpoint_returns_plain_solver(self):
        config = make_config()
        config.deepseek.fallbacks = []
        solver = build_solver(config, log=lambda *_: None)
        self.assertNotIsInstance(solver, FailoverSolver)


if __name__ == "__main__":
    unittest.main()
