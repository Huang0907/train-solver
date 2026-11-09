"""按配置构造 Solver。

支持「主模型 + 若干备用模型」：每个端点建一个 DeepSeekSolver，多于一个时用 FailoverSolver 包起来，
主模型 503/超时就自动切到备用（比如本地网关挂了就切 DeepSeek 官方 API）。
"""

from __future__ import annotations

from ..config import Config, ModelEndpoint
from .base import Solver, SolverError
from .deepseek import DeepSeekClient, DeepSeekSolver, api_key_from_env
from .failover import FailoverSolver
from .stub import PromptSolver, StubSolver, load_answer_file

_NO_KEY_HINT = (
    "未找到 DeepSeek API Key。三种填法任选其一：\n"
    "  1) 命令行 --api-key sk-xxx\n"
    "  2) 环境变量 setx DEEPSEEK_API_KEY sk-xxx\n"
    "  3) config.toml 里 [deepseek] api_key = \"sk-xxx\"\n"
    "想先离线跑通流程可用 --solver stub --answers fixtures/answers.json"
)


def _make_solver_for(config: Config, endpoint: ModelEndpoint, *, log) -> DeepSeekSolver:
    client = DeepSeekClient(
        endpoint.api_key,
        base_url=endpoint.base_url,
        model=endpoint.model,
        temperature=config.deepseek.temperature,
        max_tokens=config.deepseek.max_tokens,
        timeout=config.deepseek.timeout,
        retries=config.deepseek.retries,
        log=log,
    )
    solver = DeepSeekSolver(client, log=log)
    solver.label = endpoint.display()  # 供 FailoverSolver 打日志
    return solver


def build_solver(config: Config, *, log=print, answers_file: str | None = None) -> Solver:
    kind = (config.run.solver or "auto").lower()

    if kind == "stub":
        answers = load_answer_file(answers_file) if answers_file else {}
        return StubSolver(answers)
    if kind == "prompt":
        return PromptSolver()

    if kind not in ("auto", "deepseek"):
        raise SystemExit(f"未知 solver：{kind}（可选 auto / deepseek / stub / prompt）")

    if not config.deepseek.api_key:
        config.deepseek.api_key = api_key_from_env()
    if not config.deepseek.api_key:
        raise SystemExit(_NO_KEY_HINT)

    endpoints = config.deepseek.endpoints()
    if not endpoints:
        raise SystemExit(_NO_KEY_HINT)

    solvers = [_make_solver_for(config, endpoint, log=log) for endpoint in endpoints]
    if len(solvers) == 1:
        return solvers[0]
    log(f"已配置 {len(solvers)} 个模型端点，主模型失败会自动切换")
    return FailoverSolver(solvers, log=log)


__all__ = ["build_solver", "FailoverSolver", "SolverError"]
