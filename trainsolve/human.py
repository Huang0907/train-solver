"""浏览器侧：登录 → 打开答题页 → 逐字符打字 → 真实点击提交。

只在「作答」这一步用浏览器；取题、自检、评分仍走 API（混合方案）。
依赖 playwright（仅用于 CDP 连接，不需要下载浏览器）：

    pip install playwright

首次使用需要一个带调试端口的 Chrome：

    chrome.exe --remote-debugging-port=9333 --user-data-dir=<独立配置目录>

`ensure_chrome()` 会在端口没监听时自动拉起来。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .humantype import (
    HumanStopped,
    HumanTyper,
    TypingProfile,
    TypingStopped,
    first_diff,
    normalize_code,
)

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

EDITOR_SELECTOR = ".cm-content"
LOGIN_USER_INPUT = "input[name=username]"
LOGIN_PASS_INPUT = "input[name=password]"
SUBMIT_TEXTS = ("提交", "交卷", "Submit")
CONFIRM_TEXTS = ("确定", "确认", "提交", "OK")

READ_DOC_JS = """() => {
  const el = document.querySelector('.cm-content');
  if (!el) return null;
  const lines = el.querySelectorAll('.cm-line');
  if (lines.length) return Array.from(lines).map(l => l.textContent).join('\\n');
  return el.innerText;
}"""

# 抓平台的暂存/提交请求体：确认「App 真正持有的代码」和编辑器里的是否一致。
# 实测把整篇打完立刻点「提交」，平台收到的代码会少最后几千字符区间的尾巴
# （前端 onChange → 组件模型有 debounce），必须等它同步完再交。
# 注意：**只有请求体不够**。Angular 的组件状态是在 post-answer 的**响应**回调里
# 更新的，所以必须等到「请求体对得上 + 响应 code=0」才算真的同步完成，
# 否则点提交时带的仍是旧快照（实测 2026-11-02 那次 0/12：平台编译的是 339 字符的旧前缀）。
CAPTURE_JS = """() => {
  if (window.__tsHooked) return true;
  window.__tsHooked = true;
  window.__ts = [];
  const push = (entry) => { try { window.__ts.push(entry); } catch (e) {} };
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { try { this.__tsUrl = u; } catch (e) {} return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    const entry = {url: String(this.__tsUrl || ''), method: 'XHR',
                   body: (typeof b === 'string' ? b : null), done: false, status: null, resp: null};
    push(entry);
    try {
      this.addEventListener('loadend', () => {
        try { entry.done = true; entry.status = this.status;
              entry.resp = String(this.responseText || '').slice(0, 200000); } catch (e) { entry.done = true; }
      });
    } catch (e) {}
    return XS.apply(this, arguments);
  };
  if (window.fetch) {
    const F = window.fetch;
    window.fetch = function (r, i) {
      const entry = {url: String((r && r.url) || r || ''), method: (i && i.method) || 'GET',
                     body: (i && typeof i.body === 'string' ? i.body : null), done: false, status: null, resp: null};
      push(entry);
      const p = F.apply(this, arguments);
      try {
        p.then((resp) => {
          try {
            entry.status = resp.status;
            resp.clone().text().then((t) => {
              entry.done = true; entry.resp = String(t || '').slice(0, 200000);
            }).catch(() => { entry.done = true; });
          } catch (e) { entry.done = true; }
        }, () => { entry.done = true; });
      } catch (e) {}
      return p;
    };
  }
  return true;
}"""

CAPTURE_READ_JS = """(since) => (window.__ts || []).slice(since).map(e => ({
  url: e.url, method: e.method, body: e.body,
  done: !!e.done, status: e.status,
  resp: e.resp ? String(e.resp).slice(0, 20000) : null,
}))"""

BLUR_JS = """() => { const a = document.activeElement; if (a && a.blur) a.blur(); return true; }"""

FOCUS_JS = """() => {
  const a = document.activeElement;
  if (!a) return '(none)';
  const inEditor = !!(a.closest && a.closest('.cm-content, .cm-editor'));
  return (inEditor ? 'editor:' : 'other:') + (a.className || a.tagName);
}"""

# 取编辑器中心点并顺手 DOM 聚焦；返回坐标供 CDP 鼠标点击落光标。
# 全程在页面/渲染进程内完成，不需要 Chrome 窗口在前台。
EDITOR_BOX_JS = """(() => {
  const el = document.querySelector('.cm-content') || document.querySelector('.cm-editor');
  if (!el) return null;
  el.focus();
  const r = el.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + Math.min(Math.max(8, r.height - 4), 24)};
})()"""


# 只做 DOM 聚焦、不动选区：恢复焦点不该顺手把光标挪走（0.6.7）。
EDITOR_FOCUS_JS = """(() => {
  const el = document.querySelector('.cm-content') || document.querySelector('.cm-editor');
  if (!el) return false;
  el.focus();
  return true;
})()"""


class HumanError(RuntimeError):
    pass


# ---------------------------------------------------------------- 浏览器 IO


class PlaywrightIO:
    """把 playwright 的 Page 适配成 TyperIO。

    `shift_symbols=True` 时，需要 Shift 的符号（`{ " # %` 等）会用
    `press("Shift+<底键>")` 发送，产生 ShiftLeft/ShiftRight + 键 的真实组合，
    与真人敲符号的 keySequence 更接近；否则 Playwright 的 type() 只发一个
    无 Shift 的 keydown。
    """

    # US 布局：符号 → 底键
    SHIFTED = {
        "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
        "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
        "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
    }

    def __init__(self, page, *, shift_symbols: bool = True) -> None:
        self.page = page
        self.shift_symbols = shift_symbols

    def click_editor(self) -> None:
        self.page.click(EDITOR_SELECTOR, timeout=15000)

    def type_char(self, ch: str) -> None:
        if not ch.isascii():
            # 中文/全角字符没有对应的物理键：交给 insertText（等价于输入法上屏），
            # 只算一次单字符插入，不会碰「>50 字符才算粘贴」的阈值（0.6.7）。
            self.page.keyboard.insert_text(ch)
            return
        if self.shift_symbols and ch in self.SHIFTED:
            self.page.keyboard.press(f"Shift+{self.SHIFTED[ch]}")
            return
        self.page.keyboard.type(ch)

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)

    def read_doc(self) -> str | None:
        return self.page.evaluate(READ_DOC_JS)

    def focus_info(self) -> str:
        return str(self.page.evaluate(FOCUS_JS))

    def ensure_focus(self) -> str:
        info = self.focus_info()
        if info.startswith("editor:"):
            return info
        try:
            self.click_editor()
        except Exception:  # noqa: BLE001 - 聚焦失败交给调用方按校验结果处理
            return str(info)
        return str(self.focus_info())


# ---------------------------------------------------------------- 原生按键

# US 布局：无需 Shift 的符号 → (code, 虚拟键码)
_UNSHIFTED = {
    ";": ("Semicolon", 186), "=": ("Equal", 187), ",": ("Comma", 188),
    "-": ("Minus", 189), ".": ("Period", 190), "/": ("Slash", 191),
    "`": ("Backquote", 192), "[": ("BracketLeft", 219), "\\": ("Backslash", 220),
    "]": ("BracketRight", 221), "'": ("Quote", 222),
}
# 需要 Shift 的符号 → 底键
_SHIFT_PAIRS = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
}
_SHIFT_MASK = 8
_CTRL_MASK = 2

# 特殊键 → (code, 虚拟键码)
_SPECIAL_KEYS = {
    "End": ("End", 35), "Home": ("Home", 36),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowRight": ("ArrowRight", 39),
    "Tab": ("Tab", 9), "Escape": ("Escape", 27), "Delete": ("Delete", 46),
}


def _key_info(ch: str) -> tuple[str, int, bool] | None:
    """字符 → (code, 虚拟键码, 是否需要 Shift)。不认识的（含非 ASCII）返回 None。"""
    if len(ch) != 1:
        return None
    # 只处理 ASCII：中文/全角字符没有对应的物理键，硬编出来就是 "Key中"+vk 20013
    # 这种非法组合，平台记下的 keySequence 也就不像真人（0.6.7）。
    if not ch.isascii():
        return None
    if ch.isalpha():
        upper = ch.upper()
        return f"Key{upper}", ord(upper), ch.isupper()
    if ch.isdigit():
        return f"Digit{ch}", 48 + int(ch), False
    if ch == " ":
        return "Space", 32, False
    if ch in _UNSHIFTED:
        code, vk = _UNSHIFTED[ch]
        return code, vk, False
    base = _SHIFT_PAIRS.get(ch)
    if base:
        info = _key_info(base)
        if info:
            code, vk, _ = info
            return code, vk, True
    return None


class CDPKeyboard:
    """用原生 CDP `Input.dispatchKeyEvent` 逐键发送。

    为什么不用 playwright 的 `keyboard.type()`：
    它按"插入这段文本"的语义发事件，**忽略按住 Shift**，
    于是 `(` 会被插成 `9`，也无法产生 `ShiftLeft` 这类按键记录。
    这里显式给出 code / key / 虚拟键码 / modifiers，文本与按键都正确，
    需要 Shift 的符号还会在连续出现时"按住 Shift"，与真人节奏一致。
    """

    def __init__(self, cdp_session) -> None:
        self.cdp = cdp_session
        self.shift_down = False

    # ---------------------------------------------------- 基础

    def _send(self, payload: dict) -> None:
        self.cdp.send("Input.dispatchKeyEvent", payload)

    def _shift(self, down: bool) -> None:
        self._send({
            "type": "keyDown" if down else "keyUp",
            "code": "ShiftLeft", "key": "Shift",
            "windowsVirtualKeyCode": 16, "nativeVirtualKeyCode": 16,
            "modifiers": _SHIFT_MASK,
        })
        self.shift_down = down

    def release_all(self) -> None:
        if self.shift_down:
            self._shift(False)

    # ---------------------------------------------------- 对外

    def type_char(self, ch: str) -> None:
        info = _key_info(ch)
        if info is None:
            raise ValueError(f"不支持的字符：{ch!r}")
        code, vk, need_shift = info
        if need_shift and not self.shift_down:
            self._shift(True)
        elif not need_shift and self.shift_down:
            self._shift(False)

        modifiers = _SHIFT_MASK if need_shift else 0
        # unmodifiedText 必须是**没按修饰键**时那个字符：Shift+A 是 "a" 不是 "A"
        if ch in _SHIFT_PAIRS:
            base = _SHIFT_PAIRS[ch]
        else:
            base = ch.lower() if ch.isupper() else ch
        self._send({
            "type": "keyDown", "code": code, "key": ch, "text": ch, "unmodifiedText": base,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": modifiers,
        })
        self._send({
            "type": "keyUp", "code": code, "key": ch,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": modifiers,
        })

    def press(self, key: str) -> None:
        if key == "Enter":
            self.release_all()
            self._send({"type": "keyDown", "code": "Enter", "key": "Enter", "text": "\r",
                        "unmodifiedText": "\r", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            self._send({"type": "keyUp", "code": "Enter", "key": "Enter",
                        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        elif key == "Backspace":
            self.release_all()
            self._send({"type": "keyDown", "code": "Backspace", "key": "Backspace",
                        "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})
            self._send({"type": "keyUp", "code": "Backspace", "key": "Backspace",
                        "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8})
        elif key in ("Control+a", "Meta+a"):
            self.release_all()
            modifier = _CTRL_MASK if key.startswith("Control") else 4
            mod_code = "ControlLeft" if key.startswith("Control") else "MetaLeft"
            mod_vk = 17 if key.startswith("Control") else 91
            self._send({"type": "keyDown", "code": mod_code, "key": mod_code[:-4],
                        "windowsVirtualKeyCode": mod_vk, "nativeVirtualKeyCode": mod_vk, "modifiers": modifier})
            self._send({"type": "keyDown", "code": "KeyA", "key": "a",
                        "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": modifier})
            self._send({"type": "keyUp", "code": "KeyA", "key": "a",
                        "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": modifier})
            self._send({"type": "keyUp", "code": mod_code, "key": mod_code[:-4],
                        "windowsVirtualKeyCode": mod_vk, "nativeVirtualKeyCode": mod_vk})
        elif key in ("Control+End", "Meta+End"):
            self.release_all()
            modifier = _CTRL_MASK if key.startswith("Control") else 4
            mod_code = "ControlLeft" if key.startswith("Control") else "MetaLeft"
            mod_vk = 17 if key.startswith("Control") else 91
            self._send({"type": "keyDown", "code": mod_code, "key": mod_code[:-4],
                        "windowsVirtualKeyCode": mod_vk, "nativeVirtualKeyCode": mod_vk,
                        "modifiers": modifier})
            self._send({"type": "keyDown", "code": "End", "key": "End",
                        "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35,
                        "modifiers": modifier})
            self._send({"type": "keyUp", "code": "End", "key": "End",
                        "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35,
                        "modifiers": modifier})
            self._send({"type": "keyUp", "code": mod_code, "key": mod_code[:-4],
                        "windowsVirtualKeyCode": mod_vk, "nativeVirtualKeyCode": mod_vk})
        elif key in _SPECIAL_KEYS:
            self.release_all()
            code, vk = _SPECIAL_KEYS[key]
            self._send({"type": "keyDown", "code": code, "key": key,
                        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            self._send({"type": "keyUp", "code": code, "key": key,
                        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        else:
            raise ValueError(f"未实现的按键：{key}")


class CDPTyperIO:
    """TyperIO 的 CDP 实现：走原生按键，聚焦/回读也走**同一个** CDP 会话。

    回读必须和按键在同一个 CDP 会话里（Playwright 的 `page.evaluate` 走 driver
    自己的会话，与 `Input.dispatchKeyEvent` 没有先后保证）——实测用 page.evaluate
    会读到期中间状态，进而让「对齐缩进/行内校正」误退格，把上一行和当前行拼在一起。
    """

    def __init__(self, page, cdp_session, *, log: Callable[[str], None] = print) -> None:
        self.page = page
        self.cdp = cdp_session
        self.keyboard = CDPKeyboard(cdp_session)
        self.log = log

    def _evaluate(self, expression: str):
        result = self.cdp.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise HumanError(f"页面求值失败：{result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    def click_editor(self) -> None:
        self.page.click(EDITOR_SELECTOR, timeout=15000)

    def type_char(self, ch: str) -> None:
        if not ch.isascii() and len(ch) == 1:
            self.insert_text(ch)
            return
        self.keyboard.type_char(ch)

    def insert_text(self, text: str) -> None:
        """非 ASCII 单字符走 insertText：没有物理键位，但仍是「一次插入一个字」。"""
        self.cdp.send("Input.insertText", {"text": text})

    def press(self, key: str) -> None:
        self.keyboard.press(key)

    def read_doc(self) -> str | None:
        return self._evaluate(f"({READ_DOC_JS})()")

    def focus_info(self) -> str:
        return str(self._evaluate(f"({FOCUS_JS})()"))

    def ensure_focus(self) -> str:
        """焦点不在编辑器时把焦点放回去，**并且不把光标带走**。

        分两级：先只做页面内 DOM 聚焦（不改选区）；只有 DOM 聚焦无效时才用
        CDP 鼠标点一下——点完立刻 Ctrl+End 把光标送回文末，否则后面打的字
        会插到点击位置（正文中间），整段代码就烂了（0.6.7）。

        两级都是 CDP 注入，**不要求 Chrome 窗口在前台**，也不会把窗口拽到前面。
        """
        info = self.focus_info()
        if info.startswith("editor:"):
            return info
        try:
            self._evaluate(EDITOR_FOCUS_JS)
        except Exception:  # noqa: BLE001 - 聚焦失败按后续校验兜底
            pass
        info = self.focus_info()
        if info.startswith("editor:"):
            self.log("  焦点已恢复（DOM 聚焦，未移动光标）")
            return info
        try:
            box = self._evaluate(EDITOR_BOX_JS)
            if box:
                x, y = float(box["x"]), float(box["y"])
                for kind in ("mousePressed", "mouseReleased"):
                    self.cdp.send("Input.dispatchMouseEvent", {
                        "type": kind, "x": x, "y": y, "button": "left", "clickCount": 1,
                    })
                # 点击让光标落到点上，必须送回文末：打字永远是「在文末追加」
                self.keyboard.press("Control+End")
                self.log("  焦点已恢复（鼠标点回，光标已移回文末）")
        except Exception:  # noqa: BLE001 - 聚焦失败按后续校验兜底
            pass
        return self.focus_info()


# ---------------------------------------------------------------- 工具


def _code_from_body(body: str | None) -> str | None:
    """从平台请求体里挑出代码字符串（post-answer / run-code 都适用）。"""
    if not body:
        return None
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            if len(value) > 30 and ("int main" in value or "#include" in value):
                found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found[0] if found else None


def _api_code(entry: dict) -> int | None:
    """从平台响应体里取业务码（`{"code": 0}` → 0）；拿不到就返回 None。"""
    text = entry.get("resp") or ""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("code"), int):
        return data["code"]
    return None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_chrome(cdp_url: str = "http://127.0.0.1:9333", *, log: Callable[[str], None] = print) -> str:
    """确保调试端口可用；必要时用独立 profile 拉起 Chrome。返回 cdp_url。"""
    from urllib.parse import urlparse

    parsed = urlparse(cdp_url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 9333
    if _port_open(host, port):
        return cdp_url

    exe = next((path for path in CHROME_CANDIDATES if Path(path).exists()), None) or shutil.which("chrome")
    if not exe:
        raise HumanError("找不到 Chrome/Edge，无法做浏览器输入")

    profile = Path(tempfile.gettempdir()) / "trainsolve-human-profile"
    profile.mkdir(parents=True, exist_ok=True)
    log(f"  启动浏览器：{exe}（profile={profile}）")
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        time.sleep(0.5)
        if _port_open(host, port):
            return cdp_url
    raise HumanError(f"浏览器调试端口 {port} 一直没起来")


# ---------------------------------------------------------------- 会话


@dataclass
class HumanResult:
    ok: bool
    message: str
    typed_chars: int = 0
    attempts: int = 0


class HumanBrowser:
    """负责"打开答题页 → 打字 → 提交"这一小段。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        cdp_url: str = "http://127.0.0.1:9333",
        profile: TypingProfile | None = None,
        log: Callable[[str], None] = print,
        headless: bool = False,
        native_keys: bool = True,
    ) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.username = username
        self.password = password
        self.cdp_url = cdp_url
        self.profile = profile or TypingProfile()
        self.log = log
        self.headless = headless
        self.config_native_keys = native_keys
        self.typing_stats: dict[str, float] = {}
        self._net: list[dict] = []          # Playwright 网络事件抓到的 post-answer/run-code 响应
        self._net_hooked = False
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    # -------------------------------------------------------- 生命周期

    def __enter__(self) -> "HumanBrowser":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise HumanError("需要 playwright：pip install playwright") from exc

        ensure_chrome(self.cdp_url, log=self.log)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = [p for p in self._context.pages if not p.url.startswith("devtools://")]
        self.page = pages[0] if pages else self._context.new_page()
        self.page.set_default_timeout(20000)

    def close(self) -> None:
        """只断开 CDP 连接，**不关浏览器**。

        注意：`browser.close()` 在 connect_over_cdp 场景下会把整个 Chrome 关掉
        （实测会顺手关掉用户自己的浏览器），所以这里只停 driver。
        """
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._playwright = None
        self.page = None

    # -------------------------------------------------------- 登录

    def login(self) -> None:
        """登录。

        注意：这个 SPA 登录成功后 hash **不一定** 变成 /main/xxx（实测清掉会话后
        hash 还停在 #/main/home），所以不能用 URL 判断，只能看「登录表单是否消失」。
        """
        page = self.page
        page.goto(f"{self.base_url}#/login", wait_until="domcontentloaded")
        time.sleep(1.5)

        if page.locator(LOGIN_USER_INPUT).count() == 0:
            self.log("  浏览器已是登录态")
            return

        page.fill(LOGIN_USER_INPUT, self.username)
        time.sleep(0.2)
        page.fill(LOGIN_PASS_INPUT, self.password)
        time.sleep(0.3)
        page.click("button:has-text('登录')")
        self.log("  已点击登录，等待表单消失…")

        for _ in range(60):  # 最多 30s
            time.sleep(0.5)
            if page.locator(LOGIN_USER_INPUT).count() == 0:
                self.log("  浏览器登录成功")
                return
        raise HumanError(f"浏览器登录失败（登录表单一直没消失），URL：{page.url}")

    # -------------------------------------------------------- 打字

    def open_answer_page(self, session_id: str) -> None:
        url = f"{self.base_url}#/main/go-test/{session_id}"
        self.log(f"  打开答题页：{url}")
        if not self._navigate_to_answer(url):
            raise HumanError("答题页没出现编辑器（会话可能已结束或不是编程题）")
        time.sleep(0.6)

    def _editor_visible(self, timeout_ms: int = 15000) -> bool:
        try:
            self.page.wait_for_selector(EDITOR_SELECTOR, timeout=timeout_ms, state="visible")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _dismiss_dialog(self) -> None:
        """答题页可能弹「继续上次作答 / 题目已提交」之类确认框，顺手点掉。"""
        for text in ("确定", "继续", "好的", "是"):
            try:
                button = self.page.locator(f"mat-dialog-container button:has-text('{text}')")
                if button.count():
                    button.first.click()
                    self.log(f"  弹窗「{text}」")
                    time.sleep(0.6)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _navigate_to_answer(self, url: str) -> bool:
        """打开答题页，并保证编辑器真的渲染出来了。

        SPA 里如果当前 hash 就是目标地址（比如刚提交完又重开同一会话），
        `goto(同一 URL)` 不会重新渲染，页面停在「已提交」视图、永远没有编辑器；
        所以这里刷新一次、再兜底走「历史记录 → 答题页」重新路由。
        """
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 打开地址失败：{exc}")
        if self._editor_visible(8000):
            return True
        self._dismiss_dialog()

        self.log("  没等到编辑器，强制刷新一次…")
        try:
            self.page.reload(wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 刷新失败：{exc}")
        if self._editor_visible(15000):
            return True
        self._dismiss_dialog()

        self.log("  还是没有，走「历史记录 → 答题页」重新路由…")
        try:
            self.page.goto(f"{self.base_url}#/main/history", wait_until="domcontentloaded")
            time.sleep(1.2)
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 重新路由失败：{exc}")
        if self._editor_visible(20000):
            return True
        self._dismiss_dialog()
        return self._editor_visible(8000)

    def type_code(self, code: str, *, should_stop=None) -> tuple[bool, int]:
        """优先用原生 CDP 按键（符号带真 Shift）；拿不到 CDP 会话则退回 playwright。"""
        cdp_session = None
        if self.config_native_keys and self._context is not None:
            try:
                cdp_session = self._context.new_cdp_session(self.page)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  ⚠ 无法创建 CDP 会话（{exc}），退回 playwright 按键")
        io = (CDPTyperIO(self.page, cdp_session, log=self.log)
              if cdp_session else PlaywrightIO(self.page, shift_symbols=False))
        typer = HumanTyper(io, self.profile, log=self.log, should_stop=should_stop)
        self.typer = typer
        expected_len = len(normalize_code(code))
        try:
            ok = typer.type_and_verify(code)
        except TypingStopped as exc:
            self.typing_stats = {
                "indent_fixes": typer.indent_fixes,
                "line_repairs": typer.line_repairs,
                "rounds": typer.rounds_used,
                "cpm": typer.speed_cpm,
            }
            raise HumanStopped("已按「停止」中断打字") from exc
        self.typing_stats = {
            "indent_fixes": typer.indent_fixes,
            "line_repairs": typer.line_repairs,
            "rounds": typer.rounds_used,
            "cpm": typer.speed_cpm,
        }
        return ok, expected_len

    # -------------------------------------------------------- 前端状态核对

    def install_capture(self) -> None:
        """装两层抓包：页面内 XHR/fetch 钩子（拿请求体）+ Playwright 网络事件（拿响应）。"""
        try:
            self.page.evaluate(CAPTURE_JS)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 抓包钩子装不上（{exc}），跳过提交前核对")
        self._hook_network()

    def _hook_network(self) -> None:
        """用 Playwright 的 response 事件记录 post-answer / run-code 的**响应**。

        页面内钩子能拿到请求体，但实测这两条请求的 `loadend` 不触发
        （前端大概用了特殊发送方式），所以「响应是否回来、平台返回什么 code」
        只能从浏览器网络层看。
        """
        if getattr(self, "_net_hooked", False) or self.page is None:
            return

        def on_response(response) -> None:
            try:
                request = response.request
                url = (request.url or "")
                if "post-answer" not in url and "run-code" not in url and "/submit" not in url:
                    return
                body = None
                try:
                    body = request.post_data
                except Exception:  # noqa: BLE001
                    body = None
                text = ""
                if "post-answer" in url or "/submit" in url:
                    try:
                        text = response.text() or ""
                    except Exception:  # noqa: BLE001
                        text = ""
                self._net.append({
                    "url": url,
                    "body": body,
                    "status": response.status,
                    "resp": text[:20000],
                    "done": True,
                })
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", on_response)
            self._net_hooked = True
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 网络事件监听装不上（{exc}）")

    def _net_len(self) -> int:
        return len(self._net)

    def _net_captured(self, since: int) -> list[dict]:
        return list(self._net[since:])

    def _pump(self, seconds: float) -> None:
        """等一会儿，同时让 Playwright 把网络事件派发给监听器。"""
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:  # noqa: BLE001
            time.sleep(seconds)

    def _capture_len(self) -> int:
        try:
            return int(self.page.evaluate("() => (window.__ts || []).length") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _captured(self, since: int) -> list[dict]:
        try:
            return list(self.page.evaluate(CAPTURE_READ_JS, since) or [])
        except Exception:  # noqa: BLE001
            return []

    def flush_editor(self, wait: float = 1.2) -> None:
        """让编辑器失焦并等前端把 doc 变更同步进组件模型。

        前端是 CodeMirror onChange → 组件属性（有 debounce），
        打完字立刻点提交会提交到「少最后几个字符」的旧快照（实测少 6 个字符）。
        """
        try:
            self.page.evaluate(BLUR_JS)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(wait)

    def nudge_editor(self) -> None:
        """在文末做一次「空格 + 退格」，逼前端 onChange 把 doc 同步进组件模型。"""
        typer = getattr(self, "typer", None)
        if typer is None:
            return
        try:
            typer.io.press("End")
            typer.io.type_char(" ")
            typer.io.press("Backspace")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ⚠ 触发前端同步失败（{exc}）")

    def _has_button(self, text: str) -> bool:
        try:
            return self.page.locator(f"button:has-text('{text}')").count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _sync_candidates(self, *, since: int, net_since: int) -> list[dict]:
        """合并两层抓包：页面内钩子（请求体）+ Playwright 网络事件（响应）。"""
        merged: list[dict] = []
        for entry in self._captured(since):
            if "post-answer" in (entry.get("url") or ""):
                merged.append({**entry, "source": "page"})
        for entry in self._net_captured(net_since):
            if "post-answer" in (entry.get("url") or ""):
                merged.append({**entry, "source": "net"})
        return merged

    def run_code(self, *, since: int, net_since: int | None = None, timeout: float = 12.0) -> str | None:
        """点一次「运行」，从 run-code 请求体里读出 App 侧代码（不提交，安全）。"""
        if not self._has_button("运行"):
            self.log("  ⚠ 页面上没有「运行」按钮，跳过运行核对")
            return None
        self.log("  点「运行」核对 App 侧代码…")
        try:
            self.page.click("button:has-text('运行')")
        except Exception:  # noqa: BLE001
            return None
        mark = self._net_len() if net_since is None else net_since
        deadline = time.time() + timeout
        while True:
            entries = list(self._captured(since)) + list(self._net_captured(mark))
            for entry in entries:
                if "run-code" in (entry.get("url") or ""):
                    code = _code_from_body(entry.get("body"))
                    if code:
                        self._pump(0.8)  # 别和随后的提交撞车
                        return code
            if time.time() > deadline:
                return None
            self._pump(0.3)

    def wait_app_code(
        self,
        expected: str,
        *,
        since: int,
        net_since: int = 0,
        timeout: float = 18.0,
    ) -> bool | None:
        """等前端把当前代码**暂存成功**，并校验一致。

        判定要求（缺一不可）：

        1. `post-answer` 携带的代码与目标逐字一致；
        2. 这次请求真的完成（网络层已回执 2xx），平台业务 `code=0`。

        只满足第 1 条就点提交是不够的：实测 Angular 的组件状态在响应回调里才更新，
        于是会出现「请求体里已是完整代码，点提交却带旧快照」——
        2026-11-02 真机因此让平台编译了 339 字符的截断代码、判 0/12。
        """
        want = normalize_code(expected)
        deadline = time.time() + timeout
        last_len = -1
        last_rejection: int | None = None
        pending_logged = False
        while time.time() < deadline:
            pending = False
            for entry in self._sync_candidates(since=since, net_since=net_since):
                body_code = _code_from_body(entry.get("body"))
                if not body_code:
                    continue
                api_code = _api_code(entry)
                if api_code is not None and api_code != 0:
                    if api_code != last_rejection:
                        last_rejection = api_code
                        self.log(f"  ⚠ 暂存被平台拒绝（code={api_code}），不提交，继续等前端重发…")
                    continue
                actual = normalize_code(body_code)
                if actual != want:
                    if len(actual) != last_len:
                        last_len = len(actual)
                        self.log(f"  暂存还是旧快照（{last_len} / {len(want)} 字符），继续等…")
                    continue
                if not entry.get("done"):
                    pending = True
                    continue
                status = entry.get("status")
                if status is not None and not (200 <= int(status) < 300):
                    self.log(f"  ⚠ 暂存请求返回 HTTP {status}，继续等…")
                    continue
                self.log(
                    f"  App 侧代码已暂存成功（{len(want)} 字符，"
                    f"平台已回执 code={api_code if api_code is not None else 'n/a'}）"
                )
                self._pump(0.6)   # 让提交处理器读到更新后的组件状态
                return True
            if pending and not pending_logged:
                pending_logged = True
                self.log("  完整代码的暂存请求已发出，等平台回执…")
            self._pump(0.5)

        got = self.run_code(since=since, net_since=net_since)
        if got is None:
            self.log("  ⚠ 等不到暂存回执、也抓不到运行请求，无法核对 App 侧代码")
            return False
        actual = normalize_code(got)
        same = actual == want
        self.log(f"  App 侧代码核对（运行）：{len(actual)} / {len(want)} 字符 → {'一致' if same else '不一致'}")
        if not same:
            line, column, w, a = first_diff(want, actual)
            self.log(f"      首个差异：第 {line} 行第 {column} 列  期望 {w!r} / 实际 {a!r}")
            return False
        self._pump(0.8)   # 「运行」也会走一遍前端同步，等它落库再提交
        return True

    # -------------------------------------------------------- 提交

    def _submit_verdict(self, net_since: int) -> tuple[bool, int | None]:
        """看平台对提交请求的回执：返回 (是否接受, 业务码)。

        抓不到提交响应时不拦（浏览器侧点提交成功通常也够），
        但拿到非 2xx 或 `code != 0` 就必须当失败——
        2026-11-02 那次 0/12 就是「以为交了、其实平台判的是旧快照」。
        """
        entries = [
            entry for entry in self._net_captured(net_since)
            if "/submit" in (entry.get("url") or "") and "post-answer" not in (entry.get("url") or "")
        ]
        if not entries:
            return True, None
        last = entries[-1]
        status = last.get("status")
        if status is not None and not (200 <= int(status) < 300):
            return False, None
        code = _api_code(last)
        if code is not None and code != 0:
            return False, code
        return True, code

    def submit(self) -> int | None:
        """点「提交」并确认弹窗；返回平台业务码（抓不到回执时返回 None）。"""
        net_mark = self._net_len()
        page = self.page
        clicked = False
        for text in SUBMIT_TEXTS:
            locator = page.locator(f"button:has-text('{text}')")
            if locator.count():
                locator.first.click()
                clicked = True
                self.log(f"  点击「{text}」")
                break
        if not clicked:
            raise HumanError("找不到提交按钮")

        self._pump(0.8)
        for text in CONFIRM_TEXTS:
            dialog_button = page.locator(f"mat-dialog-container button:has-text('{text}')")
            if dialog_button.count():
                dialog_button.first.click()
                self.log(f"  确认弹窗「{text}」")
                break
        self._pump(2.0)

        ok, code = self._submit_verdict(net_mark)
        if not ok:
            raise HumanError(
                f"平台没接受这次提交（code={code if code is not None else 'HTTP 错误'}）"
                f"，记录可能仍可作答，已按失败处理"
            )
        if code is None:
            self.log("  （没抓到提交回执，按已提交处理）")
        else:
            self.log(f"  平台已接受提交（code={code}）")
        return code

    # -------------------------------------------------------- 一体化入口

    def answer(self, session_id: str, code: str, *, dry_run: bool = False,
               should_stop=None) -> HumanResult:
        """打开会话 → 打字 → 核对前端状态 →（可选）提交。

        `should_stop` 传入后，打字过程中每敲一个字符都会问一次，命中即抛
        HumanStopped，不再继续（也不会提交）。
        """
        if should_stop is not None and should_stop():
            raise HumanStopped("已按「停止」，未开始作答")
        self.open_answer_page(session_id)
        self.install_capture()
        mark = self._capture_len()
        net_mark = self._net_len()
        ok, chars = self.type_code(code, should_stop=should_stop)
        if should_stop is not None and should_stop():
            raise HumanStopped("已按「停止」，打字后不再提交")
        if not ok:
            return HumanResult(False, "打字校验未通过（文档与目标代码不一致）", chars, 1)

        # 提交前必须确认前端已经把当前代码同步出去（否则平台收到的是旧快照）
        self.log("  等前端把代码同步上去…")
        self.nudge_editor()
        verdict = self.wait_app_code(code, since=mark, net_since=net_mark, timeout=18.0)
        if verdict is False:
            self.log("  ⚠ App 侧还是旧快照，再触发一次编辑并等待…")
            self.nudge_editor()
            verdict = self.wait_app_code(code, since=mark, net_since=net_mark, timeout=10.0)
        if verdict is False:
            return HumanResult(False, "前端组件里的代码没同步完整，已放弃提交（避免白交一次）", chars, 1)

        if dry_run:
            return HumanResult(True, f"已输入 {chars} 字符并校验通过（dry-run，未提交）", chars, 1)
        self.submit()
        return HumanResult(True, f"已输入 {chars} 字符并提交", chars, 1)
