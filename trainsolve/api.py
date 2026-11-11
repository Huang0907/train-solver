"""平台 HTTP 客户端。

所有接口路径与请求体形状都是从平台前端产物中还原出来的，见 README 的「接口速查」。
"""

from __future__ import annotations

import errno
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

DEFAULT_BASE_URL = "http://172.22.181.82/train/"

# sid 失效类状态码：服务端没受理这次请求，重登后重试是安全的（2026-11-05 P1-c）。
SESSION_EXPIRED_STATUSES = frozenset({401, 403})

# 幂等的写接口：同样的请求体重复发送，服务端状态一致（覆盖写）。
# 其余 POST 一律视为非幂等：读超时后不能自动重发，否则会多建记录 / 多扣额度 / 重复交卷。
IDEMPOTENT_POST_PATHS = frozenset({"api/student/post-answer"})


def _is_idempotent(method: str, path: str) -> bool:
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True
    return path.strip("/") in IDEMPOTENT_POST_PATHS


def _is_connect_failure(exc: BaseException) -> bool:
    """失败是否发生在「请求还没发出去」阶段 —— 这种可以无条件重试。"""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionError):
        return True
    if isinstance(reason, socket.gaierror):
        return True
    if isinstance(reason, TimeoutError):
        # urllib 把「连接阶段超时」包成 URLError(TimeoutError)；读超时则是裸 TimeoutError。
        # 包在 URLError 里说明连接还没建立，请求没发出去。
        return isinstance(exc, urllib.error.URLError)
    if isinstance(reason, OSError) and reason.errno in (
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EADDRNOTAVAIL,
    ):
        return True
    return False

# choose-q 返回码
CHOOSE_Q_OK = 0
CHOOSE_Q_NO_Q = 1
CHOOSE_Q_WEEK_FINISHED = 2
CHOOSE_Q_TOO_FREQUENT = 3   # 跳过后短时间内不许再抽题，返回 waitSeconds

# post-answer 返回码
POST_ANSWER_OK = 0
POST_ANSWER_RECORD_NOT_FOUND = 1
POST_ANSWER_SUBMITTED = 2
POST_ANSWER_EXAM_NOT_FOUND = 3
POST_ANSWER_STUDENT_NOT_FOUND = 4
POST_ANSWER_PING_TIME_OUT = 5
POST_ANSWER_BOUGHT_CODE = 6
POST_ANSWER_TIMEOUT = 7
POST_ANSWER_VERIFICATION_FAILED = 8
POST_ANSWER_DEADLINE_EXPIRED = 9
POST_ANSWER_NOT_STARTED = 10

POST_ANSWER_MESSAGES = {
    POST_ANSWER_RECORD_NOT_FOUND: "答题记录不存在",
    POST_ANSWER_SUBMITTED: "记录已提交",
    POST_ANSWER_EXAM_NOT_FOUND: "试卷不存在",
    POST_ANSWER_STUDENT_NOT_FOUND: "学生不存在",
    POST_ANSWER_PING_TIME_OUT: "心跳超时",
    POST_ANSWER_BOUGHT_CODE: "该题已被购买",
    POST_ANSWER_TIMEOUT: "已超时",
    POST_ANSWER_VERIFICATION_FAILED: "校验码错误（检查本机时钟/时区）",
    POST_ANSWER_DEADLINE_EXPIRED: "已过截止时间",
    POST_ANSWER_NOT_STARTED: "会话未开始",
}

# 重做类接口（reanswer / half-redo / rescore）返回业务码，不是布尔：
# 0 = 成功；1 = 平台拒绝（实测记录仍是「未完成」时 reanswer 返回 1）。
REDO_OK = 0

# 记录状态
STATE_UNDONE = 0
STATE_DONE = 1
STATE_SCORING = 2
STATE_SKIPPED = 3
STATE_DO_IT_LATER = 4
STATE_FAILED = 5

STATE_NAMES = {
    STATE_UNDONE: "未完成",
    STATE_DONE: "已完成",
    STATE_SCORING: "评分中",
    STATE_SKIPPED: "已跳过",
    STATE_DO_IT_LATER: "稍后做",
    STATE_FAILED: "失败",
}

# 题型：选择页 chip 的取值
QBASE_SINGLE = 0
QBASE_MULTI = 1
QBASE_JUDGE = 2
QBASE_BLANK = 3
QBASE_PROGRAM = 4

QBASE_NAMES = {
    QBASE_SINGLE: "单选题",
    QBASE_MULTI: "多选题",
    QBASE_JUDGE: "判断题",
    QBASE_BLANK: "填空题",
    QBASE_PROGRAM: "编程题",
}

# 题目 qType -> answerPaper 的分组键
QTYPE_BUCKET = {0: "q0", 1: "q1", 2: "q2", 3: "q3", 4: "q4", 5: "q4", 6: "q4", 7: "q4", 8: "q4", 9: "q4", 10: "q5"}
PROGRAMMING_QTYPES = frozenset({5, 6, 7, 8, 9})
OBJECTIVE_QTYPES = frozenset({0, 1, 2, 3, 4})


def qtype_kind(qtype: int) -> str:
    """返回 'programming' / 'objective' / 'design'。"""
    if qtype in PROGRAMMING_QTYPES:
        return "programming"
    if qtype in OBJECTIVE_QTYPES:
        return "objective"
    return "design"


class TrainError(Exception):
    """本项目所有可预期错误的基类。"""


class ApiError(TrainError):
    def __init__(self, message: str, status: int | None = None, body: str = "", reason: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.reason = reason      # "session-expired" / "empty-body" / ""

    def __str__(self) -> str:  # pragma: no cover - 展示用
        extra = f" (HTTP {self.status})" if self.status else ""
        return f"{super().__str__()}{extra}"


class LoginError(TrainError):
    pass


class NoQuestionAvailable(TrainError):
    pass


class WeekFinished(TrainError):
    pass


class TooFrequent(TrainError):
    """选得太快：平台要求等 wait_seconds 秒后再抽。"""

    def __init__(self, wait_seconds: float) -> None:
        super().__init__(f"选题过于频繁，需等待 {wait_seconds:.0f} 秒")
        self.wait_seconds = wait_seconds


class NotSubmitted(ApiError):
    """提交接口返回了非 2xx。"""


@dataclass
class Session:
    """登录会话。"""

    sid: str
    student_id: int = 0
    code: str = ""
    name: str = ""
    expire: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Session":
        return cls(
            sid=str(payload.get("sid", "")),
            student_id=int(payload.get("id") or 0),
            code=str(payload.get("code") or ""),
            name=str(payload.get("name") or ""),
            expire=int(payload.get("expire") or 0),
            raw=payload,
        )

    def to_json(self) -> dict[str, Any]:
        return {"sid": self.sid, "id": self.student_id, "code": self.code, "name": self.name, "expire": self.expire}


class TrainClient:
    """平台 API 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 25.0,
        retries: int = 3,
        session: Session | None = None,
        log=print,
    ) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.retries = retries
        self.session = session
        self.log = log
        self._null_body_variants: dict[str, bytes | None] = {}   # 按端点分别探测/缓存请求体形态
        self._relogin_hook: Callable[[], bool] | None = None
        self._relogin_busy = False

    def enable_auto_relogin(self, code: str, password: str) -> None:
        """装上「sid 失效就重登一次」的钩子。

        只在请求报出 sid 失效类错误时触发；**启动时不做探活**（那会白花一次 auth 写调用）。
        """
        def _relogin() -> bool:
            if self._relogin_busy:
                return False
            self._relogin_busy = True
            try:
                self.login(code, password)
                return True
            except TrainError:
                return False
            finally:
                self._relogin_busy = False

        self._relogin_hook = _relogin

    # ---------------------------------------------------------------- 基础

    @property
    def sid(self) -> str:
        return self.session.sid if self.session else ""

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        query: dict[str, Any] = {"sid": self.sid}
        if params:
            query.update(params)
        qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        return f"{self.base_url}{path}?{qs}"

    def _open(self, request: urllib.request.Request, *, idempotent: bool = True) -> tuple[int, bytes]:
        """发请求；重试策略取决于这次请求是否幂等（2026-11-05 修）。

        规则：
        * 5xx：服务端已经回过话，只有幂等请求才重试；
        * 连接建立失败（请求没发出去）：任何请求都可以重试；
        * 读超时 / 其他网络异常：幂等请求重试，非幂等请求立刻停下并报「结果未知」，
          因为此刻服务端可能已经执行了。
        """
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:  # 4xx/5xx
                body = exc.read()
                if exc.code >= 500 and idempotent and attempt < self.retries:
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return exc.code, body
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if _is_connect_failure(exc) or idempotent:
                    if attempt < self.retries:
                        time.sleep(1.5 * (attempt + 1) + random.random())
                        continue
                    raise ApiError(f"网络错误：{exc}") from exc
                raise ApiError(
                    f"{request.get_method()} {request.full_url.split('?')[0]} 请求可能已被服务端执行"
                    f"（结果未知），未自动重试：{exc}"
                ) from exc
        raise ApiError(f"网络错误：{last}")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
        _relogin: bool = True,
    ) -> Any:
        if _relogin:
            return self._with_relogin(
                lambda: self._request_once(method, path, params=params, body=body, raw_body=raw_body)
            )
        return self._request_once(method, path, params=params, body=body, raw_body=raw_body)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
    ) -> Any:
        data = raw_body if raw_body is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(self._url(path, params), data=data, method=method)
        request.add_header("Accept", "application/json, text/plain, */*")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        status, payload = self._open(request, idempotent=_is_idempotent(method, path))
        text = payload.decode("utf-8", "replace")
        if status >= 400:
            raise ApiError(
                f"{method} {path} 失败",
                status=status,
                body=text[:500],
                reason="session-expired" if status in SESSION_EXPIRED_STATUSES else "",
            )
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(f"{method} {path} 返回非 JSON：{text[:200]}") from exc

    def _with_relogin(self, call: Callable[[], Any], *, allow_empty_body: bool = False) -> Any:
        """P1-c：sid 失效时重登一次、把这次请求重试一次（只在出错时触发，不额外探活）。

        401/403 说明服务端根本没受理这次请求，任何方法都安全；「读记录拿到空体」
        只对只读请求生效，避免把非幂等写请求再打一遍。
        """
        try:
            return call()
        except ApiError as exc:
            expired = exc.status in SESSION_EXPIRED_STATUSES
            empty = getattr(exc, "reason", "") == "empty-body"
            if not (expired or (allow_empty_body and empty)):
                raise
            if self._relogin_hook is None or self._relogin_busy:
                raise
            if not self._relogin_hook():
                raise
            return call()          # 只重试一次，再失败就把错误抛出去


    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, params=params or None, body=body)

    def post_no_body(self, path: str, **params: Any) -> Any:
        """提交类接口前端传的是 null body，这里自动探测可用的请求体形态。

        形态**按端点分别缓存**（0.6.10 审计）：submit 接受的形态不一定被 reanswer /
        half-redo 接受，全局共用一份会把「先提交再重答」变成 400，而新进程直接重答
        却能成功——同一操作因调用顺序不同而行为不同，属于逻辑缺陷。
        """
        if path in self._null_body_variants:
            return self._post_variant(path, self._null_body_variants[path], params)

        for variant in (b"null", b"{}", None):
            try:
                result = self._post_variant(path, variant, params)
            except ApiError as exc:
                if exc.status in (400, 415):
                    continue
                raise
            self._null_body_variants[path] = variant
            return result
        raise ApiError(f"POST {path} 的所有请求体形态都被拒绝", status=415)

    def _post_variant(self, path: str, variant: bytes | None, params: dict[str, Any]) -> Any:
        return self.request("POST", path, params=params or None, raw_body=variant)

    # ---------------------------------------------------------------- 业务

    def login(self, code: str, password: str) -> Session:
        payload = self.request(
            "POST", "api/student/auth", params={"sid": ""}, body={"code": code, "pwd": password}, _relogin=False
        )
        if not isinstance(payload, dict) or not payload.get("sid"):
            raise LoginError(f"登录失败：{payload}")
        self.session = Session.from_payload(payload)
        return self.session

    def use_session(self, session: Session) -> None:
        self.session = session

    def tags(self, qbase_type: int, level: int = 0) -> list[dict[str, Any]]:
        data = self.get("api/student/list-tag", qBaseType=qbase_type, level=level)
        return list(data or [])

    def available_tags(self, qbase_type: int, level: int = 0) -> list[dict[str, Any]]:
        return [tag for tag in self.tags(qbase_type, level) if tag.get("available") is not False]

    def choose(self, qbase_type: int, level: int = 0, tag_id: int = -1) -> str:
        number = 5 if qbase_type < 4 else 1
        payload = [{"qBaseType": qbase_type, "number": number, "level": level, "tagId": tag_id}]
        result = self.post("api/student/choose-q", payload)
        code = (result or {}).get("code")
        if code == CHOOSE_Q_OK:
            return str(result["sessionId"])
        if code == CHOOSE_Q_NO_Q:
            raise NoQuestionAvailable("暂无新题（该标签下的题目都已做过）")
        if code == CHOOSE_Q_WEEK_FINISHED:
            raise WeekFinished("本周题量已达上限")
        if code == CHOOSE_Q_TOO_FREQUENT:
            raise TooFrequent(float((result or {}).get("waitSeconds") or 60))
        raise ApiError(f"选题失败：{result}")

    def paper(self, session_id: str) -> dict[str, Any]:
        """取答题记录。空体/非 dict 一律抛 ApiError（2026-11-05 修）。

        旧实现把 `None` 原样返回，`runner` 里 `record.get(...)` 直接抛
        AttributeError，绕过 `run()` 的异常分支把线程/进程掀掉。

        空体也可能是 sid 失效的表现，所以这里带一次「重登后重试」（P1-c）；
        重试仍空就会把错误抛出去，交给上层诊断。
        """
        return self._with_relogin(lambda: self._paper_once(session_id), allow_empty_body=True)

    def _paper_once(self, session_id: str) -> dict[str, Any]:
        payload = self.get(f"api/student/record/{session_id}")
        if not isinstance(payload, dict) or not payload:
            raise ApiError(
                f"取答题记录失败：平台返回空值（session={session_id[:8]}…），"
                "可能 sid 已失效或记录已被清理",
                reason="empty-body",
            )
        return payload

    def post_answer(self, session_id: str, answer_paper: dict[str, Any], verification_code: str) -> dict[str, Any]:
        body = {"sessionId": session_id, "answerPaper": answer_paper, "verificationCode": verification_code}
        return self.post("api/student/post-answer", body)

    def submit(self, session_id: str) -> Any:
        return self.post_no_body("api/student/submit", sessionId=session_id)

    def score(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"api/student/score-result/{session_id}") or [])

    def run_code(self, code: str, language: int, stdin_text: str) -> list[dict[str, Any]]:
        """服务端编译试跑。language 传题目 language-1（C=0）。"""
        payload = {"code": code, "language": language, "input": [stdin_text]}
        return list(self.post("api/student/run-code", payload) or [])

    def format_code(self, code: str, language: int) -> str:
        result = self.post("api/student/format-code", {"code": code, "language": language})
        return str((result or {}).get("data") or code)

    def history(self, tag_id: int = 0, index: int = 0, size: int = 10, search: str = "") -> dict[str, Any]:
        return self.post("api/student/history", {"index": index, "size": size, "search": search}, tagId=tag_id)

    def wait_score(self, session_id: str, timeout: float = 300.0, interval: float = 2.5, log=None) -> list[dict[str, Any]]:
        """轮询评分结果，直到拿到非空且 state 不再是「评分中」。"""
        deadline = time.time() + timeout
        log = log or self.log
        last: list[dict[str, Any]] = []
        while time.time() < deadline:
            last = self.score(session_id)
            if last and all(_score_settled(item) for item in last):
                return last
            time.sleep(interval)
        log(f"评分轮询超时（{timeout:.0f}s），返回最后一次结果")
        return last

    def wait_record(self, session_id: str, timeout: float = 300.0, interval: float = 3.0, log=None,
                    should_stop=None) -> dict[str, Any]:
        """轮询答题记录状态，直到离开「未完成/评分中」。

        比只轮询 score-result 更可靠：平台判题是异步队列，score-result 可能长时间为空，
        但记录的 state 一旦变成「已完成/失败」就说明判完了。

        传入 `should_stop` 后，每轮先看一次停止标志：用户点了停止就立刻返回手上的
        最后一条记录（可能是「评分中」），把「要不要继续等」交给调用方决定（0.6.6）。
        """
        deadline = time.time() + timeout
        log = log or self.log
        record: dict[str, Any] = {}
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                log("  已请求停止：不再等评分，记录留给平台自己判")
                return record
            try:
                record = self.paper(session_id)
            except ApiError as exc:
                # 轮询是只读的：读失败（空体/瞬时抖动）继续等，别把「等待评分」整段打断。
                log(f"  读取答题记录失败（{exc}），继续等待…")
                time.sleep(interval)
                continue
            state = int(record.get("state", STATE_UNDONE))
            if state not in (STATE_UNDONE, STATE_SCORING):
                return record
            time.sleep(interval)
        log(f"等待评分超时（{timeout:.0f}s），返回最后一次记录状态")
        return record

    # ------------------------------------------------------------- 重做

    # 平台这几个接口返回的是**业务码**（不是布尔）：0 = 成功，1 = 拒绝。
    # 实测 2026-11-02：记录还是「未完成」时 reanswer 一律返回 1（前端按钮也不可用），
    # 旧代码把它当 truthy 当成功处理，导致后续状态判断才兜住。
    def reanswer(self, session_id: str) -> int:
        """重新答题（前端按钮「重新答题」）。返回业务码：0=成功，1=平台拒绝。"""
        return _redo_code(self.post_no_body("api/student/reanswer", sessionId=session_id))

    def half_redo(self, session_id: str) -> int:
        """折半重做（重做后总分按折半计；实测成功返回 0，并消耗 2 次重置额度）。"""
        return _redo_code(self.post_no_body("api/student/half-redo", sessionId=session_id))

    def rescore(self, session_id: str) -> int:
        """对已提交的答案重新计分（不重做）。返回业务码。"""
        return _redo_code(self.post_no_body("api/student/rescore", sessionId=session_id))

    def skip_this(self, session_id: str) -> Any:
        """跳过当前题（前端按钮「此题重复我要跳过」），记录会被置为「跳过」。"""
        return self.post_no_body("api/student/skip-this", sessionId=session_id)

    def cancel_skip(self, session_id: str) -> Any:
        """取消跳过（会删除此次记录，同一道题可能再被抽到）。"""
        return self.post_no_body("api/student/cancel-skip", sessionId=session_id)

    def reset_quota(self) -> dict[str, Any]:
        return self.get("api/student/get-reset") or {}


def _redo_code(payload: Any) -> int:
    """把重做类接口的返回值归一成业务码（0=成功，1=拒绝）；认不出返回 -1。"""
    if isinstance(payload, bool):
        return -1
    if isinstance(payload, int):
        return payload
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return -1


def _score_settled(item: dict[str, Any]) -> bool:
    detail = item.get("scoreResult") or {}
    if not detail:
        return False
    state = detail.get("state")
    if state is None:
        return True
    return state >= 0


def total_score(results: Iterable[dict[str, Any]]) -> float:
    return round(sum(float(item.get("score") or 0) for item in results), 2)
