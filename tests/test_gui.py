"""客户端（tkinter）离线性状测试：构造界面、配置读写、控件映射。

没有图形环境时自动跳过。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk

from trainsolve import gui


# 整个模块共用的配置夹具：**绝不让单测读、更不让它写本机真实配置**。
#
# 2026-11-06 的教训：GUI 改成走统一解析器后，构造 ClientApp 会解析到开发机真实的
# `%APPDATA%\TrainSolve\config.toml`，于是
#   1) auto_cleanup 两条用例随那台机器的配置变红（auto_cleanup=False）；
#   2) 更要紧的是，设置对话框那几条用例走 save 路径，会把测试值**写进真配置**。
# 这里在模块级把解析缝钉到临时目录，所有用例都被覆盖。
_FIXTURE_CONFIG = """\
[platform]
base_url = "http://127.0.0.1/train/"
username = "test-user"
password = "test-pass"

[deepseek]
api_key = "test-key"
base_url = "http://127.0.0.1:7864/v1"
model = "test-model"

[run]
count = 1
qbase_type = 4
level = 2
judge = "local"
submit = false
dry_run = true
auto_cleanup = true
"""

_FIXTURE_DIR: Path | None = None
_FIXTURE_PATCHERS: list = []
_ORIGINALS: dict = {}


def setUpModule():
    """把配置解析钉到临时夹具：解析出来的路径与保存目标都在这里。"""
    global _FIXTURE_DIR
    # 先留一份真函数：有两条用例要验「查询不产生副作用」，必须调用真的 settings_path
    _ORIGINALS["settings_path"] = gui.settings_path
    _ORIGINALS["config_resolution"] = gui.config_resolution
    _FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="trainsolve-testcfg-"))
    fixture = _FIXTURE_DIR / "config.toml"
    fixture.write_text(_FIXTURE_CONFIG, encoding="utf-8")
    resolved = gui.ResolvedConfig(path=fixture, source="test")
    for target, value in (
        ("config_resolution", resolved),
        ("settings_path", fixture),
    ):
        patcher = mock.patch.object(gui, target, return_value=value)
        patcher.start()
        _FIXTURE_PATCHERS.append(patcher)


def tearDownModule():
    for patcher in _FIXTURE_PATCHERS:
        patcher.stop()
    _FIXTURE_PATCHERS.clear()
    if _FIXTURE_DIR is not None and _FIXTURE_DIR.exists():
        shutil.rmtree(_FIXTURE_DIR, ignore_errors=True)


def _tk_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001 - 无显示环境
        return False
    root.destroy()
    return True


class SettingsPathSideEffectTest(unittest.TestCase):
    """`settings_path()` 只回答路径，不建目录（0.5.4）。

    回归自 2026-11-06：查询函数里顺手 mkdir，于是任何未隔离 `APPDATA` 的调用都会
    在真实 `%APPDATA%\\TrainSolve` 下建出目录——「问一句路径」不该有写副作用。
    """

    @staticmethod
    def _resolved_under(tmp: str) -> gui.ResolvedConfig:
        return gui.ResolvedConfig(path=Path(tmp) / "TrainSolve" / "config.toml", source="appdata")

    def test_settings_path_does_not_create_directory(self):
        """出处（判定条件）：空目录下问路径，磁盘上不该多出任何东西。"""
        with tempfile.TemporaryDirectory() as tmp:
            resolved = self._resolved_under(tmp)
            with mock.patch.object(gui, "settings_path", _ORIGINALS["settings_path"]), \
                    mock.patch.object(gui, "config_resolution", return_value=resolved):
                got = gui.settings_path()
            self.assertEqual(got, resolved.path)
            self.assertFalse(resolved.path.parent.exists(), "查询配置路径不该在磁盘上建目录")

    def test_save_client_config_creates_directory(self):
        """反向：真正保存时目录仍必须建出来，别修成“存不进去”。"""
        with tempfile.TemporaryDirectory() as tmp:
            resolved = self._resolved_under(tmp)
            with mock.patch.object(gui, "settings_path", _ORIGINALS["settings_path"]), \
                    mock.patch.object(gui, "config_resolution", return_value=resolved):
                gui.save_client_config(gui.Config())
            self.assertTrue(resolved.path.parent.exists(), "保存后目录应已创建")
            self.assertTrue(resolved.path.exists(), "保存后配置文件应已创建")
            self.assertIn("[platform]", resolved.path.read_text(encoding="utf-8"))


@unittest.skipUnless(_tk_available(), "没有可用的图形环境")
class ClientAppTest(unittest.TestCase):
    def setUp(self):
        self.app = gui.ClientApp()
        self.app.withdraw()

    def tearDown(self):
        self.app.stop_event.set()
        self.app.destroy()

    def test_config_resolution_is_pinned_to_fixture(self):
        """护栏：单测的配置解析必须停在临时夹具，不许落到本机 %APPDATA%。

        删掉 setUpModule 的那对 patch，这条立刻红——比「跑完发现用户配置被改了」早一步。
        """
        self.assertEqual(gui.config_resolution().source, "test")
        self.assertEqual(gui.settings_path(), _FIXTURE_DIR / "config.toml")

    def test_ui_error_log_creates_directory(self):
        """界面异常日志也要落得下去：目录不存在时由写入方建出来。

        跟随 settings_path() 去掉副作用一起改的：如果没人补这步，首次运行的
        红色 traceback 会因为「目录不存在」被静默吞掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "TrainSolve" / "client-errors.log"
            with mock.patch.object(gui, "error_log_path", return_value=target):
                self.app._log_ui_error("unit-test")
            self.assertTrue(target.exists(), "异常日志文件应被创建")
            self.assertIn("kind=unit-test", target.read_text(encoding="utf-8"))

    def test_window_and_tabs(self):
        self.assertIn("自动刷题客户端", self.app.title())
        tabs = [self.app.nb.tab(i, "text") for i in range(self.app.nb.index("end"))]
        self.assertEqual(tabs, ["日志", "题目", "代码", "评分"])

    def test_code_tab_has_horizontal_scrollbar(self):
        """代码页 wrap="none"，必须配横向滚动条，否则长行行尾看不到。"""
        hbar = getattr(self.app, "_hbar_code", None)
        self.assertIsNotNone(hbar, "代码页没有横向滚动条")
        self.assertEqual(str(hbar.cget("orient")), "horizontal")
        self.assertTrue(self.app.txt_code.cget("xscrollcommand"), "横向滚动条没接上 Text")
        self.assertEqual(str(self.app.txt_code.cget("wrap")), "none")
        # 其他页是折行的，不该有横向条
        self.assertEqual(str(self.app.txt_log.cget("wrap")), "word")

    def test_log_is_capped(self):
        """日志超过上限就裁掉最旧的：控件行数与内存里都不许无限增长。"""
        with mock.patch.object(gui, "_MAX_LOG_LINES", 5):
            for i in range(12):
                self.app._push_log(f"line-{i}")
        self.assertEqual(len(self.app.log_lines), 5)
        self.assertEqual(self.app.log_lines, [f"line-{i}" for i in range(7, 12)])
        shown = self.app.txt_log.get("1.0", "end-1c").splitlines()
        self.assertEqual(shown, [f"line-{i}" for i in range(7, 12)])

    def test_clear_log(self):
        """清空日志：内存列表和控件一起空掉，并留一行提示。"""
        for i in range(3):
            self.app._push_log(f"旧日志-{i}")
        self.app.on_clear_log()
        self.app._drain_queue()  # 把「日志已清空」刷进去
        self.assertEqual(self.app.log_lines, ["日志已清空"])
        self.assertNotIn("旧日志", self.app.txt_log.get("1.0", "end"))

    def test_ui_scale_from_dpi(self):
        """缩放比由实际 DPI 推出：96→1.0，144→1.5，且不低于 1.0。"""
        with mock.patch.object(self.app, "winfo_fpixels", return_value=96.0):
            self.assertEqual(self.app._detect_scale(), 1.0)
        with mock.patch.object(self.app, "winfo_fpixels", return_value=144.0):
            self.assertEqual(self.app._detect_scale(), 1.5)
        with mock.patch.object(self.app, "winfo_fpixels", return_value=72.0):
            self.assertEqual(self.app._detect_scale(), 1.0)
        self.assertGreaterEqual(self.app.ui_scale, 1.0)

    def test_hint_wraplength_scales_with_dpi(self):
        """wraplength 是像素量：不跟着 DPI 放大，150% 下换行点会跑到一半。"""
        self.assertEqual(int(float(self.app.lbl_human.cget("wraplength"))),
                         self.app._pad(236))

    def test_padding_scales_with_dpi(self):
        """间距按缩放比放大，且 100% 下必须原样（8px 还是 8px）。"""
        app = self.app
        original = app.ui_scale
        try:
            app.ui_scale = 1.0
            self.assertEqual(app._pad(8), 8)
            self.assertEqual(app._pad(0), 0)
            app.ui_scale = 1.25
            self.assertEqual(app._pad(8), 10)
            app.ui_scale = 1.5
            self.assertEqual(app._pad(8), 12)
            self.assertEqual(app._pad(10), 15)
            # 4.5 要进位到 5：round() 会给出 4（银行家舍入），间距会有一档不齐
            self.assertEqual(app._pad(3), 5)
        finally:
            app.ui_scale = original

    def test_min_size_fits_all_children(self):
        """最小尺寸必须装得下全部控件，否则底部状态栏会被切掉（150% 实测踩过）。"""
        app = self.app
        app.update_idletasks()
        min_w, min_h = app.minsize()
        self.assertGreaterEqual(min_w, app.winfo_reqwidth(), "最小宽度小于控件需要")
        self.assertGreaterEqual(min_h, app.winfo_reqheight(), "最小高度小于控件需要")

    def test_stop_shows_requested_then_stopped(self):
        """点停止要立刻有反馈，且「已停止」只在 worker 真退出后出现。

        2026-11-05 实测：限流等待 59 秒期间状态栏一直挂「正在停止…」，
        用户看不出到底停没停，而那一轮还真的又抽了一次题。
        """

        class _Alive:
            def is_alive(self):
                return True

        self.app.worker = _Alive()
        self.app.on_stop()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertEqual(self.app.var_status.get(), "已请求停止…")

        # 过渡期的中间状态不许把提示盖掉
        self.app._handle_message("status", "⏳ 选题过于频繁，等待 59s 后重试")
        self.assertEqual(self.app.var_status.get(), "已请求停止…")

        # worker 退出后才落「已停止」
        self.app._on_worker_done("完成")
        self.assertEqual(self.app.var_status.get(), "已停止")

    def test_stop_when_idle_does_not_arm_flag(self):
        """空闲时点停止不该留下停止标志（否则下一次任务一启动就被当成已取消）。"""
        self.app.worker = None
        self.app.on_stop()
        self.assertFalse(self.app.stop_event.is_set())
        self.assertEqual(self.app.var_status.get(), "就绪")

    def test_worker_error_after_stop_is_not_hidden(self):
        """停止期间 worker 真出错，必须报错，别用「已停止」盖掉。"""

        class _Alive:
            def is_alive(self):
                return True

        self.app.worker = _Alive()
        self.app.on_stop()
        self.app._on_worker_done("出错")
        self.assertEqual(self.app.var_status.get(), "出错")

    def test_padding_applied_to_widgets(self):
        """抽查真实控件：150% 下左栏标题的 padx 必须是放大后的值。"""
        expected = self.app._pad(8)
        for widget in self.app.btn_auto.master.winfo_children():
            pad = str(widget.pack_info().get("padx", "")) if widget.winfo_manager() == "pack" else ""
            if pad in ("", "0"):
                continue
            if "," in pad:      # (0, 8) 这种二元组取第二项
                pad = pad.split(",")[-1].strip()
            self.assertEqual(int(float(pad)), expected, f"{widget.winfo_class()} 的 padx 没缩放")

    def test_defaults_come_from_config(self):
        self.assertTrue(self.app.var_base.get().startswith("http"))
        self.assertEqual(self.app.var_type.get(), "编程题")
        self.assertIn(self.app.var_judge.get(), [n for n, _ in gui.JUDGES])

    def test_snapshot_maps_combobox_values(self):
        self.app.var_type.set("编程题")
        self.app.var_level.set("中等")
        self.app.var_judge.set("平台编译")
        self.app.var_count.set(3)
        self.app.tag_map = {"随机": -1, "U1（id=3）": 3}
        self.app.var_tag.set("U1（id=3）")
        cfg = self.app._cfg_snapshot()
        self.assertEqual(cfg.run.qbase_type, 4)
        self.assertEqual(cfg.run.level, 3)
        self.assertEqual(cfg.run.judge, "server")
        self.assertEqual(cfg.run.count, 3)
        self.assertEqual(cfg.run.tag_id, 3)

    def test_unknown_tag_falls_back_to_random(self):
        self.app.tag_map = {"随机": -1}
        self.app.var_tag.set("不存在的标签")
        self.assertEqual(self.app._cfg_snapshot().run.tag_id, -1)

    def test_log_appends_and_queue_drains(self):
        self.app.log("hello")
        self.app._drain_queue()
        self.assertIn("hello", self.app.txt_log.get("1.0", "end"))

    def test_queue_pump_survives_bad_message(self):
        """一条坏消息只跳过它自己，泵和后续消息都不受影响。

        回归自 2026-11-04 真实冒烟：界面按钮点着没反应、日志不再刷新，
        但窗口照样响应消息——因为 `after` 在 except 之外，一次异常就永久停泵。
        """
        seen = []
        original = self.app._handle_message

        def flaky(kind, payload):
            seen.append(payload)
            if payload == "BOOM":
                raise RuntimeError("坏消息")
            original(kind, payload)

        with tempfile.TemporaryDirectory() as tmp:
            err_path = Path(tmp) / "client-errors.log"
            with mock.patch.object(gui, "error_log_path", return_value=err_path):
                self.app._handle_message = flaky
                try:
                    self.app.queue.put(("log", "BOOM"))
                    self.app.queue.put(("log", "AFTER-BOOM"))
                    self.app._drain_queue()
                finally:
                    self.app._handle_message = original

            self.assertEqual(seen, ["BOOM", "AFTER-BOOM"])
            self.assertIn("AFTER-BOOM", self.app.txt_log.get("1.0", "end"))
            self.assertIn("RuntimeError: 坏消息", err_path.read_text(encoding="utf-8"))

    def test_queue_pump_reschedules_after_failure(self):
        """异常之后泵要自己排回下一次：等 200ms，第二条消息必须被处理。

        `after info` 只回定时器 id、看不到函数名，所以这里不查名字，直接让事件循环跑。
        """
        seen = []

        def handler(kind, payload):
            if payload == "BOOM":
                raise RuntimeError("坏消息")
            seen.append(payload)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            gui, "error_log_path", return_value=Path(tmp) / "client-errors.log"
        ):
            self.app._handle_message = handler
            try:
                self.app.queue.put(("log", "BOOM"))
                self.app._drain_queue()  # 这一轮会抛，泵必须仍然把自己排回去
                self.app.queue.put(("log", "AFTER-BOOM"))
                deadline = time.time() + 1.0
                while time.time() < deadline and "AFTER-BOOM" not in seen:
                    self.app.update()
                    time.sleep(0.02)
            finally:
                del self.app._handle_message

        self.assertIn("AFTER-BOOM", seen)

    def test_statement_tab_render(self):
        record = {
            "paper": {
                "questions": [
                    {"id": 173, "qType": 5, "point": 10, "language": 1, "content": "题目正文", "options": []}
                ]
            }
        }
        self.app._show_record(record)
        self.app._drain_queue()
        text = self.app.txt_statement.get("1.0", "end")
        self.assertIn("Q173", text)
        self.assertIn("题目正文", text)

    def test_confirm_returns_default_when_confirmation_disabled(self):
        self.app.config_data.run.confirm = False
        self.assertTrue(self.app._confirm_from_worker("继续？"))

    def test_ask_sets_event_even_if_dialog_raises(self):
        """确认框自身抛异常时也必须放行后台线程。

        回归：`messagebox.askyesno` 抛异常会跳过 `event.set()`，后台线程白等 120 秒，
        界面表现成「任务卡住」。异常本身交给泵兜住并落盘。
        """
        event = threading.Event()
        box = {}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            gui, "error_log_path", return_value=Path(tmp) / "client-errors.log"
        ), mock.patch.object(gui.messagebox, "askyesno", side_effect=RuntimeError("弹窗炸了")):
            self.app.queue.put(("ask", ("继续？", event, box)))
            self.app._drain_queue()

        self.assertTrue(event.is_set(), "后台线程必须被唤醒，不能空等 120 秒")
        self.assertNotIn("answer", box)


class ModelDialogTest(unittest.TestCase):
    """「更换模型」按钮：直接改 API Key / 接口地址 / 模型名，保存即生效。"""

    def setUp(self):
        self.app = gui.ClientApp()
        self.app.withdraw()
        from trainsolve.config import ModelEndpoint

        cfg = self.app.config_data
        cfg.deepseek.api_key = "wbk-primary-key"
        cfg.deepseek.base_url = "http://127.0.0.1:7864/v1"
        cfg.deepseek.model = "cn:deepseek-v4.1-flash"
        cfg.deepseek.fallbacks = [
            ModelEndpoint(api_key="sk-official-key", base_url="https://api.deepseek.com",
                          model="deepseek-flash", label="官方flash"),
        ]
        cfg.deepseek.preferred = 0

    def tearDown(self):
        self.app.stop_event.set()
        self.app.destroy()

    def test_loads_current_endpoint_into_fields(self):
        dialog = gui.ModelDialog(self.app)
        try:
            self.assertEqual(len(dialog.items), 2)
            self.assertEqual(dialog.var_url.get(), "http://127.0.0.1:7864/v1")
            self.assertEqual(dialog.var_model.get(), "cn:deepseek-v4.1-flash")
        finally:
            dialog.destroy()

    def test_fresh_install_has_editable_slot(self):
        """公开版全新安装：没有任何端点时也要能填 Key/地址/模型。"""
        cfg = self.app.config_data
        cfg.deepseek.api_key = ""
        cfg.deepseek.base_url = "https://api.deepseek.com"
        cfg.deepseek.model = "deepseek-flash"
        cfg.deepseek.fallbacks = []
        dialog = gui.ModelDialog(self.app)
        try:
            self.assertEqual(len(dialog.items), 1)
            self.assertEqual(dialog.var_url.get(), "https://api.deepseek.com")
            self.assertEqual(dialog.var_model.get(), "deepseek-flash")
            dialog.var_key.set("sk-filled-by-user")
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_apply()
            self.assertEqual(self.app.config_data.deepseek.api_key, "sk-filled-by-user")
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    def test_edit_key_and_url_is_saved(self):
        dialog = gui.ModelDialog(self.app)
        dialog.var_index.set(0)
        dialog._load_fields()
        dialog.var_key.set("sk-new-key-123456")
        dialog.var_url.set("https://api.deepseek.com")
        dialog.var_model.set("deepseek-v4-pro")
        with mock.patch.object(gui, "save_client_config") as saver:
            dialog.on_apply()
        cfg = self.app.config_data
        self.assertEqual(cfg.deepseek.api_key, "sk-new-key-123456")
        self.assertEqual(cfg.deepseek.base_url, "https://api.deepseek.com")
        self.assertEqual(cfg.deepseek.model, "deepseek-v4-pro")
        saver.assert_called_once()
        self.assertIn("deepseek-v4-pro", self.app.var_model.get())

    def test_edit_fallback_and_make_it_current(self):
        dialog = gui.ModelDialog(self.app)
        dialog.var_index.set(1)
        dialog._load_fields()
        dialog.var_url.set("https://api.deepseek.com")
        dialog.var_key.set("sk-official-updated")
        with mock.patch.object(gui, "save_client_config"):
            dialog.on_apply()
        cfg = self.app.config_data
        self.assertEqual(cfg.deepseek.preferred, 1)
        self.assertEqual(cfg.deepseek.current().api_key, "sk-official-updated")
        self.assertEqual(cfg.deepseek.current().model, "deepseek-flash")

    def test_requires_all_three_fields(self):
        dialog = gui.ModelDialog(self.app)
        dialog.var_url.set("")
        with mock.patch.object(gui, "save_client_config") as saver:
            dialog.on_apply()
        saver.assert_not_called()
        dialog.destroy()

    def test_add_and_delete_fallback(self):
        dialog = gui.ModelDialog(self.app)
        before = len(dialog.items)
        dialog.on_add()
        self.assertEqual(len(dialog.items), before + 1)
        self.assertEqual(dialog.var_index.get(), before)
        dialog.on_delete()
        self.assertEqual(len(dialog.items), before)
        dialog.destroy()

    def test_model_label_shows_fallback_count(self):
        self.app._refresh_model_label()
        self.assertIn("另有 1 个备用", self.app.var_model.get())


class SettingsDialogTest(unittest.TestCase):
    def test_constructs(self):
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            self.assertIsNotNone(dialog.var_key)
            dialog.destroy()
        finally:
            app.stop_event.set()
            app.destroy()

    def test_auto_cleanup_runs_once_per_process(self):
        """登录/开跑时只自动清理一次，且清理失败不能影响界面。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            # 前置：显式打开开关，不让这条用例依赖夹具或本机配置里 auto_cleanup 的值
            app.config_data.run.auto_cleanup = True
            logs: list[str] = []
            app.log = logs.append  # type: ignore[method-assign]
            report = mock.Mock(cleaned=["s1"], temp_dirs=[], summary=lambda: "清理过期会话 1 条")
            with mock.patch("trainsolve.leftovers.auto_cleanup", return_value=report) as patched:
                app._auto_cleanup(app.config_data, mock.Mock())
                app._auto_cleanup(app.config_data, mock.Mock())
            self.assertEqual(patched.call_count, 1, "每个进程只清一次")
            self.assertTrue(any("启动清理" in line for line in logs), logs)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_auto_cleanup_can_be_disabled(self):
        app = gui.ClientApp()
        app.withdraw()
        try:
            app.config_data.run.auto_cleanup = False
            with mock.patch("trainsolve.leftovers.auto_cleanup") as patched:
                app._auto_cleanup(app.config_data, mock.Mock())
            patched.assert_not_called()
        finally:
            app.stop_event.set()
            app.destroy()

    def test_auto_cleanup_swallows_errors(self):
        app = gui.ClientApp()
        app.withdraw()
        try:
            # 前置：同上下——先打开开关，再验证清理抛错被吞掉
            app.config_data.run.auto_cleanup = True
            logs: list[str] = []
            app.log = logs.append  # type: ignore[method-assign]
            with mock.patch("trainsolve.leftovers.auto_cleanup", side_effect=RuntimeError("net down")):
                app._auto_cleanup(app.config_data, mock.Mock())
            self.assertTrue(any("自动清理失败" in line for line in logs), logs)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_human_typing_settings_are_saved(self):
        """「模拟人类输入」的开关/速度/CDP 端口要能存进配置。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            dialog.var_human.set(True)
            dialog.var_human_dry.set(False)
            dialog.var_cpm.set(320.0)
            dialog.var_cpm_min.set(180.0)
            dialog.var_cpm_max.set(420.0)
            dialog.var_typo.set(0.01)
            dialog.var_cdp.set("http://127.0.0.1:9333")
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            cfg = app.config_data
            self.assertTrue(cfg.run.human_input)
            self.assertFalse(cfg.run.human_dry_run)
            self.assertEqual(cfg.run.typing_cpm, 320.0)
            self.assertEqual(cfg.run.typing_cpm_min, 180.0)
            self.assertEqual(cfg.run.typing_cpm_max, 420.0)
            self.assertEqual(cfg.run.typo_rate, 0.01)
            self.assertTrue(app.var_human.get(), "主界面勾选状态要同步")
        finally:
            app.stop_event.set()
            app.destroy()

    def test_speed_range_is_normalized(self):
        """下限 > 上限时保存要自动交换，不能存成非法区间。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            dialog.var_cpm_min.set(500.0)
            dialog.var_cpm_max.set(300.0)
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            self.assertEqual(app.config_data.run.typing_cpm_min, 300.0)
            self.assertEqual(app.config_data.run.typing_cpm_max, 500.0)
        finally:
            app.stop_event.set()
            app.destroy()


    def test_auto_cleanup_settings_are_saved(self):
        """启动清理开关/超龄小时要能存进配置。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            dialog.var_auto_clean.set(False)
            dialog.var_auto_clean_all.set(True)
            dialog.var_auto_temp_hours.set(6.0)
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            cfg = app.config_data
            self.assertFalse(cfg.run.auto_cleanup)
            self.assertTrue(cfg.run.auto_cleanup_all)
            self.assertEqual(cfg.run.auto_cleanup_temp_hours, 6.0)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_submit_mode_label_tracks_config(self):
        """主界面必须直说「会不会真提交」——dry_run 默认开着最容易被误解。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            run = app.config_data.run
            run.dry_run = True
            run.submit = False
            app._refresh_submit_mode()
            self.assertIn("试运行", app.lbl_submit_mode.cget("text"))

            run.dry_run = False
            run.submit = False
            app._refresh_submit_mode()
            self.assertIn("不提交", app.lbl_submit_mode.cget("text"))

            run.dry_run = False
            run.submit = True
            app._refresh_submit_mode()
            self.assertIn("真提交", app.lbl_submit_mode.cget("text"))
        finally:
            app.stop_event.set()
            app.destroy()

    def test_main_window_pacing_controls_apply(self):
        """主界面的打字速度 / 随机思考要能直接写进配置（区间写反自动交换、越界夹紧）。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            app.var_cpm_main.set(320.0)
            app.var_delay_min_main.set(8.0)
            app.var_delay_max_main.set(3.0)
            app._on_pacing_change()
            cfg = app.config_data
            self.assertEqual(cfg.run.typing_cpm, 320.0)
            self.assertEqual(cfg.run.human_delay_min, 3.0)
            self.assertEqual(cfg.run.human_delay_max, 8.0)

            app.var_cpm_main.set(5000.0)
            app.var_delay_max_main.set(99.0)
            app._on_pacing_change()
            self.assertEqual(cfg.run.typing_cpm, 800.0)
            self.assertEqual(cfg.run.human_delay_max, 60.0)

            snap = app._cfg_snapshot()
            self.assertEqual(snap.run.typing_cpm, 800.0)
            self.assertEqual(snap.run.human_delay_max, 60.0)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_settings_save_syncs_main_controls(self):
        """设置里改完保存，主界面那两个控件要跟着变。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            dialog.var_cpm.set(400.0)
            dialog.var_delay_min.set(2.0)
            dialog.var_delay_max.set(6.0)
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            self.assertEqual(app.var_cpm_main.get(), 400.0)
            self.assertEqual(app.var_delay_min_main.get(), 2.0)
            self.assertEqual(app.var_delay_max_main.get(), 6.0)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_keep_going_settings_are_saved(self):
        """「遇到未满分也继续」勾选 = stop_on_failure 关，两个方向都要能存。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            app.config_data.run.stop_on_failure = True
            dialog = gui.SettingsDialog(app)
            self.assertFalse(dialog.var_keepgoing.get(), "默认不勾选")
            dialog.var_keepgoing.set(True)
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            self.assertFalse(app.config_data.run.stop_on_failure)

            again = gui.SettingsDialog(app)
            self.assertTrue(again.var_keepgoing.get(), "回读要反映已保存的值")
            again.var_keepgoing.set(False)
            with mock.patch.object(gui, "save_client_config"):
                again.on_save()
            self.assertTrue(app.config_data.run.stop_on_failure)
        finally:
            app.stop_event.set()
            app.destroy()

    def test_human_delay_settings_are_saved(self):
        """「每题随机思考」区间要能存进配置；写反自动交换。"""
        app = gui.ClientApp()
        app.withdraw()
        try:
            dialog = gui.SettingsDialog(app)
            dialog.var_delay_min.set(10.0)
            dialog.var_delay_max.set(3.0)
            with mock.patch.object(gui, "save_client_config"):
                dialog.on_save()
            cfg = app.config_data
            self.assertEqual(cfg.run.human_delay_min, 3.0)
            self.assertEqual(cfg.run.human_delay_max, 10.0)

            again = gui.SettingsDialog(app)
            self.assertEqual(again.var_delay_min.get(), 3.0)
            self.assertEqual(again.var_delay_max.get(), 10.0)
        finally:
            app.stop_event.set()
            app.destroy()


class ConfigPersistenceTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            with mock.patch.object(gui, "settings_path", return_value=target), \
                    mock.patch.object(gui, "load_config", wraps=gui.load_config):
                cfg = gui.load_config(None)
                cfg.deepseek.api_key = "sk-test-123"
                cfg.deepseek.model = "deepseek-reasoner"
                cfg.platform.username = "2000000001"
                cfg.run.max_repairs = 2
                gui.save_client_config(cfg)
                self.assertTrue(target.exists())

                saved = gui.load_config(str(target))
                self.assertEqual(saved.deepseek.api_key, "sk-test-123")
                self.assertEqual(saved.deepseek.model, "deepseek-reasoner")
                self.assertEqual(saved.platform.username, "2000000001")
                self.assertEqual(saved.run.max_repairs, 2)

    def test_saved_file_is_valid_toml(self):
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            with mock.patch.object(gui, "settings_path", return_value=target):
                gui.save_client_config(gui.load_config(None))
            data = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertIn("deepseek", data)
            self.assertIn("platform", data)

    def test_save_preserves_safety_switches(self):
        """保存配置不能把安全开关翻回「允许提交」。

        回归：`submit/confirm/dry_run/solver/stop_on_failure` 曾写死成
        true/true/false/"auto"/true，任何人手工改成安全档，点一次登录、
        换一次模型或保存一次设置（都走 save_client_config）就被静默改回去。
        """
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            with mock.patch.object(gui, "settings_path", return_value=target):
                cfg = gui.load_config(None)
                cfg.run.submit = False
                cfg.run.dry_run = True
                cfg.run.confirm = False
                cfg.run.stop_on_failure = False
                cfg.run.solver = "stub"
                gui.save_client_config(cfg)

            raw = tomllib.loads(target.read_text(encoding="utf-8"))["run"]
            self.assertIs(raw["submit"], False)
            self.assertIs(raw["dry_run"], True)
            self.assertIs(raw["confirm"], False)
            self.assertIs(raw["stop_on_failure"], False)
            self.assertEqual(raw["solver"], "stub")

            saved = gui.load_config(str(target))
            self.assertFalse(saved.run.submit)
            self.assertTrue(saved.run.dry_run)
            self.assertFalse(saved.run.confirm)
            self.assertFalse(saved.run.stop_on_failure)
            self.assertEqual(saved.run.solver, "stub")

    def test_every_run_field_roundtrips(self):
        """`[run]` 每个字段都必须按 config 对象里的值写，不许再有写死的常量。

        给每个字段换一个不同的值，存完再读回来逐个比对；对不上的就是被写死或在
        序列化里丢掉的——这正是 submit/dry_run 那一批常量的发现方式。
        """
        from dataclasses import fields

        from trainsolve.config import RunConfig

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            with mock.patch.object(gui, "settings_path", return_value=target):
                cfg = gui.load_config(None)
                expected = {}
                for item in fields(RunConfig):
                    current = getattr(cfg.run, item.name)
                    if isinstance(current, bool):
                        value = not current
                    elif isinstance(current, int):
                        value = current + 7
                    elif isinstance(current, float):
                        value = current + 1.5
                    elif isinstance(current, list):
                        value = [9, 8]
                    else:
                        value = f"{current}x" if current else "x"
                    setattr(cfg.run, item.name, value)
                    expected[item.name] = value

                gui.save_client_config(cfg)
                saved = gui.load_config(str(target))

            for name, value in expected.items():
                self.assertEqual(getattr(saved.run, name), value, f"[run] {name} 没有按配置写入")



    def test_dpi_awareness_helper_is_safe_to_call(self):
        """DPI 感知开关必须可重复调用、失败不抛（非 Windows / 已被设过）。"""
        gui._enable_dpi_awareness()
        gui._enable_dpi_awareness()


@unittest.skipUnless(_tk_available(), "没有可用的图形环境")
class CloseCancelsTimersTest(unittest.TestCase):
    """关窗必须取消定时器（0.5.6）。

    回归自 2026-11-07：`ClientApp` 里 7 处 `after(...)` 全都不留 id，也没有
    `WM_DELETE_WINDOW` 处理器。窗口销毁后回调仍会打到已销毁的控件上，Tk 报
    `invalid command name "..._drain_queue"`，并往 `client-errors.log` 写一条无意义的错误。

    这里钉的是"机制"，不靠肉眼看终端：关窗后 Tcl 的 after 队列必须为空。
    """

    def _new_app(self):
        app = gui.ClientApp()
        app.withdraw()
        self.addCleanup(self._safe_destroy, app)
        return app

    @staticmethod
    def _safe_destroy(app):
        try:
            app.destroy()
        except tk.TclError:          # 已经关过了
            pass

    @staticmethod
    def _pending(app) -> set[str]:
        return set(app.tk.call("after", "info"))

    def test_timers_are_registered(self):
        """先证明有定时器：否则"关窗后队列为空"就是恒真断言。"""
        app = self._new_app()
        self.assertTrue(self._pending(app), "ClientApp 启动后没有任何 after 定时器，这条用例失去意义")

    def test_cancel_pending_timers_clears_queue(self):
        app = self._new_app()
        self.assertTrue(self._pending(app))
        app._cancel_pending_timers()
        self.assertEqual(self._pending(app), set(), "取消后仍有定时器排队，关窗后还会回调到已销毁控件")

    def test_close_protocol_is_registered(self):
        app = self._new_app()
        handler = app.protocol("WM_DELETE_WINDOW")
        self.assertTrue(handler, "没挂 WM_DELETE_WINDOW，用户点关闭时不会走清理逻辑")

    def test_close_via_protocol_cancels_and_destroys(self):
        """等价用户点右上角关闭：处理器要请求停止、清空 after 队列并销毁窗口。

        为什么这里查 `stop_event`：Tk 默认给 WM_DELETE_WINDOW 绑的是 `destroy`，
        只看"窗口没了 / 定时器清了"抓不到"处理器没挂上"——`destroy()` 覆盖里也会清理。
        `stop_event` 只有真正的关闭处理器会置位，所以它是这条路径的可观测差异。
        """
        app = self._new_app()
        handler = app.protocol("WM_DELETE_WINDOW")
        self.assertTrue(handler)
        self.assertFalse(app.stop_event.is_set(), "新建窗口时不该已经处于停止状态")
        app.tk.eval(handler)
        self.assertTrue(app.stop_event.is_set(), "关窗没有请求停止：后台线程会在窗口消失后继续跑")
        with self.assertRaises(tk.TclError):
            app.winfo_exists()

    def test_direct_destroy_also_cancels(self):
        """直接 destroy（测试/异常退出路径）也必须清理，不能只认关闭按钮。"""
        app = gui.ClientApp()
        app.withdraw()
        self.assertTrue(self._pending(app))
        app.destroy()
        self.assertEqual(app._after_ids, set(), "destroy 后仍留着定时器 id，回调会打到已销毁控件")

    def test_second_app_after_close_starts_clean(self):
        app1 = self._new_app()
        app1._on_close()
        app2 = self._new_app()
        self.assertEqual(len(self._pending(app2)), 2, "新窗口应只有自己的两个启动定时器")


if __name__ == "__main__":
    unittest.main()
