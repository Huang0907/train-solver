"""独立客户端入口（也被打单文件 exe 使用）。

    python client_main.py            # 有控制台
    pythonw client_main.py           # 无控制台
"""

import sys


def main() -> int:
    from trainsolve.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
