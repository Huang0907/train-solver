"""配置查找路径：便携模式、模板回退、绝不误读别处的配置。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import trainsolve.gui as gui
from trainsolve.config import Config

PUBLIC_TEMPLATE = """
[platform]
base_url = "http://172.22.181.82/train/"
username = ""
password = ""

[deepseek]
api_key = ""
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
"""

FULL_CONFIG = """
[platform]
base_url = "http://172.22.181.82/train/"
username = "2000000001"
password = "secret"

[deepseek]
api_key = "sk-xxxxxxxx"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
"""


class SettingsPathTest(unittest.TestCase):
    """配置位置：便携优先；「程序目录的 config.toml 胜出」只对打包版成立。"""

    def test_frozen_app_dir_config_wins(self):
        """打包版（frozen=True）：程序目录里的 config.toml 胜出，source=program。

        旧断言是「同目录有 config.toml 就赢」——那对源码检出也成立，正是 0.5.2 事故的形态
        （从仓库根启动就读到仓库里那份 submit=true 的配置）。现在只对打包版成立。
        """
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            (app / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                resolved = gui.config_resolution(frozen=True)
            self.assertEqual(resolved.path, app / "config.toml")
            self.assertEqual(resolved.source, "program")

    def test_source_checkout_ignores_app_dir_config(self):
        """反向用例：非打包版（源码检出的程序目录就是仓库根）里放着 config.toml 也不许胜出。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            (app / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                resolved = gui.config_resolution(frozen=False)
            self.assertEqual(resolved.source, "appdata", "源码检出不许读程序目录/仓库里的配置")
            self.assertEqual(resolved.path, Path(appdata) / "TrainSolve" / "config.toml")

    def test_portable_marker_pins_to_exe_dir(self):
        """有 portable.txt 时：即使 %APPDATA% 里有别人的配置，也只听 exe 同目录。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            (Path(tmp) / "portable.txt").write_text("portable", encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[Path(tmp)]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                path = gui.settings_path()
            self.assertEqual(path, Path(tmp) / "config.toml")

    def test_portable_marker_beats_existing_local_config(self):
        """便携标记优先于「打包版程序目录配置」——这是新契约里的新信息。

        旧断言（只比 `settings_path()` 返回值、不隔离 `APPDATA`）与别的用例实质等价，
        而且失败轮次会摸到真实 `%APPDATA%`。现在：
        * 目录里同时有 `config.toml` 与 `portable.txt`，`frozen=True`（模拟打包版），
          断言 `source == "portable"` —— 证明便携标记压过程序目录那条候选；
        * `APPDATA` 指向临时目录，失败也不碰真实环境。
        """
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            (app / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")
            (app / "portable.txt").write_text("portable", encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                resolved = gui.config_resolution(frozen=True)

            self.assertEqual(resolved.source, "portable", "便携标记应当压过程序目录配置（source=program）")
            self.assertEqual(resolved.path, app / "config.toml")
            self.assertTrue(resolved.portable)

    def test_falls_back_to_appdata_when_no_portable_config(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            with mock.patch.object(gui, "_app_dirs", return_value=[Path(tmp)]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                path = gui.settings_path()
            self.assertEqual(path, Path(appdata) / "TrainSolve" / "config.toml")

    def test_template_path_found_next_to_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "config.example.toml"
            template.write_text(PUBLIC_TEMPLATE, encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[Path(tmp)]):
                self.assertEqual(gui.template_path(), template)

    def test_template_path_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gui, "_app_dirs", return_value=[Path(tmp)]):
                self.assertIsNone(gui.template_path())


class LoadClientConfigTest(unittest.TestCase):
    """load_client_config 的不变量：值必须来自「解析到的那一份」，不许到处乱找。

    接缝说明：这 4 条不再 mock `gui.settings_path`——`load_client_config_with_source()`
    走的是 `config_resolution()` → `resolve_config_path()`，mock 旧接缝等于绕过被测逻辑。
    改为真实解析器 + 临时夹具（`_app_dirs` 指向临时目录、`APPDATA` 指向临时目录）。
    """

    def test_existing_config_wins(self):
        """不变量：解析到的那份配置存在时，字段值必须来自它（不是默认值、不是模板）。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            (app / "portable.txt").write_text("portable", encoding="utf-8")
            (app / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                loaded = gui.load_client_config()
            self.assertEqual(loaded.platform.username, "2000000001")
            self.assertEqual(loaded.deepseek.api_key, "sk-xxxxxxxx")

    def test_public_template_used_when_no_config(self):
        """不变量：公开版没有 config.toml 时用随包模板，模板里不得有任何账号/密钥。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            (app / "config.example.toml").write_text(PUBLIC_TEMPLATE, encoding="utf-8")
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                loaded = gui.load_client_config()
            self.assertEqual(loaded.platform.username, "")
            self.assertEqual(loaded.platform.password, "")
            self.assertEqual(loaded.deepseek.api_key, "")
            self.assertEqual(loaded.deepseek.base_url, "https://api.deepseek.com")

    def test_bare_defaults_when_nothing_available(self):
        """不变量：既没有配置也没有模板时用代码默认值（凭据为空、不崩）。"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as appdata:
            app = Path(tmp)
            with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                    mock.patch.dict(os.environ, {"APPDATA": appdata}):
                loaded = gui.load_client_config()
            self.assertEqual(loaded.platform.username, "")
            self.assertEqual(loaded.deepseek.api_key, "")

    def test_does_not_wander_into_other_directories(self):
        """不变量：别处（工作目录=开发仓库）有完整配置也不能被读到。

        旧写法 mock 了 `trainsolve.config.find_config` 返回 None，把被测逻辑整个绕过，
        等于没验。现在**不 mock 任何解析函数**：真的把工作目录切到「放着完整配置的别处」，
        只把 `_app_dirs`/`APPDATA` 指向空的临时目录，断言读到的是空默认值。
        """
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "repo"
            elsewhere.mkdir()
            (elsewhere / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")

            app = Path(tmp) / "app"
            app.mkdir()
            appdata = Path(tmp) / "appdata"
            appdata.mkdir()

            previous = Path.cwd()
            os.chdir(elsewhere)
            try:
                with mock.patch.object(gui, "_app_dirs", return_value=[app]), \
                        mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                    loaded = gui.load_client_config()
            finally:
                os.chdir(previous)

            self.assertNotEqual(loaded.platform.username, "2000000001", "工作目录里的配置被读到了")
            self.assertEqual(loaded.platform.username, "")
            self.assertEqual(loaded.deepseek.api_key, "")


HUMAN_RUN_CONFIG = """
[platform]
base_url = "http://127.0.0.1/train/"
username = ""
password = ""

[deepseek]
api_key = ""

[run]
human_input = true
human_dry_run = true
cdp_url = "http://127.0.0.1:9444"
typing_cpm = 300
typing_cpm_min = 180
typing_cpm_max = 420
typo_rate = 0.03
"""


class HumanTypingConfigTest(unittest.TestCase):
    def test_speed_range_is_loaded(self):
        from trainsolve.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(HUMAN_RUN_CONFIG, encoding="utf-8")
            cfg = load_config(str(path))
        self.assertTrue(cfg.run.human_input)
        self.assertTrue(cfg.run.human_dry_run)
        self.assertEqual(cfg.run.cdp_url, "http://127.0.0.1:9444")
        self.assertEqual(cfg.run.typing_cpm, 300)
        self.assertEqual(cfg.run.typing_cpm_min, 180)
        self.assertEqual(cfg.run.typing_cpm_max, 420)
        self.assertEqual(cfg.run.typo_rate, 0.03)

    def test_speed_range_defaults_to_auto(self):
        from trainsolve.config import RunConfig

        run = RunConfig()
        self.assertEqual(run.typing_cpm_min, 0.0)
        self.assertEqual(run.typing_cpm_max, 0.0)


AUTO_CLEANUP_CONFIG = """
[platform]
base_url = "http://127.0.0.1/train/"
username = ""
password = ""

[run]
auto_cleanup = false
auto_cleanup_all = true
auto_cleanup_temp_hours = 6.5
"""


class AutoCleanupConfigTest(unittest.TestCase):
    def test_keys_are_loaded(self):
        from trainsolve.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(AUTO_CLEANUP_CONFIG, encoding="utf-8")
            cfg = load_config(str(path))
        self.assertFalse(cfg.run.auto_cleanup)
        self.assertTrue(cfg.run.auto_cleanup_all)
        self.assertEqual(cfg.run.auto_cleanup_temp_hours, 6.5)

    def test_defaults_on(self):
        from trainsolve.config import RunConfig

        run = RunConfig()
        self.assertTrue(run.auto_cleanup, "默认开着：启动时顺手清过期残留")
        self.assertFalse(run.auto_cleanup_all, "默认不动进行中的会话")
        self.assertEqual(run.auto_cleanup_temp_hours, 2.0)


if __name__ == "__main__":
    unittest.main()
