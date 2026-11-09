"""提交与重答的证据链（0.5.5）。

事故（2026-11-06 真提交实测）：`submit` 之后平台判了 0/12，紧接着自动重答
（`reanswer` 返回 1）把记录重新打开成「未完成」，成绩被撤销；
而工具当时报的是「已完成 0/12」——因为 `submit` 的返回值被丢弃，
`reanswer` 之后也没有复读记录状态。这里把四个要求逐条钉住：

1. `submit` 返回值接住、落日志与落盘，非成功码明确报「提交未确认」；
2. `reanswer` 之后复读记录：状态与业务码不一致时，以「提交状态未知」收尾，不许声称已提交；
3. 第一次重答前打出「本次重答会撤销刚才的提交」；
4. `submit`/`reanswer` 前后都落 record 快照。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trainsolve.config import Config
from trainsolve.runner import RunOutcome, Runner, _code_of


_UNSET = object()


class FakePlatform:
    """用队列控制 `paper()` 返回的 state，并记录调用顺序。"""

    def __init__(self, states, *, submit_result=_UNSET, reanswer_code=0):
        self.states = list(states)
        self.submit_result = {"code": 0} if submit_result is _UNSET else submit_result
        self.reanswer_code = reanswer_code
        self.calls: list[str] = []

    def paper(self, session_id):
        self.calls.append("paper")
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"sessionId": session_id, "state": state, "score": 0.0, "fullScore": 12.0}

    def score(self, session_id):
        self.calls.append("score")
        return []

    def submit(self, session_id):
        self.calls.append("submit")
        return self.submit_result

    def reanswer(self, session_id):
        self.calls.append("reanswer")
        return self.reanswer_code


def make_runner(client):
    runner = object.__new__(Runner)
    runner.client = client
    runner.config = Config()
    runner.logs: list[str] = []
    runner.log = runner.logs.append
    runner.should_stop = lambda: False
    return runner


def outcome_submitted() -> RunOutcome:
    return RunOutcome(session_id="sid", ok=False, state=1, score=0.0, full_score=12.0)


class CodeExtractionTest(unittest.TestCase):
    def test_读得出_code_也认得空值(self):
        self.assertEqual(_code_of({"code": 0}), 0)
        self.assertEqual(_code_of({"code": "2"}), 2)
        self.assertEqual(_code_of(3), 3)
        self.assertIsNone(_code_of(None))
        self.assertIsNone(_code_of({"msg": "没有 code"}))
        self.assertIsNone(_code_of("不是数字"))


class SubmitEvidenceTest(unittest.TestCase):
    def _submit(self, result):
        client = FakePlatform([1], submit_result=result)
        runner = make_runner(client)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        code, raw = runner._submit_with_evidence("sid", directory, 0)
        dumped = json.loads((directory / "submit_0.json").read_text(encoding="utf-8"))
        snapshot = (directory / "record_before_submit_0.json").exists()
        score_snapshot = (directory / "score_before_submit_0.json").exists()
        return code, runner.logs, dumped, snapshot, score_snapshot

    def test_成功码要落日志与落盘(self):
        code, logs, dumped, snapshot, score_snapshot = self._submit({"code": 0})
        self.assertEqual(code, 0)
        self.assertTrue(any("code=0" in line for line in logs), logs)
        self.assertEqual(dumped["code"], 0)
        self.assertIn("raw", dumped)
        self.assertTrue(snapshot, "提交前必须落 record 快照")
        self.assertTrue(score_snapshot, "提交前必须落 score 快照")

    def test_非成功码必须报提交未确认(self):
        code, logs, dumped, _, _ = self._submit({"code": 5})
        self.assertEqual(code, 5)
        self.assertTrue(any("提交未确认" in line for line in logs), logs)
        self.assertEqual(dumped["code"], 5)

    def test_空返回体也必须说清楚无法确认(self):
        code, logs, _, _, _ = self._submit(None)
        self.assertIsNone(code)
        self.assertTrue(any("无法确认" in line for line in logs), logs)


class ReanswerVerificationTest(unittest.TestCase):
    def _run(self, states, code, attempts=1):
        client = FakePlatform(states, reanswer_code=code)
        runner = make_runner(client)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        outcome = outcome_submitted()
        reopened = runner._reanswer_with_evidence("sid", directory, attempts, outcome)
        return runner, outcome, reopened, directory

    def test_平台说拒绝但记录被重开_必须报提交被撤销(self):
        runner, outcome, reopened, directory = self._run([1, 0], code=1)

        self.assertIsNone(reopened, "这种状态绝不能继续重答")
        self.assertFalse(outcome.ok, "不许声称已提交成功")
        self.assertIn("提交状态未知", outcome.detail)
        self.assertIn("撤销", outcome.detail)
        self.assertTrue(any("撤销" in line for line in runner.logs), runner.logs)
        snap = json.loads((directory / "reanswer_1.json").read_text(encoding="utf-8"))
        self.assertEqual((snap["code"], snap["state_before"], snap["state_after"]), (1, 1, 0))
        for name in ("record_before_reanswer_1.json", "score_before_reanswer_1.json", "score_after_reanswer_1.json"):
            self.assertTrue((directory / name).exists(), f"缺少快照 {name}")

    def test_拒绝且状态没动_保持原有判分结论(self):
        runner, outcome, reopened, _ = self._run([1, 1], code=1)

        self.assertIsNone(reopened)
        self.assertEqual(outcome.detail, "", "状态没变就不该改判为未知")
        self.assertTrue(any("拒绝" in line for line in runner.logs), runner.logs)

    def test_成功且记录重新打开_可以继续重答(self):
        runner, outcome, reopened, _ = self._run([1, 0], code=0)

        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["state"], 0)
        self.assertEqual(outcome.detail, "")

    def test_返回成功但记录没打开_也算状态未知(self):
        runner, outcome, reopened, _ = self._run([1, 1], code=0)

        self.assertIsNone(reopened)
        self.assertIn("提交状态未知", outcome.detail)

    def test_第一次重答前要警告会撤销提交(self):
        runner, _, _, _ = self._run([1, 0], code=0, attempts=1)
        self.assertTrue(any("撤销" in line and "重答" in line for line in runner.logs), runner.logs)

    def test_第二次重答不重复那条警告(self):
        runner, _, _, _ = self._run([1, 0], code=0, attempts=2)
        self.assertFalse(any("撤销" in line and "重答" in line for line in runner.logs), runner.logs)


class AutoFixDefaultTest(unittest.TestCase):
    def test_自动重答默认关闭(self):
        self.assertFalse(
            Config().run.auto_fix_from_judge,
            "默认必须在关闭状态：它会撤销刚提交的成绩",
        )


if __name__ == "__main__":
    unittest.main()
