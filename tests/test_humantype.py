"""模拟人类打字模块的离线测试（用假 IO，不碰浏览器）。"""

import random
import unittest

from trainsolve.humantype import (
    HumanTyper,
    TimingModel,
    TypingProfile,
    TypingStopped,
    describe_human_pacing,
    first_diff,
    normalize_code,
    prepare_for_typing,
)

CODE = """#include <stdio.h>

int main(void)
{
    int n;
    scanf("%d", &n);
    printf("%d\\n", n * 2);
    return 0;
}
"""


class FakeIO:
    """模拟 CodeMirror 的文本行为，并记录每一次按键。"""

    def __init__(self, *, drop_first_char: bool = False) -> None:
        self.doc = ""
        self.events: list[tuple[str, str]] = []
        self.selected_all = False
        self.drop_first_char = drop_first_char
        self._dropped = False

    def click_editor(self) -> None:
        self.events.append(("click", "editor"))

    def type_char(self, ch: str) -> None:
        self.events.append(("char", ch))
        if self.drop_first_char and not self._dropped:
            self._dropped = True  # 模拟"丢键"
            return
        self.doc += ch

    def press(self, key: str) -> None:
        self.events.append(("key", key))
        if key == "Control+a":
            self.selected_all = True
        elif key == "Backspace":
            if self.selected_all:
                self.doc = ""
                self.selected_all = False
            else:
                self.doc = self.doc[:-1]
        elif key == "Enter":
            self.doc += "\n"

    def read_doc(self) -> str:
        return self.doc

    def focus_info(self) -> str:
        return "editor:cm-content"

    # 统计
    def typed_chars(self) -> int:
        return sum(1 for kind, _ in self.events if kind == "char")

    def backspaces(self) -> int:
        return sum(1 for kind, val in self.events if kind == "key" and val == "Backspace")

    def max_single_insert(self) -> int:
        """单次输入的最大字符数（必须永远是 1，否则会触碰平台 50 字符阈值）。"""
        return max((len(val) for kind, val in self.events if kind == "char"), default=0)


def make_typer(io, **profile_kwargs):
    sleeps: list[float] = []
    profile = TypingProfile(**profile_kwargs)
    typer = HumanTyper(
        io,
        profile,
        log=lambda *_: None,
        rng=random.Random(42),
        sleep=lambda seconds: sleeps.append(seconds),
    )
    return typer, sleeps


class AutoIndentIO(FakeIO):
    """模拟平台 CodeMirror：回车后按上一行自动补缩进（行尾 `{` 加 2 空格，`}` 减 2）。"""

    def press(self, key: str) -> None:
        if key != "Enter":
            return super().press(key)
        last = self.doc.split("\n")[-1]
        stripped = last.strip()
        base = len(last) - len(last.lstrip(" "))
        self.events.append(("key", "Enter"))
        if stripped.endswith("{"):
            indent = base + 2
        elif stripped.startswith("}"):
            indent = max(0, base - 2)
        else:
            indent = base
        self.doc += "\n" + " " * indent


class BlankLineIO(AutoIndentIO):
    """更刁钻：`{` 回车后自动补 2 空格，下一行却是空行（目标缩进 0）。"""


CODE_2SPACE = """#include <stdio.h>

int main(void)
{
  int n;
  scanf("%d", &n);
  printf("%d\\n", n * 2);
  return 0;
}
"""

CODE_WITH_BLANK = """int main(void)
{
    int n;

    scanf("%d", &n);
    return 0;
}
"""


class TextUtilsTest(unittest.TestCase):
    def test_normalize_nbsp_and_trailing(self):
        self.assertEqual(normalize_code("a\u00a0b  \n\n"), "a b")

    def test_normalize_drops_trailing_blank_lines(self):
        self.assertEqual(normalize_code("x\n\n\n"), "x")

    def test_prepare_converts_tabs(self):
        self.assertEqual(prepare_for_typing("if\tx"), "if    x")

    def test_first_diff(self):
        self.assertEqual(first_diff("a\nb", "a\nb"), (0, 0, "", ""))
        line, column, want, got = first_diff("a\nb", "a\nc")
        self.assertEqual((line, column, want, got), (2, 1, "b", "c"))

    def test_first_diff_missing_line(self):
        line, _, want, got = first_diff("a\nb", "a")
        self.assertEqual(line, 2)
        self.assertEqual(got, "<缺少该行>")


class TimingModelTest(unittest.TestCase):
    def test_delays_within_bounds(self):
        model = TimingModel(TypingProfile(), random.Random(1))
        delays = [model.char_delay() for _ in range(500)]
        self.assertGreaterEqual(min(delays), TypingProfile().min_delay - 1e-9)
        self.assertLessEqual(max(delays), TypingProfile().max_delay + 1e-9)

    def test_average_speed_matches_cpm(self):
        model = TimingModel(TypingProfile(cpm=240), random.Random(7))
        delays = [model.char_delay() for _ in range(2000)]
        avg = sum(delays) / len(delays)
        # 对数正态的均值略大于中位数，允许 1.5 倍误差
        self.assertLess(avg, 60.0 / 240 * 1.6)

    def test_line_pause_has_think_pauses(self):
        model = TimingModel(TypingProfile(think_pause_rate=0.5), random.Random(3))
        pauses = [model.line_delay() for _ in range(200)]
        self.assertGreater(max(pauses), 0.4)      # 出现过思考停顿
        self.assertLess(max(pauses), 0.35 + 1.4)  # 不越界


class SpeedVariationTest(unittest.TestCase):
    """打字速度随机化：每次作答速度不同、每行有浮动，但仍在区间内。"""

    def test_default_band_is_plus_minus_25_percent(self):
        lo, hi = TypingProfile(cpm=400).speed_interval()
        self.assertAlmostEqual(lo, 300.0)
        self.assertAlmostEqual(hi, 500.0)

    def test_session_speed_differs_between_runs(self):
        profile = TypingProfile(cpm=300)
        speeds = [TimingModel(profile, random.Random(seed)).begin_session() for seed in range(6)]
        self.assertTrue(all(225.0 <= speed <= 375.0 for speed in speeds))
        self.assertGreater(len({round(speed, 3) for speed in speeds}), 1, "每次作答的速度应该不同")

    def test_zero_jitter_keeps_fixed_speed(self):
        model = TimingModel(TypingProfile(cpm=240, cpm_jitter=0.0), random.Random(1))
        self.assertAlmostEqual(model.begin_session(), 240.0)

    def test_explicit_range_wins(self):
        model = TimingModel(TypingProfile(cpm=220, cpm_min=400, cpm_max=500), random.Random(2))
        for _ in range(50):
            speed = model.begin_session()
            self.assertGreaterEqual(speed, 400.0)
            self.assertLessEqual(speed, 500.0)

    def test_min_cpm_floor(self):
        lo, hi = TypingProfile(cpm=20, cpm_jitter=0.9).speed_interval()
        self.assertGreaterEqual(lo, 60.0)
        self.assertGreaterEqual(hi, lo)
        model = TimingModel(TypingProfile(cpm=20, cpm_jitter=0.9), random.Random(0))
        self.assertGreaterEqual(model.begin_session(), 60.0)

    def test_line_speed_fluctuates(self):
        model = TimingModel(TypingProfile(cpm=260), random.Random(3))
        model.begin_session()
        speeds = []
        for _ in range(60):
            model.begin_line()
            speeds.append(model.current_cpm())
        self.assertGreater(max(speeds) - min(speeds), 15.0, "行间速度应该有可见浮动")
        self.assertGreaterEqual(min(speeds), 60.0)

    def test_faster_session_has_shorter_delays(self):
        fast = TimingModel(TypingProfile(cpm=600, cpm_jitter=0.0), random.Random(4))
        slow = TimingModel(TypingProfile(cpm=120, cpm_jitter=0.0), random.Random(4))
        fast.begin_session()
        slow.begin_session()
        fast.begin_line()
        slow.begin_line()
        total_fast = sum(fast.char_delay() for _ in range(200))
        total_slow = sum(slow.char_delay() for _ in range(200))
        self.assertLess(total_fast, total_slow)

    def test_typer_logs_and_records_speed(self):
        logs: list[str] = []
        io = FakeIO()
        typer = HumanTyper(
            io,
            TypingProfile(cpm=300),
            log=logs.append,
            rng=random.Random(5),
            sleep=lambda _seconds: None,
        )
        self.assertTrue(typer.type_and_verify(CODE))
        self.assertTrue(any("本轮打字速度" in line for line in logs))
        self.assertGreater(typer.speed_cpm, 0.0)


class PacingSummaryTest(unittest.TestCase):
    """启动日志里的「节奏」一行：把随机思考时间与退格配置说清楚。"""

    @staticmethod
    def _run(**kwargs):
        from types import SimpleNamespace

        base = {
            "human_delay_min": 0.0,
            "human_delay_max": 0.0,
            "backspace_rate": 0.0,
            "backspace_max": 2,
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_defaults_are_silent(self):
        self.assertEqual(describe_human_pacing(self._run()), "")

    def test_delay_range_is_reported(self):
        text = describe_human_pacing(self._run(human_delay_min=3, human_delay_max=12))
        self.assertIn("3–12 分钟", text)

    def test_reversed_range_is_shown_in_order(self):
        text = describe_human_pacing(self._run(human_delay_min=12, human_delay_max=3))
        self.assertIn("3–12 分钟", text)

    def test_backspace_is_reported(self):
        text = describe_human_pacing(self._run(backspace_rate=0.01, backspace_max=2))
        self.assertIn("随机退格 0.01 / 每次最多 2 字符", text)

    def test_both_parts_are_joined(self):
        text = describe_human_pacing(self._run(
            human_delay_min=1, human_delay_max=2, backspace_rate=0.02, backspace_max=3))
        self.assertIn("每题随机思考 1–2 分钟", text)
        self.assertIn("随机退格 0.02 / 每次最多 3 字符", text)


class StopDuringTypingTest(unittest.TestCase):
    """「停止」必须能打断正在进行的打字，而不是等整段打完（0.6.5）。"""

    def test_stop_raises_while_typing(self):
        io = FakeIO()
        state = {"stop": False}
        typer = HumanTyper(
            io, TypingProfile(typo_rate=0.0),
            log=lambda *_: None, sleep=lambda _s: None, rng=random.Random(7),
            should_stop=lambda: state["stop"],
        )
        original = io.type_char

        def type_char(ch: str) -> None:
            original(ch)
            if len(io.doc) >= 5:
                state["stop"] = True

        io.type_char = type_char
        with self.assertRaises(TypingStopped):
            typer.type_and_verify(CODE, rounds=1)
        self.assertLess(len(io.doc), len(normalize_code(CODE)), "应该停在半途，而不是打完整段")

    def test_no_stop_types_everything(self):
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.0)
        self.assertTrue(typer.type_and_verify(CODE, rounds=1))


class FocusTest(unittest.TestCase):
    """焦点自愈：焦点不在编辑器时用 CDP 鼠标点回去，不依赖窗口是否在前台。"""

    def test_ensure_focus_clicks_when_focus_is_elsewhere(self):
        from trainsolve.human import CDPTyperIO

        sent: list[tuple[str, dict]] = []

        class FakeCDP:
            def send(self, method, payload=None):
                sent.append((method, payload or {}))
                return {}

        class FakePage:
            pass

        io = CDPTyperIO(FakePage(), FakeCDP())
        focus = {"text": "other:BODY"}
        io.focus_info = lambda: focus["text"]  # type: ignore[method-assign]

        def evaluate(_expr):
            focus["text"] = "editor:cm-content"
            return {"x": 120.0, "y": 40.0}

        io._evaluate = evaluate  # type: ignore[method-assign]
        info = io.ensure_focus()
        self.assertTrue(info.startswith("editor:"))
        self.assertEqual([m for m, _ in sent], ["Input.dispatchMouseEvent"] * 2)
        self.assertEqual(sent[0][1]["button"], "left")

    def test_ensure_focus_is_noop_when_already_focused(self):
        from trainsolve.human import CDPTyperIO

        sent = []

        class FakeCDP:
            def send(self, method, payload=None):
                sent.append(method)
                return {}

        io = CDPTyperIO(object(), FakeCDP())
        io.focus_info = lambda: "editor:cm-content"  # type: ignore[method-assign]
        self.assertEqual(io.ensure_focus(), "editor:cm-content")
        self.assertEqual(sent, [])


class HumanTyperTest(unittest.TestCase):
    def test_types_whole_document(self):
        io = FakeIO()
        typer, _ = make_typer(io)
        ok = typer.type_and_verify(CODE)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE))

    def test_single_char_inserts_only(self):
        """每次输入必须只有 1 个字符——这是避开平台 50 字符回滚阈值的关键。"""
        io = FakeIO()
        typer, _ = make_typer(io)
        typer.type_and_verify(CODE)
        self.assertEqual(io.max_single_insert(), 1)

    def test_uses_enter_for_newlines(self):
        io = FakeIO()
        typer, _ = make_typer(io)
        typer.type_and_verify(CODE)
        enters = sum(1 for kind, val in io.events if kind == "key" and val == "Enter")
        self.assertEqual(enters, CODE.count("\n"))

    def test_typos_produce_backspaces(self):
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.2)
        typer.type_and_verify(CODE, rounds=1)
        self.assertGreater(io.backspaces(), 0)
        # 打错后仍然把文档打对（校验会兜住）
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE))

    def test_zero_typo_rate_has_no_backspace(self):
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.0, backspace_rate=0.0)
        typer.type_and_verify(CODE)
        self.assertEqual(io.backspaces(), 0)

    def test_random_backspace_deletes_then_retypes(self):
        """随机退格：删掉刚打的字符再原样补回，文档最终必须完全一致。"""
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.0, backspace_rate=1.0, backspace_max=2)
        ok = typer.type_and_verify(CODE, rounds=1)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE))
        self.assertGreater(io.backspaces(), 0)
        self.assertEqual(io.max_single_insert(), 1)

    def test_random_backspace_never_eats_previous_line(self):
        """退格只能落在本段已打出来的字符里，不能越过行首把上一行拆掉。"""
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.0, backspace_rate=1.0, backspace_max=5)
        typer.type_code("int a = 1;\n", allow_typos=True)
        self.assertEqual(io.read_doc(), "int a = 1;\n")

    def test_zero_backspace_rate_has_no_random_backspace(self):
        io = FakeIO()
        typer, _ = make_typer(io, typo_rate=0.0, backspace_rate=0.0)
        typer.type_and_verify(CODE)
        self.assertEqual(io.backspaces(), 0)

    def test_backspace_count_respects_max(self):
        typer, _ = make_typer(FakeIO(), backspace_max=3)
        draws = [typer.timing.backspace_count() for _ in range(60)]
        self.assertTrue(all(1 <= n <= 3 for n in draws), draws[:8])
        self.assertEqual(set(draws), {1, 2, 3})

    def test_repairs_lost_keystroke_inline(self):
        """丢键由行内校正兜住，不该整篇重打。"""
        io = FakeIO(drop_first_char=True)
        typer, _ = make_typer(io, typo_rate=0.0)
        ok = typer.type_and_verify(CODE, rounds=2)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE))
        self.assertGreater(typer.line_repairs, 0)
        self.assertNotIn(("key", "Control+a"), io.events)

    def test_survives_editor_auto_indent(self):
        """回归用例：编辑器回车自动缩进 2 空格时，不能被叠成两倍缩进。"""
        io = AutoIndentIO()
        typer, _ = make_typer(io, typo_rate=0.0)
        ok = typer.type_and_verify(CODE)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE))
        self.assertGreater(typer.indent_fixes, 0)

    def test_survives_auto_indent_when_target_is_2_spaces(self):
        io = AutoIndentIO()
        typer, _ = make_typer(io, typo_rate=0.0)
        ok = typer.type_and_verify(CODE_2SPACE)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE_2SPACE))

    def test_blank_line_after_brace_is_dedented(self):
        """`{` 后面的空行会被自动缩进补空格，必须退格回到 0。"""
        io = BlankLineIO()
        typer, _ = make_typer(io, typo_rate=0.0)
        ok = typer.type_and_verify(CODE_WITH_BLANK)
        self.assertTrue(ok)
        self.assertEqual(normalize_code(io.doc), normalize_code(CODE_WITH_BLANK))
        blank_lines = [line for line in io.doc.split("\n") if not line.strip()]
        self.assertTrue(all(line == "" for line in blank_lines))

    def test_reports_mismatch_when_cannot_fix(self):
        class BrokenIO(FakeIO):
            def type_char(self, ch: str) -> None:
                self.events.append(("char", ch))  # 永远打不进去

        typer, _ = make_typer(BrokenIO(), typo_rate=0.0)
        self.assertFalse(typer.type_and_verify(CODE, rounds=2))

    def test_sleeps_are_positive(self):
        io = FakeIO()
        typer, sleeps = make_typer(io)
        typer.type_and_verify(CODE)
        self.assertTrue(sleeps)
        self.assertGreater(min(sleeps), 0)


if __name__ == "__main__":
    unittest.main()
