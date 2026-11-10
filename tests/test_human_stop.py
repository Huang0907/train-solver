"""提交前的停止闸门（0.6.8）。

后端复核发现：`answer()` 只在打字前后查停止，打进 `wait_app_code`（最多 18+10 秒）
之后到 `submit()` 之间没有第二次检查——用户在同步窗口里点停止，仍然会提交一次。
这里锁住两条：同步窗口可被停止打断；提交前最后一次检查命中就绝不点提交。
"""

import types
import unittest

from trainsolve.human import HumanBrowser, HumanStopped


def make_browser() -> HumanBrowser:
    """只装最小 page 替身：evaluate 返回空列表、wait_for_timeout 空转。"""
    browser = HumanBrowser("http://example.invalid/", "u", "p", log=lambda *_: None)
    browser.page = types.SimpleNamespace(
        evaluate=lambda *args, **kwargs: [],
        wait_for_timeout=lambda ms: None,
    )
    browser._net = []
    return browser


class WaitAppCodeStopTest(unittest.TestCase):
    def test_同步窗口里点停止会立刻放弃等待(self):
        browser = make_browser()
        verdict = browser.wait_app_code(
            "int x;", since=0, net_since=0, timeout=5, should_stop=lambda: True
        )
        self.assertIsNone(verdict, "停了就不该继续等前端同步")

    def test_没有停止时照旧返回False表示没抓到回执(self):
        browser = make_browser()
        verdict = browser.wait_app_code(
            "int x;", since=0, net_since=0, timeout=0.2, should_stop=lambda: False
        )
        self.assertIs(verdict, False, "没抓到暂存回执时判定为「没同步好」")


class SubmitGateTest(unittest.TestCase):
    def _browser(self, calls: list[str]) -> HumanBrowser:
        browser = make_browser()
        browser.open_answer_page = lambda sid: None
        browser.install_capture = lambda: None
        browser.type_code = lambda code, should_stop=None: (True, len(code))
        browser.nudge_editor = lambda: None
        browser.submit = lambda: calls.append("submit")
        return browser

    def test_同步期间点停止就绝不提交(self):
        calls: list[str] = []
        browser = self._browser(calls)
        flag = {"stop": False}

        def wait(*_args, **_kwargs):
            flag["stop"] = True          # 同步窗口里点了停止
            return True

        browser.wait_app_code = wait
        with self.assertRaises(HumanStopped):
            browser.answer("sid", "int x;", dry_run=False,
                           should_stop=lambda: flag["stop"])
        self.assertEqual(calls, [], "停止后一次 submit 都不许发")

    def test_未停止时照常提交(self):
        calls: list[str] = []
        browser = self._browser(calls)
        browser.wait_app_code = lambda *_a, **_k: True
        result = browser.answer("sid", "int x;", dry_run=False,
                                should_stop=lambda: False)
        self.assertTrue(result.ok)
        self.assertEqual(calls, ["submit"])

    def test_dry_run永不提交(self):
        calls: list[str] = []
        browser = self._browser(calls)
        browser.wait_app_code = lambda *_a, **_k: True
        result = browser.answer("sid", "int x;", dry_run=True, should_stop=lambda: False)
        self.assertIn("dry-run", result.message)
        self.assertEqual(calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
