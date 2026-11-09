"""内部契约防回归（2026-11-05）。

背景：`_open` 加了关键字参数、`ApiError` 加了 `reason` 字段，而
`scripts\smoke_readonly.py` 是**外部脚本直接替换客户端内部实现**的，
因为签名不匹配，整条只读冒烟在登录前就崩了一次。

这里用 AST 静态检查脚本的签名（不 import、不执行那个脚本），
再对客户端的内部契约做几条断言。任何一条红了，说明有人的改动静默破坏了
今天这条验证链路 —— 先看 `_reports\internal-api-contracts.md`。
"""

import ast
import inspect
import unittest
from pathlib import Path

from trainsolve.api import ApiError, TrainClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke_readonly.py"


def _find_function(source_path: Path, name: str) -> ast.FunctionDef:
    """在源码里按名字找第一个函数定义（含嵌套定义，不执行模块）。"""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{source_path.name} 里找不到函数 {name}()")


def _accepts_keyword_arguments(fn: ast.FunctionDef) -> bool:
    args = fn.args
    if args.kwarg is not None:            # **kwargs
        return True
    return any(arg.arg == "idempotent" for arg in args.kwonlyargs)


class SmokeGuardContractTest(unittest.TestCase):
    """冒烟脚本替换 `_open` 时，必须能吃下客户端传的关键字参数。"""

    def test_guarded_open_accepts_extra_keywords(self):
        self.assertTrue(SMOKE_SCRIPT.exists(), f"找不到冒烟脚本：{SMOKE_SCRIPT}")
        fn = _find_function(SMOKE_SCRIPT, "guarded_open")
        self.assertTrue(
            _accepts_keyword_arguments(fn),
            "smoke_readonly.py 的 guarded_open 只接受位置参数；"
            "客户端 `_open(request, *, idempotent=...)` 会在登录前抛 TypeError。"
            "修法：def guarded_open(request, **kwargs)。",
        )


class ApiErrorContractTest(unittest.TestCase):
    """`reason` 必须是可选字段，不能写坏老调用。"""

    def test_reason_defaults_to_empty(self):
        self.assertEqual(ApiError("boom").reason, "")
        self.assertEqual(ApiError("boom", status=500).reason, "")

    def test_old_positional_call_still_works(self):
        exc = ApiError("boom", 500, "body")
        self.assertEqual((exc.status, exc.body, exc.reason), (500, "body", ""))

    def test_reason_is_carried(self):
        self.assertEqual(ApiError("boom", status=401, reason="session-expired").reason, "session-expired")


class ClientInternalContractTest(unittest.TestCase):
    """脚本/测试会碰的内部入口及其形状。"""

    def test_open_takes_keyword_only_idempotent(self):
        params = inspect.signature(TrainClient._open).parameters
        self.assertIn("idempotent", params)
        self.assertEqual(params["idempotent"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(params["idempotent"].default, True, "默认必须是 True，否则老调用语义改变")

    def test_request_still_accepts_legacy_arguments(self):
        params = inspect.signature(TrainClient.request).parameters
        for name in ("method", "path", "params", "body", "raw_body"):
            self.assertIn(name, params)

    def test_with_relogin_exists(self):
        self.assertTrue(callable(getattr(TrainClient, "_with_relogin", None)))

    def test_enable_auto_relogin_exists(self):
        self.assertTrue(callable(getattr(TrainClient, "enable_auto_relogin", None)))


if __name__ == "__main__":
    unittest.main()
