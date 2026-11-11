"""用真实响应 fixture 跑通「取卷 → 作答 → 组答案 → 提交 → 解析评分」全链路（不联网）。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from trainsolve import judge
from trainsolve.api import POST_ANSWER_OK, Session, total_score
from trainsolve.config import Config
from trainsolve.runner import Runner
from trainsolve.solver.stub import StubSolver

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
RECORD = json.loads((FIXTURES / "record_Q3373.json").read_text(encoding="utf-8"))
SCORE = json.loads((FIXTURES / "score_Q3373.json").read_text(encoding="utf-8"))
ANSWERS = json.loads((FIXTURES / "answers.json").read_text(encoding="utf-8"))
Q608_RECORD = json.loads((FIXTURES / "record_Q608.json").read_text(encoding="utf-8"))
Q608_CODE = (
    "#include <stdio.h>\n\nint main(void)\n{\n    short h1, l1, h2, l2;\n\n"
    '    if (scanf("a = %3hd%4hd, b = %3hd%4hd", &h1, &l1, &h2, &l2) != 4) {\n'
    "        return 1;\n    }\n\n"
    '    printf("a + b = %d\\n", (h1 + h2) * 10000 + (l1 + l2));\n    return 0;\n}\n'
)


def live_record() -> dict:
    """把已完成的 fixture 改成「正在作答」的会话。"""
    record = copy.deepcopy(RECORD)
    record["state"] = 0
    record["timeLeft"] = 3800
    record["score"] = 0.0
    record["buyCode"] = False
    return record


class FakeClient:
    """只实现 Runner 用到的接口，模拟平台行为。"""

    def __init__(self, record):
        self.record = copy.deepcopy(record)
        self.session = Session(sid="fake-sid", code="2000000001", expire=4102444800000)
        self.posted = []
        self.submitted = 0
        self.score_calls = 0
        self.skipped = []
        self.cancelled = []
        self.log = print

    def login(self, code, password):
        return self.session

    def available_tags(self, qbase_type, level=0):
        return [{"id": 3, "content": "U1", "available": True}]

    def choose(self, qbase_type, level=0, tag_id=-1):
        return "fakesession"

    def paper(self, session_id):
        return copy.deepcopy(self.record)

    def post_answer(self, session_id, answer_paper, verification_code):
        self.posted.append((copy.deepcopy(answer_paper), verification_code))
        return {"code": POST_ANSWER_OK, "timeLeft": 100}

    def submit(self, session_id):
        self.submitted += 1
        self.record["state"] = 1
        self.record["score"] = 11.0
        return {"code": 0}

    def wait_score(self, session_id, timeout=10, interval=0.1, log=None):
        return copy.deepcopy(SCORE)

    def wait_record(self, session_id, timeout=10, interval=0.1, log=None, should_stop=None):
        return copy.deepcopy(self.record)

    def score(self, session_id):
        self.score_calls += 1
        return copy.deepcopy(SCORE)

    def run_code(self, code, language, stdin_text):
        return [{"output": "compiler ok", "error": ""}, {"output": "", "error": ""}]

    def skip_this(self, session_id):
        self.skipped.append(session_id)
        self.record["state"] = 3
        return {"code": 0}

    def cancel_skip(self, session_id):
        self.cancelled.append(session_id)
        return {"code": 0}


def make_config(out_dir: str) -> Config:
    config = Config()
    config.platform.username = "2000000001"
    config.platform.password = "123456"
    config.run.out_dir = out_dir
    config.run.submit = True
    config.run.confirm = False
    config.run.judge = "local"
    config.run.max_repairs = 0
    config.run.count = 1
    config.run.sleep_between = 0
    return config


class AnswerPaperTest(unittest.TestCase):
    def test_fills_programming_bucket(self):
        config = make_config(tempfile.mkdtemp())
        runner = Runner(FakeClient(RECORD), StubSolver(ANSWERS), config)
        question = RECORD["paper"]["questions"][0]
        texts = {question["globalId"]: "int main(void){return 0;}\n"}
        paper = runner._answer_paper_for(RECORD, texts)
        self.assertEqual(len(paper["q4"]), 1)
        entry = paper["q4"][0]
        self.assertEqual(entry["answer"], "int main(void){return 0;}\n")
        self.assertEqual(entry["globalId"], question["globalId"])
        self.assertEqual(entry["qType"], 5)
        self.assertEqual(entry["keySequence"], [])
        self.assertEqual(paper["q0"], [])

    def test_keeps_existing_items(self):
        config = make_config(tempfile.mkdtemp())
        runner = Runner(FakeClient(RECORD), StubSolver(ANSWERS), config)
        paper = runner._answer_paper_for(RECORD, {})
        self.assertEqual(len(paper["q4"]), 1)
        self.assertEqual(paper["q4"][0]["answer"], "")


class ScoreParsingTest(unittest.TestCase):
    def test_total_score(self):
        self.assertEqual(total_score(SCORE), 11.0)

    def test_score_fields(self):
        detail = SCORE[0]["scoreResult"]
        self.assertEqual(detail["compareMode"], "exact")
        self.assertFalse(detail["directOutputCheat"])
        self.assertEqual(detail["dynamicTestScore"], 10.0)

    def test_summary_mentions_compare_mode(self):
        summary = Runner._score_summary(SCORE)
        self.assertIn("exact", summary)


@unittest.skipUnless(judge.find_compiler(), "需要本地 gcc/clang 才能跑编译判题")
class LocalJudgeIntegrationTest(unittest.TestCase):
    def test_reference_solution_passes(self):
        question = RECORD["paper"]["questions"][0]
        result = judge.judge_program(question, ANSWERS["3373"])
        self.assertTrue(result.passed, result.feedback)
        self.assertTrue(result.verified)

    def test_fullwidth_comma_fails_with_line_hint(self):
        question = RECORD["paper"]["questions"][0]
        broken = ANSWERS["3373"].replace('printf(",")', 'printf("\uff0c")')
        result = judge.judge_program(question, broken)
        self.assertFalse(result.passed)
        self.assertIn("第 1 行不一致", result.feedback)

    def test_missing_prompt_fails(self):
        question = RECORD["paper"]["questions"][0]
        broken = ANSWERS["3373"].replace('printf("Please enter the year:");', "")
        result = judge.judge_program(question, broken)
        self.assertFalse(result.passed)

    def test_compile_error_reported(self):
        question = RECORD["paper"]["questions"][0]
        result = judge.judge_program(question, "int main(void){ this is not c }")
        self.assertFalse(result.passed)
        self.assertIn("编译", result.feedback)

    def test_q608_scanf_format_solution_passes(self):
        question = Q608_RECORD["paper"]["questions"][0]
        result = judge.judge_program(question, Q608_CODE)
        self.assertTrue(result.passed, result.feedback)
        self.assertTrue(result.verified)


class QTypeFilterTest(unittest.TestCase):
    """题型白名单：抽到改错题(9) 就跳过，绝不提交。"""

    def _record(self, qtype: int) -> dict:
        record = live_record()
        record["paper"]["questions"][0]["qType"] = qtype
        record["answerPaper"]["q4"][0]["qType"] = qtype
        return record

    def test_wrong_qtype_is_skipped(self):
        client = FakeClient(self._record(9))
        config = make_config(tempfile.mkdtemp())
        config.run.qtypes = [5]
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)
        outcome = runner.run_once(1, 1)
        self.assertTrue(outcome.skipped)
        self.assertEqual(client.submitted, 0)
        self.assertEqual(client.posted, [])
        self.assertEqual(len(client.skipped), 1)
        self.assertIn("已跳过", outcome.detail)

    def test_allowed_qtype_is_answered(self):
        client = FakeClient(self._record(5))
        config = make_config(tempfile.mkdtemp())
        config.run.qtypes = [5]
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda _m: None)
        outcome = runner.run_once(1, 1)
        self.assertFalse(outcome.skipped)
        self.assertEqual(client.submitted, 1)

    def test_empty_allowlist_means_no_filter(self):
        client = FakeClient(self._record(9))
        config = make_config(tempfile.mkdtemp())
        config.run.qtypes = []
        config.run.require_verified = False
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)
        outcome = runner.run_once(1, 1)
        self.assertFalse(outcome.skipped)
        self.assertEqual(client.submitted, 1)


class ScriptedRunner(Runner):
    """把 run_once 换成脚本，专测主循环的调度逻辑。"""

    def __init__(self, script, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.script = list(script)
        self.calls = 0

    def run_once(self, index, total):
        self.calls += 1
        if not self.script:
            from trainsolve.api import NoQuestionAvailable

            raise NoQuestionAvailable("题库已抽空")
        return self.script.pop(0)


def ok_outcome(score=12.0, full=12.0):
    from trainsolve.runner import RunOutcome

    return RunOutcome(session_id=f"s{score}", ok=score >= full, score=score, full_score=full, state=1)


def bad_outcome(score=0.0, full=12.0):
    from trainsolve.runner import RunOutcome

    return RunOutcome(session_id=f"b{score}", ok=False, score=score, full_score=full, state=1)


def skipped_outcome():
    from trainsolve.runner import RunOutcome

    return RunOutcome(session_id="skip", ok=False, skipped=True, detail="题型不符")


class AutoLoopTest(unittest.TestCase):
    """全自动：完成一道自动抽下一道，失败不中断，带熔断。"""

    def _runner(self, script, **overrides):
        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.stop_on_failure = True
        for key, value in overrides.items():
            setattr(config.run, key, value)
        client = FakeClient(live_record())
        return ScriptedRunner(script, client, StubSolver(ANSWERS), config, log=lambda *_: None)

    def test_auto_continues_after_failure_until_pool_empty(self):
        runner = self._runner([ok_outcome(), bad_outcome(), ok_outcome()], count=0)
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 3)
        self.assertEqual([o.ok for o in outcomes], [True, False, True])
        self.assertEqual(runner.calls, 4)  # 第 4 次抽题时题库空了

    def test_auto_stops_after_consecutive_failures(self):
        runner = self._runner([bad_outcome() for _ in range(10)], count=0, max_consecutive_failures=3)
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(runner.calls, 3)

    def test_auto_resets_failure_counter_on_success(self):
        runner = self._runner(
            [bad_outcome(), bad_outcome(), ok_outcome(), bad_outcome(), ok_outcome()],
            count=0,
            max_consecutive_failures=3,
        )
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 5)

    def test_auto_stops_after_consecutive_skips(self):
        runner = self._runner([skipped_outcome() for _ in range(10)], count=0, max_consecutive_skips=2)
        outcomes = runner.run()
        self.assertEqual(outcomes, [])
        self.assertEqual(runner.calls, 2)

    def test_fixed_count_still_stops_on_failure(self):
        runner = self._runner([bad_outcome(), ok_outcome()], count=3)
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 1)  # stop_on_failure 生效

    def test_fixed_count_respects_target(self):
        runner = self._runner([ok_outcome() for _ in range(5)], count=2)
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 2)

    def test_dry_run_fixed_count_is_not_treated_as_failure(self):
        # dry-run 不提交，会话必然留在「未完成」；旧逻辑按 stop_on_failure 判失败，
        # 填了数量也只做第 1 题。现在应把数量跑满。
        runner = self._runner([bad_outcome() for _ in range(5)], count=3, dry_run=True)
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(runner.calls, 3)

    def test_dry_run_auto_loop_still_stops_on_consecutive_failures(self):
        # 全自动没有目标题数，熔断仍要生效，否则会无限抽题。
        runner = self._runner(
            [bad_outcome() for _ in range(10)], count=0, dry_run=True,
            max_consecutive_failures=3,
        )
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(runner.calls, 3)

    def test_fixed_count_keep_going_runs_past_failure(self):
        runner = self._runner(
            [bad_outcome(), ok_outcome(), ok_outcome()], count=3, stop_on_failure=False
        )
        outcomes = runner.run()
        self.assertEqual(len(outcomes), 3)


class WhitelistHintTest(unittest.TestCase):
    """指定标签却抽到被排除的题型：要给出可操作的提示，并尽快停下。"""

    def _record_with_qtype(self, qtype: int) -> dict:
        record = live_record()
        record["paper"]["questions"][0]["qType"] = qtype
        return record

    def test_fixed_tag_explains_why_it_skips(self):
        config = make_config(tempfile.mkdtemp())
        config.run.qtypes = [5, 6, 7]
        config.run.tag_id = 110
        logs: list[str] = []
        runner = Runner(FakeClient(self._record_with_qtype(9)), StubSolver(ANSWERS), config,
                        log=logs.append)
        outcome = runner.run_once(1, 1)
        self.assertTrue(outcome.skipped)
        joined = "\n".join(logs)
        self.assertIn("你指定了标签 110", joined)
        self.assertIn("不限制", joined)

    def test_fixed_tag_stops_after_two_whitelist_skips(self):
        script = [skipped_outcome(), skipped_outcome(), skipped_outcome()]
        runner = self._loop_runner(script, tag_id=110, max_consecutive_skips=5)
        outcomes = runner.run()
        self.assertEqual(outcomes, [])
        self.assertEqual(runner.calls, 2, "第二次跳过后就该停，别再烧配额")

    def test_random_tag_keeps_the_configured_skip_budget(self):
        script = [skipped_outcome() for _ in range(6)]
        runner = self._loop_runner(script, tag_id=-1, max_consecutive_skips=3)
        runner.run()
        self.assertEqual(runner.calls, 3)

    def _loop_runner(self, script, *, tag_id: int, max_consecutive_skips: int):
        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.stop_on_failure = True
        config.run.tag_id = tag_id
        config.run.max_consecutive_skips = max_consecutive_skips
        return ScriptedRunner(script, FakeClient(live_record()), StubSolver(ANSWERS),
                              config, log=lambda *_: None)


class HumanStoppedTest(unittest.TestCase):
    """打字途中按「停止」：本轮流立即中断，不提交（0.6.5）。"""

    def test_stop_marks_outcome_stopped(self):
        from trainsolve.humantype import HumanStopped

        class StoppingHuman:
            def answer(self, session_id, code, dry_run=False, should_stop=None):
                raise HumanStopped("已按「停止」中断打字")

        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.human_input = True
        client = FakeClient(live_record())
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None,
                        human=StoppingHuman())
        outcome = runner.run_once(1, 1)
        self.assertIn("已停止", outcome.detail)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.stopped, "停止的题必须标 stopped，不能被主循环记成未满分题（0.6.8）")
        self.assertEqual(client.submitted, 0)
        self.assertTrue(client.skipped, "停止的题应清理会话，不留「未完成」")

    def test_stop_during_thinking_delay_marks_stopped(self):
        """模拟思考等待期间点停止：同样要标 stopped（0.6.8）。"""
        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.human_input = True
        config.run.human_delay_min = 5
        config.run.human_delay_max = 5
        client = FakeClient(live_record())
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None, human=object())
        # 抽题阶段不停止，只有「等待思考」这一步被打断
        runner._sleep_unless_stopped = lambda seconds: True
        outcome = runner.run_once(1, 1)
        self.assertTrue(outcome.stopped)
        self.assertIn("已停止", outcome.detail)
        self.assertEqual(client.submitted, 0)
        self.assertEqual(client.posted, [], "等待中停止不该暂存答案")


class HumanInDryRunTest(unittest.TestCase):
    """真人模式下 dry-run / submit=false 也要真的打字（0.6.4 修）。

    旧逻辑在 dry_run / submit=false 时提前 return，导致「模拟真人打字」根本不发生，
    界面上配的随机思考时间与逐字输入都白设。
    """

    @staticmethod
    def _run(**overrides):
        from trainsolve.human import HumanResult

        typed: list[bool] = []

        class FakeHuman:
            def answer(self, session_id, code, dry_run=False, should_stop=None):
                typed.append(bool(dry_run))
                return HumanResult(ok=True, message="已输入并校验")

        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.human_input = True
        config.run.human_delay_min = 0
        config.run.human_delay_max = 0
        for key, value in overrides.items():
            setattr(config.run, key, value)
        client = FakeClient(live_record())
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None, human=FakeHuman())
        return runner, client, typed

    def test_dry_run_still_types_and_never_submits(self):
        runner, client, typed = self._run(dry_run=True, submit=False)
        outcome = runner.run_once(1, 1)
        self.assertEqual(typed, [True], "dry-run 应以「不提交」方式逐字输入")
        self.assertEqual(client.submitted, 0)
        self.assertEqual(client.posted, [], "dry-run 不该暂存答案")
        self.assertIn("dry-run", outcome.detail)

    def test_submit_false_still_types(self):
        runner, client, typed = self._run(dry_run=False, submit=False)
        outcome = runner.run_once(1, 1)
        self.assertEqual(typed, [True])
        self.assertEqual(client.submitted, 0)
        self.assertIn("submit=false", outcome.detail)

    def test_real_submit_still_types_in_submit_mode(self):
        runner, client, typed = self._run(dry_run=False, submit=True)
        runner.run_once(1, 1)
        self.assertEqual(typed, [False], "真提交时应真实输入并点提交")
        self.assertEqual(client.submitted, 0, "提交由浏览器完成，不走 API submit")

    def test_without_human_dry_run_still_short_circuits(self):
        runner, client, typed = self._run(dry_run=True, submit=False)
        runner.human = None
        outcome = runner.run_once(1, 1)
        self.assertEqual(client.submitted, 0)
        self.assertIn("未提交", outcome.detail)


class HumanDelayTest(unittest.TestCase):
    """模拟思考时间：每题在区间内随机等待，可被「停止」中断。"""

    def _runner(self, **overrides):
        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        for key, value in overrides.items():
            setattr(config.run, key, value)
        return Runner(FakeClient(live_record()), StubSolver(ANSWERS), config, log=lambda *_: None)

    def test_default_is_no_wait(self):
        self.assertEqual(self._runner()._human_delay_seconds(), 0.0)

    def test_random_stays_inside_range(self):
        runner = self._runner(human_delay_min=3, human_delay_max=12)
        samples = [runner._human_delay_seconds() for _ in range(50)]
        self.assertTrue(all(180.0 <= s <= 720.0 for s in samples), samples[:5])
        self.assertGreater(max(samples) - min(samples), 60, "应当随机而不是固定值")

    def test_only_upper_bound_means_zero_lower(self):
        runner = self._runner(human_delay_max=5)
        samples = [runner._human_delay_seconds() for _ in range(30)]
        self.assertTrue(all(0.0 <= s <= 300.0 for s in samples), samples[:5])

    def test_reversed_range_is_swapped(self):
        runner = self._runner(human_delay_min=10, human_delay_max=2)
        samples = [runner._human_delay_seconds() for _ in range(30)]
        self.assertTrue(all(120.0 <= s <= 600.0 for s in samples), samples[:5])

    def test_values_are_clamped_to_sixty_minutes(self):
        runner = self._runner(human_delay_min=100, human_delay_max=200)
        self.assertEqual(runner._human_delay_seconds(), 3600.0)

    def _human_runner(self, typed, slept, stop=False):
        from trainsolve.human import HumanResult

        class FakeHuman:
            def answer(self, session_id, code, dry_run=False, should_stop=None):
                typed.append(session_id)
                return HumanResult(ok=True, message="已输入并提交")

        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.score_timeout = 0.5
        config.run.human_input = True
        config.run.human_delay_min = 2
        config.run.human_delay_max = 2
        config.run.cleanup_abandoned = False
        client = FakeClient(live_record())
        runner = Runner(client, StubSolver(ANSWERS), config,
                        log=lambda *_: None, human=FakeHuman())

        def fake_sleep(seconds):
            slept.append(seconds)
            return stop

        runner._sleep_unless_stopped = fake_sleep
        return runner, client

    def test_waits_before_typing(self):
        typed, slept = [], []
        runner, client = self._human_runner(typed, slept)
        runner.run_once(1, 1)
        self.assertEqual(slept, [120.0], "每题等待应当恰好来自区间（2–2 分钟）")
        self.assertEqual(len(typed), 1, "等待之后才开始打字")
        self.assertEqual(client.submitted, 0, "真人模式下提交由浏览器完成，不走 API submit")

    def test_stop_during_wait_aborts_without_typing(self):
        typed, slept = [], []
        runner, client = self._human_runner(typed, slept, stop=True)
        outcome = runner.run_once(1, 1)
        self.assertEqual(slept, [120.0])
        self.assertEqual(typed, [], "等待中停止就不该再键入")
        self.assertEqual(client.submitted, 0)
        self.assertIn("已停止", outcome.detail)


class ExplodingSolver:
    """模拟网关 503：解题直接抛 SolverError。"""

    name = "boom"

    def __init__(self, message: str = "DeepSeek HTTP 503: no_healthy_account") -> None:
        self.message = message

    def solve(self, question):
        from trainsolve.solver.base import SolverError

        raise SolverError(self.message)

    def repair(self, question, previous, feedback):
        from trainsolve.solver.base import SolverError

        raise SolverError(self.message)


class FlakyHealthSolver(ExplodingSolver):
    def health_check(self):
        from trainsolve.solver.base import SolverError

        raise SolverError(self.message)


def infra_outcome():
    from trainsolve.runner import RunOutcome

    return RunOutcome(session_id="infra", ok=False, detail="解题失败：503", infra_error=True)


class InfraErrorTest(unittest.TestCase):
    """模型/网关故障：清理会话、不计入答错、连错即停、开跑前先自检。"""

    def test_solver_error_cleans_up_session(self):
        client = FakeClient(live_record())
        config = make_config(tempfile.mkdtemp())
        runner = Runner(client, ExplodingSolver(), config, log=lambda *_: None)

        outcome = runner.run_once(1, 1)

        self.assertTrue(outcome.infra_error)
        self.assertEqual(client.submitted, 0)
        self.assertEqual(len(client.skipped), 1)
        self.assertEqual(len(client.cancelled), 1)
        self.assertIn("解题失败", outcome.detail)

    def test_auto_stops_after_consecutive_infra_errors(self):
        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.max_consecutive_infra_errors = 2
        runner = ScriptedRunner([infra_outcome(), infra_outcome(), ok_outcome()],
                                FakeClient(live_record()), StubSolver(ANSWERS), config, log=lambda *_: None)
        outcomes = runner.run()
        self.assertEqual(outcomes, [])
        self.assertEqual(runner.calls, 2)

    def test_preflight_blocks_run_when_model_down(self):
        config = make_config(tempfile.mkdtemp())
        config.run.count = 0
        runner = ScriptedRunner([ok_outcome()], FakeClient(live_record()), FlakyHealthSolver(), config,
                                log=lambda *_: None)
        outcomes = runner.run()
        self.assertEqual(outcomes, [])
        self.assertEqual(runner.calls, 0)


class PostSubmitRepairTest(unittest.TestCase):
    """提交后未满分 → 按平台报错修复 → 重答，直到通过。"""

    FIXED = ANSWERS["3373"].replace("int main(void)", "int main(void) /* fixed */")

    def _record(self):
        record = live_record()
        record["fullScore"] = 12.0
        record["score"] = 0.0
        return record

    def _fail_results(self):
        return [{
            "globalId": RECORD["paper"]["questions"][0]["globalId"],
            "score": 0.0,
            "qType": 5,
            "scoreResult": {
                "state": -1,
                "score": 0.0,
                "dynamicTestScore": 0.0,
                "compilerErrorOutput": "",
                "directOutputCheat": False,
                "failedTestCase": {
                    "output": "Please enter the year:31,28,31,30,31,30",
                    "testCase": {"input": "2019\n", "output": "Please enter the year:31,28,31,30,31,30"},
                },
            },
        }]

    def _full_results(self, score=12.0):
        return [{
            "globalId": RECORD["paper"]["questions"][0]["globalId"],
            "score": score,
            "qType": 5,
            "scoreResult": {"state": -1, "score": score, "dynamicTestScore": 10.0, "failedTestCase": None},
        }]

    def _client(self, script):
        client = FakeClient(self._record())

        class Scripted(FakeClient):
            pass

        client.score_script = list(script)
        client.index = -1
        client.reanswers = 0
        client.last_results = []

        def submit(session_id):
            client.submitted += 1
            client.index += 1
            score, results = client.score_script[min(client.index, len(client.score_script) - 1)]
            client.last_results = results
            client.record["state"] = 1
            client.record["score"] = score
            return 0

        def score(session_id):
            return copy.deepcopy(client.last_results)

        def reanswer(session_id):
            client.reanswers += 1
            client.record["state"] = 0
            client.record["score"] = 0.0
            client.record["timeLeft"] = 3000
            return 0  # 平台业务码：0=成功

        client.submit = submit
        client.score = score
        client.reanswer = reanswer
        client.wait_record = lambda session_id, timeout=10, interval=0.1, log=None, should_stop=None: copy.deepcopy(client.record)
        return client

    def test_retries_until_full_score(self):
        client = self._client([(0.0, self._fail_results()), (12.0, self._full_results())])
        config = make_config(tempfile.mkdtemp())
        config.run.auto_fix_from_judge = True   # 默认已关：这里是显式打开以测「自动修复」这条特性
        config.run.max_reanswers = 5
        solver = StubSolver(ANSWERS, repairs=[self.FIXED])
        runner = Runner(client, solver, config, log=lambda *_: None)

        outcome = runner.run_once(1, 1)

        self.assertEqual(client.submitted, 2)
        self.assertEqual(client.reanswers, 1)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.score, 12.0)
        self.assertIn("重答 1 次", outcome.detail)
        # 第二次提交的应该是修复版代码
        self.assertIn("fixed", client.posted[-1][0]["q4"][0]["answer"])

    def test_stops_when_model_returns_same_code(self):
        client = self._client([(0.0, self._fail_results())])
        config = make_config(tempfile.mkdtemp())
        config.run.auto_fix_from_judge = True   # 默认已关：这里是显式打开以测「自动修复」这条特性
        config.run.max_reanswers = 5
        solver = StubSolver(ANSWERS, repairs=[ANSWERS["3373"]])  # 与上一版完全相同
        runner = Runner(client, solver, config, log=lambda *_: None)

        outcome = runner.run_once(1, 1)

        self.assertEqual(client.submitted, 1)
        self.assertEqual(client.reanswers, 0)
        self.assertFalse(outcome.ok)

    def test_disabled_when_max_reanswers_zero(self):
        client = self._client([(0.0, self._fail_results())])
        config = make_config(tempfile.mkdtemp())
        config.run.max_reanswers = 0
        runner = Runner(client, StubSolver(ANSWERS, repairs=[self.FIXED]), config, log=lambda *_: None)

        outcome = runner.run_once(1, 1)

        self.assertEqual(client.submitted, 1)
        self.assertEqual(client.reanswers, 0)
        self.assertFalse(outcome.ok)

    def test_stops_when_platform_refuses_reanswer(self):
        """平台返回业务码 1（拒绝重答）时必须停，不能当成成功继续发答案。"""
        client = self._client([(0.0, self._fail_results())])

        def refuse(session_id):
            client.reanswers += 1
            return 1  # 平台业务码 1 = 拒绝

        client.reanswer = refuse
        config = make_config(tempfile.mkdtemp())
        config.run.auto_fix_from_judge = True   # 默认已关：这里是显式打开以测「自动修复」这条特性
        config.run.max_reanswers = 5
        solver = StubSolver(ANSWERS, repairs=[self.FIXED])
        logs: list[str] = []
        runner = Runner(client, solver, config, log=logs.append)

        outcome = runner.run_once(1, 1)

        self.assertEqual(client.submitted, 1, "被拒绝后不应再次提交")
        self.assertEqual(client.reanswers, 1)
        self.assertFalse(outcome.ok)
        self.assertTrue(any("拒绝重新答题" in line for line in logs), logs)

    def test_revoked_submission_is_reported_as_unknown(self):
        """2026-11-06 真事故形态：reanswer 返回 1（称「拒绝」）但记录被重新打开、成绩清空。

        旧行为：只看业务码，报「已完成 0/12」；用户以为交卷了，其实已被撤回。
        新行为：状态优先于业务码 → 报「提交状态未知/被撤销」，ok=False，且不再提交。
        """
        client = self._client([(0.0, self._fail_results())])

        def revoke(session_id):
            client.reanswers += 1
            client.record["state"] = 0        # 平台把记录重新打开
            client.record["score"] = 0.0
            client.record["timeLeft"] = 3000
            return 1                          # 业务码仍然是「拒绝」

        client.reanswer = revoke
        config = make_config(tempfile.mkdtemp())
        config.run.auto_fix_from_judge = True
        config.run.max_reanswers = 5
        solver = StubSolver(ANSWERS, repairs=[self.FIXED])
        logs: list[str] = []
        runner = Runner(client, solver, config, log=logs.append)

        outcome = runner.run_once(1, 1)

        self.assertFalse(outcome.ok, "提交被撤销时绝不能声称成功")
        self.assertIn("提交状态未知", outcome.detail)
        self.assertIn("撤销", outcome.detail)
        self.assertEqual(client.submitted, 1, "被撤销后不许再提交")
        self.assertTrue(any("撤销" in line for line in logs), logs)


@unittest.skipUnless(judge.find_compiler(), "需要本地 gcc/clang")
class RunnerEndToEndOfflineTest(unittest.TestCase):
    def test_run_once_submits_and_reports_full_score(self):
        out_dir = tempfile.mkdtemp()
        client = FakeClient(live_record())
        config = make_config(out_dir)
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)

        outcome = runner.run_once(1, 1)

        self.assertEqual(client.submitted, 1)
        self.assertEqual(len(client.posted), 1)
        answer_paper, code = client.posted[0]
        self.assertEqual(len(code), 32)
        self.assertIn("#include <stdio.h>", answer_paper["q4"][0]["answer"])
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.score, 11.0)
        self.assertTrue(outcome.questions[0].judge_passed)

        artifact_dir = Path(out_dir) / "fakesession"
        self.assertTrue((artifact_dir / "Q3373.c").exists())
        self.assertTrue((artifact_dir / "statement.md").exists())
        self.assertTrue((artifact_dir / "summary.json").exists())
        summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["score"], 11.0)

    def test_dry_run_never_submits(self):
        client = FakeClient(live_record())
        config = make_config(tempfile.mkdtemp())
        config.run.dry_run = True
        config.run.submit = False
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)
        outcome = runner.run_once(1, 1)
        self.assertEqual(client.submitted, 0)
        self.assertEqual(client.posted, [])
        self.assertIn("dry-run", outcome.detail)


class RequireVerifiedTest(unittest.TestCase):
    """严格模式（--strict-verify）下题目没有可还原样例就不提交；默认则照常提交。"""

    def _record_without_sample(self) -> dict:
        record = live_record()
        record["paper"]["questions"][0]["content"] = "请编程输出若干行，题目没有给样例。"
        return record

    def test_unverifiable_question_is_skipped_in_strict_mode(self):
        client = FakeClient(self._record_without_sample())
        config = make_config(tempfile.mkdtemp())
        config.run.require_verified = True
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)
        outcome = runner.run_once(1, 1)
        self.assertEqual(client.submitted, 0)
        self.assertEqual(client.posted, [])
        self.assertIn("跳过提交", outcome.detail)

    def test_default_submits_unverifiable(self):
        self.assertFalse(Config().run.require_verified, "默认应当是允许提交（靠平台反馈修复）")
        client = FakeClient(self._record_without_sample())
        config = make_config(tempfile.mkdtemp())
        config.run.confirm = False
        runner = Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None)
        runner.run_once(1, 1)
        self.assertEqual(client.submitted, 1)


class UnverifiedSubmitGateTest(unittest.TestCase):
    """自检明确未通过的答案默认不提交；--force（allow_unverified_submit）才提交。"""

    BAD_CODE = "int main(void){ return 0; }\n"

    def _runner(self, client, **flags):
        config = make_config(tempfile.mkdtemp())
        for key, value in flags.items():
            setattr(config.run, key, value)
        return Runner(client, StubSolver({"3373": self.BAD_CODE}), config, log=lambda *_: None)

    def test_default_does_not_submit_unverified(self):
        client = FakeClient(live_record())
        outcome = self._runner(client).run_once(1, 1)
        self.assertEqual(client.submitted, 0, "自检未通过不该自动提交")
        self.assertEqual(client.posted, [])
        self.assertIn("自检未通过", outcome.detail)
        # 不提交的会话要清掉，别留「未完成」
        self.assertEqual(client.skipped, ["fakesession"])
        self.assertEqual(client.cancelled, ["fakesession"])

    def test_allow_unverified_submits_anyway(self):
        client = FakeClient(live_record())
        outcome = self._runner(client, allow_unverified_submit=True).run_once(1, 1)
        self.assertEqual(client.submitted, 1)
        self.assertEqual(client.score_calls, 1)

    def test_allow_unverified_defaults_off(self):
        self.assertFalse(Config().run.allow_unverified_submit)


class NotSubmittedNotFailureTest(unittest.TestCase):
    """按设置「不提交」不是答错：填了数量就该跑满（0.6.10 审计）。"""

    @staticmethod
    def _runner(**overrides):
        from trainsolve.human import HumanResult

        class OkHuman:
            def answer(self, session_id, code, dry_run=False, should_stop=None):
                return HumanResult(True, "已输入并校验", len(code), 1)

        config = make_config(tempfile.mkdtemp())
        config.run.sleep_between = 0
        config.run.count = 3
        human = None
        if overrides.pop("human", False):
            human = OkHuman()
            config.run.human_input = True
        for key, value in overrides.items():
            setattr(config.run, key, value)
        client = FakeClient(live_record())
        return Runner(client, StubSolver(ANSWERS), config, log=lambda *_: None, human=human)

    def test_no_submit_keeps_going(self):
        outcomes = self._runner(submit=False).run()
        self.assertEqual(len(outcomes), 3, "--no-submit 不是答错，填 3 题就该做 3 题")
        self.assertTrue(all(o.not_submitted for o in outcomes))

    def test_human_dry_run_keeps_going(self):
        outcomes = self._runner(human=True, human_dry_run=True).run()
        self.assertEqual(len(outcomes), 3, "human-dry-run 也只打字不提交，不是答错")
        self.assertTrue(all(o.not_submitted for o in outcomes))

    def test_dry_run_keeps_going(self):
        outcomes = self._runner(dry_run=True, submit=False).run()
        self.assertEqual(len(outcomes), 3, "dry-run 一直是跑满的（0.6.7 修），别回退")
        self.assertTrue(all(o.not_submitted for o in outcomes))


if __name__ == "__main__":
    unittest.main()
