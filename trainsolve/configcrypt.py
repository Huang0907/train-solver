"""配置文件加密（Windows DPAPI，绑定当前用户）。

为什么用 DPAPI 而不是自己拿口令加密：
- **不用记密码**：密钥由 Windows 按「当前用户 + 当前机器」托管，程序自己不保管任何密钥；
- **拷走也打不开**：把 `config.toml.enc` 拷到别的机器/别的用户下解不开，明文不会跟着文件走；
- 不需要第三方库（`crypt32.dll` 是系统自带）。

文件名约定：`config.toml` 旁边放一个 `config.toml.enc`。加载器**优先读 `.enc`**；
要回到明文，删掉 `.enc` 即可（或跑 `trainsolve decrypt-config`）。

格式：`MAGIC + DPAPI 密文`。带 MAGIC 是为了「后缀被改坏也能认出来」——
解密时先看头，再决定要不要走 DPAPI，报错也就能说清是「不是加密文件」还是「解不开」。
"""

from __future__ import annotations

import ctypes
import os
import secrets
import sys
from pathlib import Path

MAGIC = b"TRAINSOLVE-ENC-v1\n"
SUFFIX = ".enc"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class ConfigCryptoError(RuntimeError):
    """加密/解密失败（平台不支持、文件损坏、换了用户或机器）。"""


def available() -> bool:
    """只有 Windows 才有 DPAPI；其它平台明确不可用，不做假加密。"""
    return sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> "_DataBlob":
    payload = data or b"\x00"      # DPAPI 不接受 0 长度缓冲区，空内容给一个占位字节
    buffer = ctypes.create_string_buffer(payload, len(payload))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _crypt32():
    if not available():
        raise ConfigCryptoError("当前系统不是 Windows，不支持 DPAPI 加密")
    crypt32 = ctypes.WinDLL("crypt32")
    kernel32 = ctypes.WinDLL("kernel32")
    # 签名必须显式声明：64 位下不声明 argtypes，指针会被按 c_int 截断，
    # LocalFree 可能释放错地址（进程崩溃）。
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_bool
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_bool
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def encrypt_bytes(data: bytes) -> bytes:
    """DPAPI 加密（仅当前用户可解）。"""
    crypt32, kernel32 = _crypt32()
    in_blob = _blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "TrainSolve config", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise ConfigCryptoError(f"DPAPI 加密失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def decrypt_bytes(blob: bytes) -> bytes:
    """DPAPI 解密；换了用户/机器或文件被改过都会失败。"""
    crypt32, kernel32 = _crypt32()
    in_blob = _blob(blob)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise ConfigCryptoError(
            "DPAPI 解密失败：这份配置多半是在别的用户/别的机器上加密的，或者文件被改过"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ---------------------------------------------------------------- 文件层


def looks_encrypted(path: str | Path) -> bool:
    """只看文件头，不依赖后缀：`.enc` 改坏名字也能认出来。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(MAGIC))
    except OSError:
        return False
    return head == MAGIC


def encrypted_sibling(path: str | Path) -> Path | None:
    """`config.toml` → 存在 `config.toml.enc` 就返回它；本身是 `.enc` 时原样返回。"""
    target = Path(path)
    if target.name.endswith(SUFFIX):
        return target
    candidate = target.with_name(target.name + SUFFIX)
    return candidate if candidate.exists() else None


def read_config_text(path: str | Path) -> str:
    """读配置文本：加密文件自动解密，明文文件按 UTF-8 读。

    `.enc` 后缀却是明文内容时**拒绝按明文读**：那是「以为加密了其实没有」，
    静默接受会让用户一直以为凭据被保护着（2026-11-11 验收提的边界）。
    """
    target = Path(path)
    raw = target.read_bytes()
    if raw.startswith(MAGIC):
        try:
            return decrypt_bytes(raw[len(MAGIC):]).decode("utf-8")
        except UnicodeDecodeError as exc:  # 解密成功但内容不是 UTF-8
            raise ConfigCryptoError(f"{target} 解密后不是 UTF-8 文本") from exc
    if target.name.endswith(SUFFIX):
        raise ConfigCryptoError(
            f"{target} 名字是加密文件，内容却是明文（没有 {MAGIC.decode('utf-8').strip()} 文件头）。"
            "要么重新 encrypt-config 加密，要么把它改名成 config.toml 明确按明文用。"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigCryptoError(f"{target} 既不是加密文件也不是 UTF-8 文本") from exc


def _atomic_write(target: Path, payload: bytes) -> None:
    """先写同目录临时文件再原子替换：写到一半崩了也不会留下半个配置文件。

    临时名带 pid + 随机串：两个进程同时保存同一份配置时不会互踩同一个 `.tmp`
    （GUI 虽然单实例，但 CLI 与 GUI 可能同时在写）。
    """
    token = f"{os.getpid()}.{secrets.token_hex(4)}"
    tmp = target.with_name(f"{target.name}.{token}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_config_text(path: str | Path, text: str, *, remove_plaintext: bool = False) -> None:
    """写配置：路径以 `.enc` 结尾就加密写，否则按明文写。

    `remove_plaintext=True` 时，加密写成功后把同名的明文 `config.toml` 删掉——
    「配置文件加密」要求磁盘上不再留一份明文凭据，否则加密等于白做。
    只有加密确实写成功（`os.replace` 完成）才删，写失败时明文原样保留。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.name.endswith(SUFFIX):
        payload = MAGIC + encrypt_bytes(text.encode("utf-8"))
    else:
        # 直接写字节而不是 write_text：配置统一 LF，避免 Windows 上写出 CRLF
        payload = text.encode("utf-8")
    _atomic_write(target, payload)
    if remove_plaintext and target.name.endswith(SUFFIX):
        plain = target.with_name(target.name[: -len(SUFFIX)])
        if plain.exists() and plain != target:
            plain.unlink()


def encrypt_config_file(src: str | Path, dst: str | Path | None = None) -> Path:
    """把明文配置加密成 `<src>.enc`（默认同名加后缀）；不删源文件。

    两道守卫（2026-11-11 界面实测出来的两个坑）：
    * 目标不能就是源文件——否则命令行再 `--remove-plaintext` 会把唯一一份配置删掉，
      而实际写出来的还是明文；
    * 目标名必须以 `.enc` 结尾——`write_config_text` 是按后缀决定加不加密的，
      叫 `secret.dat` 就会**静默写明文**却对用户报「已加密」。
    """
    source = Path(src)
    if not source.exists():
        raise ConfigCryptoError(f"配置文件不存在：{source}")
    text = read_config_text(source)
    target = Path(dst) if dst is not None else source.with_name(source.name + SUFFIX)
    if target == source:
        raise ConfigCryptoError(
            "加密目标不能和源文件相同：那样写出来的还是明文，再删明文就把唯一一份配置删没了"
        )
    if not target.name.endswith(SUFFIX):
        raise ConfigCryptoError(
            f"加密输出名必须以 {SUFFIX} 结尾：{target}（否则写出来仍是明文，却会被当成已加密）"
        )
    write_config_text(target, text)
    return target


def decrypt_config_file(src: str | Path, dst: str | Path | None = None,
                        *, overwrite: bool = False) -> Path:
    """把加密配置还原成明文（默认去掉 `.enc` 后缀）；不删源文件。

    **目标已存在时默认拒绝**（0.6.9）：解密出来的明文可能是旧的，静默覆盖会把
    用户手上更新的那份明文冲掉（2026-11-11 验收实测）。要覆盖必须显式 `overwrite=True`。
    写盘同样走原子替换，不会留下半个文件。
    """
    source = Path(src)
    if not source.exists():
        raise ConfigCryptoError(f"配置文件不存在：{source}")
    if source.name.endswith(SUFFIX):
        default_name = source.name[: -len(SUFFIX)]
    else:
        default_name = source.name + ".decrypted.toml"
    target = Path(dst) if dst is not None else source.with_name(default_name)
    if target == source:
        raise ConfigCryptoError("解密目标不能和源文件相同")
    if target.exists() and not overwrite:
        raise ConfigCryptoError(
            f"解密目标已存在：{target}（覆盖会冲掉现有明文，确认要覆盖请加 --force）"
        )
    text = read_config_text(source)
    _atomic_write(target, text.encode("utf-8"))
    return target
