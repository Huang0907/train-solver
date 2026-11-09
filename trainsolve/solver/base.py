"""Solver 抽象：把「模型」与「刷题流程」解耦。

新增一个模型后端只需要实现这两个方法：

    class MySolver:
        name = "my-solver"
        def solve(self, question: dict) -> Answer: ...
        def repair(self, question: dict, previous: str, feedback: str) -> Answer: ...

`question` 是平台 `record/{sessionId}` 返回的 `paper.questions[]` 元素，字段见 README。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Answer:
    """一次作答。

    kind="code"    -> text 是完整 C 源码
    kind="answer"  -> text 是客观题的答案（字母/文本）
    """

    text: str
    kind: str = "code"
    rationale: str = ""
    raw: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Solver(Protocol):
    name: str

    def solve(self, question: dict) -> Answer: ...

    def repair(self, question: dict, previous: str, feedback: str) -> Answer: ...


class SolverError(Exception):
    pass
