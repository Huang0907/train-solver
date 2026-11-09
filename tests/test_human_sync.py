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


if __name__ == "__main__":
    unittest.main()
