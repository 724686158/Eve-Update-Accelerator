"""PyInstaller 的 GUI 入口（窗口程序，无控制台）。

与 :mod:`build_entry` 同样的道理：只能用绝对导入。
GUI 版直接进图形界面；万一 tkinter 没被收进来，弹一个对话框说明，
而不是让用户在「双击没反应」里猜——窗口程序没有控制台可以打栈回溯。
"""

import multiprocessing
import sys


def _main() -> int:
    try:
        from eveupdate.gui import run_gui
    except ImportError as exc:
        _explain_missing_tk(exc)
        return 1
    return run_gui()


def _explain_missing_tk(exc: BaseException) -> None:
    message = (
        f"无法加载图形界面：{exc}\n\n"
        "请改用命令行版本 eve-update-accelerator.exe，例如：\n"
        "    eve-update-accelerator.exe check\n"
        "    eve-update-accelerator.exe apply --gui"
    )
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("EVE Update Accelerator", message)
        root.destroy()
    except Exception:
        # 连 tkinter 都没有：退化成控制台输出（从命令行启动时可见）。
        print(message, file=sys.stderr)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(_main())
