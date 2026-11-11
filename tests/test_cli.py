"""命令行参数 → 配置的映射（纯函数，不联网）。"""

import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trainsolve.cli import _apply_qtypes
from trainsolve.config import Config


def args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class QTypesArgTest(unittest.TestCase):
    def test_default_is_ordinary_programming_only(self):
        """0.6.10 起默认只做普通编程题（5），6/7 改错类不再进默认池。"""
        config = Config()
        config.run.qbase_type = 4
        _apply_qtypes(config, args(qtypes=None))
        self.assertEqual(config.run.qtypes, [5])

    def test_explicit_single(self):
        config = Config()
        _apply_qtypes(config, args(qtypes="5"))
        self.assertEqual(config.run.qtypes, [5])

    def test_explicit_list_dedupes_and_sorts(self):
        config = Config()
        _apply_qtypes(config, args(qtypes="7,5,5,6"))
        self.assertEqual(config.run.qtypes, [5, 6, 7])

    def test_fullwidth_comma(self):
        config = Config()
        _apply_qtypes(config, args(qtypes="5，6"))
        self.assertEqual(config.run.qtypes, [5, 6])

    def test_all_disables_filter(self):
        config = Config()
        _apply_qtypes(config, args(qtypes="all"))
        self.assertEqual(config.run.qtypes, [])

    def test_objective_qbase_type_disables_default(self):
        config = Config()
        config.run.qbase_type = 2  # 判断题
        _apply_qtypes(config, args(qtypes=None))
        self.assertEqual(config.run.qtypes, [])

    def test_bad_value_raises_system_exit(self):
        config = Config()
        with self.assertRaises(SystemExit):
            _apply_qtypes(config, args(qtypes="abc"))


class CliDefaultsDoNotShadowConfigTest(unittest.TestCase):
    """命令行没写的参数不能带默认值下来覆盖配置文件（0.6.9 修）。"""

    SHADOWING = ("tag_id", "level", "qbase_type", "count", "judge",
                 "max_repairs", "score_timeout")

    def test_run_parser_leaves_them_absent(self):
        from trainsolve.cli import build_parser
        ns = build_parser().parse_args(["run", "--config", "x.toml"])
        for name in self.SHADOWING:
            self.assertFalse(
                hasattr(ns, name),
                f"--{name} 不该在没显式给出时带着默认值（会覆盖配置）",
            )

    def test_explicit_value_still_reaches_config(self):
        from trainsolve.cli import build_parser
        ns = build_parser().parse_args(["run", "--tag", "92", "--level", "3"])
        self.assertEqual(ns.tag_id, 92)
        self.assertEqual(ns.level, 3)

    def test_config_tag_wins_when_cli_silent(self):
        """配置里 tag_id=92、命令行不给 --tag：overrides 必须是 None。"""
        from trainsolve.cli import build_parser
        ns = build_parser().parse_args(["run", "--config", "x.toml"])
        self.assertIsNone(getattr(ns, "tag_id", None))
        config = Config()
        config.run.tag_id = 92
        override = getattr(ns, "tag_id", None)
        if override is not None:
            config.run.tag_id = override
        self.assertEqual(config.run.tag_id, 92)


class LoginErrorExitCodeTest(unittest.TestCase):
    """密码错（LoginError）要给一行提示 + 退出码 2，不能 traceback（0.6.10 审计）。"""

    @staticmethod
    def _config_file() -> str:
        directory = Path(tempfile.mkdtemp())
        path = directory / "config.toml"
        path.write_text(
            '[platform]\nbase_url = "http://127.0.0.1:9/"\n'
            'username = "2026000000"\npassword = "x"\n',
            encoding="utf-8",
        )
        return str(path)

    def test_login_error_returns_2(self):
        from trainsolve import cli
        from trainsolve.api import LoginError, TrainClient

        with mock.patch.object(TrainClient, "login", side_effect=LoginError("登录失败：密码错误")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(cli.main(["login", "--config", self._config_file()]), 2)

    def test_other_train_error_returns_2(self):
        from trainsolve import cli
        from trainsolve.api import NoQuestionAvailable, TrainClient

        with mock.patch.object(TrainClient, "login", side_effect=NoQuestionAvailable("题库抽空")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(cli.main(["login", "--config", self._config_file()]), 2)


if __name__ == "__main__":
    unittest.main()
