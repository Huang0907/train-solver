"""「停止」必须真的停下（0.6.6）。

0.6.5 只在**主循环开头**与**打字循环**里查停止标志，解题（一次模型调用可能几十秒到
几分钟）和评分（`wait_record` 最多 300 秒）两段完全不看：用户点了停止，工具照样把
已经算好的答案暂存、照样 `submit`——既违背「停止时直接中断本轮流程」，还可能白烧
一次作答机会。这里锁住四件事：

1. 解题跑完发现停止 → 不暂存、不提交，本题标 stopped；
2. 已经停止时连模型都不再请求（solve / repair 一次都不发）；
3. 等待评分期间停止 → 立刻返回，不再傻等 5 分钟，也不再重答；
4. 主循环拿到 stopped 的题就结束本轮，不再抽下一题、不再 sleep。
"""

import copy
import tempfile
import unittest

from trainsolve import judge
from trainsolve.api import POST_ANSWER_OK, Session, TrainClient
from trainsolve.config import Config
from trainsolve.runner import Runner, RunOutcome, StopRequested
from trainsolve.solver.base import Answer


def make_record(qtype: int = 5) -> dict:
    """最小可用卷面：一道编程题、状态「未完成」、剩 1 小时。"""
    return {
        "state": 0,
        "timeLeft": 3600,
        "fullScore": 12.0,
        "score": 0.0,
        "paper": {
            "questions": [
                {
                    "id": 3934,
                    "qType": qtype,
                    "globalId": "g-3934",
                    "language": 1,
                    "point": 12,
                    "content": "写一个程序。\n\n输入\n1\n\n输出\n2\n",
                }
            ]
        },
    }


class StopFlag:
    def __init__(self) -> None:
        self.value = False

    def __call__(self) -> bool:  # 供 runner.should_stop 使用
        return self.value


class CountingSolver:
    """记录调用次数；可以「在解题途中」把停止标志打开，模拟用户点停止。"""

    name = "counting"

    def __init__(self, flag: StopFlag, *, stop_during_solve: bool = False,
                 text: str = "int main(void){return 0;}\n"):
        self.flag = flag
        self.stop_during_solve = stop_during_solve
        self.text = text
        self.solve_calls = 0
        self.repair_calls = 0

    def solve(self, question) -> Answer:
        self.solve_calls += 1
        if self.stop_during_solve:
            self.flag.value = True      # 模型还在算的时候，用户点了「停止」
        return Answer(text=self.text)

    def repair(self, question, code, feedback) -> Answer:
        self.repair_calls += 1
        return Answer(text=self.text + "// fixed\n")


class StopClient:
    def __init__(self, record: dict, *, stop_in_wait: StopFlag | None = None):
        self.record = copy.deepcopy(record)
        self.session = Session(sid="fake-sid", code="2000000001", expire=4102444800000)
        self.posted = []
        self.submitted = 0
        self.score_calls = 0
        self.skipped = []
        self.cancelled = []
        self.wait_stops = 0
        self._stop_in_wait = stop_in_wait

    def login(self, code, password):
        return self.session

    def paper(self, session_id):
        return copy.deepcopy(self.record)

    def post_answer(self, session_id, answer_paper, verification_code):
        self.posted.append((copy.deepcopy(answer_paper), verification_code))
        return {"code": POST_ANSWER_OK, "timeLeft": 100}

    def submit(self, session_id):
        self.submitted += 1
        self.record["state"] = 1
        self.record["score"] = 12.0
        return {"code": 0}

    def wait_record(self, session_id, timeout=10, interval=0.1, log=None, should_stop=None):
        self.wait_stops += 1
        if self._stop_in_wait is not None:
            self._stop_in_wait.value = True     # 等评分时用户点了「停止」
        if should_stop is not None and should_stop():
            return {}
        return copy.deepcopy(self.record)

    def score(self, session_id):
        self.score_calls += 1
        return [{"scoreResult": {"score": 12.0}}]

    def skip_this(self, session_id):
        self.skipped.append(session_id)

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)


def make_config(out_dir: str) -> Config:
    config = Config()
    config.platform.username = "2000000001"
    config.platform.password = "123456"
    config.run.out_dir = out_dir
    config.run.submit = True
    config.run.dry_run = False
    config.run.confirm = False
    config.run.judge = "none"        # 不碰编译器，纯离线
    config.run.max_repairs = 0
    config.run.count = 1
    config.run.sleep_between = 0
    # 别让「遇错即停」替停止逻辑背锅：这两条默认值会把「失败后继续跑」的分支藏起来
    config.run.stop_on_failure = False
    config.run.max_consecutive_failures = 99
    config.api_key = "test-key"
    config.deepseek.api_key = "test-key"
    return config


class StopBeforeSubmitTest(unittest.TestCase):
    """解题途中点停止：算完也不许提交。"""

    def test_stop_during_solve_never_submits(self):
        flag = StopFlag()
        solver = CountingSolver(flag, stop_during_solve=True)
        config = make_config(tempfile.mkdtemp())
        client = StopClient(make_record())
        runner = Runner(client, solver, config, log=lambda *_: None, should_stop=flag)

        outcome = runner.execute("fake-sid")

        self.assertEqual(solver.solve_calls, 1, "停止是在解题过程中打的，这一次调用无法掐断")
        self.assertEqual(client.posted, [], "停止后不许再暂存答案")
        self.assertEqual(client.submitted, 0, "停止后一次 submit 都不许发")
        self.assertTrue(outcome.stopped)
        self.assertIn("已停止", outcome.detail)
        self.assertTrue(client.skipped, "不提交的会话应被清理，别留「未完成」")

    def test_already_stopped_never_calls_the_model(self):
        flag = StopFlag()
        flag.value = True
        solver = CountingSolver(flag)
        config = make_config(tempfile.mkdtemp())
        client = StopClient(make_record())
        runner = Runner(client, solver, config, log=lambda *_: None, should_stop=flag)

        outcome = runner.execute("fake-sid")

        self.assertEqual(solver.solve_calls, 0, "已经点了停止，不该再花一次模型调用")
        self.assertEqual(client.submitted, 0)
        self.assertTrue(outcome.stopped)

    def test_stop_during_repair_never_asks_model_again(self):
        flag = StopFlag()
        solver = CountingSolver(flag)
        config = make_config(tempfile.mkdtemp())
        config.run.max_repairs = 3
        client = StopClient(make_record())
        runner = Runner(client, solver, config, log=lambda *_: None, should_stop=flag)

        def failing_check(question, code, workdir):
            flag.value = True           # 自检不过的那一刻用户点了停止
            return judge.JudgeResult(passed=False, verified=True, checks=[("样例", False, "答案不对")])

        runner._check_program = failing_check
        outcome = runner.execute("fake-sid")

        self.assertEqual(solver.repair_calls, 0, "停止后不该再请求模型修复")
        self.assertEqual(client.submitted, 0)
        self.assertTrue(outcome.stopped)
        self.assertIn("已停止", outcome.detail)

    def test_stop_while_waiting_for_score_returns_at_once(self):
        flag = StopFlag()
        solver = CountingSolver(flag)
        config = make_config(tempfile.mkdtemp())
        client = StopClient(make_record(), stop_in_wait=flag)
        runner = Runner(client, solver, config, log=lambda *_: None, should_stop=flag)

        outcome = runner.execute("fake-sid")

        self.assertEqual(client.submitted, 1, "停止是在提交之后，这次提交本身合法")
        self.assertTrue(outcome.stopped)
        self.assertIn("评分未回", outcome.detail)
        self.assertEqual(client.score_calls, 0, "停止后不该再拉评分结果")
        self.assertEqual(client.skipped, [], "已提交的记录不许被清理")


class WaitRecordStopTest(unittest.TestCase):
    def test_wait_record_gives_up_when_stopped(self):
        client = TrainClient("http://127.0.0.1:1")   # 不会真的联网：停止先于第一次轮询
        flag = StopFlag()
        flag.value = True

        record = client.wait_record("sid", timeout=5, interval=0.01,
                                    log=lambda *_: None, should_stop=flag)

        self.assertEqual(record, {})


class MainLoopStopTest(unittest.TestCase):
    def test_stopped_outcome_ends_the_round(self):
        class OneShotRunner(Runner):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.calls = 0

            def run_once(self, index, total):
                self.calls += 1
                return RunOutcome(session_id="s1", ok=False, stopped=True, detail="已停止（未提交）")

        config = make_config(tempfile.mkdtemp())
        config.run.count = 5
        runner = OneShotRunner(StopClient(make_record()), CountingSolver(StopFlag()),
                               config, log=lambda *_: None)
        outcomes = runner.run()

        self.assertEqual(runner.calls, 1, "停下来的题不该再去抽下一题")
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].stopped)

    def test_sleep_between_is_interruptible(self):
        """题与题之间的 sleep 以前是死睡，停止后还要干等一次。"""
        import time

        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 30
        runner = Runner(StopClient(make_record()), CountingSolver(StopFlag()),
                        config, log=lambda *_: None)
        flag = StopFlag()
        runner.should_stop = flag
        flag.value = True

        started = time.time()
        runner._sleep_unless_stopped(config.run.sleep_between)
        self.assertLess(time.time() - started, 1.0, "停止后不该再睡满 sleep_between")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
