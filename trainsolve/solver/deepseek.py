"""DeepSeek（OpenAI 兼容）Chat Completions 客户端 + Solver 实现。

只依赖标准库 urllib，不需要 openai SDK。
把 `base_url` 指向任意 OpenAI 兼容网关（vLLM / one-api / 本地模型）即可换后端。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .. import prompts
from .base import Answer, SolverError

DEFAULT_BASE_URL = "https://api.deepseek.com"
# 官方模型 id（本地网关那种 cn:xxx 的私有 id 只写在 config.toml 里）
DEFAULT_MODEL = "deepseek-flash"
# 这是推理型模型：思维链也算 completion token。4096 会让中等难度的题
# 只吐出「内心独白」而没有代码，实测 16000 才稳定。
DEFAULT_MAX_TOKENS = 16000


class DeepSeekClient:
    """极简 Chat Completions 客户端。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = 180.0,
        retries: int = 3,
        log=print,
    ) -> None:
        if not api_key:
            raise SolverError("缺少 DeepSeek API Key（--api-key / DEEPSEEK_API_KEY / config.toml）")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.log = log

    # -------------------------------------------------------------- HTTP

    def _endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def chat(self, messages: list[dict], *, temperature: float | None = None,
             max_tokens: int | None = None, warn_on_truncation: bool = True) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(self._endpoint(), data=data, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8", "replace"))
                choices = body.get("choices") or []
                if choices and choices[0].get("finish_reason") == "length" and warn_on_truncation:
                    # 2026-11-05：只有「求解」路径才报这条；探针的截断不算错误（见 health_check）。
                    self.log(
                        f"⚠ 求解输出被 max_tokens={payload['max_tokens']} 截断（推理型模型会把思维链算进 completion），"
                        "必要时调大 [deepseek].max_tokens"
                    )
                return self._content_of(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = 2 ** (attempt + 1)
                    self.log(f"DeepSeek 返回 {exc.code}，{delay}s 后重试…")
                    time.sleep(delay)
                    last_error = SolverError(f"DeepSeek HTTP {exc.code}: {detail}")
                    continue
                raise SolverError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    delay = 2 ** (attempt + 1)
                    self.log(f"DeepSeek 连接异常（{exc}），{delay}s 后重试…")
                    time.sleep(delay)
                    last_error = exc
                    continue
                raise SolverError(f"DeepSeek 连接失败：{exc}") from exc

        raise SolverError(f"DeepSeek 调用失败：{last_error}")

    @staticmethod
    def _content_of(body: dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            raise SolverError(f"DeepSeek 返回无 choices：{json.dumps(body)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):  # 部分网关返回分段数组
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)


def _looks_like_c(code: str) -> bool:
    """粗略判断是不是一段能编译的 C 代码，用来识别「模型只输出了思考过程」。"""
    if not code or len(code.strip()) < 40:
        return False
    text = code.strip()
    markers = ("#include", "int main", "void main", "main(", "printf(")
    return any(marker in text for marker in markers)


_REASON_ONLY_NUDGE = (
    "你上一轮只输出了思考过程，没有给出代码。现在不要输出任何思考、解释或 Markdown 说明，"
    "直接输出一个 ```c 代码块，里面是完整可编译的程序。"
)


class DeepSeekSolver:
    """用 DeepSeek 解题。"""

    name = "deepseek"

    def __init__(self, client: DeepSeekClient, *, log=print) -> None:
        self.client = client
        self.log = log

    def _ask_for_code(self, messages: list[dict], *, label: str = "") -> str:
        """要代码；拿到的不是代码（例如推理型模型只吐了思维链）就加大预算重问一次。"""
        raw = self.client.chat(messages)
        code = prompts.extract_code_block(raw)
        if _looks_like_c(code):
            return code

        self.log(f"  模型没给出可用代码{label}，加大 token 预算并要求「只输出代码」后重试…")
        retry_messages = list(messages) + [
            {"role": "assistant", "content": (raw or "")[:4000] or "(空)"},
            {"role": "user", "content": _REASON_ONLY_NUDGE},
        ]
        raw2 = self.client.chat(retry_messages, temperature=0, max_tokens=self.client.max_tokens * 2)
        code2 = prompts.extract_code_block(raw2)
        if _looks_like_c(code2):
            return code2
        preview = (raw2 or raw or "")[:200].replace("\n", " ")
        raise SolverError(f"模型未返回可识别的 C 代码（可能是思维链被截断）：{preview}")

    HEALTH_CHECK_MAX_TOKENS = 64

    def health_check(self) -> None:
        """开跑前探一次模型（网关常见 503/no_healthy_account，早发现少开会话）。

        探针只关心「能不能拿到非空回复」。推理型模型会把思维链算进 completion，
        给少了必然 `finish_reason=length`，所以：
        * 用探针自己的预算 64（不借用求解的 16000，也不再用必然截断的 8）；
        * `length` 截断对探针不算错误，因此关掉截断告警（求解路径仍然照旧告警）。
        """
        reply = self.client.chat(
            [{"role": "user", "content": "回复 ok"}],
            temperature=0,
            max_tokens=self.HEALTH_CHECK_MAX_TOKENS,
            warn_on_truncation=False,
        )
        if not (reply or "").strip():
            raise SolverError("模型自检返回空响应（网关可能没把请求转给可用账号）")

    def solve(self, question: dict) -> Answer:
        qtype = int(question.get("qType") or 0)
        if qtype >= 5:
            messages = prompts.build_programming_messages(question)
            code = self._ask_for_code(messages, label=f"（Q{question.get('id')}）")
            return Answer(text=code, kind="code", raw=code)
        if qtype <= 4:
            options = question.get("options") or []
            messages = prompts.build_objective_messages(question, options)
            raw = self.client.chat(messages)
            data = prompts.extract_json_object(raw)
            answer = str(data.get("answer") or "").strip()
            if not answer:
                answer = raw.strip().splitlines()[0].strip() if raw.strip() else ""
            return Answer(text=answer, kind="answer", rationale=str(data.get("reason") or ""), raw=raw)
        raise SolverError(f"不支持的题型 qType={qtype}（设计题）")

    def repair(self, question: dict, previous: str, feedback: str) -> Answer:
        messages = prompts.build_repair_messages(question, previous, feedback)
        code = self._ask_for_code(messages, label="（修复轮）")
        return Answer(text=code, kind="code", raw=code)


def api_key_from_env() -> str:
    for name in ("DEEPSEEK_API_KEY", "TRAINSOLVE_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""
