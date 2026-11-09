"""扫单文件 exe 里是否内嵌了不该出现的敏感串 —— **解包后再扫**。

为什么不能直接搜 exe 字节：
    PyInstaller 把源码打进 PYZ，而 PYZ 的每个模块都是 zlib 压缩的。拿 exe 原始
    字节当 ASCII 搜，源码里的字面量一个都命中不了（2026-11-04 实测：`portable.txt`
    这类必然存在的字符串都搜不到），于是这道门禁等于没有。原 build_release.ps1 的
    注释写着「查的是解压后的明文」，实现却是在原始字节里搜——注释与实现不符。

这里的做法：
    CArchiveReader 打开 exe → 解出 `PYZ.pyz` → ZlibArchiveReader 解出每个模块的
    code 对象 → 遍历 co_consts 里的字符串常量（含嵌套函数/推导式）比对敏感串；
    其余数据条目按原始字节、UTF-8、UTF-16LE 三种形态比对。

两级严重度（2026-11-05 实测后拆分）：
    FORBIDDEN 真凭据 —— 命中即失败（exit 1），构建必须停下来。
    PRIVATE   私有但非凭据（本机代理地址、网关模型 id）—— 只在源码里当示例文案
              存在，命中仅告警。若一条都当失败，每次构建都会被拦住，门禁会被关掉，
              反而更不安全。

    python scripts/scan_exe_secrets.py dist/TrainSolveClient.exe
    python scripts/scan_exe_secrets.py <exe> --json

退出码：0 = 无凭据；1 = 命中凭据；2 = 扫描器自身出错（不能当成通过）。
"""

from __future__ import annotations

import argparse
import json
import marshal
import os
import sys
import tempfile
from dataclasses import dataclass, field

# 真实凭据不写进仓库：构建脚本从本机 config.toml 读出学号 / Key 前缀，用 --forbid 传进来。
# 这里保持为空，避免开源版把开发者自己的凭据当默认值带走。
DEFAULT_FORBIDDEN: tuple[str, ...] = ()

# 私有但不算凭据：本机代理地址与网关模型 id（源码里作为示例文案/预设出现）
DEFAULT_PRIVATE: tuple[str, ...] = (
    "127.0.0.1:7864",
    "cn:deepseek-v4.1-flash",
)

_MAX_ENTRY_BYTES = 64 * 1024 * 1024   # 单个条目最多看 64 MB
_MAX_WALK_DEPTH = 6                   # code 对象嵌套深度上限


@dataclass
class ScanReport:
    exe: str
    entries: int = 0
    modules: int = 0
    hits: list[tuple[str, str]] = field(default_factory=list)      # 凭据命中 (串, 位置)
    warnings: list[tuple[str, str]] = field(default_factory=list)  # 私有串命中
    skipped: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.hits

    def summary(self) -> str:
        return (
            f"扫描 {self.exe}\n"
            f"  归档条目 {self.entries} 个，PYZ 模块 {self.modules} 个"
            f"（解压后逐模块比对字符串常量）"
        )


def iter_code_strings(code, depth: int = 0):
    """遍历 code 对象里的字符串常量。

    要穿透两类容器，否则会漏：
    - 嵌套 code（内嵌函数、生成器）；
    - 常量里的元组/列表/冻结集（`('a', 'b')`、`{"x", "y"}` 这种字面量组合在
      co_consts 里是容器，不是散落的 str）。
    """
    if depth > _MAX_WALK_DEPTH:
        return
    for const in getattr(code, "co_consts", ()) or ():
        if isinstance(const, str):
            yield const
        elif hasattr(const, "co_consts"):
            yield from iter_code_strings(const, depth + 1)
        elif isinstance(const, (tuple, list, frozenset, set)):
            yield from _iter_container(const, depth + 1)


def _iter_container(items, depth: int):
    if depth > _MAX_WALK_DEPTH:
        return
    for item in items:
        if isinstance(item, str):
            yield item
        elif hasattr(item, "co_consts"):
            yield from iter_code_strings(item, depth + 1)
        elif isinstance(item, (tuple, list, frozenset, set)):
            yield from _iter_container(item, depth + 1)


def _needle_forms(needle: str) -> tuple[bytes, ...]:
    """敏感串在二进制里的几种可能编码。"""
    forms = [needle.encode("utf-8")]
    try:
        forms.append(needle.encode("utf-16-le"))
    except UnicodeEncodeError:  # pragma: no cover - 极端字符
        pass
    return tuple(forms)


def hit_in_text(text: str, needles) -> list[str]:
    """一段文本里命中了哪些敏感串（纯函数，便于单测）。"""
    return [n for n in needles if n in text]


def hit_in_bytes(data: bytes, needles) -> list[str]:
    """一段字节里命中了哪些敏感串（ASCII/UTF-8/UTF-16LE 都能抓）。"""
    found = []
    for needle in needles:
        if any(form in data for form in _needle_forms(needle)):
            found.append(needle)
    return found


def code_like(payload) -> bool:
    return hasattr(payload, "co_consts")


def _as_code(payload):
    """extract() 可能返回 code 对象，也可能是 marshal 字节。"""
    if isinstance(payload, (bytes, bytearray)):
        try:
            return marshal.loads(bytes(payload))
        except Exception:  # noqa: BLE001 - 不是 marshal 就是普通数据
            return None
    if code_like(payload):
        return payload
    return None


def scan(
    exe_path: str | os.PathLike,
    forbidden=DEFAULT_FORBIDDEN,
    private=DEFAULT_PRIVATE,
) -> ScanReport:
    """解包并扫描一个 PyInstaller 单文件 exe。

    forbidden 命中进 report.hits（决定退出码）；private 命中进 report.warnings。
    """
    from PyInstaller.archive.readers import CArchiveReader

    exe = str(exe_path)
    report = ScanReport(exe=exe)
    forbid = tuple(forbidden)
    priv = tuple(private)
    all_needles = forbid + priv
    archive = CArchiveReader(exe)
    toc = getattr(archive, "toc", {}) or {}
    report.entries = len(toc)

    def keep(found, where: str) -> None:
        for needle in found:
            if needle in forbid:
                report.hits.append((needle, where))
            else:
                report.warnings.append((needle, where))

    for name in toc:
        try:
            payload = archive.extract(name)
        except Exception as exc:  # noqa: BLE001 - 个别条目解不出不该中断整轮
            report.skipped.append(f"{name}: {exc}")
            continue

        lower = name.lower()
        if lower.endswith(".pyz"):
            hits, warns = _scan_pyz(payload, all_needles, forbid, report)
            report.hits.extend(hits)
            report.warnings.extend(warns)
            continue

        if code_like(payload) or lower.endswith((".pyc", ".pyo")):
            code = _as_code(payload)
            if code is not None:
                for text in iter_code_strings(code):
                    keep(hit_in_text(text, all_needles), f"{name}(code 常量)")
                continue

        data = payload if isinstance(payload, (bytes, bytearray)) else b""
        if not data:
            continue
        if len(data) > _MAX_ENTRY_BYTES:
            report.skipped.append(f"{name}: 超过 {_MAX_ENTRY_BYTES} 字节，未扫")
            continue
        keep(hit_in_bytes(data, all_needles), name)

    return report


def _scan_pyz(payload, all_needles, forbid, report: ScanReport):
    """解出 PYZ 里每个模块的 code 对象并比对字符串常量。返回 (凭据命中, 私有命中)。"""
    from PyInstaller.archive.readers import ZlibArchiveReader

    hits: list[tuple[str, str]] = []
    warns: list[tuple[str, str]] = []
    data = payload if isinstance(payload, (bytes, bytearray)) else b""
    if not data:
        report.skipped.append("PYZ.pyz: 取不到内容")
        return hits, warns

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".pyz")
        with os.fdopen(fd, "wb") as fp:
            fp.write(bytes(data))
        inner = ZlibArchiveReader(tmp)
        names = list(getattr(inner, "toc", {}) or {})
        report.modules = len(names)
        for mod in names:
            try:
                payload_mod = inner.extract(mod)
            except Exception as exc:  # noqa: BLE001
                report.skipped.append(f"PYZ:{mod}: {exc}")
                continue
            code = _as_code(payload_mod)
            if code is None:
                continue
            for text in iter_code_strings(code):
                for needle in hit_in_text(text, all_needles):
                    item = (needle, f"PYZ:{mod}")
                    if needle in forbid:
                        hits.append(item)
                    else:
                        warns.append(item)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return hits, warns


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认是 gbk，直接 print('✓') 会抛 UnicodeEncodeError
    （2026-11-05 实测：崩在打印结果那一行）。后果比崩溃本身更糟——
    退出码同样是 1，和"命中凭据"撞在一起，构建门禁拦得住却**说不清**
    哪条凭据、落在哪个模块，最后一定被人嫌吵关掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="解包后扫描 exe 内的敏感串")
    parser.add_argument("exe", help="PyInstaller 单文件 exe 路径")
    parser.add_argument("--forbid", nargs="*", default=list(DEFAULT_FORBIDDEN),
                        help="真凭据：命中即失败（默认取内置列表）")
    parser.add_argument("--private", nargs="*", default=list(DEFAULT_PRIVATE),
                        help="私有但非凭据：只告警（默认取内置列表）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    try:
        report = scan(args.exe, args.forbid, args.private)
    except Exception as exc:  # noqa: BLE001 - 扫描器自身问题必须显式失败
        print(f"✗ 扫描器出错，不能当作通过：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "exe": report.exe,
            "entries": report.entries,
            "modules": report.modules,
            "clean": report.clean,
            "hits": [{"needle": n, "where": w} for n, w in report.hits],
            "warnings": [{"needle": n, "where": w} for n, w in report.warnings],
            "skipped": report.skipped,
        }, ensure_ascii=False, indent=2))
    else:
        print(report.summary())
        for note in report.skipped:
            print(f"  ! 跳过：{note}")
        for needle, where in report.warnings:
            print(f"  ~ 私有串（不阻断）：{needle!r} @ {where}")
        if report.clean:
            print(f"  ✓ 未发现任何凭据（比对 {len(args.forbid)} 条）")
        else:
            for needle, where in report.hits:
                print(f"  ✗ 命中凭据 {needle!r} @ {where}")
    return 0 if report.clean else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
