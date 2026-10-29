"""PyInstaller 的 CLI 入口（控制台程序）。

为什么需要这个文件：直接把 ``src/eveupdate/__main__.py`` 当入口喂给 PyInstaller，
它会被当成顶层脚本 ``__main__`` 打包，里面的 ``from .cli import main`` 是相对导入，
启动时就报 ``attempted relative import with no known parent package``。
所以入口脚本一律用**绝对导入** ``from eveupdate.cli import main``。
"""

import multiprocessing
import sys

from eveupdate.cli import main

if __name__ == "__main__":
    # 打包成单文件后，子进程会重新执行入口，没有这个调用会无限开窗口。
    multiprocessing.freeze_support()
    sys.exit(main())
