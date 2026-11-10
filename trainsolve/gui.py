"""桌面客户端（tkinter，零第三方依赖）。

    python -m trainsolve gui
    python client_main.py

设计要点：
- 网络/模型调用全部在后台线程，UI 通过队列收消息，不阻塞界面；
- 「自动刷题」= Runner.run()（AI 解题 + 自检 + 提交 + 轮询评分）；
- 「抽新题」= Runner.peek()，**会新建会话**（POST choose-q，占一个题量名额），之后可在「代码」页手写代码并直接提交（人工模式）；
- 「模拟真人打字」= 勾选后作答环节交给浏览器：逐字符输入 + 真实点击提交
  （Playwright 仅用 connect_over_cdp 连本机 Chrome，取题/自检/评分仍走 API）。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

from typing import Callable

from . import __version__
from .api import QBASE_NAMES, STATE_NAMES, ApiError, TrainClient, qtype_kind
from .config import (
    Config,
    ModelEndpoint,
    ResolvedConfig,
    default_app_dir,
    describe_config_source,
    load_config,
    resolve_config_path,
)
from .judge import find_compiler
from .runner import Runner
from .solver.base import Answer
from .solver.factory import build_solver

LEVELS = [("随机", 0), ("初学者", 1), ("容易", 2), ("中等", 3), ("困难", 4), ("极难", 5)]
TYPES = [(QBASE_NAMES[i], i) for i in (4, 3, 2, 1, 0)]  # 编程优先
JUDGES = [("本地 gcc", "local"), ("平台编译", "server"), ("两者", "both"), ("不判题", "none")]
# 题型白名单预设：qType 5 普通编程 / 6 实验编程 / 7 复杂编程 / 8、9 改错题 / 4 程序填空 / 10 设计
QTYPE_PRESETS = [
    ("只做普通编程题（5）", [5]),
    ("编程类 5/6/7，排除改错题", [5, 6, 7]),
    ("不限制（含改错题、程序填空）", []),
]
# 官方（api.deepseek.com）可用的模型 id；第一位就是默认值。
# 自建网关请在「模型名」里手填它自己的 id（例如 cn:xxx），不要从这里选。
MODELS = [
    "deepseek-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
]


def _app_dirs() -> list[Path]:
    """程序目录：打包版是 exe 所在目录，源码检出是仓库根。

    **当前工作目录不参与**：0.5.2 的事故就是「从仓库根启动 → 读到仓库里那份
    submit=true 的配置」，界面上毫无提示。要读 cwd 那份，必须显式指定。
    """
    return [default_app_dir()]


PORTABLE_MARKERS = ("portable.txt", "portable")


def config_resolution(*, frozen: bool | None = None) -> ResolvedConfig:
    """当前配置解析结果：绝对路径 + 来源（explicit/portable/program/appdata）。

    配置在哪只由 `config.resolve_config_path()` 一处决定，界面不再自备一套查找顺序
    ——之前两套并存，正是「从仓库根启动就读到仓库里那份配置」的根因。
    """
    dirs = _app_dirs()
    return resolve_config_path(app_dir=dirs[0] if dirs else None, frozen=frozen)


def settings_path(*, frozen: bool | None = None) -> Path:
    """配置文件位置。

    **只回答“在哪”，不建目录、不写盘**——判定逻辑在 `config_resolution()`。
    建目录属于写盘动作，统一放在真正的写入方（`save_client_config()` /
    `_log_ui_error()`）里。这里以前顺手 mkdir，后果是「问一句路径」也在磁盘上
    留下目录：测试未隔离 `APPDATA` 时会在真实 `%APPDATA%\\TrainSolve` 下建目录。
    """
    return config_resolution(frozen=frozen).path


def error_log_path() -> Path:
    """界面线程异常日志：与 config.toml 同目录。

    pythonw / 打包后的 exe 没有控制台，Tk 回调的 traceback 默认直接消失，
    所以必须落盘，否则“界面没反应”这类问题下次还是抓瞎。
    """
    return settings_path().parent / "client-errors.log"


def template_path() -> Path | None:
    """打包时随 exe 附带的 config.example.toml（公开版给新用户当默认值）。"""
    for directory in _app_dirs():
        candidate = directory / "config.example.toml"
        if candidate.exists():
            return candidate
    return None


def load_client_config_with_source() -> tuple[Config, ResolvedConfig]:
    """读配置，并一并交出「这份配置到底从哪来」（启动日志与验收证据用）。

    1) 已解析到的 config.toml → 用它；
    2) 否则若有随包附带的 config.example.toml → 当默认值（公开版靠这个给出干净默认值）；
    3) 再否则用代码里的默认值。
    """
    resolved = config_resolution()
    if resolved.path.exists():
        return load_config(str(resolved.path)), resolved

    template = template_path()
    if template is not None:
        return load_config(str(template)), resolved

    return Config(), resolved


def load_client_config() -> Config:
    """读配置（只要配置对象时用这个；要来源信息用 load_client_config_with_source()）。"""
    return load_client_config_with_source()[0]


def save_client_config(config: Config, log: Callable[[str], None] | None = None) -> None:
    """写配置；覆盖已有文件时留一行日志（写到哪个文件、原来多大、现在多大）。

    0.5.2 那次仓库根的 config.toml 被静默改写就是缺这一行：事后没人说得清
    是哪个动作写的、写到哪个文件、覆盖掉了什么。
    """
    path = settings_path()
    # 建目录放在这里（真正的写入方）：settings_path() 只回答路径，不再产生副作用。
    # 首次运行时 %APPDATA%\TrainSolve 可能还不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    before = path.stat().st_size if existed else 0
    text = f"""# TrainSolve 客户端配置
[platform]
base_url = "{config.platform.base_url}"
username = "{config.platform.username}"
password = "{config.platform.password}"
timeout = {config.platform.timeout}

[deepseek]
api_key = "{config.deepseek.api_key}"
base_url = "{config.deepseek.base_url}"
model = "{config.deepseek.model}"
temperature = {config.deepseek.temperature}
max_tokens = {config.deepseek.max_tokens}
timeout = {config.deepseek.timeout}
retries = {config.deepseek.retries}
preferred = {config.deepseek.preferred}
"""
    for endpoint in config.deepseek.fallbacks:
        text += f"""
[[deepseek.fallback]]
api_key = "{endpoint.api_key}"
base_url = "{endpoint.base_url}"
model = "{endpoint.model}"
label = "{endpoint.label or '备用'}"
"""
    text += f"""
[run]
qbase_type = {config.run.qbase_type}
level = {config.run.level}
tag_id = {config.run.tag_id}
count = {config.run.count}
judge = "{config.run.judge}"
max_repairs = {config.run.max_repairs}
gcc = "{config.run.gcc}"
out_dir = "{config.run.out_dir}"
submit = {"true" if config.run.submit else "false"}
confirm = {"true" if config.run.confirm else "false"}
dry_run = {"true" if config.run.dry_run else "false"}
solver = "{config.run.solver}"
score_timeout = {config.run.score_timeout}
sleep_between = {config.run.sleep_between}
stop_on_failure = {"true" if config.run.stop_on_failure else "false"}
require_verified = {"true" if config.run.require_verified else "false"}
qtypes = {json.dumps(list(config.run.qtypes or []))}
auto_fix_from_judge = {"true" if config.run.auto_fix_from_judge else "false"}
max_reanswers = {config.run.max_reanswers}
max_consecutive_failures = {config.run.max_consecutive_failures}
max_consecutive_skips = {config.run.max_consecutive_skips}
max_consecutive_infra_errors = {config.run.max_consecutive_infra_errors}
cleanup_abandoned = {"true" if config.run.cleanup_abandoned else "false"}
auto_cleanup = {"true" if config.run.auto_cleanup else "false"}
auto_cleanup_all = {"true" if config.run.auto_cleanup_all else "false"}
auto_cleanup_temp_hours = {config.run.auto_cleanup_temp_hours}
allow_unverified_submit = {"true" if config.run.allow_unverified_submit else "false"}
human_input = {"true" if config.run.human_input else "false"}
human_dry_run = {"true" if config.run.human_dry_run else "false"}
cdp_url = "{config.run.cdp_url}"
typing_cpm = {config.run.typing_cpm}
typing_cpm_min = {config.run.typing_cpm_min}
typing_cpm_max = {config.run.typing_cpm_max}
typo_rate = {config.run.typo_rate}
human_delay_min = {config.run.human_delay_min}
human_delay_max = {config.run.human_delay_max}
backspace_rate = {config.run.backspace_rate}
backspace_max = {config.run.backspace_max}
"""
    path.write_text(text, encoding="utf-8")
    if log is not None:
        after = path.stat().st_size
        if existed:
            log(f"⚠ 已覆盖配置文件：{path}（原有 {before} B → 现在 {after} B）")
        else:
            log(f"已创建配置文件：{path}（{after} B）")


class EditorSolver:
    """人工模式：直接把编辑框里的代码当成答案。"""

    name = "editor"

    def __init__(self, text: str) -> None:
        self.text = text

    def solve(self, question: dict) -> Answer:
        kind = "code" if qtype_kind(int(question.get("qType") or 0)) == "programming" else "answer"
        return Answer(text=self.text, kind=kind)

    def repair(self, question: dict, previous: str, feedback: str) -> Answer:
        return Answer(text=self.text, kind="code")


# 设计基准尺寸（按 100% DPI 的逻辑像素算）；真实窗口与最小尺寸都按 ui_scale 放大
_DESIGN_WIDTH, _DESIGN_HEIGHT = 1180, 800
_DESIGN_MIN_WIDTH, _DESIGN_MIN_HEIGHT = 980, 680

# 日志页最多保留的行数：全自动可跑整夜，不设上限 Text 会越来越慢
_MAX_LOG_LINES = 4000


def _enable_dpi_awareness() -> None:
    """Windows：声明 Per-Monitor V2 DPI 感知。

    必须在创建 tk.Tk() **之前**调用。Tk 9 会据此把 tk scaling 自动设成
    dpi/72，字体按真实 DPI 排版；否则整窗被 Windows 按位图拉伸，中文发虚。
    非 Windows 或调用失败时静默跳过，行为与改动前一致。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:  # Win10 1703+
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
                return
        except (AttributeError, OSError):
            pass
        try:  # Win8.1+
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # PER_MONITOR_DPI_AWARE
                return
        except (AttributeError, OSError):
            pass
        user32.SetProcessDPIAware()  # Vista+
    except Exception:  # noqa: BLE001 - DPI 只是显示优化，失败不该影响启动
        return


class ClientApp(tk.Tk):
    def __init__(self) -> None:
        _enable_dpi_awareness()  # 必须在 tk.Tk() 之前
        super().__init__()
        self.ui_scale = self._detect_scale()
        self.title(f"程序设计训练平台 · 自动刷题客户端 v{__version__}")
        self.geometry(
            f"{round(_DESIGN_WIDTH * self.ui_scale)}x{round(_DESIGN_HEIGHT * self.ui_scale)}"
        )
        self.minsize(
            round(_DESIGN_MIN_WIDTH * self.ui_scale), round(_DESIGN_MIN_HEIGHT * self.ui_scale)
        )

        self.config_data, self.config_source = load_client_config_with_source()
        self.client: TrainClient | None = None
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._auto_cleaned = False   # 每个进程只自动清一次残留
        self.current_session = ""
        self.current_qid = None
        self.log_lines: list[str] = []

        self._build_ui()
        self._fill_from_config()
        self._apply_min_size()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_ids: set[str] = set()
        self._schedule(120, self._drain_queue)
        self._schedule(300, self._first_run_hint)

    # ------------------------------------------------------------ 定时器管理
    def _schedule(self, delay_ms: int, callback) -> str:  # noqa: ANN001
        """登记一个 `after` 定时器，窗口关闭时统一取消。

        不登记的话，窗口销毁后定时器仍会在解释器里排队，回调打到已销毁的控件上，
        Tk 会打印 `invalid command name "..._drain_queue"`，并在关窗时写一条无意义的
        `client-errors.log`。
        """
        holder: dict[str, str] = {}

        def _fire() -> None:
            self._after_ids.discard(holder.get("id", ""))
            callback()

        handle = self.after(delay_ms, _fire)
        holder["id"] = handle
        self._after_ids.add(handle)
        return handle

    def _cancel_pending_timers(self) -> None:
        """取消所有未触发的定时器；解释器可能已销毁，取消失败一律忽略。"""
        for handle in tuple(self._after_ids):
            try:
                self.after_cancel(handle)
            except Exception:  # noqa: BLE001 - Tk 已销毁时 after_cancel 会抛 TclError
                pass
        self._after_ids.clear()

    def _on_close(self) -> None:
        """用户点关闭：先请求停止（别让后台线程在窗口没了之后还接着跑），再清定时器、销毁。"""
        try:
            self.stop_event.set()
        except Exception:  # noqa: BLE001 - 极早期关闭时事件还没建好
            pass
        self._cancel_pending_timers()
        self.destroy()

    def destroy(self) -> None:
        # 直接 destroy()（测试、异常退出）也要清理，不能只靠关闭按钮那条路径
        self._cancel_pending_timers()
        super().destroy()

    def _apply_min_size(self) -> None:
        """最小尺寸按控件实际需要算，不写死。

        间距与字号都随 DPI 放大，写死的下限（680 逻辑像素）在 150% 下装不下
        放大后的左栏和状态栏——实测窗口请求 1037px 而下限只有 1019px，
        「提交我的代码」和底部状态栏会被切掉一截。
        """
        self.update_idletasks()
        width = max(round(_DESIGN_MIN_WIDTH * self.ui_scale), self.winfo_reqwidth())
        height = max(round(_DESIGN_MIN_HEIGHT * self.ui_scale), self.winfo_reqheight())
        margin = self._pad(8)
        self.minsize(width + margin, height + margin)

    def _detect_scale(self) -> float:
        """按实际 DPI 返回相对 96 DPI 的缩放比（100% → 1.0，150% → 1.5）。"""
        try:
            return max(1.0, round(self.winfo_fpixels("1i") / 96.0, 3))
        except tk.TclError:
            return 1.0

    def _pad(self, value: int) -> int:
        """把设计稿里的像素间距按 DPI 缩放（8px 在 150% 下变 12px）。

        只作用于 padx/pady 这类**几何间距**，不动字号——字号由 tk scaling 统一
        放大；间距不跟着放大的话，150% 下界面会比 100% 时明显更挤。

        用 `+0.5` 再取整，不用 round()：round 是银行家舍入，round(4.5) 得 4，
        同一套间距在相邻缩放档上会出现该进位不进位的情况。
        """
        return int(value * self.ui_scale + 0.5)

    def _first_run_hint(self) -> None:
        """首次运行（没填账号或 Key）时给一句指引，别让人对着窗口发呆。"""
        cfg = self.config_data
        # 启动第一行就交代：实际用的是哪个配置文件、来源是什么、两个安全开关的值。
        # 0.5.2 的事故就是「配置被换了一份、界面毫无提示」，这行是验收硬证据。
        self.log(describe_config_source(self.config_source, cfg))
        missing = []
        if not cfg.platform.username or not cfg.platform.password:
            missing.append("平台账号 / 密码（顶部输入框）")
        if not cfg.deepseek.api_key:
            missing.append("模型 API Key（右上「更换模型…」或「设置」里填）")
        if not missing:
            self.log(f"配置就绪：{cfg.deepseek.current().display() if cfg.deepseek.current() else '未配置模型'}")
            return
        self.log("👋 首次使用，还缺这些信息：")
        for item in missing:
            self.log(f"   · {item}")
        self.set_status("请先补全配置")

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        top = ttk.LabelFrame(self, text="连接")
        top.pack(fill="x", padx=self._pad(10), pady=(self._pad(10), self._pad(4)))

        ttk.Label(top, text="平台地址").grid(row=0, column=0, sticky="w",
                                        padx=self._pad(6), pady=self._pad(5))
        self.var_base = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_base, width=30).grid(row=0, column=1, padx=self._pad(6))

        ttk.Label(top, text="学号").grid(row=0, column=2, sticky="w", padx=self._pad(6))
        self.var_user = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_user, width=16).grid(row=0, column=3, padx=self._pad(6))

        ttk.Label(top, text="密码").grid(row=0, column=4, sticky="w", padx=self._pad(6))
        self.var_pass = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_pass, width=14, show="*").grid(
            row=0, column=5, padx=self._pad(6))

        ttk.Button(top, text="登录", command=self.on_login).grid(row=0, column=6, padx=self._pad(6))
        ttk.Button(top, text="设置", command=self.on_settings).grid(row=0, column=7, padx=self._pad(6))
        self.lbl_conn = ttk.Label(top, text="未连接", foreground="#b3261e")
        self.lbl_conn.grid(row=0, column=8, padx=self._pad(10), sticky="w")

        # 第二行：当前模型 + 更换模型
        ttk.Label(top, text="当前模型").grid(row=1, column=0, sticky="w",
                                       padx=self._pad(6), pady=(0, self._pad(6)))
        self.var_model = tk.StringVar(value="")
        self.lbl_model = ttk.Label(top, textvariable=self.var_model, foreground="#1a4d8f")
        self.lbl_model.grid(row=1, column=1, columnspan=4, sticky="w",
                            padx=self._pad(6), pady=(0, self._pad(6)))
        self.btn_model = ttk.Button(top, text="更换模型…", command=self.on_switch_model)
        self.btn_model.grid(row=1, column=6, padx=self._pad(6), pady=(0, self._pad(6)), sticky="w")
        ttk.Button(top, text="测一测", command=self.on_test_current_model).grid(
            row=1, column=7, padx=self._pad(6), pady=(0, self._pad(6)), sticky="w"
        )

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=self._pad(10), pady=self._pad(4))

        left = ttk.LabelFrame(body, text="选题")
        left.pack(side="left", fill="y", padx=(0, self._pad(8)))
        left.configure(width=270)

        ttk.Label(left, text="题型").pack(anchor="w", padx=self._pad(8), pady=(self._pad(8), 0))
        self.var_type = tk.StringVar(value=TYPES[0][0])
        ttk.Combobox(left, textvariable=self.var_type, values=[t for t, _ in TYPES],
                     state="readonly", width=22).pack(padx=self._pad(8), pady=self._pad(2))

        ttk.Label(left, text="难度").pack(anchor="w", padx=self._pad(8), pady=(self._pad(8), 0))
        self.var_level = tk.StringVar(value="容易")
        ttk.Combobox(left, textvariable=self.var_level, values=[n for n, _ in LEVELS],
                     state="readonly", width=22).pack(padx=self._pad(8), pady=self._pad(2))

        ttk.Label(left, text="标签").pack(anchor="w", padx=self._pad(8), pady=(self._pad(8), 0))
        self.var_tag = tk.StringVar(value="随机")
        self.cmb_tag = ttk.Combobox(left, textvariable=self.var_tag, values=["随机"],
                                    state="readonly", width=22)
        self.cmb_tag.pack(padx=self._pad(8), pady=self._pad(2))
        self.tag_map: dict[str, int] = {"随机": -1}

        row = ttk.Frame(left)
        row.pack(fill="x", padx=self._pad(8), pady=(self._pad(10), self._pad(2)))
        ttk.Label(row, text="数量").pack(side="left")
        self.var_count = tk.IntVar(value=1)
        ttk.Spinbox(row, from_=1, to=10, textvariable=self.var_count, width=5).pack(
            side="left", padx=self._pad(6))

        ttk.Label(left, text="判题方式").pack(anchor="w", padx=self._pad(8), pady=(self._pad(8), 0))
        self.var_judge = tk.StringVar(value="本地 gcc")
        ttk.Combobox(left, textvariable=self.var_judge, values=[n for n, _ in JUDGES],
                     state="readonly", width=22).pack(padx=self._pad(8), pady=self._pad(2))

        self.var_human = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="模拟真人打字提交", variable=self.var_human,
                        command=self._on_human_toggle).pack(anchor="w", padx=self._pad(8),
                                                           pady=(self._pad(8), 0))
        self.lbl_human = ttk.Label(left, text="勾选后：在浏览器里逐字符输入，再点提交",
                                   foreground="#777",
                                   # wraplength 是像素，得跟着 DPI 缩放走，
                                   # 否则 150% 下同样 236px 只装得下一半的字，换行点会变。
                                   wraplength=self._pad(236), justify="left")
        self.lbl_human.pack(anchor="w", padx=self._pad(8))
        # 0.6.2：把「到底会不会真提交」直接写在界面上——发布包默认 dry_run=true，
        # 勾了「模拟真人打字提交」却只在本地解题，是实测里最容易误解的一处。
        self.lbl_submit_mode = ttk.Label(left, text="", foreground="#777", justify="left",
                                         wraplength=self._pad(236))
        self.lbl_submit_mode.pack(anchor="w", padx=self._pad(8), pady=(self._pad(2), 0))

        ttk.Separator(left).pack(fill="x", pady=self._pad(10), padx=self._pad(8))

        ttk.Button(left, text="刷新标签", command=self.on_refresh_tags).pack(
            fill="x", padx=self._pad(8), pady=self._pad(3))
        self.btn_auto = ttk.Button(left, text="开始自动刷题", command=self.on_auto)
        self.btn_auto.pack(fill="x", padx=self._pad(8), pady=self._pad(3))
        self.btn_loop = ttk.Button(left, text="全自动（连续刷）", command=self.on_auto_loop)
        self.btn_loop.pack(fill="x", padx=self._pad(8), pady=self._pad(3))
        self.btn_peek = ttk.Button(left, text="抽新题（新建会话）", command=self.on_peek)
        self.btn_peek.pack(fill="x", padx=self._pad(8), pady=self._pad(3))
        ttk.Button(left, text="停止", command=self.on_stop).pack(
            fill="x", padx=self._pad(8), pady=self._pad(3))

        ttk.Separator(left).pack(fill="x", pady=self._pad(10), padx=self._pad(8))
        ttk.Label(left, text="人工模式", foreground="#555").pack(anchor="w", padx=self._pad(8))
        ttk.Button(left, text="本地自检我的代码", command=self.on_selfcheck).pack(
            fill="x", padx=self._pad(8), pady=self._pad(3))
        self.btn_submit = ttk.Button(left, text="提交我的代码", command=self.on_submit_mine)
        self.btn_submit.pack(fill="x", padx=self._pad(8), pady=self._pad(3))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        self.txt_log = self._text_tab("日志", readonly=True)
        self.txt_statement = self._text_tab("题目", readonly=True)
        self.txt_code = self._text_tab("代码", readonly=False, mono=True)
        self.txt_score = self._text_tab("评分", readonly=True)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=self._pad(10), pady=(0, self._pad(8)))
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=self._pad(160))
        self.progress.pack(side="right")
        self.btn_clear_log = ttk.Button(bar, text="清空日志", command=self.on_clear_log)
        self.btn_clear_log.pack(side="right", padx=(0, self._pad(10)))

    def _text_tab(self, title: str, readonly: bool, mono: bool = False) -> tk.Text:
        """建一个带滚动条的文本框；等宽（代码）页额外配横向滚动条。

        scrolledtext.ScrolledText 只带纵向条，而代码页是 wrap="none"：
        没有横向条时，超出一屏的长行看不到行尾，只能靠光标把视图带过去。
        """
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=title)

        vbar = ttk.Scrollbar(frame, orient="vertical")
        vbar.pack(side="right", fill="y")
        hbar = ttk.Scrollbar(frame, orient="horizontal") if mono else None
        if hbar is not None:
            hbar.pack(side="bottom", fill="x")

        widget = tk.Text(
            frame,
            wrap="none" if mono else "word",
            undo=not readonly,
            yscrollcommand=vbar.set,
            xscrollcommand=hbar.set if hbar is not None else "",
        )
        if mono:
            widget.configure(font=("Consolas", 10))
        if readonly:
            widget.configure(state="disabled")
        widget.pack(side="left", fill="both", expand=True)

        vbar.configure(command=widget.yview)
        if hbar is not None:
            hbar.configure(command=widget.xview)
            self._hbar_code = hbar  # 留引用，方便测试断言
        return widget

    # ------------------------------------------------------------ 小工具

    def _fill_from_config(self) -> None:
        cfg = self.config_data
        self.var_base.set(cfg.platform.base_url)
        self.var_user.set(cfg.platform.username)
        self.var_pass.set(cfg.platform.password)
        level_name = next((n for n, v in LEVELS if v == cfg.run.level), "容易")
        self.var_level.set(level_name)
        type_name = QBASE_NAMES.get(cfg.run.qbase_type, "编程题")
        self.var_type.set(type_name)
        judge_name = next((n for n, v in JUDGES if v == cfg.run.judge), "本地 gcc")
        self.var_judge.set(judge_name)
        self.var_count.set(cfg.run.count)
        self.var_human.set(bool(cfg.run.human_input))
        self._on_human_toggle()
        self._refresh_model_label()

    def _refresh_model_label(self) -> None:
        endpoints = self.config_data.deepseek.endpoints()
        if not endpoints:
            self.var_model.set("<未配置模型>")
            return
        current = endpoints[0]
        extra = len(endpoints) - 1
        text = f"{current.model}  @ {current.base_url}"
        if extra:
            text += f"（另有 {extra} 个备用，失败自动切换）"
        self.var_model.set(text)

    def on_switch_model(self) -> None:
        ModelDialog(self)

    def on_test_current_model(self) -> None:
        cfg = self._cfg_snapshot()
        current = cfg.deepseek.current()
        if current is None:
            messagebox.showinfo("提示", "还没有配置任何模型端点。", parent=self)
            return
        self.log(f"测试模型：{current.model} @ {current.base_url}")
        self._set_model_test_result(None)

        def work() -> None:
            from .solver.deepseek import DeepSeekClient

            try:
                client = DeepSeekClient(
                    current.api_key, base_url=current.base_url, model=current.model,
                    max_tokens=64, timeout=min(90.0, cfg.deepseek.timeout), retries=0, log=lambda *_: None,
                )
                reply = client.chat([{"role": "user", "content": "只回复两个字：可用"}], temperature=0)
                self._set_model_test_result((True, f"✓ {current.model} 可用：{reply.strip()[:20]}"))
            except Exception as exc:  # noqa: BLE001
                self._set_model_test_result((False, f"✗ {str(exc)[:100]}"))

        threading.Thread(target=work, daemon=True).start()
        self._poll_model_test()

    def _set_model_test_result(self, value) -> None:
        self._model_test_result = value

    def _poll_model_test(self) -> None:
        result = getattr(self, "_model_test_result", None)
        if result is None:
            self._schedule(250, self._poll_model_test)
            return
        ok, message = result
        self.lbl_model.configure(foreground="#1a7f37" if ok else "#b3261e")
        self.log(message)
        self.set_status("模型可用" if ok else "模型不可用")
        self._schedule(4000, lambda: self.lbl_model.configure(foreground="#1a4d8f"))

    def _cfg_snapshot(self) -> Config:
        cfg = self.config_data
        cfg.platform.base_url = self.var_base.get().strip() or cfg.platform.base_url
        cfg.platform.username = self.var_user.get().strip()
        cfg.platform.password = self.var_pass.get()
        cfg.run.qbase_type = next(v for n, v in TYPES if n == self.var_type.get())
        cfg.run.level = next(v for n, v in LEVELS if n == self.var_level.get())
        cfg.run.tag_id = self.tag_map.get(self.var_tag.get(), -1)
        cfg.run.count = 0 if getattr(self, "auto_mode", False) else max(1, int(self.var_count.get() or 1))
        cfg.run.judge = next(v for n, v in JUDGES if n == self.var_judge.get())
        cfg.run.human_input = bool(self.var_human.get())
        # 选了客观题型时，编程类白名单不适用 → 不限制
        if cfg.run.qbase_type < 4 and cfg.run.qtypes in ([5], [5, 6, 7]):
            cfg.run.qtypes = []
        return cfg

    def log(self, message: str) -> None:
        self.queue.put(("log", message))

    def _append(self, widget: tk.Text, text: str, clear: bool = False) -> None:
        widget.configure(state="normal")
        if clear:
            widget.delete("1.0", "end")
        widget.insert("end", text + "\n")
        widget.see("end")
        if widget is not self.txt_code:
            widget.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _push_log(self, line: str) -> None:
        """追加一行日志；超过上限就裁掉最旧的，控件和内存都不无限增长。"""
        self.log_lines.append(line)
        self._append(self.txt_log, line)
        overflow = len(self.log_lines) - _MAX_LOG_LINES
        if overflow <= 0:
            return
        del self.log_lines[:overflow]
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", f"{overflow + 1}.0")
        self.txt_log.configure(state="disabled")

    def on_clear_log(self) -> None:
        """清空日志页（整夜连续刷题后手动收一下）。"""
        self.log_lines.clear()
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")
        self.log("日志已清空")

    def _on_human_toggle(self) -> None:
        """勾选/取消「模拟真人打字」时更新提示（具体速度/CDP 在设置里改）。"""
        on = bool(self.var_human.get())
        cfg = self.config_data
        text = (
            f"已开启：基准 {int(cfg.run.typing_cpm)} 字符/分，每次作答随机浮动，逐字符打字"
            + ("后真实点击提交" if self._really_submits() else "（当前不会提交）")
            if on
            else "勾选后：在浏览器里逐字符输入，再点提交"
        )
        self.lbl_human.configure(text=text, foreground="#1a7f37" if on else "#777")
        self._refresh_submit_mode()

    def _really_submits(self) -> bool:
        """这次运行到底会不会真提交：dry_run 或 submit=false 都不会。"""
        cfg = self.config_data
        return bool(cfg.run.submit) and not bool(cfg.run.dry_run)

    def _refresh_submit_mode(self) -> None:
        """主界面常驻一行：试运行 / 不提交 / 真提交。"""
        cfg = self.config_data
        if cfg.run.dry_run:
            text, color = "当前：试运行（dry_run=true），只解题不提交", "#b26a00"
        elif not cfg.run.submit:
            text, color = "当前：不提交（submit=false），只解题", "#b26a00"
        else:
            text, color = "当前：真提交（每次提交消耗一次作答机会）", "#1a7f37"
        self.lbl_submit_mode.configure(text=text, foreground=color)

    # ------------------------------------------------------------ 队列

    def _drain_queue(self) -> None:
        """把后台线程的消息刷到界面。

        两条硬约束（2026-11-04 冒烟踩过，界面看着活着、日志全停）：
        1) 单条消息处理失败只跳过它，不能毒死整条泵；
        2) 无论本轮成功还是失败，都必须重新排上 `after` —— 否则日志/状态永久停更，
           而窗口照样响应消息（看起来只是“点了没反应”）。
        """
        try:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_message(kind, payload)
                except Exception:  # noqa: BLE001 - 一条坏消息不能拖垮整条泵
                    self._log_ui_error(kind)
        finally:
            try:
                self._schedule(120, self._drain_queue)
            except Exception:  # noqa: BLE001 - 窗口已销毁，没有界面可刷了
                pass

    def _handle_message(self, kind: str, payload) -> None:
        if kind == "log":
            self._push_log(payload)
        elif kind == "statement":
            self._append(self.txt_statement, payload, clear=True)
        elif kind == "code":
            self._append(self.txt_code, payload, clear=True)
        elif kind == "score":
            self._append(self.txt_score, payload, clear=True)
        elif kind == "session":
            self.current_session = payload
        elif kind == "tags":
            self.cmb_tag.configure(values=payload)
            self.var_tag.set("随机")
        elif kind == "status":
            if self._stopping():
                return  # 已请求停止：中间状态（如「等待 59s 重试」）不许盖掉提示
            self.set_status(payload)
        elif kind == "conn":
            ok, text = payload
            self.lbl_conn.configure(text=text, foreground="#1a7f37" if ok else "#b3261e")
        elif kind == "ask":
            question, event, box = payload
            # 弹窗本身抛异常（父窗口已销毁等）时也必须 set()，否则后台线程空等 120 秒，
            # 表现成「任务卡住」——异常交给 _drain_queue 兜住并落盘。
            try:
                answer = messagebox.askyesno("确认", question, parent=self)
                box["answer"] = answer
            finally:
                event.set()
        elif kind == "done":
            self._on_worker_done(payload)

    def _log_ui_error(self, kind: str) -> None:
        """界面线程异常落盘：打包成 pythonw 后没有控制台，traceback 不写文件就等于消失。"""
        detail = traceback.format_exc()
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 队列消息处理失败 kind={kind}\n{detail}\n"
        try:
            log_path = error_log_path()
            # settings_path() 不再建目录，这里自己保证目录存在，否则首次运行的红
            # traceback 会因为「目录不存在」被吞掉——那正是这条日志存在的意义。
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:  # noqa: BLE001 - 记不下来也不能再抛，否则又回到老路
            pass
        try:
            self._append(self.txt_log, f"⚠ 界面消息处理失败（{kind}），详情见 {error_log_path()}")
        except Exception:  # noqa: BLE001 - 日志控件本身可能就是这个异常源
            pass

    def _on_worker_done(self, message: str) -> None:
        self.progress.stop()
        for btn in (self.btn_auto, self.btn_loop, self.btn_peek, self.btn_submit):
            btn.configure(state="normal")
        if self.stop_event.is_set() and message not in ("出错", "接口错误"):
            # 用户点过停止：worker 真的退出了，才把「已请求停止」落成「已停止」
            self.set_status("已停止")
            self.log("⏹ 已停止")
        else:
            self.set_status(message)

    def _stopping(self) -> bool:
        """已经请求停止、但 worker 还没退出——这段过渡期别让中间状态盖掉提示。"""
        return self.stop_event.is_set() and bool(self.worker and self.worker.is_alive())

    def _run_worker(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务在跑，请先等它结束或点停止。", parent=self)
            return
        self.stop_event.clear()
        self.progress.start(12)
        for btn in (self.btn_auto, self.btn_loop, self.btn_peek, self.btn_submit):
            btn.configure(state="disabled")
        self.worker = threading.Thread(target=self._wrapper, args=(target, *args), daemon=True)
        self.worker.start()

    def _wrapper(self, target, *args) -> None:
        try:
            target(*args)
        except SystemExit as exc:
            self.log(f"✗ {exc}")
            self.queue.put(("done", "已中止"))
        except ApiError as exc:
            self.log(f"✗ 接口错误：{exc} {exc.body[:200] if exc.body else ''}")
            self.queue.put(("done", "接口错误"))
        except Exception as exc:  # noqa: BLE001 - 客户端要兜住所有异常
            self.log(f"✗ 未预期错误：{type(exc).__name__}: {exc}")
            self.queue.put(("done", "出错"))
        else:
            self.queue.put(("done", "完成"))

    def _confirm_from_worker(self, question: str) -> bool:
        if not self.config_data.run.confirm:
            return True
        event = threading.Event()
        box: dict[str, bool] = {}
        self.queue.put(("ask", (question, event, box)))
        event.wait(timeout=120)
        return bool(box.get("answer"))

    # ------------------------------------------------------------ 动作

    def _ensure_client(self, cfg: Config) -> TrainClient:
        client = self.client
        if client is None or client.base_url.rstrip("/") != cfg.platform.base_url.rstrip("/"):
            client = TrainClient(cfg.platform.base_url, timeout=cfg.platform.timeout, log=self.log)
            self.client = client
        return client

    def _auto_cleanup(self, cfg: Config, client: TrainClient) -> None:
        """启动/登录后清一次残留：过期「未完成」会话 + 陈旧临时目录（每进程只做一次）。

        默认**不动**仍在时限内的会话；清理失败只记日志，不影响刷题。
        """
        if not cfg.run.auto_cleanup or self._auto_cleaned:
            return
        self._auto_cleaned = True
        from .leftovers import auto_cleanup

        try:
            report = auto_cleanup(
                client,
                include_active=cfg.run.auto_cleanup_all,
                temp_max_age_hours=cfg.run.auto_cleanup_temp_hours,
                log=self.log,
            )
        except Exception as exc:  # noqa: BLE001 - 清理只是顺手维护
            self.log(f"⚠ 自动清理失败：{exc}")
            return
        if report.cleaned or report.temp_dirs:
            self.log(f"🧹 启动清理：{report.summary()}")

    def on_login(self) -> None:
        cfg = self._cfg_snapshot()  # 必须在主线程读 Tk 变量

        def work() -> None:
            client = self._ensure_client(cfg)
            session = client.login(cfg.platform.username, cfg.platform.password)
            save_client_config(cfg, log=self.log)
            self.queue.put(("conn", (True, f"已登录：{session.name or session.code}")))
            self.log(f"登录成功：{session.name}（{session.code}）")
            self.queue.put(("status", "已登录"))
            self._auto_cleanup(cfg, client)

        self._run_worker(work)

    def on_refresh_tags(self) -> None:
        cfg = self._cfg_snapshot()

        def work() -> None:
            client = self._ensure_client(cfg)
            if not client.session:
                client.login(cfg.platform.username, cfg.platform.password)
                self.queue.put(("conn", (True, "已登录")))
            tags = client.tags(cfg.run.qbase_type, cfg.run.level)
            available = [t for t in tags if t.get("available") is not False]
            names = ["随机"] + [f"{t['content']}（id={t['id']}）" for t in available]
            mapping = {"随机": -1}
            for tag in available:
                mapping[f"{tag['content']}（id={tag['id']}）"] = int(tag["id"])
            self.tag_map = mapping
            self.queue.put(("tags", names))
            self.log(f"{QBASE_NAMES.get(cfg.run.qbase_type)}：共 {len(tags)} 个标签，可用 {len(available)} 个")

        self._run_worker(work)

    def _runner(self, cfg: Config, solver, human=None) -> Runner:
        client = self._ensure_client(cfg)
        runner = Runner(client, solver, cfg, log=self.log, confirm=self._confirm_from_worker,
                        should_stop=self.stop_event.is_set, human=human)
        runner.on_record = self._show_record
        runner.on_answer = self._show_answer
        return runner

    # ------------------------------------------------------ 模拟人类输入

    def _open_human(self, cfg: Config):
        """开了「模拟真人打字」就准备浏览器；返回 HumanBrowser，否则 None。

        浏览器由 human.ensure_chrome() 负责：CDP 端口没监听时自动拉起一个
        独立配置目录的 Chrome，不会动用户正在用的浏览器。
        """
        if not cfg.run.human_input:
            return None
        from .human import HumanBrowser
        from .humantype import TypingProfile, describe_human_pacing

        profile = TypingProfile(
            cpm=cfg.run.typing_cpm,
            typo_rate=cfg.run.typo_rate,
            cpm_min=float(cfg.run.typing_cpm_min or 0.0),
            cpm_max=float(cfg.run.typing_cpm_max or 0.0),
            backspace_rate=float(cfg.run.backspace_rate or 0.0),
            backspace_max=int(cfg.run.backspace_max or 1),
        )
        browser = HumanBrowser(
            cfg.platform.base_url,
            cfg.platform.username,
            cfg.platform.password,
            cdp_url=cfg.run.cdp_url,
            profile=profile,
            log=self.log,
        )
        self.log(f"🧑‍💻 模拟人类输入：准备 Chrome（CDP {cfg.run.cdp_url}）…")
        try:
            browser.start()
            browser.login()
        except Exception:
            self._close_human(browser)
            raise
        lo, hi = profile.speed_interval()
        pacing = describe_human_pacing(cfg.run)
        self.log(
            f"✓ 浏览器就绪：基准 {int(cfg.run.typing_cpm)} 字符/分、"
            f"每次作答在 {lo:.0f}–{hi:.0f} 之间随机逐字符打字"
            + (f"；{pacing}" if pacing else "")
            + ("（dry-run：只打字不提交）" if cfg.run.human_dry_run else "")
        )
        return browser

    def _close_human(self, browser) -> None:
        if browser is None:
            return
        try:
            browser.close()
            self.log("浏览器连接已断开（Chrome 窗口保持打开）")
        except Exception as exc:  # noqa: BLE001 - 关连接失败不该影响结果
            self.log(f"⚠ 断开浏览器连接失败：{exc}")

    def _show_record(self, record: dict) -> None:
        questions = (record.get("paper") or {}).get("questions") or []
        chunks = []
        for question in questions:
            options = question.get("options") or []
            chunks.append(
                f"Q{question.get('id')}  qType={question.get('qType')}  分值={question.get('point')}  "
                f"语言={question.get('language')}\n\n{question.get('content')}\n"
                + "\n".join(f"  {chr(ord('A') + i)}. {o.get('content') or o}" for i, o in enumerate(options))
            )
        self.queue.put(("statement", "\n\n" + ("\n\n" + "-" * 60 + "\n\n").join(chunks)))

    def _show_answer(self, question: dict, outcome) -> None:
        self.current_qid = question.get("id")
        self.queue.put(("code", outcome.code_or_answer))
        if outcome.judge_passed is not None:
            self.log(f"Q{question.get('id')} 自检：{'通过' if outcome.judge_passed else '未通过'}")

    def on_auto(self) -> None:
        self._start_auto(False)

    def on_auto_loop(self) -> None:
        self._start_auto(True)

    def _start_auto(self, loop: bool) -> None:
        self.auto_mode = loop
        cfg = self._cfg_snapshot()
        if not cfg.deepseek.api_key and cfg.run.solver != "stub":
            if not messagebox.askyesno(
                "缺少 API Key",
                "还没有配置 DeepSeek API Key。\n\n要去设置里填吗？（点“否”将退出本次刷题）",
                parent=self,
            ):
                return
            self.on_settings()
            return
        if loop and not messagebox.askyesno(
            "全自动模式",
            "会连续刷题：完成一道自动抽下一道，直到题库抽空、本周限额、连续失败或点「停止」。\n\n继续吗？",
            parent=self,
        ):
            self.auto_mode = False
            return

        def work() -> None:
            solve_cfg = cfg
            client = self._ensure_client(solve_cfg)
            if not client.session and solve_cfg.platform.username and solve_cfg.platform.password:
                try:
                    client.login(solve_cfg.platform.username, solve_cfg.platform.password)
                except Exception as exc:  # noqa: BLE001 - 交给主流程再报错
                    self.log(f"⚠ 预登录失败：{exc}")
            if client.session:
                self._auto_cleanup(solve_cfg, client)
            solver = build_solver(solve_cfg, log=self.log)
            human = self._open_human(solve_cfg)
            try:
                runner = self._runner(solve_cfg, solver, human)
                self.log(f"Solver = {solver.name}；编译器 = {solve_cfg.run.gcc or find_compiler() or '未找到'}")
                if loop:
                    self.log("🤖 全自动模式已启动（点「停止」会在当前题做完后停）")
                outcomes = runner.run()
            finally:
                self._close_human(human)
            lines = []
            for outcome in outcomes:
                lines.append(
                    f"{outcome.session_id or '-'}  {outcome.state_name}  "
                    f"{outcome.score:g}/{outcome.full_score:g}\n{outcome.detail}"
                )
            full = sum(1 for o in outcomes if o.full_score and o.score >= o.full_score)
            lines.append(f"\n共完成 {len(outcomes)} 题，满分 {full} 题")
            self.queue.put(("score", "\n\n".join(lines) or "没有产生任何会话"))
            ok = outcomes and all(o.ok for o in outcomes)
            self.queue.put(("status", f"完成 {len(outcomes)} 题（满分 {full}）" if outcomes else "没有产生会话"))

        self.queue.put(("score", ""))
        self._run_worker(work)

    def on_peek(self) -> None:
        cfg = self._cfg_snapshot()

        def work() -> None:
            runner = Runner(self._ensure_client(cfg), EditorSolver(""), cfg, log=self.log)
            session_id, record = runner.peek()
            self.queue.put(("session", session_id))
            self._show_record(record)
            self.queue.put(("code", "// 在这里写你的 C 代码，然后点左侧「提交我的代码」\n"))
            self.queue.put(("status", f"会话 {session_id[:8]}… 等待作答"))

        self._run_worker(work)

    def on_selfcheck(self) -> None:
        code = self.txt_code.get("1.0", "end").strip()
        if not code:
            messagebox.showinfo("提示", "代码页是空的。", parent=self)
            return
        cfg = self._cfg_snapshot()
        session_id = self.current_session

        def work() -> None:
            from . import judge as judge_mod

            if not session_id:
                self.log("还没有会话，先用「抽新题」开一个。")
                return
            client = self._ensure_client(cfg)
            record = client.paper(session_id)
            question = (record.get("paper") or {}).get("questions", [{}])[0]
            if cfg.run.judge in ("local", "both"):
                result = judge_mod.judge_program(question, code, compiler=cfg.run.gcc or None)
            else:
                result = judge_mod.JudgeResult(passed=True, verified=False,
                                               checks=[("判题", True, "已按设置跳过本地判题")])
            self.log("自检结果：" + ("通过" if result.passed else "未通过"))
            for line in result.feedback.splitlines():
                self.log("  " + line)

        self._run_worker(work)

    def on_submit_mine(self) -> None:
        code = self.txt_code.get("1.0", "end")
        if not code.strip():
            messagebox.showinfo("提示", "代码页是空的。", parent=self)
            return
        session_id = self.current_session
        if not session_id:
            messagebox.showinfo("提示", "还没有会话。请先点「抽新题」开一个会话。", parent=self)
            return
        if not messagebox.askyesno("确认提交", "把代码页里的内容作为答案提交到平台？", parent=self):
            return
        cfg = self._cfg_snapshot()

        def work() -> None:
            human = self._open_human(cfg)
            try:
                runner = self._runner(cfg, EditorSolver(code), human)
                outcome = runner.execute(session_id, 1, 1)
            finally:
                self._close_human(human)
            self.queue.put((
                "score",
                f"{outcome.session_id}  {outcome.state_name}  "
                f"{outcome.score:g}/{outcome.full_score:g}\n{outcome.detail}",
            ))
            self.queue.put(("status", "提交完成" if outcome.ok else "提交结束（见评分页）"))

        self._run_worker(work)

    def on_stop(self) -> None:
        """点停止：立刻显示「已请求停止」，真正的「已停止」等 worker 退出后再落。

        分两个状态是因为 runner 可能还在限流等待里（实测有一次睡了 59 秒才退出），
        只挂一句「正在停止…」用户看不出到底停没停：按钮点了没反应，谁敢再点。
        """
        if not (self.worker and self.worker.is_alive()):
            self.log("当前没有正在运行的任务。")
            self.set_status("就绪")   # 与窗口初始状态用同一个词，别再造一个新状态名
            return
        self.stop_event.set()
        self.log("⏹ 已请求停止：当前这一轮结束后不再继续，也不会再抽新题。")
        self.set_status("已请求停止…")

    def on_settings(self) -> None:
        SettingsDialog(self)


class ModelDialog(tk.Toplevel):
    """更换模型：直接改接口地址（base_url）、API Key 和模型名，改完即生效并保存。"""

    def __init__(self, app: "ClientApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("更换模型 / 接口")
        self.resizable(False, False)
        cfg = app.config_data
        # 用副本编辑，点「保存并应用」才写回配置，取消不影响现状
        self.items = [ModelEndpoint(**vars(item)) for item in cfg.deepseek.all_endpoints()]
        if not self.items:
            # 全新安装（公开版）：给一个空槽位，填完 Key/地址/模型名就能用
            self.items = [ModelEndpoint(api_key="", base_url=cfg.deepseek.base_url or "https://api.deepseek.com",
                                        model=cfg.deepseek.model or "deepseek-flash", label="主模型")]

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="选择要修改的端点，直接填 API Key / 接口地址 / 模型名：",
                  foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.var_index = tk.IntVar(value=cfg.deepseek.preferred if self.items else 0)
        self.cmb = ttk.Combobox(frame, state="readonly", width=62)
        self.cmb.grid(row=1, column=0, columnspan=2, sticky="w")
        self.cmb.bind("<<ComboboxSelected>>", lambda _e: self._load_fields())
        ttk.Button(frame, text="新增备用", command=self.on_add).grid(row=1, column=2, padx=(8, 0), sticky="w")

        ttk.Label(frame, text="API Key").grid(row=2, column=0, sticky="w", pady=(10, 2))
        self.var_key = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_key, width=64, show="*").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(10, 2))

        ttk.Label(frame, text="接口地址").grid(row=3, column=0, sticky="w", pady=2)
        self.var_url = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_url, width=64).grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Label(frame, text="例：https://api.deepseek.com 或 http://127.0.0.1:7864/v1",
                  foreground="#777").grid(row=4, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="模型名").grid(row=5, column=0, sticky="w", pady=2)
        self.var_model = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_model, width=64).grid(row=5, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Label(frame, text="官方默认 deepseek-flash（也可写 deepseek-v4-pro）；自建网关填它自己的模型 id",
                  foreground="#777").grid(row=6, column=1, columnspan=2, sticky="w")

        self.lbl_result = ttk.Label(frame, text="", foreground="#555", wraplength=560)
        self.lbl_result.grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="测试当前填写", command=self.on_test).pack(side="left")
        ttk.Button(buttons, text="删除此备用", command=self.on_delete).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="保存并应用", command=self.on_apply).pack(side="right", padx=6)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")

        self._result: tuple[bool, str] | None = None
        self._refresh_list()
        self.transient(app)
        self.grab_set()

    # ---------------------------------------------------------------- 列表

    def _role(self, index: int) -> str:
        return "主模型" if index == 0 else f"备用{index}"

    def _refresh_list(self) -> None:
        names = []
        for index, item in enumerate(self.items):
            key = item.api_key
            masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"
            names.append(f"[{self._role(index)}] {item.model or '(未填模型)'} @ {item.base_url or '(未填地址)'} key={masked}")
        self.cmb.configure(values=names)
        index = self.var_index.get()
        if not (0 <= index < len(self.items)):
            index = 0
            self.var_index.set(0)
        if names:
            self.cmb.current(index)
        self._load_fields()

    def _load_fields(self) -> None:
        index = self.var_index.get()
        index = index if 0 <= index < len(self.items) else 0
        self.var_index.set(index)
        if self.cmb.current() != index:
            self.cmb.current(index)
        if not self.items:
            return
        item = self.items[index]
        self.var_key.set(item.api_key)
        self.var_url.set(item.base_url)
        self.var_model.set(item.model)

    def _collect_fields(self) -> ModelEndpoint | None:
        index = self.var_index.get()
        if not (0 <= index < len(self.items)):
            return None
        item = self.items[index]
        item.api_key = self.var_key.get().strip()
        item.base_url = self.var_url.get().strip()
        item.model = self.var_model.get().strip()
        if index > 0:
            item.label = item.label or f"备用{index}"
        return item

    # ---------------------------------------------------------------- 动作

    def on_add(self) -> None:
        self._collect_fields()
        self.items.append(ModelEndpoint(api_key="", base_url="https://api.deepseek.com",
                                        model="deepseek-flash", label=f"备用{len(self.items)}"))
        self.var_index.set(len(self.items) - 1)
        self._refresh_list()

    def on_delete(self) -> None:
        index = self.var_index.get()
        if index == 0:
            messagebox.showinfo("提示", "主模型不能删除；可以改它的地址和 Key，或把别的端点选为主模型。", parent=self)
            return
        if 0 <= index < len(self.items):
            self.items.pop(index)
            self.var_index.set(max(0, index - 1))
            self._refresh_list()

    def on_test(self) -> None:
        endpoint = self._collect_fields()
        if endpoint is None:
            return
        if not (endpoint.api_key and endpoint.base_url and endpoint.model):
            self.lbl_result.configure(text="Key / 地址 / 模型名 三项都要填", foreground="#b3261e")
            return
        self.lbl_result.configure(text=f"测试 {endpoint.model} …", foreground="#555")
        self._result = None

        def work() -> None:
            from .solver.deepseek import DeepSeekClient

            try:
                client = DeepSeekClient(
                    endpoint.api_key, base_url=endpoint.base_url, model=endpoint.model,
                    max_tokens=64, timeout=90, retries=0, log=lambda *_: None,
                )
                reply = client.chat([{"role": "user", "content": "只回复两个字：可用"}], temperature=0)
                self._result = (True, f"✓ 可用，回复：{reply.strip()[:20]}")
            except Exception as exc:  # noqa: BLE001
                self._result = (False, f"✗ {str(exc)[:160]}")

        threading.Thread(target=work, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        if self._result is None:
            self._schedule(250, self._poll)
            return
        ok, message = self._result
        self.lbl_result.configure(text=message, foreground="#1a7f37" if ok else "#b3261e")

    def on_apply(self) -> None:
        endpoint = self._collect_fields()
        if endpoint is None:
            self.destroy()
            return
        if not (endpoint.api_key and endpoint.base_url and endpoint.model):
            self.lbl_result.configure(text="Key / 地址 / 模型名 三项都要填", foreground="#b3261e")
            return

        cfg = self.app.config_data
        valid = [item for item in self.items if item.api_key and item.base_url and item.model]
        if not valid:
            self.lbl_result.configure(text="至少要有一个填完整的端点", foreground="#b3261e")
            return

        # 第 1 个写回 [deepseek] 主模型，其余写回 [[deepseek.fallback]]
        primary, *rest = valid
        cfg.deepseek.api_key = primary.api_key
        cfg.deepseek.base_url = primary.base_url
        cfg.deepseek.model = primary.model
        cfg.deepseek.fallbacks = rest

        # 让「当前正在编辑的那个」成为首选
        index = self.var_index.get()
        chosen = min(index, len(valid) - 1)
        cfg.deepseek.preferred = chosen
        save_client_config(cfg, log=self.app.log)
        self.app._refresh_model_label()
        self.app.log(f"已更换模型：{cfg.deepseek.current().model} @ {cfg.deepseek.current().base_url}"
                     f"（共 {len(valid)} 个端点，首个为主）")
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: ClientApp) -> None:
        super().__init__(app)
        self.app = app
        self.title("设置")
        self.resizable(False, False)
        cfg = app.config_data

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="DeepSeek API Key").grid(row=0, column=0, sticky="w", pady=4)
        self.var_key = tk.StringVar(value=cfg.deepseek.api_key)
        ttk.Entry(frame, textvariable=self.var_key, width=46, show="*").grid(row=0, column=1, pady=4)

        ttk.Label(frame, text="网关地址").grid(row=1, column=0, sticky="w", pady=4)
        self.var_url = tk.StringVar(value=cfg.deepseek.base_url)
        ttk.Entry(frame, textvariable=self.var_url, width=46).grid(row=1, column=1, pady=4)

        ttk.Label(frame, text="模型").grid(row=2, column=0, sticky="w", pady=4)
        self.var_model = tk.StringVar(value=cfg.deepseek.model)
        ttk.Combobox(frame, textvariable=self.var_model, values=MODELS, width=44).grid(row=2, column=1, pady=4)

        ttk.Label(frame, text="修复轮数 / 重答次数").grid(row=3, column=0, sticky="w", pady=4)
        rounds = ttk.Frame(frame)
        rounds.grid(row=3, column=1, sticky="w", pady=4)
        self.var_repairs = tk.IntVar(value=cfg.run.max_repairs)
        ttk.Spinbox(rounds, from_=0, to=8, textvariable=self.var_repairs, width=5).pack(side="left")
        ttk.Label(rounds, text="  提交后重答上限").pack(side="left")
        self.var_reanswers = tk.IntVar(value=cfg.run.max_reanswers)
        ttk.Spinbox(rounds, from_=0, to=10, textvariable=self.var_reanswers, width=5).pack(side="left")

        self.var_verified = tk.BooleanVar(value=cfg.run.require_verified)
        ttk.Checkbutton(
            frame,
            text="严格模式：连「样例还原不出、无法校验」的题也不提交",
            variable=self.var_verified,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=2)

        self.var_allow_unverified = tk.BooleanVar(value=cfg.run.allow_unverified_submit)
        ttk.Checkbutton(
            frame,
            text="自检未通过也提交（--force，不推荐：白丢一次作答机会）",
            variable=self.var_allow_unverified,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)

        self.var_keepgoing = tk.BooleanVar(value=not cfg.run.stop_on_failure)
        ttk.Checkbutton(
            frame,
            text="遇到未满分也继续（--keep-going；关掉则做完当前题就停）",
            variable=self.var_keepgoing,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)

        clean_frame = ttk.LabelFrame(frame, text="启动时自动清理残留")
        clean_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        self.var_auto_clean = tk.BooleanVar(value=cfg.run.auto_cleanup)
        ttk.Checkbutton(clean_frame, text="清理过期的「未完成」会话（不动正在做的题）",
                        variable=self.var_auto_clean).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))

        self.var_auto_clean_all = tk.BooleanVar(value=cfg.run.auto_cleanup_all)
        ttk.Checkbutton(clean_frame, text="连未过期的「未完成」也清（慎用：会删掉正在做的题）",
                        variable=self.var_auto_clean_all).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=2)

        temp_row = ttk.Frame(clean_frame)
        temp_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))
        ttk.Label(temp_row, text="临时目录超龄").pack(side="left")
        self.var_auto_temp_hours = tk.DoubleVar(value=cfg.run.auto_cleanup_temp_hours)
        ttk.Spinbox(temp_row, from_=0, to=72, increment=0.5,
                    textvariable=self.var_auto_temp_hours, width=6).pack(side="left", padx=4)
        ttk.Label(temp_row, text="小时才删（%TEMP% 里的 _MEI* 解压残留）").pack(side="left")

        human_frame = ttk.LabelFrame(frame, text="模拟人类输入（浏览器逐字符打字）")
        human_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 4))

        self.var_human = tk.BooleanVar(value=cfg.run.human_input)
        ttk.Checkbutton(human_frame, text="开启（也可在主界面左侧勾选）",
                        variable=self.var_human).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))

        speed_row = ttk.Frame(human_frame)
        speed_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Label(speed_row, text="打字速度").pack(side="left")
        self.var_cpm = tk.DoubleVar(value=cfg.run.typing_cpm)
        ttk.Spinbox(speed_row, from_=60, to=800, increment=20,
                    textvariable=self.var_cpm, width=6).pack(side="left", padx=4)
        ttk.Label(speed_row, text="字符/分    错字率").pack(side="left", padx=(8, 0))
        self.var_typo = tk.DoubleVar(value=cfg.run.typo_rate)
        ttk.Spinbox(speed_row, from_=0, to=0.2, increment=0.01,
                    textvariable=self.var_typo, width=6).pack(side="left", padx=4)

        range_row = ttk.Frame(human_frame)
        range_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Label(range_row, text="速度随机区间").pack(side="left")
        self.var_cpm_min = tk.DoubleVar(value=cfg.run.typing_cpm_min)
        ttk.Spinbox(range_row, from_=0, to=1000, increment=20,
                    textvariable=self.var_cpm_min, width=6).pack(side="left", padx=4)
        ttk.Label(range_row, text="–").pack(side="left")
        self.var_cpm_max = tk.DoubleVar(value=cfg.run.typing_cpm_max)
        ttk.Spinbox(range_row, from_=0, to=1000, increment=20,
                    textvariable=self.var_cpm_max, width=6).pack(side="left", padx=4)
        ttk.Label(range_row, text="（0 = 自动：基准 ±25%）", foreground="#777").pack(
            side="left", padx=(6, 0))

        ttk.Label(human_frame, text="浏览器调试端口").grid(row=3, column=0, sticky="w", padx=8, pady=2)
        self.var_cdp = tk.StringVar(value=cfg.run.cdp_url)
        ttk.Entry(human_frame, textvariable=self.var_cdp, width=34).grid(
            row=3, column=1, sticky="w", padx=8, pady=2)

        self.var_human_dry = tk.BooleanVar(value=cfg.run.human_dry_run)
        ttk.Checkbutton(human_frame, text="只打字、不提交（dry-run，用来校验输入是否一致）",
                        variable=self.var_human_dry).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))

        delay_row = ttk.Frame(human_frame)
        delay_row.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
        ttk.Label(delay_row, text="每题随机思考").pack(side="left")
        self.var_delay_min = tk.DoubleVar(value=cfg.run.human_delay_min)
        ttk.Spinbox(delay_row, from_=0, to=60, increment=1,
                    textvariable=self.var_delay_min, width=6).pack(side="left", padx=4)
        ttk.Label(delay_row, text="–").pack(side="left")
        self.var_delay_max = tk.DoubleVar(value=cfg.run.human_delay_max)
        ttk.Spinbox(delay_row, from_=0, to=60, increment=1,
                    textvariable=self.var_delay_max, width=6).pack(side="left", padx=4)
        ttk.Label(delay_row, text="分钟（0 = 不等待；每题在区间内随机，可被「停止」打断）",
                  foreground="#777").pack(side="left", padx=(6, 0))

        bs_row = ttk.Frame(human_frame)
        bs_row.grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
        ttk.Label(bs_row, text="随机退格").pack(side="left")
        self.var_bs_rate = tk.DoubleVar(value=cfg.run.backspace_rate)
        ttk.Spinbox(bs_row, from_=0, to=0.3, increment=0.005,
                    textvariable=self.var_bs_rate, width=6).pack(side="left", padx=4)
        ttk.Label(bs_row, text="概率，每次最多删").pack(side="left", padx=(4, 0))
        self.var_bs_max = tk.IntVar(value=cfg.run.backspace_max)
        ttk.Spinbox(bs_row, from_=1, to=5, increment=1,
                    textvariable=self.var_bs_max, width=4).pack(side="left", padx=4)
        ttk.Label(bs_row, text="字符再补回（0 = 不退格）", foreground="#777").pack(
            side="left", padx=(4, 0))

        ttk.Label(frame, text="题目类型过滤").grid(row=9, column=0, sticky="w", pady=4)
        preset = next(
            (name for name, values in QTYPE_PRESETS if list(cfg.run.qtypes) == values),
            QTYPE_PRESETS[-1][0],
        )
        self.var_qtypes = tk.StringVar(value=preset)
        ttk.Combobox(frame, textvariable=self.var_qtypes, values=[n for n, _ in QTYPE_PRESETS],
                     state="readonly", width=30).grid(row=9, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="产物目录").grid(row=10, column=0, sticky="w", pady=4)
        self.var_out = tk.StringVar(value=cfg.run.out_dir)
        ttk.Entry(frame, textvariable=self.var_out, width=46).grid(row=10, column=1, pady=4)

        ttk.Label(frame, text="gcc 路径（留空自动找）").grid(row=11, column=0, sticky="w", pady=4)
        self.var_gcc = tk.StringVar(value=cfg.run.gcc)
        ttk.Entry(frame, textvariable=self.var_gcc, width=46).grid(row=11, column=1, pady=4)

        ttk.Label(frame, text=f"配置将保存到：{settings_path()}", foreground="#555",
                  wraplength=420).grid(row=12, column=0, columnspan=2, pady=(8, 4), sticky="w")

        test_row = ttk.Frame(frame)
        test_row.grid(row=13, column=0, columnspan=2, pady=(4, 0), sticky="w")
        ttk.Button(test_row, text="测试连接", command=self.on_test).pack(side="left")
        self.lbl_test = ttk.Label(test_row, text="", foreground="#555", wraplength=330)
        self.lbl_test.pack(side="left", padx=10)

        buttons = ttk.Frame(frame)
        buttons.grid(row=14, column=0, columnspan=2, pady=(10, 0), sticky="e")
        ttk.Button(buttons, text="保存", command=self.on_save).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left")

        self._test_result: tuple[bool, str] | None = None
        self.transient(app)
        self.grab_set()

    def on_test(self) -> None:
        """用当前对话框里的 Key/网关/模型发一次最小请求，验证连通性。"""
        from .solver.deepseek import DeepSeekClient

        key = self.var_key.get().strip()
        url = self.var_url.get().strip()
        model = self.var_model.get().strip()
        if not key:
            self.lbl_test.configure(text="请先填 API Key", foreground="#b3261e")
            return
        self.lbl_test.configure(text="测试中…", foreground="#555")
        self._test_result = None

        def work() -> None:
            try:
                client = DeepSeekClient(key, base_url=url, model=model, max_tokens=32,
                                        timeout=60, retries=0, log=lambda *_: None)
                text = client.chat([{"role": "user", "content": "只回复两个字：可用"}], temperature=0)
                self._test_result = (True, f"✅ 连通（{model}）：{text.strip()[:30]}")
            except Exception as exc:  # noqa: BLE001 - 只要结果
                self._test_result = (False, f"✗ {type(exc).__name__}: {str(exc)[:110]}")

        threading.Thread(target=work, daemon=True).start()
        self._poll_test()

    def _poll_test(self) -> None:
        if self._test_result is None:
            self._schedule(200, self._poll_test)
            return
        ok, message = self._test_result
        self.lbl_test.configure(text=message, foreground="#1a7f37" if ok else "#b3261e")

    def on_save(self) -> None:
        cfg = self.app.config_data
        cfg.deepseek.api_key = self.var_key.get().strip()
        cfg.deepseek.base_url = self.var_url.get().strip() or cfg.deepseek.base_url
        cfg.deepseek.model = self.var_model.get().strip() or cfg.deepseek.model
        cfg.run.max_repairs = max(0, int(self.var_repairs.get() or 0))
        cfg.run.max_reanswers = max(0, int(self.var_reanswers.get() or 0))
        cfg.run.require_verified = bool(self.var_verified.get())
        cfg.run.qtypes = list(dict(QTYPE_PRESETS).get(self.var_qtypes.get(), []))
        cfg.run.out_dir = self.var_out.get().strip() or "out"
        cfg.run.gcc = self.var_gcc.get().strip()
        cfg.run.allow_unverified_submit = bool(self.var_allow_unverified.get())
        cfg.run.stop_on_failure = not bool(self.var_keepgoing.get())
        cfg.run.human_input = bool(self.var_human.get())
        cfg.run.human_dry_run = bool(self.var_human_dry.get())
        cfg.run.typing_cpm = min(800.0, max(60.0, float(self.var_cpm.get() or 220)))
        cpm_min = min(1000.0, max(0.0, float(self.var_cpm_min.get() or 0)))
        cpm_max = min(1000.0, max(0.0, float(self.var_cpm_max.get() or 0)))
        if cpm_min > 0 and cpm_max > 0 and cpm_min > cpm_max:
            cpm_min, cpm_max = cpm_max, cpm_min
        cfg.run.typing_cpm_min = cpm_min
        cfg.run.typing_cpm_max = cpm_max
        cfg.run.typo_rate = min(0.3, max(0.0, float(self.var_typo.get() or 0)))
        delay_min = min(60.0, max(0.0, float(self.var_delay_min.get() or 0)))
        delay_max = min(60.0, max(0.0, float(self.var_delay_max.get() or 0)))
        if delay_min > delay_max:
            delay_min, delay_max = delay_max, delay_min
        cfg.run.human_delay_min = delay_min
        cfg.run.human_delay_max = delay_max
        cfg.run.backspace_rate = min(0.3, max(0.0, float(self.var_bs_rate.get() or 0)))
        cfg.run.backspace_max = min(5, max(1, int(self.var_bs_max.get() or 1)))
        cfg.run.cdp_url = self.var_cdp.get().strip() or cfg.run.cdp_url
        cfg.run.auto_cleanup = bool(self.var_auto_clean.get())
        cfg.run.auto_cleanup_all = bool(self.var_auto_clean_all.get())
        cfg.run.auto_cleanup_temp_hours = min(72.0, max(0.0, float(self.var_auto_temp_hours.get() or 0)))
        save_client_config(cfg, log=self.app.log)
        self.app.var_human.set(cfg.run.human_input)
        self.app._on_human_toggle()
        self.app.log(f"设置已保存：{settings_path()}")
        self.destroy()


def main() -> int:
    app = ClientApp()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
