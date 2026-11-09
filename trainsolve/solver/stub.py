"""离线 / 人工 Solver。

- StubSolver：从本地文件或字典里取答案，用于在没有 API Key 的情况下跑通全链路与单元测试。
- PromptSolver：把题目打到终端，人工把答案贴进来（或用 --eval 里的交互模式兜底）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from .base import Answer, SolverError


class StubSolver:
    """答案来自 `{qid: code}` 映射或 `answers/` 目录下的 `Q<id>.c` / `Q<id>.txt`。"""

    name = "stub"

    def __init__(self, answers: dict | None = None, directory: str | Path | None = None, repairs: list[str] | None = None) -> None:
        self.answers = {str(k): v for k, v in (answers or {}).items()}
        self.directory = Path(directory) if directory else None
        self.repairs = list(repairs or [])
        self._repair_index = 0

    def _lookup(self, question: dict) -> str:
        qid = str(question.get("id"))
        if qid in self.answers:
            return self.answers[qid]
        if self.directory:
            for name in (f"Q{qid}.c", f"Q{qid}.txt", f"{qid}.c", f"{qid}.txt"):
                path = self.directory / name
                if path.exists():
                    return path.read_text(encoding="utf-8")
        raise SolverError(f"StubSolver 里没有 Q{qid} 的答案")

    def solve(self, question: dict) -> Answer:
        text = self._lookup(question)
        kind = "code" if int(question.get("qType") or 0) >= 5 else "answer"
        return Answer(text=text, kind=kind, raw=text)

    def repair(self, question: dict, previous: str, feedback: str) -> Answer:
        if self._repair_index < len(self.repairs):
            text = self.repairs[self._repair_index]
            self._repair_index += 1
            return Answer(text=text, kind="code", raw=text)
        return Answer(text=previous, kind="code", raw=previous)


class PromptSolver:
    """交互式：把题目打印出来，人工输入答案。"""

    name = "prompt"

    def __init__(self, reader: Callable[[str], str] = input, writer: Callable[[str], None] = print) -> None:
        self.reader = reader
        self.writer = writer

    def _show(self, question: dict) -> None:
        self.writer("=" * 70)
        self.writer(f"Q{question.get('id')}  qType={question.get('qType')}  分值={question.get('point')}")
        self.writer(str(question.get("content") or ""))
        for index, option in enumerate(question.get("options") or []):
            self.writer(f"  {chr(ord('A') + index)}. {option.get('content') or option.get('text') or option}")
        self.writer("=" * 70)

    def solve(self, question: dict) -> Answer:
        self._show(question)
        kind = "code" if int(question.get("qType") or 0) >= 5 else "answer"
        if kind == "code":
            self.writer("粘贴 C 代码，以单独一行 EOF 结束：")
            lines: list[str] = []
            while True:
                line = self.reader("")
                if line.strip() == "EOF":
                    break
                lines.append(line)
            return Answer(text="\n".join(lines) + "\n", kind="code")
        return Answer(text=self.reader("答案> ").strip(), kind="answer")

    def repair(self, question: dict, previous: str, feedback: str) -> Answer:
        self.writer(feedback)
        self.writer("上一版代码：")
        self.writer(previous)
        return self.solve(question)


def load_answer_file(path: str | Path) -> dict:
    """支持 JSON 或 `Q<id>.c` 目录两种形式。"""
    path = Path(path)
    if path.is_dir():
        answers = {}
        for item in path.glob("Q*.*"):
            match = re.match(r"Q(\d+)\.", item.name)
            if match:
                answers[match.group(1)] = item.read_text(encoding="utf-8")
        return answers
    return json.loads(path.read_text(encoding="utf-8"))
