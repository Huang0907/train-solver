"""生成 PyInstaller 用的 Windows 版本资源文件（`--version-file`）。

    python scripts/gen_version_info.py build/version_info.txt

版本号取自 `trainsolve.__version__`，这样 exe 属性里的 FileVersion / ProductVersion
和源码、release 目录名不会各说各话（此前 exe 完全没有版本资源，属性里是空的）。

生成的是一段可被 PyInstaller 求值的 Python 源码（VSVersionInfo 结构），这是
PyInstaller 规定的格式，不是我们自定义的。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRODUCT_NAME = "TrainSolveClient"
DESCRIPTION = "程序设计训练平台 自动刷题客户端"
# 2052 = 简体中文（PRC），1200 = Unicode；StringTable 的键是 langID<<16 | codepage
LANG_ID = 2052
CODEPAGE = 1200


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """'0.5.0' → (0, 5, 0, 0)。

    先把预发布后缀切掉再取数字：'0.6.0rc1' → (0, 6, 0, 0)，而不是 (0, 6, 0, 1)——
    否则 rc1 的 FileVersion 会比正式版还高，属性面板里看着像更新。
    四段式（'1.2.3.4'）保留第四段。
    """
    release = re.split(r"[-+~]|[A-Za-z]", version, maxsplit=1)[0] or "0"
    parts = [p for p in release.split(".") if p != ""][:4]
    digits = [int(p) if p.isdigit() else int("".join(re.findall(r"\d", p)) or 0) for p in parts]
    while len(digits) < 4:
        digits.append(0)
    return tuple(digits)  # type: ignore[return-value]


def _table_key() -> str:
    return f"{LANG_ID:04x}{CODEPAGE:04x}"


def _esc(text: str) -> str:
    """写进 Python 字面量前转义，避免单引号把生成的源码弄坏。"""
    return text.replace("\\", "\\\\").replace("'", "\\'")


def build_text(version: str, description: str = DESCRIPTION, product: str = PRODUCT_NAME) -> str:
    """返回 VSVersionInfo 的 Python 源码文本。"""
    nums = version_tuple(version)
    tuple_text = ", ".join(str(n) for n in nums)
    return (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers=({tuple_text}),\n"
        f"    prodvers=({tuple_text}),\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo(\n"
        "      [\n"
        "        StringTable(\n"
        f"          '{_table_key()}',\n"
        "          [\n"
        f"            StringStruct('FileDescription', '{_esc(description)}'),\n"
        f"            StringStruct('FileVersion', '{_esc(version)}'),\n"
        f"            StringStruct('InternalName', '{_esc(product)}'),\n"
        f"            StringStruct('OriginalFilename', '{_esc(product)}.exe'),\n"
        f"            StringStruct('ProductName', '{_esc(product)}'),\n"
        f"            StringStruct('ProductVersion', '{_esc(version)}'),\n"
        "          ]\n"
        "        )\n"
        "      ]\n"
        "    ),\n"
        f"    VarFileInfo([VarStruct('Translation', [{LANG_ID}, {CODEPAGE}])])\n"
        "  ]\n"
        ")\n"
    )


def write_file(target: Path, version: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_text(version), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from trainsolve import __version__

    target = Path(argv[0]) if argv else REPO_ROOT / "build" / "version_info.txt"
    if not target.is_absolute():
        target = REPO_ROOT / target
    write_file(target, __version__)
    print(f"已生成版本资源：{target}")
    print(f"  FileVersion / ProductVersion = {__version__} → {version_tuple(__version__)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
