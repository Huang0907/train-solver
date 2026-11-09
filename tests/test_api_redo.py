"""重做类接口（reanswer / half-redo / rescore）的返回码语义。

平台返回**业务码**而不是布尔：0=成功、1=拒绝（例如记录仍「未完成」时不允许重新答题）。
2026-11-02 那次 0/12 之后，旧代码把 1 当成功，靠后续状态判断才兜住。
"""

import unittest
from unittest import mock

from trainsolve.api import REDO_OK, TrainClient, _redo_code

CODE = "#include <stdio.h>\nint main(void)\n{\n    return 0;\n}\n"


class RedoCodeTest(unittest.TestCase):
    def test_accepts_int_payload(self):
        self.assertEqual(REDO_OK, 0)
        self.assertEqual(_redo_code(0), 0)
        self.assertEqual(_redo_code(1), 1)

    def test_accepts_dict_payload(self):
        self.assertEqual(_redo_code({"code": 0}), 0)
        self.assertEqual(_redo_code({"code": 1, "msg": "记录状态不允许"}), 1)

    def test_unknown_payloads(self):
        for payload in (True, False, None, "0", [0], {"code": "0"}, {"msg": "x"}):
            self.assertEqual(_redo_code(payload), -1, payload)


class RedoClientTest(unittest.TestCase):
    """reanswer/half_redo/rescore 必须把业务码原样交出来（不能真值化）。"""

    def _client(self, payload):
        client = TrainClient("http://example/train/")
        client.post_no_body = mock.Mock(return_value=payload)  # type: ignore[method-assign]
        return client

    def test_reanswer_returns_code(self):
        self.assertEqual(self._client(1).reanswer("sid"), 1)
        self.assertEqual(self._client(0).reanswer("sid"), 0)

    def test_half_redo_returns_code(self):
        self.assertEqual(self._client(0).half_redo("sid"), 0)

    def test_rescore_returns_code(self):
        self.assertEqual(self._client(1).rescore("sid"), 1)


if __name__ == "__main__":
    unittest.main()
