"""模型自检探针的预算与文案（2026-11-05）。

现场误判：日志出现「模型输出被 max_tokens=8 截断……必要时调大 [deepseek].max_tokens」，
被读成「求解时配置没生效」。实际是 `health_check()` 探针用 `max_tokens=8` 发「回复 ok」，
推理模型光思维链就超过 8 个 token，`finish_reason=length` 于是触发告警——
求解走的是配置里的 16000，从没被忽略。

这里锁住：探针预算 64、探针把 length 当正常、求解被截断仍然告警且文案要分得清。
"""

import json
import unittest
from unittest import mock

from trainsolve.solver.deepseek import DeepSeekClient, DeepSeekSolver
from trainsolve.solver.base import SolverError


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _body(content: str, finish_reason: str) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    }).encode()


def _install(fake, testcase):
    patcher = mock.patch("trainsolve.solver.deepseek.urllib.request.urlopen", side_effect=fake)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class HealthCheckProbeTest(unittest.TestCase):
    def _run_probe(self, body: bytes, *, max_tokens: int = 16000):
        logs: list[str] = []
        captured: list[dict] = []

        def fake(request, *args, **kwargs):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(body)

        _install(fake, self)
        client = DeepSeekClient("sk-test", base_url="http://example.invalid", max_tokens=max_tokens, log=logs.append)
        solver = DeepSeekSolver(client, log=logs.append)
        solver.health_check()
        return captured, logs

    def test_probe_uses_its_own_budget(self):
        captured, _ = self._run_probe(_body("ok", "stop"))
        self.assertEqual(captured[0]["max_tokens"], 64, "探针应有自己的预算，不该借用求解的 16000或 8")
        self.assertNotEqual(captured[0]["max_tokens"], 8, "8 个 token 对推理型模型必然被截断")

    def test_truncated_probe_reply_is_not_an_error_but_empty_is(self):
        """探针把预算全花在思维链上（content 为空、finish_reason=length）时：
        要报「空响应」这类**自检**错误，而不是求解那条「调大 max_tokens」的截断告警。"""
        logs: list[str] = []
        captured: list[dict] = []

        def fake(request, *args, **kwargs):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(_body("", "length"))

        _install(fake, self)
        client = DeepSeekClient("sk-test", base_url="http://example.invalid", max_tokens=16000, log=logs.append)
        solver = DeepSeekSolver(client, log=logs.append)

        with self.assertRaises(SolverError):
            solver.health_check()

        self.assertEqual(captured[0]["max_tokens"], 64)
        joined = "\n".join(logs)
        self.assertNotIn("截断", joined, f"探针不该打出求解的截断告警：{joined}")

    def test_probe_with_non_empty_reply_passes_even_if_truncated(self):
        captured, logs = self._run_probe(_body("ok", "length"))
        self.assertEqual(captured[0]["max_tokens"], 64)
        self.assertNotIn("截断", "\n".join(logs))


class ProbeEmptyReplyTest(unittest.TestCase):
    def test_empty_probe_reply_is_reported(self):
        logs: list[str] = []

        def fake(request, *args, **kwargs):
            return FakeResponse(_body("", "stop"))

        _install(fake, self)
        client = DeepSeekClient("sk-test", base_url="http://example.invalid", log=logs.append)
        solver = DeepSeekSolver(client, log=logs.append)

        with self.assertRaises(SolverError):
            solver.health_check()


class SolvePathStillWarnsTest(unittest.TestCase):
    def test_solve_truncation_still_warns_and_says_solve(self):
        logs: list[str] = []

        def fake(request, *args, **kwargs):
            return FakeResponse(_body("被截断的内容", "length"))

        _install(fake, self)
        client = DeepSeekClient("sk-test", base_url="http://example.invalid", max_tokens=16000, log=logs.append)

        client.chat([{"role": "user", "content": "写代码"}])

        joined = "\n".join(logs)
        self.assertIn("截断", joined, "求解路径被截断仍然必须告警")
        self.assertIn("求解", joined, "文案要让人分得清是求解还是探针")


if __name__ == "__main__":
    unittest.main()
