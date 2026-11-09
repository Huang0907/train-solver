"""配置来源解析（0.5.3 R1→R4）。

事故背景：客户端按「当前工作目录里的 config.toml」优先读，于是从仓库根启动就会用上那份
`submit = true / dry_run = false` 的配置，而 `%APPDATA%` 里的安全配置被静默绕过。

这里锁住：
* 工作目录**不再**参与自动查找（R1）；
* 便携模式（`portable.txt`）优先；
* 打包版可用程序目录；源码检出（未打包）不允许仓库配置静默胜出；
* `--config` / 环境变量显式指定，文件不存在就报错，不静默回落；
* 解析结果带 `source`，并且能被一行日志描述出来（R2，验收要拿它当硬证据）；
* CLI 与 GUI 必须用同一个解析器（R4），不允许两边读出不同文件。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trainsolve import config as cfgmod
from trainsolve.config import Config

SOURCES = {"explicit", "portable", "program", "appdata"}


def _write(path: Path, text: str = "[platform]\nusername = \"\"\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _TempDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.appdata = self.root / "appdata"
        self.appdata.mkdir()
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 事故形态：工作目录里有一份 config.toml
        _write(self.cwd / "config.toml")

    def resolve(self, **kwargs):
        kwargs.setdefault("app_dir", self.app)
        kwargs.setdefault("frozen", False)
        return cfgmod.resolve_config_path(**kwargs)

    def expected_appdata(self) -> Path:
        return self.appdata / "TrainSolve" / "config.toml"


class CwdMustNotWinTest(_TempDirs):
    def test_cwd_config_is_ignored(self):
        """事故根因：cwd 里那份不许再被自动选中。"""
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

        resolved = self.resolve()

        self.assertEqual(resolved.source, "appdata", "非便携模式下应当落到 %APPDATA%")
        self.assertEqual(resolved.path, self.expected_appdata())
        self.assertNotEqual(resolved.path, self.cwd / "config.toml", "工作目录那份不许胜出")

    def test_source_is_one_of_the_four_values(self):
        self.assertIn(self.resolve().source, SOURCES, "source 必须可直接写进日志/被验收读取")


class SourcePriorityTest(_TempDirs):
    def test_portable_marker_pins_config_to_app_dir(self):
        _write(self.app / "portable.txt", "portable")
        _write(self.app / "config.toml")
        _write(self.expected_appdata())

        resolved = self.resolve()

        self.assertEqual(resolved.source, "portable")
        self.assertEqual(resolved.path, self.app / "config.toml")
        self.assertTrue(resolved.portable)

    def test_program_dir_used_only_when_frozen(self):
        _write(self.app / "config.toml")

        frozen = self.resolve(frozen=True)
        self.assertEqual((frozen.source, frozen.path), ("program", self.app / "config.toml"))

        source_checkout = self.resolve(frozen=False)
        self.assertEqual(
            source_checkout.source, "appdata",
            "源码检出时仓库里的 config.toml 不许静默胜出（否则就是这次事故的形态）",
        )

    def test_explicit_file_wins_over_everything(self):
        _write(self.app / "portable.txt", "portable")
        _write(self.app / "config.toml")
        explicit = _write(self.root / "chosen.toml")

        resolved = self.resolve(explicit=str(explicit))

        self.assertEqual((resolved.source, resolved.path), ("explicit", explicit))

    def test_explicit_missing_file_raises_instead_of_falling_back(self):
        missing = self.root / "nope.toml"

        with self.assertRaises(cfgmod.ConfigSourceError):
            self.resolve(explicit=str(missing))

    def test_env_var_counts_as_explicit(self):
        chosen = _write(self.root / "env.toml")
        with mock.patch.dict(os.environ, {"TRAINSOLVE_CONFIG": str(chosen)}):
            resolved = self.resolve()

        self.assertEqual((resolved.source, resolved.path), ("explicit", chosen))


class DescribeTest(_TempDirs):
    def test_description_carries_absolute_path_and_safety_switches(self):
        resolved = self.resolve()
        config = Config()
        config.run.submit = False
        config.run.dry_run = True

        text = cfgmod.describe_config_source(resolved, config)

        self.assertIn(str(resolved.path), text, "必须打出解析到的绝对路径")
        self.assertIn("source=appdata", text)
        self.assertIn("submit=False", text)
        self.assertIn("dry_run=True", text)

    def test_description_never_leaks_credentials(self):
        resolved = self.resolve()
        config = Config()
        config.platform.username = "2026000000"
        config.platform.password = "secret-password"
        config.deepseek.api_key = "sk-should-not-appear"

        text = cfgmod.describe_config_source(resolved, config)

        for secret in ("2026000000", "secret-password", "sk-should-not-appear"):
            self.assertNotIn(secret, text)


class SingleResolverTest(_TempDirs):
    def test_cli_find_config_uses_the_same_resolver(self):
        """R4：CLI 的 find_config 不能再自己走 cwd→仓库根那一套。"""
        _write(self.expected_appdata())

        with mock.patch.object(cfgmod, "default_app_dir", return_value=self.app):
            cli_path = cfgmod.find_config()
            gui_resolved = cfgmod.resolve_config_path(app_dir=self.app, frozen=False)

        self.assertEqual(cli_path, gui_resolved.path, "CLI 与 GUI 必须指向同一个文件")

    def test_find_config_returns_none_when_nothing_exists(self):
        with mock.patch.object(cfgmod, "default_app_dir", return_value=self.app):
            self.assertIsNone(cfgmod.find_config())

    def test_load_config_reports_where_it_came_from(self):
        path = self.expected_appdata()
        _write(path, "[platform]\nusername = \"\"\n[run]\nsubmit = false\ndry_run = true\n")

        with mock.patch.object(cfgmod, "default_app_dir", return_value=self.app):
            config, resolved = cfgmod.load_config_with_source()

        self.assertEqual(resolved.source, "appdata")
        self.assertEqual(resolved.path, path)
        self.assertFalse(config.run.submit)
        self.assertTrue(config.run.dry_run)

    def test_load_config_with_missing_explicit_path_raises(self):
        with self.assertRaises(cfgmod.ConfigSourceError):
            cfgmod.load_config_with_source(str(self.root / "missing.toml"))


if __name__ == "__main__":
    unittest.main()
