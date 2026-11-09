"""post-answer 的校验码。

前端实现（还原自产物）：

    generateVerificationCode(sessionId) =
        md5(`${YYYY}-${MM}-${DD} ${HH}:${mm} ${sessionId}`)

时间取浏览器本地时间的「分钟」，注意是 `YYYY-MM-DD HH:mm`，日期与时间之间是一个空格。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone


def verification_string(session_id: str, now: datetime | None = None) -> str:
    stamp = now or datetime.now()
    return f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d} {stamp.hour:02d}:{stamp.minute:02d} {session_id}"


def verification_code(session_id: str, now: datetime | None = None) -> str:
    return hashlib.md5(verification_string(session_id, now).encode("utf-8")).hexdigest()


def candidate_codes(session_id: str, now: datetime | None = None, offsets_hours: int = 8):
    """服务器时区与本机不一致时的兜底：依次产出 0, +1..+8, -1..-8 小时的校验码。"""
    base = now or datetime.now()
    yield 0, verification_code(session_id, base)
    for hours in range(1, offsets_hours + 1):
        yield hours, verification_code(session_id, base_time(hours, base))
        yield -hours, verification_code(session_id, base_time(-hours, base))


def base_time(offset_hours: float = 0.0, now: datetime | None = None) -> datetime:
    """把「本机时间 + 偏移」当成服务器时间。"""
    return (now or datetime.now()) + timedelta(hours=offset_hours)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
