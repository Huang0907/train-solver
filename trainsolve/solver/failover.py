"""主/备模型自动切换。

主模型（例如本地网关的 flash）挂了（503 / 超时 / 鉴权失败）时，自动切到备用模型
（例如 DeepSeek 官方 API 的 deepseek-flash），并把切成功的那个“粘住”，后续调用直接用它，
避免每次都先撞一遍挂掉的端点。
"""

from __future__ import annotations

from .base import Answer, Solver, SolverError


class FailoverSolver:
    name = "failover"

    def __init__(self, solvers: list[Solver], *, log=print) -> None:
        if not solvers:
            raise SolverError("没有可用的模型端点")
        self.solvers = solvers
        self.log = log
        self.active = 0
        self.switches = 0

    # ---------------------------------------------------------------- 内部

    def _label(self, index: int) -> str:
        return getattr(self.solvers[index], "label", None) or getattr(self.solvers[index], "name", f"#{index}")

    def _call(self, method: str, *args):
        """按顺序尝试各端点；成功的那个会被记住。"""
        order = [self.active] + [i for i in range(len(self.solvers)) if i != self.active]
        last_error: Exception | None = None
        for index in order:
            solver = self.solvers[index]
            try:
                result = getattr(solver, method)(*args)
            except SolverError as exc:
                last_error = exc
                if len(self.solvers) > 1:
                    self.log(f"  ⚠ 模型[{self._label(index)}]失败：{str(exc)[:120]}")
                continue
            if index != self.active:
                self.log(f"  ⇄ 已切换到模型[{self._label(index)}]")
                self.active = index
                self.switches += 1
            return result
        raise SolverError(f"所有模型端点都失败：{last_error}")

    # ---------------------------------------------------------------- 接口

    def solve(self, question: dict) -> Answer:
        return self._call("solve", question)

    def repair(self, question: dict, previous: str, feedback: str) -> Answer:
        return self._call("repair", question, previous, feedback)

    def health_check(self) -> None:
        """逐个体检，选中第一个可用的作为当前端点。"""
        errors: list[str] = []
        for index, solver in enumerate(self.solvers):
            check = getattr(solver, "health_check", None)
            if not callable(check):
                self.active = index
                return
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权都可能
                errors.append(f"{self._label(index)}: {str(exc)[:120]}")
                self.log(f"  ✗ 模型[{self._label(index)}]不可用：{str(exc)[:120]}")
                continue
            self.log(f"  ✓ 使用模型[{self._label(index)}]")
            self.active = index
            return
        raise SolverError("所有模型都不可用：" + "；".join(errors))
