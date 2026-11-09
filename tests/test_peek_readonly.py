# -*- coding: utf-8 -*-
"""「看题」到底会不会写平台：peek() 会 POST choose-q，peek_existing() 一次都不发。

回归自 2026-11-05：「只看题（不提交）」的文案让人以为只读，实际上
`peek()` → `_pick_session()` → `client.choose()` → `POST api/student/choose-q`
会真建一个会话、占一个题量名额。
"""

import copy
import tempfile
import unittest
from pathlib import Path

from trainsolve.api import Session
from trainsolve.config import Config
from trainsolve.runner import Runner

RECORD = {
    "state": 0,
    "timeLeft": 3600,
    "paper": {
        "questions": [
            {
                "id": 1,
                "qType": 5,
                "point": 10,
                "language": 1,
                "content": "题目 A\n\n样例输入\n1\n\n样例输出\n2",
                "options": [],
            }
        ]
    },
}

WRITE_METHODS = ("choose", "post_answer", "submit", "skip_this", "cancel_skip", "run_code")


class CountingClient:
    """把每个会写平台的调用都计数，让「只读」变成可断言的事实。"""

    def __init__(self) -> None:
        self.session = Session(sid="fake-sid", code="2000000001", expire=4102444800000)
        self.calls = {name: 0 for name in WRITE_METHODS}
        self.paper_sessions: list[str] = []

    def login(self, code: str, password: str) -> Session:
        return self.session

    def available_tags(self, qbase_type: int, level: int = 0) -> list[dict]:
        return [{"id": 3, "content": "U1", "available": True}]

    def paper(self, session_id: str) -> dict:
        self.paper_sessions.append(session_id)
        return copy.deepcopy(RECORD)

    # ---- 写接口：只计数 ----
    def choose(self, qbase_type: int, level: int = 0, tag_id: int = -1) -> str:
        self.calls["choose"] += 1
        return "new-session-id"

    def post_answer(self, *args, **kwargs):
        self.calls["post_answer"] += 1

    def submit(self, *args, **kwargs):
        self.calls["submit"] += 1

    def skip_this(self, *args, **kwargs):
        self.calls["skip_this"] += 1

    def cancel_skip(self, *args, **kwargs):
        self.calls["cancel_skip"] += 1

    def run_code(self, *args, **kwargs):
        self.calls["run_code"] += 1
        return []


class PeekReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config()
        self.config.run.out_dir = str(Path(self.tmp.name) / "out")
        self.config.run.submit = False
        self.config.run.dry_run = True

    def _runner(self, client: CountingClient) -> Runner:
        return Runner(client, object(), self.config, log=lambda _message: None)

    def test_peek_existing_sends_zero_write_calls(self):
        client = CountingClient()
        session_id, record = self._runner(client).peek_existing("existing-session")

        self.assertEqual(session_id, "existing-session")
        self.assertEqual(client.paper_sessions, ["existing-session"])
        self.assertEqual(client.calls["choose"], 0, "peek_existing 绝不能新建会话")
        self.assertEqual(
            {name: count for name, count in client.calls.items() if count},
            {},
            "peek_existing 路径上出现了写调用",
        )
        self.assertTrue((record.get("paper") or {}).get("questions"))

    def test_peek_creates_one_session(self):
        client = CountingClient()
        session_id, _record = self._runner(client).peek()

        self.assertEqual(client.calls["choose"], 1, "peek 应当恰好新建一个会话")
        self.assertEqual(session_id, "new-session-id")
        self.assertEqual(client.calls["submit"], 0)

    def test_peek_existing_rejects_empty_session(self):
        with self.assertRaises(ValueError):
            self._runner(CountingClient()).peek_existing("")


if __name__ == "__main__":
    unittest.main()
