"""启动时清理残留。

两类残留：

1. **平台上过期的「未完成」会话**——答题时限已过，不会再计分，却一直挂在「未完成」列表里；
2. **本机临时目录里遗留的 `_MEI*`**——单文件 exe 被强杀时留下的 PyInstaller 解压目录（每个约 100 MB）。

安全策略（默认）：
* 只清 `timeLeft <= 0` 的会话；还在时限内的**一律不动**（可能是你正在做的题）；
  想连这些一起清，显式开 `include_active`（配置 `auto_cleanup_all = true`）；
* **读不到剩余时间（空体 / 读失败 / 非数值）一律算「状态未知」，保留不动**——
  删除不可撤销（`cancel-skip` 即删记录），不能拿「读不到」当「已过期」；
* 只清超过 `temp_max_age_hours` 小时没动过的 `_MEI*`，避免删掉正在运行的程序正在用的目录。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .api import STATE_UNDONE, ApiError

MAX_HISTORY = 100


@dataclass
class CleanupReport:
    """一次自动清理的结果（都只用于打日志/测试断言）。"""

    expired: list[str] = field(default_factory=list)      # 判定为过期的会话
    cleaned: list[str] = field(default_factory=list)      # 实际删掉的会话
    active: list[str] = field(default_factory=list)       # 还在时限内、有意跳过
    unknown: list[str] = field(default_factory=list)      # 读不到状态、按未知保留（2026-11-05 新增）
    temp_dirs: list[Path] = field(default_factory=list)   # 删掉的临时目录
    failures: list[str] = field(default_factory=list)     # 失败原因（不中断流程）

    def summary(self) -> str:
        parts = []
        if self.cleaned:
            parts.append(f"清理过期会话 {len(self.cleaned)} 条")
        if self.active:
            parts.append(f"保留进行中 {len(self.active)} 条")
        if self.unknown:
            parts.append(f"保留状态未知 {len(self.unknown)} 条")
        if self.temp_dirs:
            parts.append(f"删除临时目录 {len(self.temp_dirs)} 个")
        if self.failures:
            parts.append(f"{len(self.failures)} 项失败")
        return "，".join(parts) if parts else "没有需要清理的残留"


def list_undone(client, *, limit: int = MAX_HISTORY) -> list[dict]:
    """列出平台上的「未完成」会话，并补上每条记录的剩余时间。"""
    history = client.history(0, 0, limit)
    rows = [row for row in (history.get("data") or []) if row.get("state") == STATE_UNDONE]
    sessions: list[dict] = []
    for row in rows:
        session_id = row.get("sessionId")
        if not session_id:
            continue
        entry: dict = {"sessionId": str(session_id), "timeLeft": None, "state": STATE_UNDONE}
        try:
            record = client.paper(entry["sessionId"]) or {}
        except ApiError as exc:  # 记录可能刚好被清掉/过期：当作过期处理
            entry["error"] = str(exc)
            sessions.append(entry)
            continue
        entry["state"] = record.get("state", STATE_UNDONE)
        entry["timeLeft"] = record.get("timeLeft")
        sessions.append(entry)
    return sessions


def is_expired(entry: dict) -> bool:
    """**只有确定过期**才返回 True（2026-11-05 反转语义）。

    判据：`timeLeft` 是数值且 `<= 0`。读不到（`None`、非数值、读取出错）一律算
    「状态未知」，返回 False —— 未知不能当成过期，否则一次接口抖动就会把别人
    正在做的题删掉（`skip-this` + `cancel-skip` 会真正删记录）。
    """
    if entry.get("error"):
        return False
    left = entry.get("timeLeft")
    if left is None:
        return False
    try:
        return float(left) <= 0
    except (TypeError, ValueError):
        return False


def _time_left_known(entry: dict) -> bool:
    """剩余时间是否拿到了确定的数值。拿不到 = 状态未知。"""
    if entry.get("error"):
        return False
    left = entry.get("timeLeft")
    if left is None:
        return False
    try:
        float(left)
    except (TypeError, ValueError):
        return False
    return True


def cleanup_sessions(
    client,
    *,
    include_active: bool = False,
    log: Callable[[str], None] = print,
    dry_run: bool = False,
) -> CleanupReport:
    """清理过期（默认）或全部「未完成」会话：`skip-this` + `cancel-skip` 会删掉记录。"""
    report = CleanupReport()
    try:
        sessions = list_undone(client)
    except Exception as exc:  # noqa: BLE001 - 清理失败不该影响主流程
        report.failures.append(f"取「未完成」列表失败：{exc}")
        return report

    for entry in sessions:
        session_id = entry["sessionId"]
        if entry.get("state") not in (STATE_UNDONE,):
            continue
        if not _time_left_known(entry):
            # 读不到剩余时间：状态未知，一律保留（连 include_active 也不删）。
            report.unknown.append(session_id)
            continue
        if not include_active and not is_expired(entry):
            report.active.append(session_id)
            continue
        report.expired.append(session_id)
        if dry_run:
            continue
        try:
            client.skip_this(session_id)
            client.cancel_skip(session_id)
            report.cleaned.append(session_id)
        except Exception as exc:  # noqa: BLE001 - 单条失败继续清下一条
            report.failures.append(f"{session_id}: {exc}")
    return report


def cleanup_temp_dirs(
    *,
    max_age_hours: float = 2.0,
    log: Callable[[str], None] = print,
    dry_run: bool = False,
    temp_root: Path | None = None,
    now: float | None = None,
) -> list[Path]:
    """删掉 `%TEMP%` 下够旧的 `_MEI*`（PyInstaller 单文件解压目录）。"""
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    cutoff = (now if now is not None else time.time()) - max(0.0, float(max_age_hours)) * 3600.0
    removed: list[Path] = []
    try:
        candidates = sorted(root.glob("_MEI*"))
    except OSError:
        return removed
    for path in candidates:
        try:
            if not path.is_dir():
                continue
            if path.stat().st_mtime > cutoff:
                continue          # 太新，可能在用
            if not dry_run:
                shutil.rmtree(path)
            removed.append(path)
        except OSError as exc:
            log(f"  ⚠ 临时目录删不掉：{path}（{exc}）")
    return removed


def auto_cleanup(
    client=None,
    *,
    include_active: bool = False,
    temp_max_age_hours: float = 2.0,
    log: Callable[[str], None] = print,
    dry_run: bool = False,
    temp_root: Path | None = None,
) -> CleanupReport:
    """启动时的自动清理：过期会话 + 陈旧临时目录。任何失败都只记日志，不影响主流程。"""
    report = CleanupReport()
    if client is not None:
        try:
            report = cleanup_sessions(client, include_active=include_active, log=log, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响刷题
            report.failures.append(f"清理会话失败：{exc}")
        for session_id in report.cleaned:
            log(f"  🧹 已清理过期会话 {session_id}")
        for reason in report.failures:
            log(f"  ⚠ 清理会话失败：{reason}")
        if report.active:
            log(f"  保留 {len(report.active)} 条进行中的会话（未过期，不动）")
        if report.unknown:
            log(f"  保留 {len(report.unknown)} 条状态未知的会话（读不到剩余时间，不动）")
    report.temp_dirs = cleanup_temp_dirs(
        max_age_hours=temp_max_age_hours, log=log, dry_run=dry_run, temp_root=temp_root
    )
    if report.temp_dirs:
        log(f"  🧹 删掉 {len(report.temp_dirs)} 个遗留临时目录（{temp_max_age_hours:g} 小时前的）")
    return report
