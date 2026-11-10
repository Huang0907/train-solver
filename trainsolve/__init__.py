"""train-solve —— 程序设计训练平台自动刷题工具（DeepSeek 可插拔）。

核心（取题/解题/自检/提交/评分）纯标准库实现；可选的「模拟人类打字」模式
（GUI 勾选或 `--human`）用 Playwright 的 connect_over_cdp 驱动本机 Chrome。
"""

__version__ = "0.6.3"