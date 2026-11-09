# Windows 自动化踩坑记录（TrainSolveClient）

适用：用 PowerShell / Python + ctypes 驱动 Tk 客户端做冒烟、截图、点击。
每条按「现象 → 根因 → 正确做法」写，可直接复制。最后更新 2026-11-06。

## 1. Tk 不实现 UIA provider

**现象**：UIA 树里 Tk 控件全是无名 `Pane` / `TkChild`，`role:Button` 只命中标题栏三个系统按钮；学号框、密码框、业务按钮一律不在树里。
**根因**：Tk 没有实现 UIA provider，也不提供可用的 MSAA 桥，不暴露控件语义（名称、角色、可点区域都没有）。
**做法**：别再试“UIA 语义定位 Tk 控件”。按窗口标题找顶层窗口，用坐标点击，或 Tab 顺序 + 空格触发。
```python
# 只有标题栏那三颗会命中，业务控件一个都不会出现
[el for el in uia.find_all(ControlType.Button)]   # → 最小化 / 最大化 / 关闭
```

## 2. 单文件 exe 的窗口在子进程

**现象**：进程明明起来了，但 `Start-Process -PassThru` 拿到的 pid 上 `MainWindowHandle` 恒为 0，按它判断会误判成“没起来”。
**做法**：窗口在 bootloader 拉起的子进程上。按标题找窗口、用窗口自己的进程关窗，完整做法见 §8。

## 3. ctypes 不设 argtypes 会静默失败

**现象**：`SetWindowPos` 不报错，窗口却没置顶；点击要么落空，要么打到 Chrome 等别人窗口上。
**根因**：未声明 `argtypes` 时 64 位 HWND 被按 32 位 int 处理、句柄被截断，调用失败但不抛异常——不报错，也不做事。
**做法**：先设全 `argtypes` / `restype`，再谈逻辑。
```python
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, c_int, c_int, c_int, c_int, c_uint]; u.SetWindowPos.restype = wt.BOOL
u.WindowFromPoint.argtypes = [wt.POINT]; u.ClientToScreen.argtypes = [wt.HWND, POINTER(wt.POINT)]
```

## 4. DPI 换算

**现象**：按 `winfo_rootx/rooty` 算出的点位会点到旁边控件；同一版式的按钮坐标换个进程就不准。
**根因**：150% 缩放下逻辑尺寸与物理像素差 1.5 倍；进程不声明 DPI 感知时，Win32 坐标还会被系统虚拟化。
**做法**：进程启动即声明 DPI 感知，坐标统一用物理像素，且不跨缩放比复用点位。
```python
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
# 150%：1180×800 逻辑尺寸 = 1794×1259 物理像素
```

## 5. 配置被重写的时机

**现象**：手工把 `submit=false` / `dry_run=true` 改成安全档，点一次登录就被静默翻回允许提交。
**根因**：不是启动时重写。是 `save_client_config()` 在 `on_login`（登录）、模型对话框 `on_apply`、设置对话框 `on_save` 三处被调用；该方法曾把 `submit/confirm/dry_run/solver/stop_on_failure` 写死成常量，2026-11-05 已修。
**做法**：改配置前先确认用的是修复后的版本；改配置要“先关客户端再改”，否则内存里的旧值会在下次保存时覆盖你的修改。
```powershell
(Get-FileHash "$env:APPDATA\TrainSolve\config.toml" -Algorithm SHA256).Hash   # 改前改后各取一次
```

## 6. 队列泵曾会永久停摆

**现象**：窗口照样响应消息（不卡死），但按钮点了没反应、日志和状态栏再也不更新，进程看起来“活着但死了”。
**根因**：`_drain_queue` 的 `after(120, …)` 原文在 `try` 外、`except` 只接 `queue.Empty`；任一 handler 抛异常就不再重排，且 pythonw / exe 没有控制台，traceback 直接消失。
**做法**：已改为逐条 `try` + `finally` 重排，异常落盘 `client-errors.log`。冒烟第一步先看这个文件，非空即说明界面线程出过错。
```powershell
Get-Content "$env:APPDATA\TrainSolve\client-errors.log" -Tail 40
```

## 7. 哪些按钮/接口会写平台

**现象**：按钮写着「不提交」，点下去其实建了会话；冒烟误点一次就产生真实记录。
**根因**：写操作藏在不显眼的按钮后面，文档只讲流程、不讲写面。
**做法**：动界面前先对表；只读入口只有 `peek --session`、`tags`、`show`，其余默认按写处理。

| 入口 | 请求 | 说明 |
|---|---|---|
| 登录 | `POST api/student/auth` | 鉴权，不产生业务数据 |
| 刷新标签 | `GET api/student/list-tag` | 只读 |
| 抽新题（新建会话） | `POST api/student/choose-q` | **建会话、占一个题量名额** |
| 开始自动刷题 / 全自动 | choose-q → post-answer → submit | 全链路写 |
| 提交我的代码 | `post-answer` + `submit` | 写 |
| 本地自检 / 服务端试跑 | `POST api/student/run-code` | POST，但不建业务记录 |
| 停止 | 无请求 | 只置本地事件 |
| CLI `resume` / `redo` / `cleanup` | post-answer / submit / skip+cancel | 写；`redo` 还消耗重置额度 |

```powershell
python -m trainsolve peek --session <sid>   # 只读看题（不 choose-q）
```

## 8. 窗口句柄属于哪个进程

**现象**：`Start-Process -PassThru` 的 pid 上 `MainWindowHandle` 恒为 0，看着像“没起来”；`PostMessage(WM_CLOSE)` 关窗 10 秒不退，看着像“客户端不肯退出”。
**根因**：单文件 exe 是 bootloader + 子进程两段，窗口在**子进程**上；消息发给 bootloader 侧句柄，没人处理——是消息投错了进程，不是应用 bug。
**做法**：一律按标题找窗口，再用窗口自己的进程关窗；不要用启动进程的句柄，也不要 `PostMessage(WM_CLOSE)`。

```powershell
# 取窗口：标题匹配的那个才是真宿主（属于子进程，不是 Start-Process 返回的 pid）
$app = Get-Process TrainSolveClient | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
$app.Id; $app.MainWindowTitle

# 关窗：交给窗口自己的进程（实测 0.1s 退出；PostMessage 那次 10s 没动）
$app.CloseMainWindow() | Out-Null; $app.WaitForExit(5000)
```

```python
# 纯 Win32 路线：EnumWindows 找到 hwnd 后，确认它到底属于哪个进程
user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wt.DWORD()))
```

## 9. 打包脚本的编码与留底

**现象**：用“写文本文件”的通用方式重写 `build_release.ps1` 后，副本里一跑构建就报
`Missing closing '}' in statement block` / `The string is missing the terminator`，
但文件本身看着完全正常。

**根因**：Windows PowerShell 5.1 解析 `.ps1` 时按 **ANSI（本机 GBK）** 读无 BOM 的 UTF-8 文件。
脚本里的中文字符串因此被拆坏，报错位置在**后面几百行**（第一个被误解的引号处），
指向的却不是真正原因。0.5.4 打第一遍就撞上这个，之前那份脚本带 BOM 所以从没暴露。

**做法**：`.ps1` 一律存成 **UTF-8 with BOM**；改完先做语法校验，再跑构建。

```powershell
# 补 BOM（已带就跳过）
$p = 'build_release.ps1'; $b = [IO.File]::ReadAllBytes($p)
if (-not ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)) {
    [IO.File]::WriteAllBytes($p, ([byte[]](0xEF,0xBB,0xBF)) + $b)
}
# 语法校验：errors.Count 必须是 0
$err = $null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $p), [ref]$null, [ref]$err); $err.Count
```

**留底规矩**：仓库是交付源，**不要把 `.bak-*` 放进仓库**（尤其 `dist\` 里的旧 exe，
会被当成“该跑的是哪个”）。要留底就放到仓库外：
`D:\DS Bot\Engine\_attic\<日期>\`。


