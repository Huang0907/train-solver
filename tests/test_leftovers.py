"""启动时自动清理残留：只清过期会话 + 陈旧临时目录，不碰正在做的题。"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trainsolve.api import ApiError
from trainsolve.leftovers import (
    auto_cleanup,
    cleanup_sessions,
    cleanup_temp_dirs,
    is_expired,
    list_undone,
)


class FakeClient:
    """只实现清理用到的四个接口。"""

    def __init__(self, rows):
        self.rows = rows
        self.papers = {
            row["sessionId"]: {"state": row.get("state", 0), "timeLeft": row.get("timeLeft")}
            for row in rows
        }
        self.skipped: list[str] = []
        self.cancelled: list[str] = []

    def history(self, tag_id=0, index=0, size=10, search=""):
        return {"data": [{"sessionId": r["sessionId"], "state": r.get("state", 0)} for r in self.rows]}

    def paper(self, session_id):
        return dict(self.papers[session_id])

    def skip_this(self, session_id):
        self.skipped.append(session_id)

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)


ROWS = [
    {"sessionId": "expired-1", "timeLeft": -120},
    {"sessionId": "expired-2", "timeLeft": 0},
    {"sessionId": "active-1", "timeLeft": 3000},
    {"sessionId": "done-1", "state": 1, "timeLeft": None},
]


class IsExpiredTest(unittest.TestCase):
    def test_expiry_rules(self):
        self.assertTrue(is_expired({"timeLeft": -5}))
        self.assertTrue(is_expired({"timeLeft": 0}))
        self.assertFalse(is_expired({"timeLeft": 30}))
        # 2026-11-05 反转：读不到 ≠ 过期。未知态一律保留（旧断言把危险行为固化成了规范）。
        self.assertFalse(is_expired({"timeLeft": None}), "读不到剩余时间 = 状态未知，不能当成过期")
        self.assertFalse(is_expired({"error": "记录不存在"}), "读取失败 = 状态未知，不能当成过期")


class ListUndoneTest(unittest.TestCase):
    def test_only_undone_rows(self):
        sessions = list_undone(FakeClient(ROWS))
        self.assertEqual([s["sessionId"] for s in sessions], ["expired-1", "expired-2", "active-1"])
        self.assertEqual(sessions[0]["timeLeft"], -120)


class CleanupSessionsTest(unittest.TestCase):
    def test_default_only_cleans_expired(self):
        client = FakeClient(ROWS)
        report = cleanup_sessions(client, log=lambda *_: None)

        self.assertEqual(report.expired, ["expired-1", "expired-2"])
        self.assertEqual(report.cleaned, ["expired-1", "expired-2"])
        self.assertEqual(report.active, ["active-1"])
        self.assertEqual(client.skipped, ["expired-1", "expired-2"])
        self.assertEqual(client.cancelled, ["expired-1", "expired-2"], "必须 skip+cancel 才真正删记录")

    def test_include_active_cleans_everything_undone(self):
        client = FakeClient(ROWS)
        report = cleanup_sessions(client, include_active=True, log=lambda *_: None)

        self.assertEqual(report.cleaned, ["expired-1", "expired-2", "active-1"])
        self.assertEqual(report.active, [])
        self.assertNotIn("done-1", client.skipped)

    def test_dry_run_touches_nothing(self):
        client = FakeClient(ROWS)
        report = cleanup_sessions(client, log=lambda *_: None, dry_run=True)

        self.assertEqual(report.expired, ["expired-1", "expired-2"])
        self.assertEqual(report.cleaned, [])
        self.assertEqual(client.skipped, [])


class CleanupTempDirsTest(unittest.TestCase):
    def _make(self, root: Path, name: str, age_hours: float) -> Path:
        path = root / name
        path.mkdir()
        (path / "x").write_text("x", encoding="utf-8")
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_only_old_dirs_are_removed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self._make(root, "_MEIold", 5)
            fresh = self._make(root, "_MEInew", 0.1)
            keep = self._make(root, "other", 10)

            removed = cleanup_temp_dirs(max_age_hours=2, log=lambda *_: None, temp_root=root)

            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists(), "太新的目录可能在用，不能删")
            self.assertTrue(keep.exists(), "只删 _MEI*，别的目录不碰")

    def test_dry_run_keeps_dirs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self._make(root, "_MEIold", 5)
            removed = cleanup_temp_dirs(max_age_hours=2, log=lambda *_: None, dry_run=True, temp_root=root)
            self.assertEqual(removed, [old])
            self.assertTrue(old.exists())


class AutoCleanupTest(unittest.TestCase):
    def test_sessions_and_temp_together(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "_MEIold"
            old.mkdir()
            stamp = time.time() - 5 * 3600
            os.utime(old, (stamp, stamp))

            client = FakeClient(ROWS)
            logs: list[str] = []
            report = auto_cleanup(
                client, temp_max_age_hours=2, log=logs.append, temp_root=root
            )

            self.assertEqual(report.cleaned, ["expired-1", "expired-2"])
            self.assertEqual(report.temp_dirs, [old])
            self.assertFalse(old.exists())
            self.assertTrue(any("清理过期会话" in line or "已清理过期会话" in line for line in logs))

    def test_client_failure_does_not_raise(self):
        class Broken:
            def history(self, *args, **kwargs):
                raise RuntimeError("net down")

            def paper(self, *args, **kwargs):
                raise RuntimeError("net down")

        with TemporaryDirectory() as tmp:
            report = auto_cleanup(Broken(), log=lambda *_: None, temp_root=Path(tmp))
        self.assertEqual(report.cleaned, [])
        self.assertTrue(report.failures)


class UnknownClient:
    """三种「读不到」形态：返回无 timeLeft 的空体 / 抛 ApiError / 明确还在时限内。"""

    def __init__(self):
        self.skipped: list[str] = []
        self.cancelled: list[str] = []

    def history(self, tag_id=0, index=0, size=10, search=""):
        return {"data": [
            {"sessionId": "none-1", "state": 0},
            {"sessionId": "err-1", "state": 0},
            {"sessionId": "active-1", "state": 0},
        ]}

    def paper(self, session_id):
        if session_id == "err-1":
            raise ApiError("记录不存在")
        if session_id == "none-1":
            return {"state": 0}                     # 平台返回空体 / 缺 timeLeft
        return {"state": 0, "timeLeft": 600}

    def skip_this(self, session_id):
        self.skipped.append(session_id)

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)


class MixedClient:
    """过期 / 进行中 / 未知 / 已完成 四种状态混在一起，用来验「一条都不多删」。"""

    def __init__(self):
        self.skipped: list[str] = []
        self.cancelled: list[str] = []

    def history(self, tag_id=0, index=0, size=10, search=""):
        return {"data": [
            {"sessionId": "exp-1", "state": 0},
            {"sessionId": "exp-2", "state": 0},
            {"sessionId": "active-1", "state": 0},
            {"sessionId": "none-1", "state": 0},
            {"sessionId": "err-1", "state": 0},
            {"sessionId": "done-1", "state": 1},
        ]}

    def paper(self, session_id):
        if session_id == "err-1":
            raise ApiError("记录不存在")
        table = {
            "exp-1": {"state": 0, "timeLeft": -1},
            "exp-2": {"state": 0, "timeLeft": 0},
            "active-1": {"state": 0, "timeLeft": 900},
            "none-1": {"state": 0},
            "done-1": {"state": 1, "timeLeft": 0},
        }
        return table[session_id]

    def skip_this(self, session_id):
        self.skipped.append(session_id)

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)


class UnknownStateTest(unittest.TestCase):
    """P0：读不到 ≠ 已过期。未知态必须保留，且要能被界面看见。"""

    def test_is_expired_false_for_unknown(self):
        self.assertFalse(is_expired({"timeLeft": "abc"}), "非数值同样是未知，不能当成过期")

    def test_unknown_entries_are_kept_and_reported(self):
        client = UnknownClient()
        report = cleanup_sessions(client, log=lambda *_: None)

        self.assertEqual(sorted(report.unknown), ["err-1", "none-1"])
        self.assertEqual(report.expired, [])
        self.assertEqual(report.cleaned, [])
        self.assertEqual(client.skipped, [], "未知态一条都不能 skip")
        self.assertEqual(client.cancelled, [], "未知态一条都不能 cancel（cancel 即删记录）")
        self.assertEqual(report.active, ["active-1"])

    def test_include_active_never_deletes_unknown(self):
        client = UnknownClient()
        report = cleanup_sessions(client, include_active=True, log=lambda *_: None)

        self.assertEqual(client.cancelled, ["active-1"], "明确在时限内的才允许按开关清理")
        self.assertNotIn("none-1", client.cancelled)
        self.assertNotIn("err-1", client.cancelled)
        self.assertEqual(sorted(report.unknown), ["err-1", "none-1"])

    def test_mixed_rows_delete_exactly_the_expired(self):
        client = MixedClient()
        report = cleanup_sessions(client, log=lambda *_: None)

        self.assertEqual(report.expired, ["exp-1", "exp-2"])
        self.assertEqual(report.cleaned, ["exp-1", "exp-2"])
        self.assertEqual(client.skipped, ["exp-1", "exp-2"])
        self.assertEqual(client.cancelled, ["exp-1", "exp-2"])
        self.assertEqual(len(client.cancelled), len(report.expired), "删除次数必须正好等于过期条数")
        self.assertEqual(sorted(report.unknown), ["err-1", "none-1"])
        self.assertEqual(report.active, ["active-1"])

    def test_auto_cleanup_logs_unknown(self):
        logs: list[str] = []
        with TemporaryDirectory() as tmp:
            report = auto_cleanup(
                UnknownClient(), log=logs.append, temp_root=Path(tmp)
            )
        self.assertEqual(sorted(report.unknown), ["err-1", "none-1"])
        self.assertTrue(
            any("未知" in line for line in logs),
            f"未知态必须在日志里出现一次，实际日志：{logs}",
        )


if __name__ == "__main__":
    unittest.main()
