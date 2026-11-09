"""命令行参数 → 配置的映射（纯函数，不联网）。"""

import argparse
import unittest

from trainsolve.cli import _apply_qtypes
from trainsolve.config import Config


def args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class QTypesArgTest(unittest.TestCase):
    def test_default_is_programming_pool(self):
        config = Config()
        config.run.qbase_type = 4
        _apply_qtypes(config, args(qtypes=None))
        self.assertEqual(config.run.qtypes, [5, 6, 7])

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


if __name__ == "__main__":
    unittest.main()
