#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TrainSolve 客户端 —— 无头只读冒烟（login → list-tag → record → stop）。

设计原则（全部硬编码，不提供放松开关）
--------------------------------------
1. 只读：在**传输层**按路径白名单拦截，只放行
       api/student/auth          登录（鉴权，不产生业务数据）
       api/student/list-tag      取标签（GET）
       api/student/record/<sid>  读题（GET，针对既有会话）
   其余任何请求（尤其 choose-q / post-answer / submit / skip-this /
   run-code / format-code / reanswer）在**发出前**抛 ForbiddenCall，
   一个字节都不会离开本机。
2. 前置断言：加载配置后先断言 run.submit is False 且 run.dry_run is True，
   不满足直接退出（退出码 2），绝不"先跑起来再说"。
3. 凭据：只从环境变量 TRAINSOLVE_USER / TRAINSOLVE_PASSWORD 或 config.toml 读，
   不写进脚本；报告与日志对密码、sid、api_key 全程脱敏。
4. 退出码：
       0 全通
       2 前置断言不满足 / 凭据缺失 / 配置读不到
       3 登录失败
       4 网络不可达
       5 取标签失败
       6 读题失败
       7 出现被禁止的接口调用
       9 报告里检出疑似明文凭据（自检失败）
       1 其他未预期异常

用法
----
    python scripts/smoke_readonly.py [--config PATH] [--session SID] [--out-dir DIR]
    python scripts/smoke_readonly.py --self-test      # 只验白名单/防护，不联网

    # 凭据也可以走环境变量（优先级高于 config.toml）
    set TRAINSOLVE_USER=2026xxxx
    set TRAINSOLVE_PASSWORD=******
    python scripts/smoke_readonly.py

附：将来若还要驱动 GUI（本脚本不做这件事），界面 2026-11-05 踩过的三个 Win32 坑
----------------------------------------------------------------------------
1. 单文件 exe 的窗口在**子进程**上：`Start-Process -PassThru` 拿到的 pid 的
   `MainWindowHandle` 恒为 0，按它判"窗口起没起"永远判错。按窗口标题找：
   `程序设计训练平台 · 自动刷题客户端 vX.Y.Z`（版本号随包变，用前缀匹配；它会是
   bootloader pid 的子进程）。
2. ctypes 调 Win32 **必须先 `SetProcessDpiAwarenessContext(-4)`**，并给
   `SetWindowPos` / `ClientToScreen` / `WindowFromPoint` 设全 argtypes/restype。
   不设的话句柄被截断（32 位截 64 位）、`SetWindowPos` 静默失败（不置顶也不报错）、
   坐标被 DPI 虚拟化——界面靠这个连错三次，打到了 Chrome 和密码框上。
3. 150% 缩放下 1180×800 逻辑尺寸 = 1794×1259 物理像素；同一版式的坐标不能跨缩放比复用。

Tk 8.6 在 Windows 上不实现 UIA provider（本客户端实测：56 个 UIA 元素全是无名
`TkChild`/`Pane`，`role:Button` 只有标题栏三个系统按钮，`role:Edit` 为 0），
所以 GUI 路径没有语义定位可用，只有"真实 SendInput + 坐标 + 三步校验"一条路。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from trainsolve.api import ApiError, LoginError, TrainClient  # noqa: E402
from trainsolve.config import load_config  # noqa: E402

# 传输层白名单：只放行这三个路径前缀
ALLOWED_PATHS = (
    "api/student/auth",
    "api/student/list-tag",
    "api/student/record/",
)

# 语义层的二次防线（即使有人改白名单，这些方法也先炸）
FORBIDDEN_METHODS = (
    "choose",
    "submit",
    "post_answer",
    "skip_this",
    "cancel_skip",
    "run_code",
    "format_code",
    "reanswer",
    "half_redo",
    "rescore",
    "get_reset",
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_PRECONDITION = 2
EXIT_LOGIN = 3
EXIT_NETWORK = 4
EXIT_TAGS = 5
EXIT_PAPER = 6
EXIT_FORBIDDEN = 7
EXIT_SELFTEST = 8
EXIT_LEAK = 9

# 写接口标记：**判定之前**先按出现次数累加"尝试"，避免"放行日志里没有"被当成"没尝试过"
WRITE_PATH_MARKERS = ("choose-q", "submit", "post-answer", "run-code")

# 已知残留：记录在案、明确不修（每次运行随证据一起落盘，避免以后被当成新发现）
KNOWN_RESIDUALS = {
    "NOTES.md": """# 已知残留（Known residuals）

本文件由冒烟脚本生成，随证据一起归档。下列各项**有意不修**，写在这里避免下次被当成新发现或被顺手改掉。

1. `write_attempts` 只覆盖 `choose-q` / `submit` / `post-answer` / `run-code` 四个标记，
   不是"全量零写"的证据。`skip-this` / `reanswer` 等写接口靠传输层白名单兜底，
   但不在计数口径内——报告里的口径文字为准，口头不要升级成"全量零写"。
2. `negative-precondition.txt` 不走 temp→校验→原子改名链路（内容在写盘前已过 Redactor）。
3. 泄漏分支下 `run.json` 里记的 `exit_code` 与进程最终退出码可能不一致
   （只在检测到泄漏并脱敏时触发，至今未触发）。
4. mutation 变异体已留 patch（见 `20261105-mutation-check\\m1-*.patch`、`m2-*.patch`），
   但没有自动触发机制：改动 `guarded_open` 的计数/归一化逻辑或自检用例后，必须手工重跑这两处 mutation。
5. 本目录 `script.py` 是当次运行的源码副本，**不参与泄漏扫描**（扫描对象是报告；源码按 sha 校验）。
   若账号密码本身是纯数字，扫源码会命中十六进制字符集那类字面量，属于假阳性，
   所以源码副本单独写入、不进扫描链路。
""",
}
METHOD_TO_MARKER = {
    "choose": "choose-q",
    "submit": "submit",
    "post_answer": "post-answer",
    "run_code": "run-code",
    "skip_this": "skip-this",
    "cancel_skip": "cancel-skip",
    "format_code": "format-code",
    "reanswer": "reanswer",
    "half_redo": "half-redo",
    "rescore": "rescore",
    "get_reset": "get-reset",
}


class ForbiddenCall(RuntimeError):
    """请求路径不在只读白名单里。"""


class Abort(Exception):
    """带退出码的提前终止。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


class Redactor:
    """把已知敏感串替换掉；宁可多掩，不可漏。"""

    def __init__(self) -> None:
        self.secrets: list[str] = []

    def add(self, value: str | None) -> None:
        if value and len(value) >= 3 and value not in self.secrets:
            self.secrets.append(value)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise Abort(EXIT_PRECONDITION, f"指定的配置文件不存在：{path}")
        return path
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "TrainSolve" / "config.toml"
        if candidate.exists():
            return candidate
    candidate = Path.cwd() / "config.toml"
    if candidate.exists():
        return candidate
    raise Abort(EXIT_PRECONDITION, "找不到 config.toml（既不在 %APPDATA%\\TrainSolve 也不在当前目录）")


def newest_local_session(out_dir: Path) -> str | None:
    if not out_dir.is_dir():
        return None
    best: tuple[float, str] | None = None
    for record in out_dir.glob("*/record.json"):
        try:
            mtime = record.stat().st_mtime
        except OSError:
            continue
        sid = record.parent.name
        try:
            int(sid, 16)  # 用解析代替写死十六进制字符集，免得密码撞上字面量
        except (TypeError, ValueError):
            continue
        if len(sid) == 32:
            if best is None or mtime > best[0]:
                best = (mtime, sid)
    return best[1] if best else None


class Smoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.redact = Redactor()
        self.steps: list[dict] = []
        self.calls: list[dict] = []
        self.console: list[str] = []
        self.forbidden_hits: list[str] = []
        # 写接口"尝试"计数：在放行判定之前累加，和放行日志(Self.calls)互不依赖
        self.write_attempts: dict[str, int] = {marker: 0 for marker in WRITE_PATH_MARKERS}
        # 本脚本派生的子进程：记录 rc，用来替代"无后台进程"这类无根据断言
        self.child_procs: list[dict] = []
        self.exit_code = EXIT_OK
        self.failure = ""
        self.config_path: Path | None = None
        self.config_sha = ""
        self.session_id = ""

    # ------------------------------------------------------------------ 输出
    def say(self, text: str) -> None:
        safe = self.redact(text)
        self.console.append(safe)
        try:
            print(safe, flush=True)
        except UnicodeEncodeError:  # 老控制台
            print(safe.encode("ascii", "replace").decode("ascii"), flush=True)

    def step(self, name: str, ok: bool, started: float, detail: str = "") -> dict:
        record = {
            "step": name,
            "ok": ok,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "detail": self.redact(detail),
        }
        self.steps.append(record)
        self.say(f"[{'OK  ' if ok else 'FAIL'}] {name} {record['duration_ms']:>8.1f} ms  {record['detail']}")
        return record

    # ------------------------------------------------------------------ 防护
    def install_guards(self, client: TrainClient) -> None:
        redact = self.redact
        calls = self.calls
        hits = self.forbidden_hits
        attempts = self.write_attempts
        allowed = ALLOWED_PATHS
        original_open = client._open

        def guarded_open(request, **kwargs):  # noqa: ANN001, ANN003
            """只检查 request 本身，重试相关参数（如 idempotent）原样转发。

            `api.py` 的 `_open(request, *, idempotent=...)` 在 2026-11-05 加了关键字
            参数；守卫若只收一个位置参数，`login()` 一进来就 TypeError（当天实测整条
            只读链路因此停摆）。这里显式接收并**转发**给原实现：拦截只看 URL 与
            method，重试语义仍按客户端真实行为走，不因为套了守卫就变了。
            """
            url = request.full_url
            raw = url.split("?", 1)[0]
            base = client.base_url
            if raw.startswith(base):
                raw = raw[len(base):]

            # 0) 先解百分号编码：posixpath.normpath 不认 %2e%2e，必须先解码再判定
            decoded = urllib.parse.unquote(raw)
            if "%" in decoded:  # 解码后仍有 %：双重编码或畸形，直接拒
                hits.append(f"{request.get_method()} {raw}")
                raise ForbiddenCall(f"只读冒烟：路径含残余百分号编码，拒绝发出 {request.get_method()} {raw}")

            # 1) 先记"尝试"：与是否放行无关，否则"日志里没有"就是构造性恒真
            for marker in WRITE_PATH_MARKERS:
                if marker in decoded:
                    attempts[marker] = attempts.get(marker, 0) + 1

            # 2) 路径归一化后再判白名单，堵 record/../submit 这类前缀绕过
            normalized = posixpath.normpath(decoded)
            if ".." in decoded.split("/") or normalized != decoded or normalized.startswith(("/", "\\")):
                hits.append(f"{request.get_method()} {raw}")
                raise ForbiddenCall(f"只读冒烟：路径不规范，拒绝发出 {request.get_method()} {raw}")

            if not normalized.startswith(allowed):
                hits.append(f"{request.get_method()} {normalized}")
                raise ForbiddenCall(f"只读冒烟：拒绝发出 {request.get_method()} {normalized}")

            started = time.perf_counter()
            status, payload = original_open(request, **kwargs)
            calls.append(
                {
                    "method": request.get_method(),
                    "path": redact(normalized),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
            return status, payload

        client._open = guarded_open  # type: ignore[method-assign]

        def refuse(name: str):
            marker = METHOD_TO_MARKER.get(name, name)

            def _refuse(*_a, **_k):
                attempts[marker] = attempts.get(marker, 0) + 1
                hits.append(f"method:{name}")
                raise ForbiddenCall(f"只读冒烟：禁止调用 TrainClient.{name}()")

            return _refuse

        for name in FORBIDDEN_METHODS:
            if hasattr(client, name):
                setattr(client, name, refuse(name))

    # ------------------------------------------------------------------ 自检
    def run_self_test(self) -> int:
        """不联网验证白名单真的按预期拦/放，产物写到 out-dir/guard-selftest.txt。"""
        import urllib.request

        client = TrainClient("http://127.0.0.1:9/", timeout=1.0, retries=0, log=lambda _m: None)
        self.install_guards(client)
        cases: list[tuple[str, str, object]] = []

        def add(name: str, expected: str, fn) -> None:  # noqa: ANN001
            try:
                fn()
            except ForbiddenCall:
                got = "ForbiddenCall"
            except ApiError:
                got = "ApiError(网络)"
            except Exception as exc:  # noqa: BLE001
                got = type(exc).__name__
            else:
                got = "无异常"
            cases.append((name, expected, got))

        def forbidden_request(path: str, method: str, data: bytes | None = None):
            def _call():
                client._open(urllib.request.Request(f"http://127.0.0.1:9/{path}?sid=x", data=data, method=method))

            return _call

        add("semantic-guard", "ForbiddenCall", lambda: client.choose(4, 2, -1))
        add("transport-choose-q", "ForbiddenCall", forbidden_request("api/student/choose-q", "POST", b"[]"))
        add("transport-submit", "ForbiddenCall", forbidden_request("api/student/submit", "POST", b"null"))
        add("transport-post-answer", "ForbiddenCall", forbidden_request("api/student/post-answer", "POST", b"{}"))
        add("transport-run-code", "ForbiddenCall", forbidden_request("api/student/run-code", "POST", b"{}"))
        # 前缀绕过：归一化后必须拒绝，不能因为 startswith("api/student/record/") 放过去
        add(
            "traversal-record-to-submit",
            "ForbiddenCall",
            forbidden_request("api/student/record/../submit", "POST", b"null"),
        )
        add(
            "traversal-record-to-choose",
            "ForbiddenCall",
            forbidden_request("api/student/record/../choose-q", "POST", b"[]"),
        )
        # 百分号编码绕过：normpath 不解码，必须先 unquote 再判
        add(
            "traversal-encoded-record-to-submit",
            "ForbiddenCall",
            forbidden_request("api/student/record/%2e%2e/submit", "POST", b"null"),
        )
        # 正向对照：白名单内的路径必须放过去（这里只会在网络层失败，说明没被白名单误杀）
        add("allow-list-permits-list-tag", "ApiError(网络)", forbidden_request("api/student/list-tag", "GET"))

        out = self._out_dir()
        out.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 只读防护自检 {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"# 白名单: {', '.join(ALLOWED_PATHS)}",
            "",
        ]
        all_ok = True
        for name, expected, got in cases:
            ok = expected == got
            all_ok = all_ok and ok
            lines.append(f"{'PASS' if ok else 'FAIL'}  {name:32s} expect={expected:16s} got={got}")
        # 尝试计数必须真的动了：证明它不是"放行日志里没有"的同义反复
        counted = {key: value for key, value in self.write_attempts.items()}
        counters_ok = all(counted.get(key, 0) >= 1 for key in WRITE_PATH_MARKERS)
        all_ok = all_ok and counters_ok
        lines.append(
            f"{'PASS' if counters_ok else 'FAIL'}  attempt-counters                "
            f"expect=每个>=1 got={counted}"
        )
        lines.append("")
        lines.append("RESULT: " + ("ALL PASS" if all_ok else "FAILED"))
        text = "\n".join(lines)
        (out / "guard-selftest.txt").write_text(text, encoding="utf-8")
        self.say(text)
        return EXIT_OK if all_ok else EXIT_SELFTEST

    # ------------------------------------------------------------------ CLI
    def _run_cli_peek(self, sid: str) -> tuple[bool, str]:
        """跑一次真实 CLI：`trainsolve peek --session <sid>`，验 CLI 确实接到 peek_existing。

        会话 id 走环境变量，**不进 argv**（argv 会出现在进程列表里）。
        """
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["TRAINSOLVE_PEEK_SID"] = sid
        env["TRAINSOLVE_PEEK_CONFIG"] = str(self.config_path)
        launcher = (
            "import os, sys; "
            "from trainsolve.cli import main; "
            "sys.exit(main(['peek', '--session', os.environ['TRAINSOLVE_PEEK_SID'], "
            "'--config', os.environ['TRAINSOLVE_PEEK_CONFIG']]))"
        )
        record: dict = {"kind": "cli-peek", "cmd": "trainsolve peek --session <env>", "pid": None, "returncode": None}
        self.child_procs.append(record)
        out = err = ""
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", launcher],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            record["pid"] = proc.pid
            out, err = proc.communicate(timeout=max(60.0, self.args.timeout * 6))
            record["returncode"] = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            record["returncode"] = "timeout"
            return False, "CLI peek 超时"
        text = (out or "") + (err or "")
        marks = []
        if "题目已保存到" in text:
            marks.append("saved=yes")
        if "choose-q" in text:
            marks.append("mentions-choose-q")
        if "Traceback" in text:
            marks.append("traceback")
        detail = f"rc={record['returncode']} pid={record['pid']} out_bytes={len(text)} " + " ".join(marks)
        ok = record["returncode"] == 0 and "题目已保存到" in text and "Traceback" not in text
        return ok, detail

    # ------------------------------------------------------------------ 负向
    def _run_negative_precondition(self) -> tuple[bool, str]:
        """拿一份"不安全配置"跑子进程，断言它 exit 2 且不联网。"""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="smoke-neg-") as tmpdir:
            tmp = Path(tmpdir)
            blob = self.config_path.read_bytes().decode("latin-1")
            blob = blob.replace("submit = false", "submit = true").replace("dry_run = true", "dry_run = false")
            unsafe = tmp / "config.toml"
            unsafe.write_bytes(blob.encode("latin-1"))
            record: dict = {"kind": "negative-precondition", "cmd": "self --config <unsafe>", "pid": None, "returncode": None}
            self.child_procs.append(record)
            out = ""
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--config",
                        str(unsafe),
                        "--out-dir",
                        str(tmp / "out"),
                        "--no-negative",
                    ],
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                record["pid"] = proc.pid
                out = proc.communicate(timeout=max(60.0, self.args.timeout * 4))[0] or ""
                record["returncode"] = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                record["returncode"] = "timeout"
                return False, "负向用例超时"
        ok = record["returncode"] == 2 and "拒绝运行" in out
        # 原文落盘：摘要不能代替证据，让人能直接指到那句"配置不是只读档，拒绝运行"
        try:
            raw_file = self._out_dir() / "negative-precondition.txt"
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            raw_file.write_text(self.redact(out), encoding="utf-8")
        except OSError:
            pass
        detail = (
            f"rc={record['returncode']}(期望2)  refused={'拒绝运行' in out}  "
            f"out_bytes={len(out)}  stdout=negative-precondition.txt"
        )
        return ok, detail

    # ------------------------------------------------------------------ 主流程
    def run(self) -> int:
        try:
            return self._run()
        except Abort as exc:
            self.exit_code = exc.code
            self.failure = str(exc)
            self.say(f"[ABORT] {exc}")
        except ForbiddenCall as exc:
            self.exit_code = EXIT_FORBIDDEN
            self.failure = str(exc)
            self.say(f"[ABORT] {exc}")
        except Exception as exc:  # noqa: BLE001
            self.exit_code = EXIT_UNEXPECTED
            self.failure = f"{type(exc).__name__}: {exc}"
            self.say(f"[ERROR] {self.failure}")
            self.say(traceback.format_exc(limit=3))
        finally:
            leak = self.write_report()
            if leak:
                self.exit_code = EXIT_LEAK
        return self.exit_code

    def _run(self) -> int:
        args = self.args

        # ---- 步骤 0：配置与前置断言 --------------------------------
        started = time.perf_counter()
        self.config_path = resolve_config_path(args.config)
        self.config_sha = sha256_file(self.config_path)
        config = load_config(str(self.config_path))
        run = config.run
        self.redact.add(config.platform.password)
        self.redact.add(config.deepseek.api_key)
        self.redact.add(os.environ.get("TRAINSOLVE_PASSWORD"))

        unsafe = []
        if run.submit is not False:
            unsafe.append(f"submit={run.submit!r}")
        if run.dry_run is not True:
            unsafe.append(f"dry_run={run.dry_run!r}")
        if unsafe:
            raise Abort(
                EXIT_PRECONDITION,
                "配置不是只读档，拒绝运行：" + ", ".join(unsafe) + f"（{self.config_path}）",
            )
        self.step(
            "precondition",
            True,
            started,
            f"submit=False dry_run=True  auto_cleanup={run.auto_cleanup}  config={self.config_path.name}",
        )
        if run.auto_cleanup:
            self.say("       ⚠ auto_cleanup=True（不阻断，但登录后客户端可能清理残留会话）")

        # ---- 步骤 0b：负向用例（不安全配置必须被拒、且不联网）----
        if not getattr(args, "no_negative", False):
            started = time.perf_counter()
            neg_ok, neg_detail = self._run_negative_precondition()
            self.step("negative-precondition", neg_ok, started, neg_detail)
            if not neg_ok:
                raise Abort(EXIT_PRECONDITION, f"负向用例未通过：{neg_detail}")

        # ---- 凭据 -------------------------------------------------
        user = os.environ.get("TRAINSOLVE_USER") or config.platform.username
        password = os.environ.get("TRAINSOLVE_PASSWORD") or config.platform.password
        self.redact.add(password)
        started = time.perf_counter()
        if not user or not password:
            raise Abort(EXIT_PRECONDITION, "缺少账号或密码（环境变量 / config.toml 都没有）")
        self.step(
            "credentials",
            True,
            started,
            f"user={user} source={'env' if os.environ.get('TRAINSOLVE_PASSWORD') else 'config.toml'}",
        )

        # ---- 客户端与防护 -----------------------------------------
        client = TrainClient(config.platform.base_url, timeout=args.timeout, retries=1, log=self.say)
        self.install_guards(client)

        # ---- 步骤 1：登录 -----------------------------------------
        started = time.perf_counter()
        try:
            session = client.login(user, password)
        except LoginError as exc:
            raise Abort(EXIT_LOGIN, f"登录失败：{exc}") from exc
        except ApiError as exc:
            text = str(exc)
            code = EXIT_NETWORK if "网络错误" in text else EXIT_LOGIN
            raise Abort(code, f"登录异常：{text}") from exc
        self.redact.add(session.sid)
        self.session_id = session.sid
        self.step("login", True, started, f"name={session.name} code={session.code} sid={mask(session.sid)}")

        # ---- 步骤 2：取标签 ---------------------------------------
        started = time.perf_counter()
        try:
            tags = client.tags(run.qbase_type, run.level)
        except ApiError as exc:
            raise Abort(EXIT_TAGS, f"取标签失败：{exc}") from exc
        available = [tag for tag in tags if tag.get("available") is not False]
        self.step(
            "list-tag",
            True,
            started,
            f"qBaseType={run.qbase_type} level={run.level} total={len(tags)} available={len(available)}",
        )

        # ---- 步骤 3：读题（既有会话，绝不 choose-q）---------------
        started = time.perf_counter()
        local_root = Path(run.out_dir)
        if not local_root.is_absolute():
            local_root = REPO_ROOT / local_root
        if args.session and not re.fullmatch(r"[0-9a-fA-F]{32}", args.session):
            raise Abort(EXIT_PRECONDITION, "--session 必须是 32 位十六进制会话 id")
        sid = args.session or newest_local_session(local_root)
        if not sid:
            self.step("read-paper", False, started, "没有可用的既有会话 id（本地 out/ 下没有 record.json，也未传 --session）")
            raise Abort(EXIT_PAPER, "读题需要既有 sessionId；choose-q 会新建会话，按只读约束不调用")
        self.redact.add(sid)
        try:
            record = client.paper(sid)
        except ApiError as exc:
            raise Abort(EXIT_PAPER, f"读取会话 {mask(sid)} 失败：{exc}") from exc
        paper = (record or {}).get("paper") or {}
        questions = paper.get("questions") or []
        if not questions:
            self.step("read-paper", False, started, f"会话 {mask(sid)} 记录中没有题目")
            raise Abort(EXIT_PAPER, "读到的记录里没有 paper.questions")
        qtypes = sorted({int(q.get("qType") or 0) for q in questions})
        preview = str(questions[0].get("content") or "").strip().replace("\n", " ")[:60]
        self.step(
            "read-paper",
            True,
            started,
            f"session={mask(sid)} state={record.get('state')} questions={len(questions)} qTypes={qtypes}",
        )
        self.say(f"       题面预览：Q{questions[0].get('id')} {preview}…")

        # ---- 步骤 3b：peek_existing（Runner 真只读路径，choose-q 必须 0 次）----
        started = time.perf_counter()
        hits_before = len(self.forbidden_hits)
        calls_before = len(self.calls)
        attempts_before = dict(self.write_attempts)
        from trainsolve.runner import Runner

        class _NoopSolver:  # peek_existing 不需要 solver
            pass

        runner = Runner(client, _NoopSolver(), config, log=lambda _m: None)  # type: ignore[arg-type]
        try:
            peeked_sid, peeked = runner.peek_existing(sid)
        except ForbiddenCall as exc:
            raise Abort(EXIT_FORBIDDEN, f"peek_existing 触发了被禁止的调用：{exc}") from exc
        except ApiError as exc:
            raise Abort(EXIT_PAPER, f"peek_existing 失败：{exc}") from exc
        new_calls = self.calls[calls_before:]
        choose_q_attempts = self.write_attempts["choose-q"] - attempts_before["choose-q"]
        peek_questions = ((peeked or {}).get("paper") or {}).get("questions") or []
        self.step(
            "peek-existing",
            bool(peek_questions) and choose_q_attempts == 0,
            started,
            f"session={mask(peeked_sid)} questions={len(peek_questions)} "
            f"choose_q_attempts={choose_q_attempts} blocked={len(self.forbidden_hits) - hits_before} "
            f"requests={len(new_calls)}",
        )
        if choose_q_attempts:
            raise Abort(EXIT_FORBIDDEN, f"peek_existing 期间出现 {choose_q_attempts} 次 choose-q 尝试")

        # ---- 步骤 3c：CLI `peek --session`（子进程，验 CLI 接线）----
        started = time.perf_counter()
        cli_ok, cli_detail = self._run_cli_peek(sid)
        self.step("cli-peek-existing", cli_ok, started, cli_detail)
        if not cli_ok:
            raise Abort(EXIT_PAPER, f"CLI peek --session 失败：{cli_detail}")

        # ---- 步骤 4：停止（实测线程/子进程，不做无根据断言）----
        started = time.perf_counter()
        if self.forbidden_hits:
            raise Abort(EXIT_FORBIDDEN, "检测到被禁止的调用：" + "; ".join(self.forbidden_hits))
        other_threads = [t for t in threading.enumerate() if t is not threading.current_thread() and t.is_alive()]
        thread_names = [t.name for t in other_threads]
        spawned = len(self.child_procs)
        exited = sum(1 for item in self.child_procs if item.get("returncode") is not None)
        total_attempts = sum(self.write_attempts.values())
        attempts_text = ",".join(f"{key}={value}" for key, value in self.write_attempts.items())
        detail = (
            f"forbidden_calls={len(self.forbidden_hits)}  requests_main={len(self.calls)}  "
            f"write_attempts_total={total_attempts}({attempts_text})  "
            f"other_threads={len(other_threads)}{thread_names}  spawned_children={spawned} exited={exited}"
        )
        # 容忍 0 个：残留线程就是泄漏信号，不给自己留后门
        self.step("stop", total_attempts == 0 and len(other_threads) == 0, started, detail)
        self.exit_code = EXIT_OK
        return EXIT_OK

    # ------------------------------------------------------------------ 报告
    def _out_dir(self) -> Path:
        return Path(self.args.out_dir).expanduser().resolve()

    def write_report(self) -> bool:
        """写 run.json / run.md / console.log，并做一次泄密自检。返回是否检出泄密。"""
        out = self._out_dir()
        out.mkdir(parents=True, exist_ok=True)
        history = out / "history"
        history.mkdir(exist_ok=True)
        script_path = Path(__file__).resolve()
        script_sha = sha256_file(script_path)
        spawned = len(self.child_procs)
        cli_children = sum(1 for item in self.child_procs if item.get("kind") == "cli-peek")

        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "exit_code": self.exit_code,
            "failure": self.redact(self.failure),
            "script": {"path": str(script_path), "sha256": script_sha},
            "config": {"path": str(self.config_path) if self.config_path else "", "sha256": self.config_sha},
            "platform": os.environ.get("COMPUTERNAME", ""),
            "python": sys.version.split()[0],
            "steps": self.steps,
            "write_attempts": dict(self.write_attempts),
            "scope": {
                "requests_table": "主进程（受白名单约束）发出的请求",
                "child_processes_included": False,
                "child_processes": self.child_procs,
                "note": (
                    f"子进程共 {spawned} 个（CLI peek {cli_children} 个、负向用例 "
                    f"{spawned - cli_children} 个），它们的请求不在本表统计内"
                ),
            },
            "requests": self.calls,
            "forbidden_hits": [self.redact(item) for item in self.forbidden_hits],
        }
        json_text = json.dumps(payload, ensure_ascii=False, indent=2)

        lines = [
            "# TrainSolve 无头只读冒烟",
            "",
            f"- 时间：{payload['generated_at']}",
            f"- 退出码：**{self.exit_code}**" + (f"（{self.redact(self.failure)}）" if self.failure else ""),
            f"- 生成脚本：`{script_path.name}`",
            f"- **脚本 SHA256：`{script_sha}`**",
            f"- 配置文件：`{payload['config']['path']}`",
            f"- 配置 SHA256：`{self.config_sha}`",
            f"- Python：{payload['python']}",
            "",
            "## 步骤",
            "",
            "| 步骤 | 结果 | 耗时(ms) | 说明 |",
            "| --- | --- | ---: | --- |",
        ]
        for item in self.steps:
            lines.append(
                f"| {item['step']} | {'PASS' if item['ok'] else 'FAIL'} | {item['duration_ms']} | {item['detail']} |"
            )
        lines += [
            "",
            "## 主进程（受白名单约束）发出的请求",
            "",
            f"> 口径：仅统计本进程内被 `guarded_open` 放行并记录的请求。"
            f"CLI peek 是独立子进程（{cli_children} 个），它自己会发 `auth` + `record` 两次请求，**不在本表内**。",
            "",
            "| 方法 | 路径 | HTTP | 耗时(ms) |",
            "| --- | --- | ---: | ---: |",
        ]
        for call in self.calls:
            lines.append(f"| {call['method']} | {call['path']} | {call['status']} | {call['duration_ms']} |")
        attempt_rows = "，".join(f"{k}={v}" for k, v in self.write_attempts.items())
        lines += [
            "",
            "## 只读保证",
            "",
            f"- 传输层白名单只放行：{', '.join(ALLOWED_PATHS)}",
            "- 路径先 `urllib.parse.unquote` 解码、再 `posixpath.normpath` 归一化；"
            "含 `..`、归一化后变形、或解码后仍残留 `%` 的一律拒绝（堵 `record/../submit` 与 `%2e%2e`）",
            f"- **写接口尝试计数**（在放行判定之前累计，与是否被拦无关）：{attempt_rows}",
            f"- 被拦截的调用次数：**{len(self.forbidden_hits)}**",
            "",
            f"- 本目录 `script.py` 即当次运行的完整代码，SHA256 见上方（与 run.json 的 `script.sha256` 一致）",
            "",
        ]
        md_text = self.redact("\n".join(lines))
        console_text = self.redact("\n".join(self.console))
        # 按字节读，别走文本模式：文本写入会把 \n 翻成 \r\n，副本 sha 就和报告头对不上了
        script_bytes = script_path.read_bytes()

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        targets = {
            out / "run.json": json_text,
            out / "run.md": md_text,
            out / "console.log": console_text,
            history / f"run-{stamp}-exit{self.exit_code}.json": json_text,
        }
        for name, text in KNOWN_RESIDUALS.items():
            targets[out / name] = text
        temps: list[tuple[Path, Path]] = []
        for path, text in targets.items():
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            temps.append((tmp, path))

        # 先把**临时文件 + 既有全部报告（含 history/）**一起扫描、就地脱敏，再原子改名
        leaked = self._scan_and_scrub(out, [tmp for tmp, _ in temps])
        for tmp, path in temps:
            os.replace(tmp, path)

        # 源码副本单独写：它不参与泄漏扫描。
        # 理由：扫描对象是"报告里有没有凭据"，源码按 sha 校验；纯数字密码会撞上代码里的
        # 字符集字面量（真实误报），所以不让源码进扫描链路。
        script_tmp = (out / "script.py").with_name("script.py.tmp")
        script_tmp.write_bytes(script_bytes)
        os.replace(script_tmp, out / "script.py")

        if leaked:
            self.say("[LEAK] 报告里检出明文凭据，已就地脱敏：" + ", ".join(leaked))
        return bool(leaked)

    def _scan_and_scrub(self, root: Path, temps: list[Path]) -> list[str]:
        """扫描临时文件与 root 下所有既有报告（含 history/），命中就在原地脱敏。"""
        leaked: list[str] = []
        secrets = [item for item in self.redact.secrets if len(item) >= 3]
        if not secrets:
            return leaked
        known = {path.resolve() for path in temps}
        existing = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.resolve() not in known and path.suffix in {".json", ".md", ".txt", ".log"}
        ]
        for path in list(temps) + existing:
            try:
                blob = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hit = False
            for secret in secrets:
                if secret in blob:
                    blob = blob.replace(secret, "[REDACTED]")
                    hit = True
                    try:
                        where = path.relative_to(root)
                    except ValueError:
                        where = path.name
                    leaked.append(f"{where}:<{mask(secret)}>")
            if hit:
                path.write_text(blob, encoding="utf-8")
        return leaked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrainSolve 无头只读冒烟（login → list-tag → record → stop）")
    parser.add_argument("--config", help="config.toml 路径；默认 %%APPDATA%%\\TrainSolve\\config.toml")
    parser.add_argument("--session", help="只读用的既有 sessionId（默认自动挑 out/ 下最新的 record.json）")
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT.parent.parent / "reports" / "evidence" / "20261105-smoke-headless"),
        help="证据输出目录",
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="单请求超时（秒）")
    parser.add_argument("--self-test", action="store_true", help="只验只读白名单防护，不联网、不读凭据")
    parser.add_argument("--no-negative", action="store_true", help="跳过「不安全配置必须被拒」的负向用例")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    smoke = Smoke(args)
    if args.self_test:
        code = smoke.run_self_test()
        print(f"\nexit={code}  evidence={smoke._out_dir()}", flush=True)
        return code
    code = smoke.run()
    print(f"\nexit={code}  evidence={smoke._out_dir()}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
