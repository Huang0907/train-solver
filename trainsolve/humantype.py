"""模拟人类逐字符输入 —— 针对平台 CodeMirror 6 编辑器。

设计依据（见 docs/评估-模拟人类输入.md）：

* 平台禁止粘贴：`paste` / `drop` / `beforeinput(insertFromPaste)` / `contextmenu` 全被拦；
* 单次文档变更插入 **> 50 字符** 会触发 guardPaste，把**整篇文档回滚**；
* 每次 keydown 被记成 `keySequence=[ts, keyCode]`，每次输入被记成
  `inputSequence=[ts, typeCode, 长度]`（0=insertText、2=粘贴、3=拖拽…）。

因此这里的核心约束是：
1. **一字符一次输入**（永远不出现长度>1 的 insertText 事件，更不会碰 50 阈值）；
2. **节奏不是匀速**：每次作答随机抽一个基准速度、每行速度再浮动、字符间隔带对数正态抖动，
   行末停顿、偶发打错再退格，避免"匀速机器流"；
3. 打完必须回读文档逐字符比对，不一致就清空重打（不做半截修补，简单可靠）。
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

NBSP = "\u00a0"


# ---------------------------------------------------------------- 抽象层


class TyperIO(Protocol):
    """把"怎么敲"和"敲什么"分开，便于离线测试。"""

    def click_editor(self) -> None: ...

    def type_char(self, ch: str) -> None: ...

    def press(self, key: str) -> None: ...

    def read_doc(self) -> str | None: ...

    def focus_info(self) -> str: ...


# ---------------------------------------------------------------- 参数


@dataclass
class TypingProfile:
    """打字风格参数。默认值来自真人写代码的常见区间。"""

    cpm: float = 220.0                 # 基准速度（每分钟字符数）
    # 每次作答的基准速度在 cpm×(1±jitter) 内均匀随机；0 = 固定速度（旧行为）
    cpm_jitter: float = 0.25
    # 显式指定速度区间时优先用它（两个都 > 0 才生效），否则用 cpm ± jitter
    cpm_min: float = 0.0
    cpm_max: float = 0.0
    min_cpm: float = 60.0              # 有效速度兜底下限（防止极端抖动把行速压到不可用）
    line_speed_sigma: float = 0.12     # 每行速度相对基准的倍率抖动（对数正态）
    speed_drift: float = 0.05          # 行间速度的缓慢漂移幅度（均值回归随机游走）
    jitter_sigma: float = 0.45         # 每字符间隔的对数正态 sigma
    min_delay: float = 0.035           # 单字符最短间隔
    max_delay: float = 1.2             # 单字符最长间隔（再长交给停顿）
    line_pause: tuple[float, float] = (0.08, 0.35)
    think_pause_rate: float = 0.12     # 行末触发"思考停顿"的概率
    think_pause: tuple[float, float] = (0.4, 1.4)
    typo_rate: float = 0.02            # 打错概率（打错后立即退格）
    # 随机退格：每打完一个字符按该概率删掉刚打的 1..backspace_max 个字符再补回来。
    # 和 typo_rate 不同，它不插入错字，只是"回头改一下"，用来打断匀速机器流。
    backspace_rate: float = 0.01
    backspace_max: int = 2
    burst_rate: float = 0.15           # 小段连打概率
    burst_len: tuple[int, int] = (2, 5)
    burst_delay: float = 0.03
    tab_width: int = 4                 # Tab 换成空格（避免 CodeMirror 的 Tab 行为差异）

    def base_delay(self) -> float:
        return 60.0 / max(1.0, self.cpm)

    def speed_interval(self) -> tuple[float, float]:
        """本次作答的基准速度随机区间（lo, hi），已按 min_cpm 兜底。"""
        if self.cpm_min > 0 and self.cpm_max > 0:
            lo, hi = self.cpm_min, self.cpm_max
            if lo > hi:
                lo, hi = hi, lo
        elif self.cpm_jitter <= 0:
            lo = hi = self.cpm          # 显式关掉随机：固定速度（旧行为）
        else:
            jitter = min(0.95, self.cpm_jitter)
            lo = self.cpm * (1.0 - jitter)
            hi = self.cpm * (1.0 + jitter)
        lo = max(self.min_cpm, lo)
        hi = max(lo, hi)
        return lo, hi


class TimingModel:
    """可注入随机源，测试可复现。

    速度是三层随机的：每次作答抽一个基准速度（`begin_session`），
    每行在这个基准上乘一个抖动倍率（`begin_line`），每个字符再叠对数正态抖动。
    这样同一个人不同题、同一题不同行都不会是匀速机器流。
    """

    def __init__(self, profile: TypingProfile | None = None, rng: random.Random | None = None) -> None:
        self.profile = profile or TypingProfile()
        self.rng = rng or random.Random()
        self.session_cpm: float | None = None   # 本次作答抽到的基准速度
        self._line_factor = 1.0                 # 当前行的速度倍率
        self._drift = 1.0                       # 行间缓慢漂移（均值回归到 1）

    # -------------------------------------------------------- 速度随机化

    def begin_session(self) -> float:
        """每当作答开始时抽一个基准速度，返回本次速度（字符/分钟）。"""
        lo, hi = self.profile.speed_interval()
        self.session_cpm = self.rng.uniform(lo, hi)
        self._drift = 1.0
        self._line_factor = 1.0
        return self.session_cpm

    def begin_line(self) -> None:
        """每行开头更新一次速度漂移与行倍率。"""
        p = self.profile
        # 均值回归随机游走：相邻行速度不会跳太远，但整篇会缓慢起伏
        self._drift = 1.0 + (self._drift - 1.0) * 0.6 + self.rng.gauss(0.0, p.speed_drift)
        self._drift = max(0.6, min(1.5, self._drift))
        factor = math.exp(self.rng.gauss(0.0, p.line_speed_sigma)) * self._drift
        self._line_factor = max(0.5, min(2.0, factor))

    def session_speed(self) -> float:
        return self.session_cpm if self.session_cpm is not None else self.profile.cpm

    def current_cpm(self) -> float:
        """当前行实际使用的速度（基准 × 行倍率，带兜底）。"""
        lo, hi = self.profile.speed_interval()
        cpm = self.session_speed() * self._line_factor
        return max(lo * 0.6, min(hi * 1.6, max(self.profile.min_cpm, cpm)))

    # -------------------------------------------------------- 逐字符/逐行节奏

    def char_delay(self) -> float:
        p = self.profile
        raw = 60.0 / max(1.0, self.current_cpm()) * math.exp(self.rng.gauss(0, p.jitter_sigma))
        return max(p.min_delay, min(p.max_delay, raw))

    def line_delay(self) -> float:
        p = self.profile
        delay = self.rng.uniform(*p.line_pause)
        if self.rng.random() < p.think_pause_rate:
            delay += self.rng.uniform(*p.think_pause)
        return delay

    def should_typo(self) -> bool:
        return self.rng.random() < self.profile.typo_rate

    def burst(self) -> int:
        p = self.profile
        if self.rng.random() >= p.burst_rate:
            return 0
        return self.rng.randint(*p.burst_len)

    def typo_char(self) -> str:
        return self.rng.choice("abcdefghijklmnopqrstuvwxyz0123456789;,(){}")

    def should_backspace(self) -> bool:
        return self.rng.random() < self.profile.backspace_rate

    def backspace_count(self) -> int:
        return self.rng.randint(1, max(1, int(self.profile.backspace_max)))


# ---------------------------------------------------------------- 文本处理


def describe_human_pacing(run) -> str:
    """把「随机思考 / 随机退格」配置压成一行启动提示；全关时返回空串。

    只做展示，不参与判定：区间写反按交换后的顺序显示，便于用户核对设置
    到底有没有生效（0.6.1 起浏览器就绪日志里带这一段）。
    """
    parts: list[str] = []
    low = max(0.0, min(60.0, float(getattr(run, "human_delay_min", 0.0) or 0.0)))
    high = max(0.0, min(60.0, float(getattr(run, "human_delay_max", 0.0) or 0.0)))
    if low > high:
        low, high = high, low
    if high > 0:
        parts.append(f"每题随机思考 {low:g}–{high:g} 分钟")
    rate = max(0.0, min(0.3, float(getattr(run, "backspace_rate", 0.0) or 0.0)))
    if rate > 0:
        count = max(1, min(5, int(getattr(run, "backspace_max", 2) or 1)))
        parts.append(f"随机退格 {rate:g} / 每次最多 {count} 字符")
    return "；".join(parts)


def normalize_code(text: str) -> str:
    """把两侧文本归一到可比较形态（NBSP→空格、去行尾空白、去尾部空行）。"""
    if text is None:
        return ""
    out = text.replace(NBSP, " ")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in out.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def prepare_for_typing(code: str, tab_width: int = 4) -> str:
    """打字前把 CRLF / Tab 规整掉（C 代码里 Tab 与空格等价，评测只看行为）。"""
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\t", " " * tab_width)


def first_diff(expected: str, actual: str) -> tuple[int, int, str, str]:
    """返回 (行号, 列号, 期望行, 实际行)，都从 1 开始；完全一致时返回 (0,0,'','')。"""
    exp_lines = expected.split("\n")
    act_lines = actual.split("\n")
    for index in range(max(len(exp_lines), len(act_lines))):
        want = exp_lines[index] if index < len(exp_lines) else "<缺少该行>"
        got = act_lines[index] if index < len(act_lines) else "<缺少该行>"
        if want != got:
            column = next(
                (i + 1 for i, (a, b) in enumerate(zip(want, got)) if a != b),
                min(len(want), len(got)) + 1,
            )
            return index + 1, column, want[:80], got[:80]
    return 0, 0, "", ""


# ---------------------------------------------------------------- 打字器


class HumanTyper:
    """把一段代码"像人一样"打进编辑器，并校验结果。"""

    def __init__(
        self,
        io: TyperIO,
        profile: TypingProfile | None = None,
        *,
        log: Callable[[str], None] = print,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.io = io
        self.profile = profile or TypingProfile()
        self.timing = TimingModel(self.profile, rng)
        self.log = log
        self.sleep = sleep
        # 统计（写进日志，便于判断真机行为和离线测试是否一致）
        self.indent_fixes = 0
        self.line_repairs = 0
        self.rounds_used = 0
        self.speed_cpm = 0.0   # 本次（最近一轮）实际使用的基准速度

    # -------------------------------------------------------- 基础动作

    def clear_all(self) -> None:
        """清空编辑器（Ctrl+A + Backspace；删除不计入 50 字符插入阈值）。"""
        self.io.click_editor()
        self.sleep(0.15)          # 等点击真正把焦点给到编辑器
        self.io.press("Control+a")
        self.io.press("Backspace")
        self.sleep(0.05)

    def _pause_char(self, burst_left: int) -> int:
        if burst_left > 0:
            self.sleep(self.profile.burst_delay)
            return burst_left - 1
        self.sleep(self.timing.char_delay())
        return self.timing.burst()

    def _type_body(self, text: str, *, allow_typos: bool = False) -> None:
        """逐字符打一段内容（不含换行），保留抖动 / 连打 / 偶发打错再退格。

        另有「随机退格」：按概率删掉刚打的 1..N 个字符再原样补回，
        内容不变，只是多几次真实按键与回读前的停顿，像人回头改一下。
        """
        burst_left = 0
        typed = 0
        for ch in text:
            burst_left = self._pause_char(burst_left)
            if allow_typos and self.timing.should_typo():
                self.io.type_char(self.timing.typo_char())
                self.sleep(self.timing.char_delay())
                self.io.press("Backspace")
            self.io.type_char(ch)
            typed += 1
            if allow_typos and typed >= 2 and self.timing.should_backspace():
                count = min(self.timing.backspace_count(), typed)
                for _ in range(count):
                    self.io.press("Backspace")
                    self.sleep(self.timing.char_delay())
                for fix_ch in text[typed - count:typed]:
                    self.io.type_char(fix_ch)
                    self.sleep(self.timing.char_delay())

    # -------------------------------------------------------- 自动缩进 / 行内校正

    @staticmethod
    def _leading_spaces(line: str) -> int:
        count = 0
        for ch in line:
            if ch in (" ", NBSP):
                count += 1
            else:
                break
        return count

    def _read(self) -> str:
        return (self.io.read_doc() or "").replace(NBSP, " ")

    def _last_line(self) -> str:
        """光标所在行。打字始终在文末追加，所以就是文档最后一行。"""
        return self._read().split("\n")[-1]

    def _settle(self, *, timeout: float = 1.5, interval: float = 0.035) -> str:
        """回读直到连续两次一致。

        真机上 `Input.dispatchKeyEvent` 由渲染进程的输入线程排队，而 CDP 求值
        可能先于按键生效返回（实测会读到"上一行的状态"）。两条读一致才算稳定，
        否则缩进对齐/行内校正会按旧值退格，把行拼在一起。
        """
        import time as _time

        previous = self._read()
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            self.sleep(interval)
            current = self._read()
            if current == previous:
                return current
            previous = current
        return previous

    def _align_indent(self, want: int, *, max_steps: int = 16) -> None:
        """把当前行行首空格数调到 want（回车会自动补缩进，可能是多也可能是少）。"""
        fixed = False
        for _ in range(max_steps):
            actual = self._leading_spaces(self._settle().split("\n")[-1])
            if actual == want:
                if fixed:
                    self.indent_fixes += 1
                return
            if actual > want:
                self.io.press("Backspace")
            else:
                self.io.type_char(" ")
            fixed = True
        self.indent_fixes += 1

    def _repair_line(self, want: str, *, max_steps: int = 90) -> bool:
        """回读当前行，和目的一行不一致就逐字符退格/补打，直到一致。

        每次只动一个字符并重新回读，因此对「丢键」「多插入字符」都收敛；
        限制步数，避免读值异常时死循环。
        """
        want_cmp = want.rstrip(" ")
        step = 0
        fixed = False
        while step < max_steps:
            got = self._settle().split("\n")[-1]
            if got.rstrip(" ") == want_cmp:
                if fixed:
                    self.line_repairs += 1
                return True
            common = 0
            for a, b in zip(got, want):
                if a != b:
                    break
                common += 1
            if len(got) > common:
                self.io.press("Backspace")
            elif len(got) < len(want):
                self.io.type_char(want[len(got)])
            else:  # 长度相同但内容不同：退一格再补
                self.io.press("Backspace")
            fixed = True
            step += 1
            self.sleep(0.012)
        self.line_repairs += 1
        return self._last_line().rstrip(" ") == want_cmp

    def _newline(self, expected_newlines: int) -> None:
        """按回车开新行；没开成（丢键）就再按，最多 3 次。"""
        for _ in range(3):
            self.io.press("Enter")
            doc = self._settle()
            if doc.count("\n") >= expected_newlines:
                return
        self.log("    ⚠ 回车多次没生效，继续打（最终校验会兜底）")

    # -------------------------------------------------------- 打字主流程

    def type_code(self, code: str, *, allow_typos: bool = True) -> None:
        """按行打字：逐行对齐缩进 → 打内容 → 回读校正 → 回车。

        与"整篇逐字符流"相比，这里每行多几次回读与微调，
        换来的是对编辑器自动缩进/自动闭合/偶发丢键的鲁棒性。
        """
        text = prepare_for_typing(code, self.profile.tab_width)
        lines = text.split("\n")
        for index, line in enumerate(lines):
            self.timing.begin_line()
            if index:
                self._newline(index)
                self.sleep(self.timing.line_delay())
            want_indent = self._leading_spaces(line)
            self._align_indent(want_indent)
            self._type_body(line[want_indent:], allow_typos=allow_typos)
            self._repair_line(line)
            if (index + 1) % 10 == 0:
                self.log(f"    已输入 {index + 1}/{len(lines)} 行")

    # -------------------------------------------------------- 校验

    def read_back(self) -> str:
        return normalize_code(self._settle())

    def verify(self, expected: str) -> bool:
        actual = self.read_back()
        want_norm = normalize_code(expected)
        line, column, want, got = first_diff(want_norm, actual)
        if line == 0:
            return True
        want_lines = want_norm.split("\n")
        act_lines = actual.split("\n")
        self.log(
            f"    ✗ 第 {line} 行第 {column} 列不一致"
            f"（期望 {len(want_lines)} 行 / 实际 {len(act_lines)} 行）"
        )
        self.log(f"      期望: {want!r}")
        self.log(f"      实际: {got!r}")
        for offset in (-1, 1):
            idx = line - 1 + offset
            if 0 <= idx < max(len(want_lines), len(act_lines)):
                w = want_lines[idx] if idx < len(want_lines) else "<无>"
                g = act_lines[idx] if idx < len(act_lines) else "<无>"
                if w != g:
                    self.log(f"      {idx + 1} 行 期望 {w[:90]!r} / 实际 {g[:90]!r}")
        return False

    def type_and_verify(self, code: str, *, rounds: int = 2) -> bool:
        """打一遍 + 校验；不一致就整体清空重打（第二遍不再模拟打错）。"""
        expected = normalize_code(prepare_for_typing(code, self.profile.tab_width))
        self.io.click_editor()
        self.sleep(0.15)
        self.log(f"  编辑器焦点: {self.io.focus_info()}")

        for attempt in range(1, rounds + 1):
            if (self.io.read_doc() or "").strip():
                self.log("  编辑器里已有内容，先清空")
                self.clear_all()
                self.sleep(0.2)
            elif attempt > 1:
                self.clear_all()
                self.sleep(0.2)
            if attempt > 1:
                self.log(f"  第 {attempt} 次重打（清空后）")
            self.indent_fixes = 0
            self.line_repairs = 0
            # 每轮重新抽速度：真人重打一遍也不会一模一样
            cpm = self.timing.begin_session()
            self.speed_cpm = cpm
            lo, hi = self.profile.speed_interval()
            self.log(
                f"  🎲 本轮打字速度 ≈ {cpm:.0f} 字符/分"
                f"（区间 {lo:.0f}–{hi:.0f}，行内还会上下浮动）"
            )
            self.type_code(code, allow_typos=(attempt == 1))
            self.rounds_used = attempt
            if self.verify(expected):
                self.log(
                    f"  ✓ 文档一致（{len(expected.splitlines())} 行 / {len(expected)} 字符）"
                    f"，缩进对齐 {self.indent_fixes} 次 / 行内修复 {self.line_repairs} 次 / 第 {attempt} 轮"
                )
                return True
        return False


def is_probably_same(expected: str, actual: str) -> bool:
    """宽松比较：忽略行尾空白与多余空行。"""
    return normalize_code(expected) == normalize_code(actual)


_WHITESPACE_ONLY = re.compile(r"^\s*$")


def blank_line_ratio(text: str) -> float:
    lines = (text or "").split("\n") or [""]
    blanks = sum(1 for line in lines if _WHITESPACE_ONLY.match(line))
    return blanks / len(lines)
