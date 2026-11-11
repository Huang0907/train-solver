"""前端暂存同步的判定：必须等到 post-answer 响应 code=0 才允许提交。

2026-11-02 真机事故：只校验「请求体 == 目标代码」就点提交，平台拿到的是旧快照
（编译了 339 字符的截断代码，判 0/12）。原因是 Angular 组件状态在响应回调里才更新。
"""

import json
import unittest
from unittest import mock

from trainsolve.human import HumanBrowser, _api_code

CODE = "#include <stdio.h>\nint main(void)\n{\n    return 0;\n}\n"
STALE = "#include <stdio.h>\nint main(void)\n{"  # 长度够长、但内容不完整


def entry(body_code, *, done=True, code=0, url="api/student/post-answer"):
    body = json.dumps({"answerPaper": {"q4": [{"answer": body_code}]}})
    resp = json.dumps({"code": code, "data": None}) if code is not None else None
    return {"url": url, "method": "XHR", "body": body, "done": done, "status": 200, "resp": resp}


class ApiCodeTest(unittest.TestCase):
    def test_parses_business_code(self):
        self.assertEqual(_api_code({"resp": '{"code":0}'}), 0)
        self.assertEqual(_api_code({"resp": '{"code":8,"msg":"校验码错误"}'}), 8)

    def test_returns_none_when_unavailable(self):
        self.assertIsNone(_api_code({}))
        self.assertIsNone(_api_code({"resp": None}))
        self.assertIsNone(_api_code({"resp": "not-json"}))
        self.assertIsNone(_api_code({"resp": '{"code":"0"}'}))


class WaitAppCodeTest(unittest.TestCase):
    def setUp(self):
        self.browser = HumanBrowser("http://example/train/", "", "", log=lambda *_: None)

    def _wait(self, entries, timeout=0.2, net=None):
        with mock.patch.object(self.browser, "_captured", return_value=entries), \
                mock.patch.object(self.browser, "_net_captured", return_value=list(net or [])), \
                mock.patch.object(self.browser, "_has_button", return_value=False), \
                mock.patch("trainsolve.human.time.sleep"):
            return self.browser.wait_app_code(CODE, since=0, net_since=0, timeout=timeout)

    def test_accepted_full_snapshot_passes(self):
        self.assertTrue(self._wait([entry(CODE)], timeout=1.0))

    def test_in_flight_request_is_not_enough(self):
        """请求体虽完整但响应还没回来：组件状态可能还没更新，不能提交。"""
        self.assertFalse(self._wait([entry(CODE, done=False)]))

    def test_rejected_snapshot_blocks_submit(self):
        """平台回 code=8（校验码错误）：暂存没落库，绝不能提交。"""
        self.assertFalse(self._wait([entry(CODE, code=8)]))

    def test_stale_snapshot_is_not_enough(self):
        self.assertFalse(self._wait([entry(STALE)]))

    def test_mixed_entries_use_the_accepted_one(self):
        self.assertTrue(self._wait([entry(STALE), entry(CODE, code=8), entry(CODE)], timeout=1.0))

    def test_network_response_alone_confirms(self):
        """页面内钩子抓不到响应时，Playwright 网络事件（status + 响应体 code）也算回执。"""
        net = [{"url": "api/student/post-answer", "body": entry(CODE)["body"],
                "status": 200, "resp": '{"code":0}', "done": True}]
        self.assertTrue(self._wait([entry(CODE, done=False)], timeout=1.0, net=net))

    def test_network_http_error_is_not_confirmed(self):
        net = [{"url": "api/student/post-answer", "body": entry(CODE)["body"],
                "status": 500, "resp": "", "done": True}]
        self.assertFalse(self._wait([entry(CODE, done=False)], net=net))

    def test_indent_difference_still_confirms(self):
        """0.6.9：打字把行首缩进交给编辑器，编辑器会补更多空格——同步判定必须照样通过。

        真机实测：目标 996 字符，编辑器 1140 字符，差别全在行首缩进；旧比较永远等不到。
        """
        padded = "\n".join("    " + line if line.strip() else line for line in CODE.splitlines())
        self.assertTrue(self._wait([entry(padded + "\n")], timeout=1.0))

    def test_no_code_body_says_so(self):
        """一条带代码的暂存都没有，和「挑错串」要分开报。"""
        logs: list[str] = []
        self.browser.log = logs.append
        entries = [{"url": "api/student/post-answer", "body": None, "done": True,
                    "status": 200, "resp": '{"code":0}'}]
        self.assertFalse(self._wait(entries))
        self.assertIn("一条带代码的 post-answer 都没抓到", "\n".join(logs))

    def test_wrong_body_reports_candidates(self):
        """抓到暂存但对不上：日志要列出候选串（路径+长度），别再说「旧快照」。"""
        logs: list[str] = []
        self.browser.log = logs.append
        prompt = json.dumps({"content": "#include <stdio.h>\nint main(void){/*题目描述*/}"})
        entries = [{"url": "api/student/post-answer", "body": prompt, "done": True,
                    "status": 200, "resp": '{"code":0}'}]
        self.assertFalse(self._wait(entries))
        joined = "\n".join(logs)
        self.assertIn("没有一条等于目标代码", joined)
        self.assertIn("$.content", joined)


class AnswerFieldPreferenceTest(unittest.TestCase):
    """`_code_from_body` 必须优先取答案字段，别被排在前面题面/模板抢走。"""

    def test_answer_field_wins_over_prompt_text(self):
        from trainsolve.human import _code_from_body

        body = json.dumps({
            "content": "#include <stdio.h>\nint main(void){ return 0; }  // 题面",
            "answerPaper": {"q4": [{"answer": CODE}]},
        })
        self.assertEqual(_code_from_body(body), CODE)

    def test_template_before_answer_is_ignored(self):
        from trainsolve.human import _code_from_body

        body = json.dumps({
            "template": "int main() { /* 模板 请在此作答 */ }",
            "answerPaper": {"q4": [{"answer": CODE}]},
        })
        self.assertEqual(_code_from_body(body), CODE)

    def test_falls_back_to_code_like_string(self):
        from trainsolve.human import _code_from_body

        body = json.dumps({"payload": CODE})
        self.assertEqual(_code_from_body(body), CODE)


class SubmitVerdictTest(unittest.TestCase):
    """提交回执：非 2xx 或 code!=0 必须当失败，不能假装交上了。"""

    def setUp(self):
        self.browser = HumanBrowser("http://example/train/", "", "", log=lambda *_: None)

    def _verdict(self, net):
        with mock.patch.object(self.browser, "_net_captured", return_value=list(net)):
            return self.browser._submit_verdict(0)

    def test_missing_receipt_is_allowed(self):
        self.assertEqual(self._verdict([]), (True, None))

    def test_ok_receipt(self):
        net = [{"url": "api/student/submit?sessionId=x", "status": 200, "resp": '{"code":0}'}]
        self.assertEqual(self._verdict(net), (True, 0))

    def test_rejected_receipt(self):
        net = [{"url": "api/student/submit?sessionId=x", "status": 200, "resp": '{"code":8}'}]
        self.assertEqual(self._verdict(net), (False, 8))

    def test_http_error_receipt(self):
        net = [{"url": "api/student/submit?sessionId=x", "status": 500, "resp": ""}]
        self.assertEqual(self._verdict(net), (False, None))

    def test_post_answer_entries_are_ignored(self):
        net = [{"url": "api/student/post-answer?sid=x", "status": 200, "resp": '{"code":8}'}]
        self.assertEqual(self._verdict(net), (True, None))


class CursorLineJsTest(unittest.TestCase):
    """光标列号必须用 Range 量（0.6.9 真机事故的防回退）。"""

    def test_uses_dom_range_not_text_node_walk(self):
        from trainsolve.human import CURSOR_LINE_JS

        self.assertIn("getRangeAt(0)", CURSOR_LINE_JS)
        self.assertIn("createRange", CURSOR_LINE_JS)
        self.assertIn("setEnd(", CURSOR_LINE_JS)
        # 遍历文本节点累加列号在「锚点变成 .cm-line 元素」时会把行尾算成光标位，
        # 2026-11-11 实测把 `for (int` 整行退格成空。这条断言就是拦住改回去。
        self.assertNotIn("createTreeWalker", CURSOR_LINE_JS)

    def test_returns_before_after_fields(self):
        from trainsolve.human import CURSOR_LINE_JS

        for field in ("before", "after", "line", "col", "doc"):
            self.assertIn(field, CURSOR_LINE_JS)


if __name__ == "__main__":
    unittest.main()
