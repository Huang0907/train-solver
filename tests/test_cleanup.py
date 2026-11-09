"""cleanup 命令：把「未完成」会话跳过并删除（纯离线测试）。"""

import copy
import unittest

from trainsolve.api import Session
from trainsolve.cli import cmd_cleanup
from trainsolve.config import Config


class FakeClient:
    def __init__(self):
        self.session = Session(sid="sid", expire=4102444800000)
        self.records = {
            "done": {"state": 1, "timeLeft": 0, "paper": {"questions": [{"id": 1, "qType": 5}]}},
            "todo": {"state": 0, "timeLeft": 1200, "paper": {"questions": [{"id": 2, "qType": 5}]}},
        }
        self.skipped = []
        self.cancelled = []

    def history(self, tag_id=0, index=0, size=10, search=""):
        return {"total": len(self.records), "data": [{"sessionId": k, "state": v["state"]} for k, v in self.records.items()]}

    def paper(self, session_id):
        return copy.deepcopy(self.records[session_id])

    def skip_this(self, session_id):
        self.skipped.append(session_id)
        return 1

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)
        self.records.pop(session_id, None)
        return 1


class CleanupTest(unittest.TestCase):
    def _args(self, **kwargs):
        class Args:
            session = kwargs.get("session")
            dry_run = kwargs.get("dry_run", False)
        return Args()

    def test_cleans_unfinished_only(self):
        client = FakeClient()
        code = cmd_cleanup(Config(), self._args(), client=client)
        self.assertEqual(code, 0)
        self.assertEqual(client.skipped, ["todo"])
        self.assertEqual(client.cancelled, ["todo"])
        self.assertNotIn("todo", client.records)
        self.assertIn("done", client.records)

    def test_dry_run_changes_nothing(self):
        client = FakeClient()
        cmd_cleanup(Config(), self._args(dry_run=True), client=client)
        self.assertEqual(client.skipped, [])
        self.assertEqual(client.cancelled, [])
        self.assertIn("todo", client.records)

    def test_specific_session(self):
        client = FakeClient()
        cmd_cleanup(Config(), self._args(session="todo"), client=client)
        self.assertEqual(client.cancelled, ["todo"])


if __name__ == "__main__":
    unittest.main()
