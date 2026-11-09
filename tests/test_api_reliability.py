"""网络层可靠性（2026-11-05）：方法感知重试 + paper() 空体不再掀进程。

P1-a：`_open` 原先与请求方法无关地重试，`choose-q`/`submit`/`half-redo` 这类
非幂等 POST 一次读超时就会在服务端多执行一遍。现在只有**幂等**请求才重试；
非幂等请求遇到「可能已经发出去了」的失败，一律停手并报「结果未知」。
连接建立失败（请求还没发出去）仍然可以无条件重试。

P1-b：`paper()` 拿到空体/非 dict 时直接返回，`runner` 里 `record.get(...)` 抛
`AttributeError` 掀掉进程。现在 `paper()` 自己抛 `ApiError`，进得了 `run()` 的
错误分支。
"""

import io
import unittest
import urllib.error
from unittest import mock

from trainsolve.api import ApiError, TrainClient
from trainsolve.config import Config
from trainsolve.runner import Runner


class FakeResponse:
    def __init__(self, body: bytes = b'{"ok":1}', status: int = 200):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def make_client(retries: int = 2) -> TrainClient:
    return TrainClient("http://example.invalid/train/", timeout=0.01, retries=retries, log=lambda *_: None)


class RetryPolicyTest(unittest.TestCase):
    """一次失败序列，数清楚 urlopen 到底被调用了几次。"""

    def _run(self, method, path, failure, *, retries=2, body=None):
        client = make_client(retries)
        calls: list[str] = []

        def fake_urlopen(request, *args, **kwargs):
            calls.append(request.get_method())
            raise failure

        with mock.patch("trainsolve.api.urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("trainsolve.api.time.sleep"):
                with self.assertRaises(ApiError) as ctx:
                    client.request(method, path, body=body)
        return calls, str(ctx.exception)

    def test_get_timeout_is_retried(self):
        calls, _ = self._run("GET", "api/student/list-tag", TimeoutError("timed out"))
        self.assertEqual(len(calls), 3, "GET 幂等，读超时必须重试到上限")

    def test_choose_q_read_timeout_is_not_retried(self):
        calls, message = self._run("POST", "api/student/choose-q", TimeoutError("timed out"))
        self.assertEqual(len(calls), 1, "choose-q 非幂等：读超时后不能重发，否则服务端多建一条记录")
        self.assertIn("结果未知", message)

    def test_submit_read_timeout_is_not_retried(self):
        calls, message = self._run("POST", "api/student/submit", TimeoutError("timed out"))
        self.assertEqual(len(calls), 1, "submit 不可撤销，超时后绝不能自动重发")
        self.assertIn("结果未知", message)

    def test_post_answer_read_timeout_is_retried(self):
        calls, _ = self._run("POST", "api/student/post-answer", TimeoutError("timed out"))
        self.assertEqual(len(calls), 3, "post-answer 幂等（同码同答案覆盖写），应当保留重试")

    def test_connection_refused_is_retried_even_for_choose_q(self):
        failure = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        calls, _ = self._run("POST", "api/student/choose-q", failure)
        self.assertEqual(len(calls), 3, "连接建立失败 = 请求根本没发出去，非幂等也可以重试")

    def test_connect_phase_timeout_is_retried(self):
        failure = urllib.error.URLError(TimeoutError("connect timed out"))
        calls, _ = self._run("POST", "api/student/choose-q", failure)
        self.assertEqual(len(calls), 3, "URLError 包住的超时发生在连接阶段，请求未发出，可重试")

    def test_get_5xx_is_retried(self):
        failure = urllib.error.HTTPError("http://x", 502, "bad gateway", {}, io.BytesIO(b"err"))
        calls, _ = self._run("GET", "api/student/list-tag", failure)
        self.assertEqual(len(calls), 3)

    def test_choose_q_5xx_is_not_retried(self):
        failure = urllib.error.HTTPError("http://x", 502, "bad gateway", {}, io.BytesIO(b"err"))
        calls, message = self._run("POST", "api/student/choose-q", failure)
        self.assertEqual(len(calls), 1, "服务端已经回过 5xx，非幂等请求不能盲目再来一次")
        self.assertIn("502", message)


class PaperBodyTest(unittest.TestCase):
    """P1-b：空体 / null / {} 必须是可诊断的 ApiError。"""

    def _paper(self, body: bytes):
        client = make_client()
        with mock.patch("trainsolve.api.urllib.request.urlopen", return_value=FakeResponse(body)):
            return client.paper("abcdef123456")

    def test_empty_body_raises_api_error(self):
        with self.assertRaises(ApiError) as ctx:
            self._paper(b"")
        self.assertIn("空", str(ctx.exception))

    def test_null_body_raises_api_error(self):
        with self.assertRaises(ApiError):
            self._paper(b"null")

    def test_empty_object_raises_api_error(self):
        with self.assertRaises(ApiError):
            self._paper(b"{}")

    def test_normal_record_is_returned(self):
        record = self._paper(b'{"state":0,"timeLeft":600}')
        self.assertEqual(record["timeLeft"], 600)


class PeekGuardTest(unittest.TestCase):
    """`_peek_session` 自己也要挡住非 dict，别让 AttributeError 冒到线程顶层。"""

    def test_peek_session_raises_api_error_when_record_is_none(self):
        runner = object.__new__(Runner)
        runner.client = mock.Mock()
        runner.client.paper.return_value = None
        runner.config = Config()
        runner.log = lambda *_: None

        with self.assertRaises(ApiError):
            runner._peek_session("abcdef123456")

    def test_execute_raises_api_error_when_record_is_none(self):
        """替身客户端直接返回 None 时，execute 也必须报 ApiError（不能 AttributeError）。"""
        from trainsolve.api import Session

        class NonePaperClient:
            session = None

            def login(self, username, password):
                self.session = Session(sid="sid", expire=9_999_999_999_999)
                return self.session

            def paper(self, session_id):
                return None

        runner = Runner(NonePaperClient(), None, Config(), log=lambda *_: None)

        with self.assertRaises(ApiError):
            runner.execute("abcdef123456")


def _sequenced(results):
    """按顺序返回预置结果（异常则抛出），并记录每次 urlopen 的方法。"""
    calls: list[str] = []

    def fake_urlopen(request, *args, **kwargs):
        calls.append(request.get_method())
        index = len(calls) - 1
        item = results[index] if index < len(results) else results[-1]
        if isinstance(item, Exception):
            raise item
        return item

    return fake_urlopen, calls


def _unauthorized():
    return urllib.error.HTTPError("http://example.invalid", 401, "unauthorized", {}, io.BytesIO(b""))


class ReloginTest(unittest.TestCase):
    """P1-c：sid 失效才重登一次并重试一次；启动不探活。"""

    def _client_with_hook(self, results):
        client = make_client()
        hooks: list[int] = []

        def hook() -> bool:
            hooks.append(1)
            return True

        client._relogin_hook = hook
        fake, calls = _sequenced(results)
        patcher = mock.patch("trainsolve.api.urllib.request.urlopen", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, calls, hooks

    def test_401_relogins_once_and_retries(self):
        client, calls, hooks = self._client_with_hook([_unauthorized(), FakeResponse(b'{"ok":1}')])

        self.assertEqual(client.get("api/student/list-tag"), {"ok": 1})
        self.assertEqual(len(hooks), 1, "401 之后应当重登一次")
        self.assertEqual(len(calls), 2, "重登后应当把这次请求重发一次")

    def test_repeated_401_gives_up_after_one_retry(self):
        client, calls, hooks = self._client_with_hook([_unauthorized(), _unauthorized()])

        with self.assertRaises(ApiError):
            client.get("api/student/list-tag")
        self.assertEqual(len(hooks), 1, "只允许重登一次，不能循环")
        self.assertEqual(len(calls), 2)

    def test_empty_record_relogins_once(self):
        client, calls, hooks = self._client_with_hook([FakeResponse(b""), FakeResponse(b'{"state":0,"timeLeft":600}')])

        record = client.paper("abcdef123456")
        self.assertEqual(record["timeLeft"], 600)
        self.assertEqual(len(hooks), 1)
        self.assertEqual(len(calls), 2)

    def test_empty_body_on_write_is_not_replayed(self):
        """非幂等写请求拿到空体：宁可不重试，也不能多建一条记录。"""
        client, calls, hooks = self._client_with_hook([FakeResponse(b"")])

        self.assertIsNone(client.request("POST", "api/student/choose-q", body={}))
        self.assertEqual(hooks, [], "空体不属于可重放信号")
        self.assertEqual(len(calls), 1)

    def test_failed_relogin_propagates_original_error(self):
        client = make_client()
        client._relogin_hook = lambda: False
        fake, calls = _sequenced([_unauthorized()])
        with mock.patch("trainsolve.api.urllib.request.urlopen", side_effect=fake):
            with self.assertRaises(ApiError):
                client.get("api/student/list-tag")
        self.assertEqual(len(calls), 1, "重登失败就不该再发一次")

    def test_enable_auto_relogin_returns_false_when_login_fails(self):
        client = make_client()
        client.enable_auto_relogin("u", "p")
        fake, _ = _sequenced([_unauthorized()])
        with mock.patch("trainsolve.api.urllib.request.urlopen", side_effect=fake):
            self.assertFalse(client._relogin_hook(), "登录也 401 时钩子必须返回 False，不能递归")


class RunnerReloginWiringTest(unittest.TestCase):
    """Runner 登录时必须把钩子装上（即便这次复用缓存 sid）。"""

    def test_login_installs_hook_even_with_cached_session(self):
        from trainsolve.api import Session

        class RecordingClient:
            def __init__(self):
                self.session = Session(sid="cached", expire=9_999_999_999_999)
                self.enabled: list[str] = []

            def enable_auto_relogin(self, code, password):
                self.enabled.append(code)

            def login(self, code, password):        # 不该被调用
                raise AssertionError("缓存 sid 还有效，不该重新登录")

        config = Config()
        config.platform.username = "2026000000"
        config.platform.password = "secret"
        client = RecordingClient()
        runner = Runner(client, None, config, log=lambda *_: None)

        runner.login()

        self.assertEqual(client.enabled, ["2026000000"], "复用缓存 sid 时也要装钩子，否则中途失效没救")


if __name__ == "__main__":
    unittest.main()
