"""本地判题：样例抽取 → gcc 编译运行 → 逐字节比对。

平台判题模式的字段是 `compareMode: "exact"`，即 stdout 文本严格比对，
所以这里刻意按「规范化后严格相等」来判定：
  - CRLF 统一成 LF
  - 去掉每行行尾空白
  - 去掉末尾多余空行
除此之外不做任何宽容（大小写、逗号全半角都必须一致）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

EXAMPLE_MARKERS = (
    "程序运行结果示例",
    "程序运行示例",
    "运行结果示例",
    "运行示例",
    "示例输出",
    "输出示例",
    "示例运行结果",
)
STOP_MARKERS = ("输入提示", "输出提示", "输入格式", "输出格式", "取值范围", "注意", "提示：", "说明：")

_QUOTE_PAIRS = [("\u201c", "\u201d"), ('"', '"'), ("\u300c", "\u300d")]


@dataclass
class Sample:
    """一条从题面抽出的运行样例。

    `expected_stdout` 是「程序的 stdout」，注意题面示例里用户键入的数字与回车是终端回显，
    不属于 stdout。因此：
        Please enter the year:2016        <- 提示语 + 键入的值（同一条视觉行）
        31,29,31,30,31,30                 <- 程序的输出
    对应的 stdout 是 `Please enter the year:31,29,31,30,31,30\n...`。
    但也有的参考程序会在提示后面多打一个换行，所以 `alternates` 里存了这种变体。
    """

    stdin: str | None
    expected_stdout: str | None
    prompt: str = ""
    raw: str = ""
    note: str = ""
    alternates: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.stdin is not None and self.expected_stdout is not None

    def candidates(self) -> list[str]:
        pool = [self.expected_stdout or "", *self.alternates]
        seen: list[str] = []
        for item in pool:
            if item and item not in seen:
                seen.append(item)
        return seen


@dataclass
class RunResult:
    ok: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    compile_error: str = ""
    timed_out: bool = False
    command: str = ""

    @property
    def detail(self) -> str:
        if self.compile_error:
            return f"编译失败：\n{self.compile_error}"
        if self.timed_out:
            return "运行超时"
        if self.returncode not in (0, None):
            return f"运行时返回码 {self.returncode}\nstderr:\n{self.stderr}"
        return ""


@dataclass
class JudgeResult:
    passed: bool
    verified: bool = False
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        if not self.checks:
            return "本地没有可用的样例，仅做了编译检查。"
        lines = []
        for name, ok, detail in self.checks:
            lines.append(f"- {name}: {'通过' if ok else '失败'}")
            if detail:
                lines.append("  " + detail.replace("\n", "\n  "))
        return "\n".join(lines)


# ---------------------------------------------------------------- 题面解析


def _strip_quotes(text: str) -> str:
    out = text.strip()
    for left, right in _QUOTE_PAIRS:
        if out.startswith(left) and out.endswith(right) and len(out) >= len(left) + len(right):
            out = out[len(left) : len(out) - len(right)]
    return out.strip()


def _unescape(text: str) -> str:
    """题面里会把 `<` 写成 `\\<`（题库导出时的转义），还原回真实字符。"""
    return re.sub(r"\\([<>])", r"\1", text)


def decode_escapes(text: str) -> str:
    """把提示语字符串里的转义还原成真实字符。

    题面用 `"\\nPlease enter the linked list (non-number indicates end)\\n"` 这种写法声明提示语，
    其中的 `\\n` / `\\t` 是真实换行与制表符——不还原就会把输出格式算错。
    """
    placeholder = "\x00"
    out = text.replace("\\\\", placeholder)
    out = out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    out = re.sub(r"\\([<>])", r"\1", out)
    return out.replace(placeholder, "\\")


def _split_quoted(text: str) -> list[str]:
    return [decode_escapes(item) for item in re.findall(r'[“"]([^”"]*)[”"]', text)]


def extract_input_prompt(statement: str) -> str:
    """从题面的「输入提示："...\"」里取出提示语原文。"""
    match = re.search(r"输入提示(?:信息)?\s*[:：]\s*(.+)", statement)
    if not match:
        return ""
    quoted = _split_quoted(match.group(1))
    return quoted[0] if quoted else _strip_quotes(match.group(1).splitlines()[0])


def extract_prompts(statement: str) -> list[str]:
    """收集题面声明的所有「输入提示」字符串（兼容旧的单返回值用法）。"""
    return extract_prompt_kinds(statement)[0]


def extract_prompt_kinds(statement: str) -> tuple[list[str], list[str]]:
    """分别收集「输入提示」与「输出提示」的原文，转义已还原。

    两者的区别很重要：输入提示后面跟着用户键入的内容（不属于 stdout），
    输出提示则是程序自己 printf 出来的文本（属于 stdout，可能是补换行的关键）。
    """
    inputs: list[str] = []
    outputs: list[str] = []
    for line in statement.splitlines():
        if "提示" not in line:
            continue
        # 题面的写法很多：「输入提示：」「输入s之前提示：」「**输入提示信息：」…
        if "输出" in line:
            bucket = outputs
        elif "输入" in line:
            bucket = inputs
        else:
            continue
        for quoted in _split_quoted(line):
            if quoted and quoted not in bucket:
                bucket.append(quoted)
    return inputs, outputs


def extract_example_block(statement: str) -> str:
    """截出「程序运行结果示例」之后的文本块。"""
    lines = statement.splitlines()
    start = None
    for index, line in enumerate(lines):
        matched = next((marker for marker in EXAMPLE_MARKERS if marker in line), None)
        if matched is None:
            continue
        start = index
        remainder = line.split(matched, 1)[1]
        # 去掉「如下」「：」「（第一行是输入…）」以及「1：」「2、」这类示例序号
        remainder = re.sub(r"^[\s:：]*(?:如下|下面|以下)+[\s:：]*", "", remainder)
        remainder = re.sub(r"^[\s:：]*[（(][^）)]*[）)]\s*[:：]?\s*", "", remainder)
        remainder = re.sub(r"^[\s:：]*\d+\s*[.、:：)）]?\s*", "", remainder)
        remainder = remainder.strip().strip(":：").strip()
        if remainder:
            # 标记与样例写在同一行
            return _collect_example_lines([remainder] + lines[index + 1 :])
        break
    if start is None:
        return ""
    return _collect_example_lines(lines[start + 1 :])


def _collect_example_lines(lines: list[str]) -> str:
    """收集一个示例块：遇到停止标记或「下一个示例」就结束（多示例题只取第一个）。"""
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and any(stripped.startswith(marker) for marker in STOP_MARKERS):
            break
        # 多示例：程序运行示例1/示例2… 只取第一个
        if stripped and any(marker in stripped for marker in EXAMPLE_MARKERS) and collected:
            break
        collected.append(line)
    return "\n".join(collected)


def _split_prompt_and_input(line: str) -> tuple[str, str] | None:
    """把 "Please enter the year:2016" 拆成 ("Please enter the year:", "2016")。"""
    for index in range(len(line) - 1, -1, -1):
        if line[index] in ":：":
            prompt, tail = line[: index + 1], line[index + 1 :].strip()
            if tail and " " not in tail and "\t" not in tail:
                return prompt, tail
    return None


def extract_samples(statement: str) -> list[Sample]:
    """从中文题面里尽量还原「stdin / 期望 stdout」。

    题面示例是「终端会话回放」：既有程序 printf 的内容，也有用户键入的内容。
    还原规则（按顺序匹配每一行）：
      1. 命中「输入提示」→ 这一行是程序输出（提示语原样输出），下一行是用户输入；
         提示语自带 \\n 时把它一起输出；不带换行时该行形如「提示语+输入」。
      2. 命中「输出提示」→ 程序自己打印的文本，原样输出（这类提示常带前后换行，
         是补出空行的关键）。
      3. 其余行是输出数据。数据行本身不带换行，只有当上一行也是数据时补一个换行
         （程序里那句 printf("\\n") 产生的换行）。
    """
    block = extract_example_block(statement)
    if not block.strip():
        return []

    lines = [line.rstrip() for line in _unescape(block).splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    input_prompts, output_prompts = extract_prompt_kinds(statement)

    # 有的题目直接说明「第一行是输入，第二行是输出」，此时示例不是终端回放
    if re.search(r"第一行是输入|第一行.*输入.*第二行.*输出", statement):
        if len(lines) >= 2:
            return [
                Sample(
                    stdin=lines[0].strip() + "\n",
                    expected_stdout="\n".join(lines[1:]).rstrip("\n") + "\n",
                    prompt="",
                    raw=block,
                    note="题面声明「第一行输入，其余为输出」",
                )
            ]

    # 没有任何「输入提示」但有「输出提示」：把示例按输出提示切成两半——前面是输入，后面是输出
    if output_prompts and not input_prompts:
        cleaned = [line.replace("↙", "").rstrip() for line in lines]
        for idx, line in enumerate(cleaned):
            stripped = line.strip()
            for prompt in sorted(output_prompts, key=len, reverse=True):
                key = prompt.strip()
                if not key:
                    continue
                if stripped == key or stripped.startswith(key):
                    stdin_lines = [item for item in cleaned[:idx] if item.strip()]
                    if not stdin_lines:
                        break
                    tail = [item for item in cleaned[idx + 1 :] if item.strip()]
                    expected = prompt + "".join(item + "\n" for item in tail)
                    return [
                        Sample(
                            stdin="\n".join(stdin_lines) + "\n",
                            expected_stdout=expected,
                            prompt=key,
                            raw=block,
                            note="按「输出提示」切开示例：前段为输入，后段为输出",
                        )
                    ]

    # 提示语按「去掉换行后的文本」匹配，按原样（含换行）输出
    input_by_text = {}
    for prompt in sorted(input_prompts, key=len, reverse=True):
        input_by_text.setdefault(prompt.strip("\n"), prompt)
    output_by_text = {}
    for prompt in sorted(output_prompts, key=len, reverse=True):
        output_by_text.setdefault(prompt.strip("\n"), prompt)

    stdin_parts: list[str] = []
    buffer = ""
    previous_was_data = False
    used_output_prompt = 0

    def is_promptish(text: str) -> bool:
        """这一行本身是不是「提示语」（而不是用户键入的值）。"""
        candidate = text.strip()
        if candidate in input_by_text or candidate in output_by_text:
            return True
        return any("\n" not in p and candidate.startswith(p) for p in input_prompts)

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # 1) 输入提示（自带换行：提示语单独占一行，下一行才是输入）
        prompt = input_by_text.get(stripped)
        if prompt is not None:
            buffer += prompt
            previous_was_data = False
            # 有些提示语只是开场白，后面紧跟的仍是提示语/输出提示，而不是输入值
            if index + 1 < len(lines) and not is_promptish(lines[index + 1]):
                stdin_parts.append(lines[index + 1].strip())
                index += 2
            else:
                index += 1
            continue

        # 1b) 输入提示（不换行：这一行是「提示语+输入」）
        inline = next(
            (p for p in input_prompts if "\n" not in p and stripped.startswith(p)), None
        )
        if inline is not None:
            remainder = stripped[len(inline) :].strip()
            buffer += inline
            previous_was_data = False
            if remainder:
                stdin_parts.append(remainder)
            index += 1
            continue

        # 2) 输出提示：程序自己打印的文本，原样输出（保留其自带换行）
        out_prompt = output_by_text.get(stripped)
        if out_prompt is not None:
            buffer += out_prompt
            previous_was_data = False
            used_output_prompt += 1
            index += 1
            continue

        # 2b) 空行：如果下一行是「自带换行的输出提示」，这个空行由提示语里的换行表示，跳过
        if not stripped and index + 1 < len(lines):
            nxt = output_by_text.get(lines[index + 1].strip())
            if nxt is not None and nxt.startswith("\n"):
                index += 1
                continue

        # 3) 数据行
        if previous_was_data:
            buffer += "\n"
        buffer += line
        previous_was_data = True
        index += 1

    expected = (buffer.rstrip("\n") + "\n") if buffer else None
    stdin = "".join(part + "\n" for part in stdin_parts) if stdin_parts else None

    if expected is not None and stdin is not None:
        alternates: list[str] = []
        inline_all = [p for p in input_prompts if "\n" not in p]
        for prompt in inline_all:
            if expected.startswith(prompt) and not expected.startswith(prompt + "\n"):
                alternative = prompt + "\n" + expected[len(prompt) :]
                if alternative not in alternates:
                    alternates.append(alternative)
        note = f"按 {len(input_prompts)} 个输入提示"
        if output_prompts:
            note += f" + {len(output_prompts)} 个输出提示"
        note += " 还原终端会话"
        return [
            Sample(
                stdin=stdin,
                expected_stdout=expected,
                prompt=(inline_all or input_prompts or [""])[0],
                raw=block,
                note=note,
                alternates=alternates,
            )
        ]

    # 兜底：没有可用提示语时，退回「首行按冒号拆提示语与输入」的旧策略
    split = _split_prompt_and_input(lines[0]) if lines else None
    if split:
        head_prompt, stdin_value = split
        body_lines = lines[1:]
        prefix = extract_input_prompt(statement) or head_prompt
        data_lines = body_lines or [""]
        expected = (prefix + data_lines[0] + "\n" + "\n".join(data_lines[1:])).rstrip("\n") + "\n"
        alternate = ("\n".join([prefix, *data_lines])).rstrip("\n") + "\n"
        return [
            Sample(
                stdin=stdin_value + "\n",
                expected_stdout=expected,
                prompt=prefix,
                raw=block,
                note="从示例首行还原「提示语+输入」",
                alternates=[alternate] if alternate != expected else [],
            )
        ]

    if input_prompts:
        return [Sample(stdin=None, expected_stdout=None, prompt=input_prompts[0], raw=block,
                       note="示例缺少输入值")]

    return [Sample(stdin=None, expected_stdout=None, raw=block, note="无法从示例中还原输入")]


# ---------------------------------------------------------------- 比对


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def compare(expected: str, actual: str) -> tuple[bool, str]:
    normalized_expected, normalized_actual = normalize(expected), normalize(actual)
    if normalized_expected == normalized_actual:
        return True, ""

    expected_lines = normalized_expected.split("\n")
    actual_lines = normalized_actual.split("\n")
    for index in range(max(len(expected_lines), len(actual_lines))):
        want = expected_lines[index] if index < len(expected_lines) else "<缺少该行>"
        got = actual_lines[index] if index < len(actual_lines) else "<缺少该行>"
        if want != got:
            return (
                False,
                "第 {} 行不一致：\n  期望: {!r}\n  实际: {!r}".format(index + 1, want, got),
            )
    return False, "长度一致但内容不同"


def compare_any(sample: Sample, actual: str) -> tuple[bool, str]:
    """按 sample 的主判定 + 备选判定依次比对，全部失败时返回最接近那条的 diff。"""
    candidates = sample.candidates()
    if not candidates:
        return False, "样例期望输出为空"
    first_detail = ""
    for index, expected in enumerate(candidates):
        ok, detail = compare(expected, actual)
        if ok:
            return True, ""
        if index == 0:
            first_detail = detail
    return False, first_detail


# ---------------------------------------------------------------- 编译运行


def find_compiler() -> str:
    for name in ("gcc", "cc", "clang"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (r"C:\msys64\ucrt64\bin\gcc.exe", r"C:\msys64\mingw64\bin\gcc.exe"):
        if Path(candidate).exists():
            return candidate
    return ""


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def compile_and_run(
    code: str,
    stdin_text: str = "",
    *,
    compiler: str | None = None,
    workdir: str | Path | None = None,
    timeout: float = 15.0,
    standard: str = "c99",
) -> RunResult:
    compiler = compiler or find_compiler()
    if not compiler:
        return RunResult(ok=False, compile_error="未找到 gcc/clang，可用 --judge server 或安装 MSYS2 gcc")

    keep = workdir is not None
    directory = Path(workdir or tempfile.mkdtemp(prefix="trainsolve-"))
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "main.c"
    source.write_text(code, encoding="utf-8")
    binary = directory / ("main.exe" if os.name == "nt" else "main")

    compile_command = [compiler, f"-std={standard}", "-O2", "-o", str(binary), str(source), "-lm"]
    compiled = subprocess.run(compile_command, capture_output=True, timeout=120)
    if compiled.returncode != 0:
        return RunResult(
            ok=False,
            compile_error=_decode(compiled.stderr or compiled.stdout),
            command=" ".join(compile_command),
        )

    try:
        run = subprocess.run(
            [str(binary)],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, timed_out=True, command=str(binary))
    finally:
        if not keep:
            shutil.rmtree(directory, ignore_errors=True)

    return RunResult(
        ok=run.returncode == 0,
        returncode=run.returncode,
        stdout=_decode(run.stdout),
        stderr=_decode(run.stderr),
        command=str(binary),
    )


def judge_program(
    question: dict,
    code: str,
    *,
    samples: Iterable[Sample] | None = None,
    compiler: str | None = None,
    workdir: str | Path | None = None,
    timeout: float = 15.0,
) -> JudgeResult:
    """编译 + 逐样例比对。返回结构化结果，`feedback` 可直接回灌给模型。"""
    statement = str(question.get("content") or "")
    sample_list = list(samples if samples is not None else extract_samples(statement))
    result = JudgeResult(passed=True)

    # 没装编译器是「无法校验」，不是「自检未通过」：不能让答案因此被拦下不提交。
    if not (compiler or find_compiler()):
        result.checks.append(("编译", True, "未找到 gcc/clang，跳过本地自检（可用 --judge server / both）"))
        result.passed = True
        result.verified = False
        return result

    if not sample_list:
        run = compile_and_run(code, "", compiler=compiler, workdir=workdir, timeout=timeout)
        ok = not run.compile_error
        result.checks.append(("编译", ok, run.compile_error))
        result.passed = ok
        result.verified = False
        return result

    first = sample_list[0]
    run = compile_and_run(code, first.stdin or "", compiler=compiler, workdir=workdir, timeout=timeout)
    if run.compile_error:
        result.checks.append(("编译", False, run.compile_error))
        result.passed = False
        return result
    result.checks.append(("编译", True, ""))

    if not first.usable:
        result.checks.append(("样例输出", False, f"无法自动还原样例（{first.note}），已跳过输出比对"))
        result.passed = True  # 编译通过即放行，但未验证
        result.verified = False
        return result

    stdout_ok, diff = compare_any(first, run.stdout)
    result.checks.append(("样例输出", stdout_ok, diff))
    result.verified = True
    result.passed = stdout_ok
    if not stdout_ok and run.stderr.strip():
        result.checks.append(("stderr", False, run.stderr.strip()))
    return result
