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

    def ensure_focus(self) -> str:
        """尽力把焦点放回编辑器，返回焦点描述。

        默认实现不做任何事（离线测试的假 IO 只需记住状态）；真机实现会在焦点
        不在编辑器时用页面内 DOM 聚焦 / CDP 鼠标点一下，**不依赖窗口是否在前台**。
        """
        return self.focus_info()


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
    typo = max(0.0, min(0.5, float(getattr(run, "typo_rate", 0.0) or 0.0)))
    if typo > 0:
        # 错字率以前只在设置里能改、日志里完全看不到，等于静默生效（0.6.8）
        parts.append(f"错字 {typo:g}")
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


class TypingStopped(RuntimeError):
    """打字过程中收到「停止」请求：立刻收手，不再继续敲。"""


class HumanStopped(RuntimeError):
    """真人作答流程被「停止」打断（打字中或打字后、提交前）。"""


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
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.io = io
        self.profile = profile or TypingProfile()
        self.timing = TimingModel(self.profile, rng)
        self.log = log
        self.sleep = sleep
        # 停止信号：每打一个字符都问一次，命中就抛 TypingStopped（0.6.5）
        self.should_stop = should_stop or (lambda: False)
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
        if self.should_stop():
            raise TypingStopped()
        if burst_left > 0:
            self.sleep(self.profile.burst_delay)
            return burst_left - 1
        self.sleep(self.timing.char_delay())
        return self.timing.burst()

    def _current_line(self, *, settle: bool = False) -> str:
        """读当前（光标所在，也就是正在打的）那一行。"""
        if settle:
            return self._settle().split("\n")[-1]
        doc = self.io.read_doc()
        if doc is None:
            return ""
        return doc.replace(NBSP, " ").split("\n")[-1]

    def _ensure_focus(self) -> str | None:
        """把焦点放回编辑器（实现不支持时返回 None）。"""
        ensure = getattr(self.io, "ensure_focus", None)
        if ensure is None:
            return None
        try:
            return str(ensure())
        except Exception:  # noqa: BLE001 - 聚焦失败按后续校验兜底
            return None

    def _cursor_state(self) -> dict | None:
        """读「光标前/后」；实现不支持或读失败时返回 None（退回整行比较）。"""
        reader = getattr(self.io, "cursor_line", None)
        if reader is None:
            return None
        try:
            state = reader()
        except Exception:  # noqa: BLE001 - 读失败就走回退路径
            return None
        if not isinstance(state, dict) or state.get("before") is None:
            return None
        return state

    def _delete_chars(self, count: int, *, forward: bool = False) -> None:
        """删字符：`forward=True` 用 Delete（删光标**后**的自动补全尾巴）。"""
        for _ in range(max(0, count)):
            if self.should_stop():
                raise TypingStopped()
            self.io.press("Delete" if forward else "Backspace")
            self.sleep(min(0.06, max(self.profile.burst_delay, 0.012)))

    def _type_char_paced(self, ch: str, *, allow_typos: bool, burst_left: int) -> int:
        """带节奏地敲一个字符（含偶发打错再退格），返回剩余的连打计数。"""
        burst_left = self._pause_char(burst_left)
        if allow_typos and self.timing.should_typo():
            self.io.type_char(self.timing.typo_char())
            self.sleep(self.timing.char_delay())
            self.io.press("Backspace")
        self.io.type_char(ch)
        return burst_left

    def _type_body(self, text: str, *, allow_typos: bool = False) -> None:
        """逐字符打一段内容（不含换行）：每敲一个字符前先读**光标前/后**再决定下一个键。

        平台编辑器会自动补括号（敲 `(` 得到 `()`、光标夹在中间）。只看整行会把自动补的
        `)` 当成多余字符退格掉，于是和编辑器互相对抗、永远打不完（0.6.8 现场实测）。
        所以按光标两侧判断：

        * 光标前正好是目标的正确前缀 → 补打下一个字符；
        * 光标前已经等于目标、光标后还有字符 → 是自动补的尾巴，用 Delete 删掉；
        * 光标前打多了/打错了 → 退格回到公共前缀。

        **行首缩进不参与比较**（2026-11-11 真机）：这个编辑器还会「智能缩进」——回车把新行
        补成 8 个空格，打 `{` 时整行重排并补 `}`。C 代码的缩进不影响编译与判分，所以这里
        只对齐缩进之后的**内容**，不再为空格和编辑器拉锯（拉锯会把刚打的字删光）。

        没有 `cursor_line` 的实现（离线假 IO）退回「整行比较」的旧逻辑。
        """
        want = text.rstrip(" ")
        target = want.lstrip(" \t")          # 要去匹配的正文（不含行首缩进）
        burst_left = 0
        fallback_typed = 0        # 回退路径用：上一轮文档里的字符数（发现丢键）
        last_before: str | None = None
        stalls = 0                # 连着多少次「按键没落到文档里」
        steps = 0
        limit = max(80, len(text) * 3 + 60)   # 上限只是防读值异常死循环
        while steps < limit:
            steps += 1
            if self.should_stop():
                raise TypingStopped()
            # 每 12 个按键主动确认一次焦点：平台前端重渲染会把焦点弄丢，
            # 丢了之后后面所有按键都打空（2026-11-11 真机实测，第 11 行起丢）。
            if steps % 12 == 1:
                self._ensure_focus()

            state = self._cursor_state()
            if state is not None:
                before_raw = str(state.get("before") or "").replace(NBSP, " ")
                before = before_raw.lstrip(" \t")
                after = str(state.get("after") or "").replace(NBSP, " ")
                if last_before is not None and before == last_before and before != target:
                    stalls += 1
                    if stalls >= 2:
                        # 键盘打不动了：先把焦点抢回来再继续
                        self._ensure_focus()
                    if stalls >= 6:
                        self.log("    ⚠ 按键连续打不进编辑器，放弃这一行")
                        return
                else:
                    stalls = 0
                last_before = before
                if before == target:
                    if not after:
                        return
                    self.line_repairs += 1
                    self._delete_chars(len(after), forward=True)   # 自动补的尾巴
                    continue
                if target.startswith(before):
                    burst_left = self._type_char_paced(
                        target[len(before)], allow_typos=allow_typos, burst_left=burst_left
                    )
                    if (allow_typos and len(before) >= 1
                            and self.timing.should_backspace()):
                        # 随机退格：删掉刚打的 1..N 个字符，**立即原样补回**
                        count = min(self.timing.backspace_count(), len(before))
                        self._delete_chars(count)
                        for fix_ch in target[len(before) - count:len(before)]:
                            self.io.type_char(fix_ch)
                            self.sleep(self.timing.char_delay())
                    continue
                # 光标前与目标不一致：退到公共前缀
                common = 0
                for a, b in zip(before, target):
                    if a != b:
                        break
                    common += 1
                self.line_repairs += 1
                self._delete_chars(len(before) - common)
                continue

            # ---- 回退路径：没有光标信息时按「文档最后一行」比较 ----
            line = self._current_line()
            got = line[self._leading_spaces(line):]
            if got.rstrip(" ") == target:
                return
            # 不一致时做一次稳定回读：CDP 求值可能读到「按键还没生效」的旧值，
            # 按旧值补打就会重复输入。
            line = self._current_line(settle=True)
            got = line[self._leading_spaces(line):]
            if got.rstrip(" ") == target:
                return
            if fallback_typed > len(got):
                # 发出去的键比文档里多出来少 → 丢了键，补打属于修复
                self.line_repairs += 1
            fallback_typed = len(got)
            common = 0
            for a, b in zip(got, target):
                if a != b:
                    break
                common += 1
            if len(got) > common:
                # 多了字符（自动补全/多插）→ 退格删掉
                burst_left = self._pause_char(burst_left)
                self.io.press("Backspace")
                continue
            # 少了字符（丢键 / 还没打）→ 补下一个
            ch = target[len(got)]
            burst_left = self._type_char_paced(ch, allow_typos=allow_typos, burst_left=burst_left)
            fallback_typed = len(got) + 1
            if allow_typos and len(got) >= 1 and self.timing.should_backspace():
                # 随机退格：删掉刚打的 1..N 个字符（含刚敲的这个），**原样补回**——
                # 只制造真实按键，不能真把进度删掉（否则自适应循环会一直打同一行）。
                done = len(got) + 1
                count = min(self.timing.backspace_count(), done)
                start = done - count
                for _ in range(count):
                    self.io.press("Backspace")
                    self.sleep(self.timing.char_delay())
                for fix_ch in target[start:done]:
                    self.io.type_char(fix_ch)
                    self.sleep(self.timing.char_delay())
                fallback_typed = done
        self.log(f"    ⚠ 行内自适应输入没收敛：{want[:40]!r}")

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
            if self.should_stop():
                raise TypingStopped()
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

    def _newline(self, expected_newlines: int) -> None:
        """按回车开新行；没开成（丢键/丢焦点）就自愈焦点再按，最多 5 次。"""
        for attempt in range(5):
            if attempt and attempt % 2 == 0:
                self._ensure_focus()
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
            if index % 8 == 0:
                # 每 8 行确认一次焦点还在编辑器里：窗口被切走 / 页面重绘都可能在
                # 中途把焦点弄丢，丢了之后按键就不进编辑器了（0.6.5）。
                ensure = getattr(self.io, "ensure_focus", None)
                if ensure is not None:
                    ensure()
            cursor_state = self._cursor_state()
            if cursor_state is not None:
                # 缩进交给编辑器的智能缩进，不再手动对齐（手动对齐会和它拉锯）
                self._type_body(line, allow_typos=allow_typos)
            else:
                self._align_indent(want_indent)
                self._type_body(line[want_indent:], allow_typos=allow_typos)
            if (index + 1) % 10 == 0:
                self.log(f"    已输入 {index + 1}/{len(lines)} 行")

    # -------------------------------------------------------- 校验

    def read_back(self) -> str:
        return normalize_code(self._settle())

    def verify(self, expected: str) -> bool:
        """比对文档：**行首缩进不计**（C 代码的缩进不影响编译与判分）。

        这个编辑器会「智能缩进」（回车补到 8 空格、打 `{` 时整行重排），硬要逐格对齐
        缩进只会和它拉锯、把刚打的字删光（2026-11-11 真机）。文档是否完整、有没有截断，
        去掉行首空白后照样比得出来。
        """
        actual = comparable_code(self.read_back())
        want_norm = comparable_code(expected)
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
                    f"，缩进对齐 {self.indent_fixes} 次 / 丢键补打 {self.line_repairs} 次 / 第 {attempt} 轮"
                )
                return True
        return False


def _strip_indent(text: str) -> str:
    """逐行去掉行首空白（只动行首，行内空格保留）。"""
    return "\n".join(line.lstrip(" \t") for line in text.split("\n"))


def comparable_code(text: str) -> str:
    """比较用的规范形：normalize + 去掉行首缩进。

    打字校验、前端暂存同步、运行核对**必须用同一把尺子**：0.6.9 起打字把行首缩进
    交给编辑器的智能缩进，编辑器会补出比目标更多的空格（实测 996 → 1140）。
    只改打字校验不改同步判定，提交前那一关就会永远等不到（2026-11-11 真机）。
    """
    return _strip_indent(normalize_code(text))


def is_probably_same(expected: str, actual: str) -> bool:
    """宽松比较：忽略行首缩进、行尾空白与多余空行。"""
    return comparable_code(expected) == comparable_code(actual)


_WHITESPACE_ONLY = re.compile(r"^\s*$")


def blank_line_ratio(text: str) -> float:
    lines = (text or "").split("\n") or [""]
    blanks = sum(1 for line in lines if _WHITESPACE_ONLY.match(line))
    return blanks / len(lines)
