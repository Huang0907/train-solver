"""刷题编排：登录 → 选题 → 解题 → 自检 → 提交 → 轮询评分 → 落盘。"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import judge as judge_mod
from . import prompts, verify
from .api import (
    POST_ANSWER_OK,
    QBASE_NAMES,
    QTYPE_BUCKET,
    REDO_OK,
    STATE_DONE,
    STATE_NAMES,
    STATE_SCORING,
    STATE_UNDONE,
    ApiError,
    NoQuestionAvailable,
    TooFrequent,
    TrainClient,
    WeekFinished,
    qtype_kind,
)
from .config import Config
from .solver.base import Solver, SolverError


class StopRequested(Exception):
    """用户已请求停止：不再发任何抽题请求，直接收尾（2026-11-05）。

    存在的理由：`_pick_session` 在限流等待里可能睡几十秒，等待期间用户点了「停止」。
    以前睡醒直接 `continue` 又抽一次，服务端多一条记录、多花一次配额。
    """


def _code_of(payload: Any) -> int | None:
    """从业务码载荷里取出整数 code；认不出就返回 None。

    2026-11-06：`submit` 的返回值以前被直接丢掉，事后无法判定提交是否成立。
    这里刻意不把「认不出」当成 0——那会把「不知道」伪装成「成功」。
    """
    if isinstance(payload, bool) or payload is None:
        return None
    if isinstance(payload, int):
        return payload
    if isinstance(payload, dict):
        for key in ("code", "result", "state"):
            if key in payload:
                try:
                    return int(payload[key])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return int(str(payload).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class QuestionOutcome:
    qid: Any
    qtype: int
    kind: str
    language: int
    code_or_answer: str
    judge_passed: bool | None = None
    judge_verified: bool = False
    judge_feedback: str = ""
    repairs: int = 0


@dataclass
class RunOutcome:
    session_id: str
    ok: bool
    state: int | None = None
    score: float = 0.0
    full_score: float = 0.0
    questions: list[QuestionOutcome] = field(default_factory=list)
    detail: str = ""
    artifact_dir: str = ""
    skipped: bool = False
    # 模型/网关故障（503、超时、鉴权失败等），不是“答错”，不该计入连续失败
    infra_error: bool = False

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, str(self.state))


class Runner:
    def __init__(
        self,
        client: TrainClient,
        solver: Solver,
        config: Config,
        *,
        log: Callable[[str], None] = print,
        confirm: Callable[[str], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        human=None,
    ) -> None:
        self.client = client
        self.solver = solver
        self.config = config
        self.log = log
        self.confirm = confirm or (lambda _msg: True)
        self.should_stop = should_stop or (lambda: False)
        # 人工模拟输入（浏览器逐字符打字）；None 表示走 API 提交
        self.human = human
        self._code_offset = 0
        # 供 UI 观察的钩子
        self.on_record: Callable[[dict], None] | None = None
        self.on_answer: Callable[[dict, QuestionOutcome], None] | None = None

    # ------------------------------------------------------------ 登录

    def login(self) -> None:
        cfg = self.config.platform
        # P1-c：装一个「sid 失效才重登」的钩子。即便这次复用缓存 sid 也要装，
        # 否则运行中失效就没得救；钩子只在出错时触发，不额外探活。
        if cfg.username and cfg.password and hasattr(self.client, "enable_auto_relogin"):
            self.client.enable_auto_relogin(cfg.username, cfg.password)
        if self.client.session and self.client.session.expire > time.time() * 1000:
            return
        session = self.client.login(cfg.username, cfg.password)
        self.log(f"登录成功：{session.name or cfg.username}（sid={session.sid[:8]}…）")

    # ------------------------------------------------------------ 选题

    SLEEP_SLICE = 0.25   # 可中断睡眠的切片长度（秒）
    MAX_THROTTLE_RETRIES = 3   # 连续限流超过这个次数就放弃本轮抽题

    def _sleep_unless_stopped(self, seconds: float) -> bool:
        """切睡 `seconds` 秒，每片都查一次停止标志；返回 True 表示期间被请求停止。"""
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self.should_stop():
                return True
            slice_ = min(self.SLEEP_SLICE, remaining)
            time.sleep(slice_)
            remaining -= slice_
        return self.should_stop()

    def _human_delay_seconds(self) -> float:
        """每题作答前的随机等待秒数：在 [human_delay_min, human_delay_max] 分钟里均匀取。

        两个都为 0（默认）时不等待；只填上限时下限按 0 处理；写反了自动交换。
        """
        run = self.config.run
        low = max(0.0, min(60.0, float(run.human_delay_min or 0.0)))
        high = max(0.0, min(60.0, float(run.human_delay_max or 0.0)))
        if low > high:
            low, high = high, low
        if high <= 0:
            return 0.0
        return random.uniform(low * 60.0, high * 60.0)

    def _pick_session(self) -> str:
        run = self.config.run
        if run.tag_id is not None and run.tag_id >= 0:
            candidates = [run.tag_id]
        else:
            tags = self.client.available_tags(run.qbase_type, run.level)
            if not tags:
                raise NoQuestionAvailable(f"{QBASE_NAMES.get(run.qbase_type)} 在难度 {run.level} 下没有可用标签")
            # 2026-11-06：可用标签按随机顺序依次尝试，`tagId=-1`（平台随机题）放最后兜底。
            # 以前是「-1 在最前面」，随机题可能抽到 qtypes 之外的题型，白耗一次配额 + 限流等待；
            # 而固定按 id 升序会让抽题集中在 id 靠前的标签上。
            ids: list[int] = []
            for tag in tags:
                try:
                    value = int(tag["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if value not in ids:
                    ids.append(value)
            random.shuffle(ids)
            candidates = ids + [-1]
            self.log(
                f"可用标签 {len(ids)} 个：随机顺序尝试；都不行再用随机题（tagId=-1）兜底"
            )

        errors: list[str] = []
        throttle_retries = 0
        for tag_id in candidates:
            while True:
                # 停止一旦被请求，连一次 choose 都不许再发。
                if self.should_stop():
                    self.log("  已请求停止，未再抽题")
                    raise StopRequested("用户已请求停止")
                try:
                    return self.client.choose(run.qbase_type, run.level, tag_id)
                except TooFrequent as exc:
                    # 跳过题之后平台会要求等一会再抽（实测 waitSeconds=60）
                    throttle_retries += 1
                    wait = max(5.0, float(exc.wait_seconds))
                    if throttle_retries > self.MAX_THROTTLE_RETRIES:
                        # 不再裸抛 TooFrequent（那会把 GUI worker 打崩，用户只看到一句看不懂的异常），
                        # 按「本轮没题可做」正常收尾。
                        self.log(
                            f"  ⚠ 连续 {throttle_retries} 次限流（每次约 {wait:.0f}s），本轮放弃抽题"
                        )
                        raise NoQuestionAvailable("连续限流后放弃") from exc
                    self.log(f"  ⏳ 选题过于频繁，等待 {wait:.0f}s 后重试（第 {throttle_retries} 次）")
                    if self._sleep_unless_stopped(wait):
                        # 睡完再查一次：等待期间点了停止，就绝不能再 continue 去抽题。
                        self.log("  已请求停止，未再抽题")
                        raise StopRequested("用户已请求停止")
                    continue
                except (NoQuestionAvailable, WeekFinished) as exc:
                    errors.append(f"tagId={tag_id}: {exc}")
                    if isinstance(exc, WeekFinished):
                        raise
                    break
        raise NoQuestionAvailable("；".join(errors) or "没有可用题目")

    # ------------------------------------------------------------ 解题

    def _answer_paper_for(self, record: dict, texts: dict[str, str]) -> dict:
        paper = json.loads(json.dumps(record.get("answerPaper") or {}))
        for bucket in ("q0", "q1", "q2", "q3", "q4", "q5"):
            paper.setdefault(bucket, [])
        for question in record["paper"]["questions"]:
            global_id = question.get("globalId")
            bucket = QTYPE_BUCKET.get(int(question.get("qType") or 0), "q4")
            items = paper[bucket]
            item = next((entry for entry in items if entry.get("globalId") == global_id), None)
            if item is None:
                item = {"answer": "", "keySequence": [], "inputSequence": [], "globalId": global_id, "qType": question.get("qType")}
                items.append(item)
            item.setdefault("keySequence", [])
            item.setdefault("inputSequence", [])
            item["answer"] = texts.get(global_id, "")
        return paper

    def _judge_local(self, question: dict, code: str, workdir: Path) -> judge_mod.JudgeResult:
        return judge_mod.judge_program(
            question,
            code,
            compiler=self.config.run.gcc or None,
            workdir=workdir,
            timeout=20.0,
        )

    def _judge_server(self, question: dict, code: str) -> judge_mod.JudgeResult:
        """用平台的 run-code 接口编译试跑，再本地比对期望输出（平台判题就是 exact 比对）。"""
        result = judge_mod.JudgeResult(passed=True)
        samples = judge_mod.extract_samples(str(question.get("content") or ""))
        sample = samples[0] if samples else None
        language = int(question.get("language") or 1) - 1
        try:
            output = self.client.run_code(code, language, (sample.stdin if sample and sample.stdin else ""))
        except ApiError as exc:
            # 平台试跑不可用属于「无法校验」，不是「自检未通过」，别把答案拦在提交门口
            result.passed = True
            result.verified = False
            result.checks.append(("服务端试跑", True, f"平台 run-code 不可用，未校验：{exc}"))
            return result
        if not output:
            result.passed = True
            result.verified = False
            result.checks.append(("服务端试跑", True, "run-code 返回空，未校验"))
            return result

        compile_stage = output[0] or {}
        if compile_stage.get("error"):
            result.passed = False
            result.checks.append(("编译", False, str(compile_stage.get("error"))))
            return result
        result.checks.append(("编译", True, ""))

        if sample and sample.usable and len(output) > 1:
            run_stage = output[1] or {}
            ok, diff = judge_mod.compare_any(sample, str(run_stage.get("output") or ""))
            result.checks.append(("样例输出", ok, diff))
            result.passed = ok
            result.verified = True
            if not ok and run_stage.get("error"):
                result.checks.append(("stderr", False, str(run_stage.get("error"))))
        else:
            result.verified = False
        return result

    def _check_program(self, question: dict, code: str, workdir: Path) -> judge_mod.JudgeResult:
        mode = self.config.run.judge
        if mode == "none":
            return judge_mod.JudgeResult(passed=True, verified=False, checks=[("判题", True, "已跳过")])
        if mode == "local":
            return self._judge_local(question, code, workdir)
        if mode == "server":
            return self._judge_server(question, code)
        # both：本地优先，本地没有编译器就退回服务端
        result = self._judge_local(question, code, workdir)
        if any("未找到 gcc" in detail for _name, ok, detail in result.checks if not ok):
            return self._judge_server(question, code)
        return result

    def solve_question(self, question: dict, workdir: Path) -> QuestionOutcome:
        qtype = int(question.get("qType") or 0)
        kind = qtype_kind(qtype)
        language = int(question.get("language") or 1)

        if kind == "design":
            raise SolverError(f"Q{question.get('id')} 是设计题（qType={qtype}），本工具不支持")
        if kind == "objective":
            answer = self.solver.solve(question)
            return QuestionOutcome(
                qid=question.get("id"), qtype=qtype, kind=kind, language=language, code_or_answer=answer.text
            )

        answer = self.solver.solve(question)
        code = answer.text
        outcome = QuestionOutcome(qid=question.get("id"), qtype=qtype, kind=kind, language=language, code_or_answer=code)

        for attempt in range(self.config.run.max_repairs + 1):
            result = self._check_program(question, code, workdir)
            outcome.judge_passed = result.passed
            outcome.judge_verified = result.verified
            outcome.judge_feedback = result.feedback
            self.log(
                f"  自检第 {attempt + 1} 轮：{'通过' if result.passed else '未通过'}"
                f"{'（已比对样例输出）' if result.verified else '（仅编译）'}"
            )
            if result.passed or attempt == self.config.run.max_repairs:
                break
            self.log("  回灌判题反馈，请求模型修复…")
            try:
                repaired = self.solver.repair(question, code, result.feedback)
            except SolverError as exc:
                self.log(f"  修复失败：{exc}")
                break
            code = repaired.text
            outcome.code_or_answer = code
            outcome.repairs += 1
        return outcome

    # ------------------------------------------------------------ 提交流程

    def _abandon_session(self, session_id: str, *, reason: str = "") -> None:
        """放弃一个不打算提交的会话：skip-this + cancel-skip 把记录删掉，避免留下「未完成」。"""
        try:
            self.client.skip_this(session_id)
            self.client.cancel_skip(session_id)
            self.log(f"  🧹 已清理会话（{reason or '放弃'}，不留未完成记录）")
        except (ApiError, OSError) as exc:
            self.log(f"  ⚠ 清理会话失败（可用 `trainsolve cleanup` 手动清）：{exc}")

    def _preflight_model(self) -> bool:
        """开跑前先探一下模型是否可用，避免模型挂了还在狂开会话。"""
        check = getattr(self.solver, "health_check", None)
        if not callable(check):
            return True
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权都可能
            self.log(f"✗ 模型自检失败：{exc}")
            self.log("  先修好模型/网关再跑；本次没有创建任何会话。")
            return False
        self.log("✓ 模型自检通过")
        return True

    def _post_answer(self, session_id: str, answer_paper: dict) -> bool:
        candidates: list[tuple[int, str]] = []
        seen: set[str] = set()

        def push(offset: int, code: str) -> None:
            if code not in seen:
                seen.add(code)
                candidates.append((offset, code))

        push(0, verify.verification_code(session_id))
        if self._code_offset:
            for signed in (self._code_offset, -self._code_offset):
                push(signed, verify.verification_code(session_id, verify.base_time(signed)))
        for hours in range(1, 9):
            for signed in (hours, -hours):
                push(signed, verify.verification_code(session_id, verify.base_time(signed)))

        for offset, code in candidates:
            response = self.client.post_answer(session_id, answer_paper, code) or {}
            state = response.get("code")
            if state == POST_ANSWER_OK:
                if offset and abs(offset) > self._code_offset:
                    self._code_offset = abs(offset)
                    self.log(f"  校验码时区偏移已校准为 {offset:+d} 小时")
                return True
            if state == 8:
                continue
            self.log(f"  暂存返回码 {state}：{response}")
            return False
        self.log("  所有时区偏移的校验码都被拒绝，请检查本机时间是否准确")
        return False

    def run_once(self, index: int, total: int) -> RunOutcome:
        self.login()
        session_id = self._pick_session()
        self.log(f"[{index}/{total}] 会话 {session_id} 创建成功")
        return self.execute(session_id, index, total)

    def peek(self) -> tuple[str, dict]:
        """开一个**新会话**并把题目打印出来，不解题、不暂存、不提交。

        ⚠ 这是写操作：`_pick_session()` → `client.choose()` → `POST api/student/choose-q`，
        点一次就占用一个题量名额、建出一个真实会话。只想读已有会话用 `peek_existing()`。
        """
        self.login()
        session_id = self._pick_session()
        return self._peek_session(session_id)

    def peek_existing(self, session_id: str) -> tuple[str, dict]:
        """只看**既有**会话的题面：只发 `GET api/student/record/<sid>`。

        除登录鉴权（`POST api/student/auth`，不产生业务数据；已有会话时不会发）外，
        全程零写请求：不 choose、不 post-answer、不 submit、不 skip。
        """
        if not session_id:
            raise ValueError("peek_existing 需要 sessionId")
        self.login()
        return self._peek_session(session_id)

    def _require_record(self, record, session_id: str) -> dict:
        """平台返回空体时给可诊断错误，而不是让 `record.get` 抛 AttributeError（2026-11-05）。"""
        if not isinstance(record, dict) or not record:
            raise ApiError(
                f"取答题记录失败：平台返回空值（session={session_id[:8]}…），"
                "可能 sid 已失效或记录已被清理"
            )
        return record

    def _peek_session(self, session_id: str) -> tuple[str, dict]:
        record = self._require_record(self.client.paper(session_id), session_id)
        directory = Path(self.config.run.out_dir) / session_id
        directory.mkdir(parents=True, exist_ok=True)
        self._dump(directory / "record.json", record)
        questions = (record.get("paper") or {}).get("questions") or []
        self._write_statement(directory, questions)
        self.log(f"会话 {session_id}（剩余 {record.get('timeLeft')}s）")
        for question in questions:
            self.log("=" * 70)
            self.log(f"Q{question.get('id')}  qType={question.get('qType')}  分值={question.get('point')}  "
                     f"语言={question.get('language')}")
            self.log(str(question.get("content") or ""))
            for option in question.get("options") or []:
                self.log(f"  - {option.get('content') or option.get('text') or option}")
            for sample in judge_mod.extract_samples(str(question.get("content") or "")):
                self.log(f"[样例] stdin={sample.stdin!r} expected={sample.expected_stdout!r} ({sample.note})")
        self.log(f"题目已保存到 {directory}")
        self.log(f"写好后执行：python -m trainsolve resume --session {session_id} --answers <答案文件>")
        return session_id, record

    def _submit_with_evidence(
        self, session_id: str, artifact_dir: Path, attempts: int, pre_score: list[dict] | None = None
    ) -> tuple[int | None, Any]:
        """调 `submit` 并把证据留下（0.5.5）。

        以前 `self.client.submit(session_id)` 的返回值被直接丢掉，于是「平台拒绝提交」
        和「提交成功」在日志里长得一模一样。现在：原始载荷落盘、业务码进日志，
        认不出码就明说「无法确认」，非 0/2 明说「提交未确认」。

        快照**不额外发网络请求**：提交前的分数用调用方上一轮已经取到的结果。
        """
        before = self.client.paper(session_id)
        self._dump(artifact_dir / f"record_before_submit_{attempts}.json", before)
        self._dump(artifact_dir / f"score_before_submit_{attempts}.json", list(pre_score or []))
        result = self.client.submit(session_id)
        code = _code_of(result)
        self._dump(artifact_dir / f"submit_{attempts}.json", {"attempt": attempts, "code": code, "raw": result})
        if code in (0, 2):
            self.log(f"  已提交（平台返回 code={code}），等待评分…")
        elif code is None:
            self.log("  已提交，平台未返回业务码（无法确认；下一步以记录状态为准）")
        else:
            self.log(f"  ⚠ 提交未确认：平台返回 code={code}；不做自动补偿，下一步以记录状态为准")
        return code, result

    def _reanswer_with_evidence(
        self, session_id: str, artifact_dir: Path, attempts: int, outcome: RunOutcome,
        pre_score: list[dict] | None = None,
    ) -> dict | None:
        """发起重答并复读记录：只有「记录确实回到未完成」才算重答成功（0.5.5）。

        2026-11-06 现场：`reanswer` 返回 code=1（旧代码解释为「平台拒绝」），
        但记录实际被重新打开成「未完成」，成绩被清空——提交被撤销而工具还在报「已完成」。
        所以这里**状态优先于业务码**：两者不一致一律按「提交状态未知」收尾。
        """
        if attempts == 1:
            self.log("  ⚠ 注意：本次重答会撤销刚才的提交（平台会把记录重新打开），成绩将被清空")
        before = self._require_record(self.client.paper(session_id), session_id)
        before_state = int(before.get("state", -1))
        self._dump(artifact_dir / f"record_before_reanswer_{attempts}.json", before)
        self._dump(artifact_dir / f"score_before_reanswer_{attempts}.json", list(pre_score or []))
        code = self.client.reanswer(session_id)
        after = self._require_record(self.client.paper(session_id), session_id)
        after_state = int(after.get("state", -1))
        self._dump(artifact_dir / f"score_after_reanswer_{attempts}.json", self.client.score(session_id))
        self._dump(artifact_dir / f"reanswer_{attempts}.json", {
            "attempt": attempts,
            "code": code,
            "state_before": before_state,
            "state_after": after_state,
        })
        self.log(
            f"  重答返回 code={code}：记录 {STATE_NAMES.get(before_state, before_state)}"
            f" → {STATE_NAMES.get(after_state, after_state)}"
        )
        if code != REDO_OK and after_state == STATE_UNDONE:
            outcome.ok = False
            outcome.detail = (
                f"提交状态未知：reanswer 返回 code={code}（平台称拒绝），但记录已回到「未完成」"
                "——刚才的提交被撤销"
            )
            self.log("  ⚠ " + outcome.detail)
            return None
        if code != REDO_OK:
            self.log(f"  平台拒绝重新答题（code={code}），记录状态未变，停止重答")
            return None
        if after_state != STATE_UNDONE:
            outcome.ok = False
            outcome.detail = (
                "提交状态未知：reanswer 返回成功，但记录没有回到「未完成」"
                f"（当前 {STATE_NAMES.get(after_state, after_state)}）"
            )
            self.log("  ⚠ " + outcome.detail)
            return None
        return after

    def execute(self, session_id: str, index: int = 1, total: int = 1) -> RunOutcome:
        run_config = self.config.run
        self.login()

        record = self._require_record(self.client.paper(session_id), session_id)
        state = int(record.get("state", STATE_UNDONE))
        time_left = int(record.get("timeLeft") or 0)
        questions = (record.get("paper") or {}).get("questions") or []
        self.log(f"  试题 {len(questions)} 道，状态={STATE_NAMES.get(state, state)}，剩余 {time_left}s")

        if state != STATE_UNDONE or time_left <= 0:
            return RunOutcome(
                session_id=session_id,
                ok=False,
                state=state,
                detail=f"会话不可作答（状态 {STATE_NAMES.get(state, state)}，剩余 {time_left}s）",
            )

        artifact_dir = Path(run_config.out_dir) / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self._dump(artifact_dir / "record.json", record)
        if self.on_record:
            self.on_record(record)

        # 题型白名单：抽到不想要的题型（例如改错题）就跳过，绝不提交
        allowed = {int(item) for item in (run_config.qtypes or [])}
        if allowed and questions:
            got = [int(question.get("qType") or 0) for question in questions]
            if any(item not in allowed for item in got):
                self.log(f"  抽到题型 {sorted(set(got))}，不在白名单 {sorted(allowed)} 内 → 跳过")
                if run_config.tag_id is not None and run_config.tag_id >= 0:
                    # 指定了标签还抽到被排除的题型：这个标签下的题就是这样，再抽也是白烧配额
                    self.log(
                        f"  ⚠ 你指定了标签 {run_config.tag_id}，它给的题型就是 {sorted(set(got) - allowed)}；"
                        "要么把「题型」改成「不限制（含改错题、程序填空）」，要么换个标签"
                    )
                try:
                    self.client.skip_this(session_id)
                    self.log("  已调用 skip-this（该记录标记为「跳过」）")
                except ApiError as exc:
                    self.log(f"  skip-this 失败：{exc}")
                skipped_outcome = RunOutcome(
                    session_id=session_id,
                    ok=False,
                    state=state,
                    detail=f"已跳过（题型 {sorted(set(got))} 不在白名单）",
                    artifact_dir=str(artifact_dir),
                    skipped=True,
                )
                return skipped_outcome

        outcome = RunOutcome(session_id=session_id, ok=False, state=state, artifact_dir=str(artifact_dir))
        outcome.full_score = float(record.get("fullScore") or 0)

        texts: dict[str, str] = {}
        try:
            for question in questions:
                qid = question.get("id")
                self.log(f"  → Q{qid} qType={question.get('qType')} 分值={question.get('point')}")
                question_outcome = self.solve_question(question, artifact_dir / f"build_Q{qid}")
                outcome.questions.append(question_outcome)
                texts[question.get("globalId")] = question_outcome.code_or_answer
                self._write_answer(artifact_dir, question, question_outcome)
                if self.on_answer:
                    self.on_answer(question, question_outcome)
        except SolverError as exc:
            outcome.detail = f"解题失败：{exc}"
            outcome.infra_error = True
            self.log(f"  ✗ {outcome.detail}")
            if run_config.cleanup_abandoned:
                self._abandon_session(session_id, reason="模型未给出答案")
            return outcome

        answer_paper = self._answer_paper_for(record, texts)
        self._dump(artifact_dir / "answerPaper.json", answer_paper)
        self._write_statement(artifact_dir, questions)

        # 真人模式下「打字」本身就是作答：dry-run / submit=false 也要先打完再决定交不交，
        # 否则界面上配的思考时间与逐字输入都不会发生（0.6.4 修）。
        human_ready = bool(
            self.human is not None
            and outcome.questions
            and outcome.questions[0].kind == "programming"
        )
        will_submit = bool(run_config.submit) and not run_config.dry_run

        if run_config.dry_run and not human_ready:
            outcome.detail = "dry-run：未提交"
            self.log("  dry-run：已保存题目与答案，未调用 submit")
            return outcome

        if not run_config.submit and not human_ready:
            self.log("  --no-submit：未提交（该会话将留在「未完成」状态）")
            outcome.detail = "未提交"
            return outcome

        unverified = [q for q in outcome.questions if q.kind == "programming" and q.judge_passed is False]
        unchecked = [
            q for q in outcome.questions
            if q.kind == "programming" and q.judge_passed is not False and not q.judge_verified
        ]

        # 自检明确未通过（编译不过 / 样例输出对不上）默认绝不提交：
        # 交上去基本就是 0 分，还白占一次作答机会。确实要交就 --force。
        if will_submit and unverified and not run_config.allow_unverified_submit:
            outcome.detail = (
                f"跳过提交：{len(unverified)} 道题自检未通过（默认不提交未过校验的答案；"
                "确要强制提交加 --force / 配置 allow_unverified_submit = true）"
            )
            self.log(f"  ⏭ {outcome.detail}")
            if run_config.cleanup_abandoned:
                self._abandon_session(session_id, reason="自检未通过，不提交")
            return outcome

        # 题目样例没还原出来（无法校验）默认仍提交，靠平台判分反馈修复；--strict-verify 才拦。
        if will_submit and unchecked and run_config.require_verified:
            outcome.detail = (
                f"跳过提交：{len(unchecked)} 道题无法校验输出（题面样例没还原出来）"
                "（strict-verify 模式；去掉 --strict-verify 即可照常提交）"
            )
            self.log(f"  ⏭ {outcome.detail}")
            if run_config.cleanup_abandoned:
                self._abandon_session(session_id, reason="无法校验，不提交")
            return outcome

        if will_submit and unchecked:
            self.log(
                f"  ⚠ {len(unchecked)} 道题题面没有可还原的样例，无法本地逐字节比对；"
                "按默认策略仍提交，由平台判分驱动修复"
            )

        # ---- 人工模拟输入分支：用浏览器逐字符打字 + 真实点击提交 ----
        used_human = False
        if human_ready:
            code = outcome.questions[0].code_or_answer
            delay = self._human_delay_seconds()
            if delay > 0:
                self.log(
                    f"  ⏳ 模拟思考：本题随机等待 {delay / 60:.1f} 分钟"
                    f"（区间 {run_config.human_delay_min:g}–{run_config.human_delay_max:g} 分钟）"
                )
                if self._sleep_unless_stopped(delay):
                    outcome.detail = "已停止（模拟思考等待中被中断，未作答）"
                    self.log("  ⏹ 随机等待期间收到停止，本题不提交")
                    if run_config.cleanup_abandoned:
                        self._abandon_session(session_id, reason="停止：模拟思考等待中未作答")
                    return outcome
            self.log(
                "  🧑‍💻 浏览器作答：逐字符输入（模拟真人）"
                + ("+ 真实点击提交" if will_submit else "（本题按设置不提交）")
            )
            try:
                result = self.human.answer(
                    session_id, code, dry_run=run_config.human_dry_run or not will_submit
                )
            except Exception as exc:  # noqa: BLE001 - 浏览器问题不该把整个流程炸掉
                outcome.detail = f"浏览器输入失败：{type(exc).__name__}: {exc}"
                self.log(f"  ✗ {outcome.detail}")
                if run_config.cleanup_abandoned:
                    self._abandon_session(session_id, reason="浏览器输入失败")
                return outcome

            if not result.ok:
                outcome.detail = f"浏览器输入失败：{result.message}"
                self.log(f"  ✗ {outcome.detail}")
                if run_config.cleanup_abandoned:
                    self._abandon_session(session_id, reason="打字校验未通过")
                return outcome

            self.log(f"  ✓ {result.message}")
            if run_config.human_dry_run:
                outcome.detail = "human dry-run：已按真人方式输入并校验，未提交"
                return outcome
            if not will_submit:
                outcome.detail = (
                    "dry-run：已按真人方式输入并校验，未提交"
                    if run_config.dry_run
                    else "submit=false：已按真人方式输入并校验，未提交"
                )
                self.log(f"  {outcome.detail}")
                return outcome
            used_human = True

        if not used_human:
            if not self._post_answer(session_id, answer_paper):
                outcome.detail = "暂存答案失败"
                return outcome
            self.log("  答案已暂存")

        attempts = 0
        results: list[dict] = []
        while True:
            if not used_human or attempts:
                # 首次真人提交由浏览器完成；后续重答走 API（真人重打一遍成本太高）
                submit_code, _submit_raw = self._submit_with_evidence(session_id, artifact_dir, attempts, results)
            else:
                submit_code = None
                self.log("  浏览器已提交，等待评分…")
            record = self.client.wait_record(session_id, timeout=run_config.score_timeout, log=self.log)
            results = self.client.score(session_id)

            if not record or int(record.get("state", STATE_SCORING)) in (STATE_UNDONE, STATE_SCORING):
                record = self._require_record(self.client.paper(session_id), session_id)
            outcome.state = int(record.get("state", STATE_SCORING))
            outcome.score = float(record.get("score") or 0)
            outcome.full_score = float(record.get("fullScore") or 0)
            outcome.ok = outcome.state == STATE_DONE and outcome.score >= outcome.full_score
            outcome.detail = self._score_summary(results)
            if attempts:
                outcome.detail += f"（重答 {attempts} 次）"
            if submit_code is not None and submit_code not in (0, 2) and outcome.state == STATE_UNDONE:
                outcome.ok = False
                outcome.detail = f"提交未确认：平台返回 code={submit_code}，记录仍是「未完成」"

            self._dump(artifact_dir / f"score_{attempts}.json", results)
            self._dump(artifact_dir / "record_after.json", record)
            self._dump(artifact_dir / "summary.json", {
                "sessionId": session_id,
                "state": outcome.state,
                "score": outcome.score,
                "fullScore": outcome.full_score,
                "attempts": attempts,
                "submit": {"code": submit_code},
                "questions": [asdict(item) for item in outcome.questions],
                "scoreResult": results,
            })

            # 未满分 → 按平台报错修复后再提交
            if (
                not run_config.auto_fix_from_judge
                or attempts >= run_config.max_reanswers
                or outcome.state != STATE_DONE
                or (outcome.full_score and outcome.score >= outcome.full_score)
            ):
                break

            feedback = self._judge_feedback(results)
            if not feedback:
                self.log("  平台没有给出可用的失败细节，停止重答")
                break

            attempts += 1
            self.log(
                f"  平台判分 {outcome.score:g}/{outcome.full_score:g}，"
                f"用判题反馈修复（第 {attempts}/{run_config.max_reanswers} 次重答）"
            )
            question = questions[0]
            global_id = question.get("globalId")
            previous_code = texts.get(global_id, "")
            try:
                repaired = self.solver.repair(question, previous_code, feedback)
            except SolverError as exc:
                self.log(f"  ✗ 修复失败：{exc}")
                break

            if repaired.text.strip() == previous_code.strip():
                self.log("  ⇥ 模型给出与上一版完全相同的代码，再提交也没意义，停止重答")
                break

            texts[global_id] = repaired.text
            if outcome.questions:
                outcome.questions[0].code_or_answer = repaired.text
                outcome.questions[0].repairs += 1
            self._write_answer(artifact_dir, question, outcome.questions[0])

            check = self._check_program(question, repaired.text, artifact_dir / f"build_Q{question.get('id')}_r{attempts}")
            self.log("  修复版自检：" + ("通过" if check.passed else "未通过"))
            if check.passed:
                outcome.questions[0].judge_passed = True
                outcome.questions[0].judge_verified = check.verified

            reopened = self._reanswer_with_evidence(session_id, artifact_dir, attempts, outcome, results)
            if reopened is None:
                break
            answer_paper = self._answer_paper_for(reopened, texts)
            if not self._post_answer(session_id, answer_paper):
                self.log("  修复版暂存失败，停止重答")
                break
            self.log("  修复版答案已暂存，重新提交…")

        return outcome

    # ------------------------------------------------------------ 落盘

    @staticmethod
    def _judge_feedback(results: list[dict]) -> str:
        """把平台评分里的失败信息整理成能给模型看的反馈。"""
        blocks: list[str] = []
        for item in results or []:
            detail = item.get("scoreResult") or {}
            if detail.get("compilerErrorOutput"):
                blocks.append("平台编译失败：\n" + str(detail["compilerErrorOutput"])[:1500])
            failed = detail.get("failedTestCase") or {}
            if failed:
                case = failed.get("testCase") or {}
                blocks.append(
                    "有测试用例不通过：\n"
                    f"  输入: {case.get('input')!r}\n"
                    f"  期望输出: {case.get('output')!r}\n"
                    f"  实际输出: {failed.get('output')!r}"
                )
            if detail.get("directOutputCheat"):
                blocks.append("平台判定你把样例输出硬编码了（directOutputCheat），必须改成通用算法。")
        return "\n\n".join(blocks)

    @staticmethod
    def _dump(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_answer(directory: Path, question: dict, outcome: QuestionOutcome) -> None:
        if outcome.kind == "programming":
            suffix = ".c"
            body = outcome.code_or_answer
        else:
            suffix = ".txt"
            body = outcome.code_or_answer
        (directory / f"Q{question.get('id')}{suffix}").write_text(body, encoding="utf-8")

    @staticmethod
    def _write_statement(directory: Path, questions: list[dict]) -> None:
        chunks = []
        for question in questions:
            options = question.get("options") or []
            option_text = "\n".join(
                f"  {chr(ord('A') + i)}. {o.get('content') or o.get('text') or o}" for i, o in enumerate(options)
            )
            chunks.append(
                f"# Q{question.get('id')}（qType={question.get('qType')}，分值 {question.get('point')}）\n\n"
                f"{question.get('content')}\n\n{option_text}\n"
            )
        (directory / "statement.md").write_text("\n\n---\n\n".join(chunks), encoding="utf-8")

    @staticmethod
    def _score_summary(results: list[dict]) -> str:
        parts = []
        for item in results:
            detail = item.get("scoreResult") or {}
            parts.append(
                "Q?{} score={} dynamic={} exact={}".format(
                    item.get("globalId", "")[:6], item.get("score"), detail.get("dynamicTestScore"), detail.get("compareMode")
                )
            )
            if detail.get("directOutputCheat"):
                parts.append("  ⚠ 被判为硬编码输出（directOutputCheat）")
            if detail.get("compilerErrorOutput"):
                parts.append(f"  编译错误：{str(detail.get('compilerErrorOutput'))[:200]}")
            if detail.get("failedTestCase"):
                parts.append(f"  失败用例：{str(detail.get('failedTestCase'))[:200]}")
        return "；".join(parts)

    # ------------------------------------------------------------ 主循环

    def run(self) -> list[RunOutcome]:
        """主循环。

        `count > 0`：做固定题数；
        `count <= 0`（--auto）：全自动——做完一道自动抽下一道，直到题库抽空、本周限额、
        连续失败/连续跳过达到上限，或用户点停止。
        """
        run_config = self.config.run
        auto = run_config.count <= 0
        target = None if auto else run_config.count
        outcomes: list[RunOutcome] = []
        done = 0
        failures = 0
        skips = 0
        infra_errors = 0
        attempts = 0
        dry_noted = False
        max_attempts = 10_000 if auto else max(10, run_config.count * 8)

        if not self._preflight_model():
            return outcomes

        if auto:
            self.log("🤖 全自动模式：完成一道自动抽下一道，直到没有题 / 限额 / 连续失败 / 手动停止")

        if run_config.auto_fix_from_judge:
            # 默认已关（0.5.5）。老配置里写着 true 的仍会生效，所以这里也必须明说后果。
            self.log(
                "  ⚠ 自动重答已开启（auto_fix_from_judge=true）："
                "重答会把已提交的记录重新打开、清空成绩——等于撤销刚才的提交"
            )

        while True:
            if self.should_stop():
                self.log("收到停止信号，结束。")
                break
            if target is not None and done >= target:
                break
            if attempts >= max_attempts:
                self.log("⚠ 尝试次数达到上限，停止。")
                break
            attempts += 1

            head = f"第 {done + 1} 题" if target is None else f"第 {done + 1}/{target} 题"
            self.log(f"\n===== {head} =====")
            try:
                outcome = self.run_once(done + 1, target or 0)
            except StopRequested:
                self.log("⏹ 已请求停止，未再抽题。")
                break
            except WeekFinished as exc:
                self.log(f"⏹ {exc}，停止。")
                break
            except NoQuestionAvailable as exc:
                self.log(f"⏹ {exc}，题库已抽空，停止。")
                break
            except ApiError as exc:
                self.log(f"✗ 接口错误：{exc} {exc.body[:200] if exc.body else ''}")
                outcome = RunOutcome(session_id="", ok=False, detail=str(exc))

            if outcome.skipped:
                skips += 1
                self.log(f"↷ 跳过：{outcome.detail}（自动抽下一题）")
                if (run_config.tag_id is not None and run_config.tag_id >= 0
                        and skips >= 2):
                    # 指定标签还连续抽到被排除的题型：再抽只会多烧一次配额 + 限流
                    self.log(
                        f"⚠ 指定标签 {run_config.tag_id} 连续 {skips} 次抽到被排除的题型，先停下："
                        "把「题型」改成「不限制」或换标签再跑。"
                    )
                    break
                if skips >= run_config.max_consecutive_skips:
                    self.log(
                        f"⚠ 连续跳过 {skips} 次，可能白名单挡掉了大部分题（当前白名单 "
                        f"{sorted(int(i) for i in (run_config.qtypes or []))}），停止。"
                    )
                    break
                continue
            skips = 0

            if outcome.infra_error:
                # 模型/网关故障：不算答错、不计入进度，连错两次就停（多半是网关挂了）
                infra_errors += 1
                self.log(f"⚠ 模型侧故障 {infra_errors} 次：{outcome.detail[:160]}")
                if infra_errors >= run_config.max_consecutive_infra_errors:
                    self.log("⏹ 连续出现模型故障，先停下来（检查网关/Key 后再跑）。")
                    break
                time.sleep(min(30, run_config.sleep_between * 2))
                continue
            infra_errors = 0

            done += 1
            outcomes.append(outcome)
            self._print_outcome(outcome)
            self._print_progress(done, outcomes, auto)

            if outcome.ok:
                failures = 0
            elif run_config.dry_run and target is not None:
                # dry-run 不调用 submit，会话必然停在「未完成」。这不是答错，
                # 不能拿它触发 stop_on_failure，否则填了数量也只会做第 1 题。
                if not dry_noted:
                    dry_noted = True
                    self.log("（dry-run 未提交、会话留「未完成」，不计为失败；按数量继续跑满）")
            else:
                failures += 1
                if target is not None and run_config.stop_on_failure:
                    self.log("（stop_on_failure=true，遇错即停；可用 --keep-going 或 --auto 继续）")
                    break
                if failures >= run_config.max_consecutive_failures:
                    self.log(f"⚠ 连续 {failures} 题未满分，先停下来看看（--max-failures 可调）。")
                    break

            if target is None or done < target:
                time.sleep(run_config.sleep_between)

        if auto:
            self._print_progress(done, outcomes, auto, final=True)
        return outcomes

    def _print_progress(self, done: int, outcomes: list[RunOutcome], auto: bool, final: bool = False) -> None:
        scored = [o for o in outcomes if o.full_score]
        full = sum(1 for o in scored if o.score >= o.full_score)
        total_score = sum(o.score for o in scored)
        total_full = sum(o.full_score for o in scored)
        prefix = "🏁 收工" if final else "📊 进度"
        self.log(
            f"{prefix}：已完成 {done} 题，满分 {full} 题，未满分 {len(scored) - full} 题，"
            f"累计 {total_score:g}/{total_full:g}"
        )

    def _print_outcome(self, outcome: RunOutcome) -> None:
        if outcome.session_id:
            mark = "✅" if outcome.ok else "⚠"
            self.log(
                f"{mark} 会话 {outcome.session_id}：{outcome.state_name} "
                f"得分 {outcome.score:g}/{outcome.full_score:g}  {outcome.detail}"
            )
        else:
            self.log(f"⚠ {outcome.detail}")
