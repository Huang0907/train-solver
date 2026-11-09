"""启动警告的覆盖（0.5.5）：`auto_fix_from_judge` 开启时必须把后果说清。

背景：0.5.5 把 `auto_fix_from_judge` 的默认值从 true 改成 false——因为平台的「重答」会把
已提交的记录重新打开、**清空刚才的成绩**，等于撤销提交。老配置里写着 true 的仍然生效，
所以 runner 在开跑前必须打一行警告，否则用户会以为"自动修复"是无害的。

这两条断言的方向相反，缺一不可：
* 开着 → **必须**打出「自动重答已开启…等于撤销刚才的提交」；
* 关着（默认） → **不许**打，避免把警告变成噪音、让人以后直接无视它。
"""

import unittest
from unittest import mock

from trainsolve.config import Config
from trainsolve.runner import Runner


class _NoNetworkClient:
    """任何请求都当作错误：这个用例只验日志，不该碰到网络。"""

    def __getattr__(self, name):  # pragma: no cover - 只会在行为退化时才被调用
        def _boom(*_args, **_kwargs):
            raise AssertionError(f"启动警告用例不该发请求：{name}")

        return _boom


class _NoSolver:
    def solve(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("启动警告用例不该解题")


def _startup_logs(auto_fix: bool) -> list[str]:
    """跑一次"开跑即停"的主循环，只收日志。"""
    config = Config()
    config.run.auto_fix_from_judge = auto_fix
    logs: list[str] = []
    runner = Runner(
        _NoNetworkClient(),
        _NoSolver(),
        config,
        log=lambda message, *_args: logs.append(str(message)),
        should_stop=lambda: True,          # 进循环就停，不抽题
    )
    with mock.patch.object(Runner, "_preflight_model", return_value=True):
        runner.run()
    return logs


class AutoFixStartupWarningTest(unittest.TestCase):
    def test_warning_is_logged_when_enabled(self):
        logs = _startup_logs(True)
        joined = "\n".join(logs)
        self.assertIn("自动重答已开启", joined, f"开着却没警告，日志：{logs}")
        self.assertIn("撤销刚才的提交", joined, "警告没说清后果，用户不知道成绩会被清空")

    def test_no_warning_when_disabled(self):
        """默认关：不许有这行，否则警告会变成噪音。"""
        logs = _startup_logs(False)
        joined = "\n".join(logs)
        self.assertNotIn("自动重答已开启", joined, f"关着却还在警告，日志：{logs}")

    def test_default_is_off(self):
        """默认值本身也要钉住：改成 True 就等于把危险行为设成出厂状态。"""
        self.assertFalse(Config().run.auto_fix_from_judge)


if __name__ == "__main__":
    unittest.main()
