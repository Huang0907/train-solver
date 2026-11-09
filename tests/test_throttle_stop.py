"""「停止」必须在限流等待期间生效（2026-11-05 现场事故）。

真实经过：抽到白名单外题型 → 自动 skip → 平台要求等 59s → 用户此时点「停止」，
但 `time.sleep(59)` 睡完直接 `continue` 又抽了一次，服务端因此多了一条记录、
多花一次配额。这里锁住三条：等待中停下就不再抽、停止后一次 choose 都不发、
没点停止时照旧重试。
"""

import unittest
from unittest import mock

from trainsolve.api import NoQuestionAvailable, TooFrequent
from trainsolve.config import Config
from trainsolve.runner import Runner, StopRequested


class ThrottleClient:
    """第一次抽题抛限流，第二次就该成功——用来数「到底抽了几次」。"""

    def __init__(self, wait: float = 5.0):
        self.choose_calls = 0
        self.wait = wait

    def available_tags(self, qbase_type, level=0):
        return [{"id": 7}]

    def choose(self, qbase_type, level=0, tag_id=-1):
        self.choose_calls += 1
        if self.choose_calls == 1:
            raise TooFrequent(self.wait)
        return "session-2"


def make_runner(client, stop_flag):
    runner = object.__new__(Runner)
    runner.client = client
    runner.config = Config()
    runner.config.run.tag_id = -1
    runner.log = lambda *_: None
    runner.should_stop = lambda: stop_flag[0]
    return runner


class StopDuringThrottleTest(unittest.TestCase):
    def test_stop_during_throttle_prevents_second_choose(self):
        client = ThrottleClient(wait=5.0)
        stop_flag = [False]
        runner = make_runner(client, stop_flag)

        def fake_sleep(seconds):
            stop_flag[0] = True          # 睡眠期间用户点了「停止」

        raised = False
        with mock.patch("trainsolve.runner.time.sleep", side_effect=fake_sleep):
            try:
                runner._pick_session()
            except StopRequested:
                raised = True

        self.assertEqual(client.choose_calls, 1, "停止之后一次 choose 都不许再发（改前是 2）")
        self.assertTrue(raised, "等待中被请求停止时应当抛 StopRequested，让 run() 收尾")

    def test_long_wait_is_sliced_so_stop_is_noticed_quickly(self):
        """59s 的等待必须切片睡，否则停止要等一整分钟才被看见。"""
        client = ThrottleClient(wait=59.0)
        stop_flag = [False]
        runner = make_runner(client, stop_flag)
        slices: list[float] = []

        def fake_sleep(seconds):
            slices.append(seconds)
            if len(slices) >= 3:
                stop_flag[0] = True

        with mock.patch("trainsolve.runner.time.sleep", side_effect=fake_sleep):
            with self.assertRaises(StopRequested):
                runner._pick_session()

        self.assertGreater(len(slices), 1, "应当切片睡，而不是一次睡 59s")
        self.assertLessEqual(max(slices), 0.25, f"单片不得超过 0.25s，实际 {max(slices)}")

    def test_stop_already_requested_before_pick(self):
        client = ThrottleClient()
        runner = make_runner(client, [True])

        with self.assertRaises(StopRequested):
            runner._pick_session()

        self.assertEqual(client.choose_calls, 0, "已经请求停止就不该再抽题")

    def test_no_stop_still_retries_after_wait(self):
        client = ThrottleClient(wait=5.0)
        runner = make_runner(client, [False])

        with mock.patch("trainsolve.runner.time.sleep", return_value=None):
            session = runner._pick_session()

        self.assertEqual(session, "session-2", "没点停止时应当照旧重试直到抽到题")
        self.assertEqual(client.choose_calls, 2)


class ThrottleGiveUpTest(unittest.TestCase):
    """连续限流到底要按「没题可做」收尾，不能把 TooFrequent 抛进 GUI worker。"""

    def _runner(self, client):
        runner = object.__new__(Runner)
        runner.client = client
        runner.config = Config()
        runner.config.run.tag_id = -1
        runner.logs = []
        runner.log = runner.logs.append
        runner.should_stop = lambda: False
        return runner

    def test_four_consecutive_throttles_give_up_cleanly(self):
        client = ThrottleClient(wait=5.0)
        client.choose = _always_throttled(client, TooFrequent(5.0))
        runner = self._runner(client)

        with mock.patch("trainsolve.runner.time.sleep", return_value=None):
            with self.assertRaises(NoQuestionAvailable) as ctx:
                runner._pick_session()

        self.assertIn("放弃", str(ctx.exception), "对外应当是「没题可做」，不是原始限流异常")
        self.assertEqual(client.choose_calls, 4, "连续 4 次限流后放弃；不许有第 5 次抽题")
        joined = "\n".join(runner.logs)
        self.assertIn("放弃", joined, f"日志里要有放弃字样：{joined}")


def _always_throttled(client, exc):
    def choose(qbase_type, level=0, tag_id=-1):
        client.choose_calls += 1
        raise exc
    return choose


class StopRequestedIsHandledTest(unittest.TestCase):
    def test_run_stops_cleanly_on_stop_requested(self):
        """run() 必须把 StopRequested 收成「正常停止」，不能让它当异常抛出去。"""
        runner = object.__new__(Runner)
        runner.log = lambda *_: None
        runner.should_stop = lambda: False
        runner.config = Config()
        runner.config.run.count = 3

        def fake_run_once(index, total):
            raise StopRequested("用户已请求停止")

        runner.run_once = fake_run_once
        runner._preflight_model = lambda: True

        outcomes = runner.run()

        self.assertEqual(outcomes, [], "收到停止请求应当直接收尾，不产出 outcome")


if __name__ == "__main__":
    unittest.main()
