"""tkinter 图形界面：一个按钮测速，一个按钮生效，一个按钮撤销。

三条工程约束，都是踩过坑才这么写的：

* **所有耗时操作在工作线程里跑**。Windows 的 UAC 提权
  （``ShellExecuteExW`` + ``WaitForSingleObject``）会阻塞到用户点完为止，
  放在 GUI 线程里界面会假死；测速同理（要跑十几秒）。
* **线程只往队列里塞消息，不碰控件**。tkinter 不是线程安全的，跨线程改控件
  会出现「偶发崩溃」，那种 bug 最难查。所以工作线程只写 ``queue``，
  主线程用 ``after`` 轮询并更新界面。
* **tkinter 缺失时不要栈回溯**。它是可选依赖（精简版 Python、某些 Linux
  发行版会缺），缺了应当退化成清晰的命令行提示。

界面刻意保持简单：这是一个「一次性修好、平时不打开」的工具，不需要花哨。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback

from . import APP_NAME, __version__, elevate, hosts
from .scan import ScanResult, scan

#: 界面文案。中文为主（目标用户是国内 EVE 玩家），英文备选。
STRINGS = {
    "zh": {
        "title": f"{APP_NAME} v{__version__}",
        "intro": (
            "EVE 的更新下载走 binaries.eveonline.com。\n"
            "这个工具实测当前网络下「真的能下到内容、而且最快」的地址，\n"
            "然后写进 hosts，让启动器优先走它。"
        ),
        "note_hosts": (
            "注意：hosts 只是把已知可用的地址加进候选，不保证被优先选中。\n"
            "如果实测发现 DNS 给的地址本来就又快又稳，就没必要改。"
        ),
        "scan": "开始测速（只读，不改系统）",
        "apply": "测速并生效（需要授权）",
        "revert": "撤销（恢复原样）",
        "doctor": "诊断现状",
        "ready": "就绪。点「开始测速」先看看情况。",
        "busy": "正在工作，请稍候…",
        "done": "完成。",
        "cancel": "已取消授权，未做任何修改。",
        "ask_apply": "将修改系统 hosts 文件（会先自动备份，可随时撤销）。继续？",
        "ask_revert": "将移除本工具写入的内容，恢复 hosts 原样。继续？",
    },
    "en": {
        "title": f"{APP_NAME} v{__version__}",
        "intro": (
            "EVE downloads its updates from binaries.eveonline.com.\n"
            "This tool measures which endpoints actually serve content and are\n"
            "fastest on your network, then pins them in the hosts file."
        ),
        "note_hosts": (
            "Note: the hosts file only *adds* candidates; it cannot outrank DNS.\n"
            "If your DNS already gives fast, working addresses, no change is needed."
        ),
        "scan": "Measure only (read-only)",
        "apply": "Measure and apply (needs admin)",
        "revert": "Revert changes",
        "doctor": "Diagnose current setup",
        "ready": "Ready. Start with 'Measure only'.",
        "busy": "Working, please wait…",
        "done": "Done.",
        "cancel": "Authorization cancelled; nothing was changed.",
        "ask_apply": "This will modify your system hosts file (backed up first, revertible). Continue?",
        "ask_revert": "This will remove what this tool wrote and restore the hosts file. Continue?",
    },
}


def pick_language(preferred: str | None = None) -> str:
    """默认跟随系统语言：中文环境给中文，其余给英文。

    三级优先，顺序不能乱（每一条都被真实环境验证过）：

    1. **显式参数** —— 调用方说了算；
    2. **环境变量** ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG`` —— 这是「用户此刻
       明确选择」的语言，必须压过系统设置，否则在中文 macOS 上 ``LANG=en_US``
       会失效；
    3. **操作系统自己的设置** —— macOS 的 ``defaults -g AppleLocale`` /
       Windows 的 ``GetUserDefaultUILanguage``：这是**真实界面语言**；
    4. **locale 接口**放最后 —— 它经常不可信：实测在中文 macOS 上
       ``locale.getlocale()`` 返回 ``('en_US', 'UTF-8')``（C 库兜底值），
       而 ``defaults`` 里明明是 ``zh_CN``。
    """
    if preferred in STRINGS:
        return preferred

    env_blob = " ".join(
        os.environ.get(key, "") for key in ("LC_ALL", "LC_MESSAGES", "LANG")
    ).lower()
    if "zh" in env_blob or "chinese" in env_blob:
        return "zh"
    if any(code in env_blob for code in ("en_", "en.", "en-", "english")):
        return "en"

    hint = _platform_locale_hint()
    if "zh" in hint:
        return "zh"

    try:
        import locale

        local_blob = (locale.getlocale()[0] or "").lower()
    except Exception:  # pragma: no cover - 平台差异
        local_blob = ""
    if local_blob.startswith("zh"):
        return "zh"
    return "en"


def _platform_locale_hint() -> str:
    """从操作系统自己的设置里取语言线索（取不到就返回空串）。

    实测：macOS 上 ``LANG`` 常常只是 ``utf8``（不带语言），``locale.getlocale()``
    又返回 ``(None, None)`` —— 唯一的真相在 ``defaults`` 里（``AppleLocale=zh_CN``）。
    所以这条路不是锦上添花，而是中文环境下能正确显示中文的关键。
    """
    try:
        if sys.platform == "darwin":
            import subprocess

            out: list[str] = []
            for key in ("AppleLocale", "AppleLanguages"):
                proc = subprocess.run(
                    ["defaults", "read", "-g", key],
                    capture_output=True,
                    timeout=5,
                    text=True,
                )
                out.append(proc.stdout or "")
            return " ".join(out)
        if sys.platform.startswith("win"):  # pragma: no cover - 仅在 Windows 生效
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # 低 10 位是主语言 ID：0x04 = 中文（简繁港都算）
            return "zh" if (lang_id & 0x3FF) == 0x004 else str(lang_id)
    except Exception:  # pragma: no cover - 尽力而为，失败不影响运行
        return ""
    return ""


class AcceleratorApp:
    """主窗口。把「测速 / 生效 / 撤销」三件事串起来。"""

    def __init__(self, lang: str | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.lang = pick_language(lang)
        self.s = STRINGS[self.lang]
        self.tk = tk
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.result: ScanResult | None = None
        self.busy = False

        self.root = tk.Tk()
        self.root.title(self.s["title"])
        self.root.minsize(720, 520)

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=self.s["intro"], justify="left").pack(anchor="w")
        ttk.Label(
            frame, text=self.s["note_hosts"], justify="left", foreground="#666"
        ).pack(anchor="w", pady=(6, 10))

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(0, 8))
        self.btn_scan = ttk.Button(
            buttons, text=self.s["scan"], command=lambda: self._start("scan")
        )
        self.btn_scan.pack(side="left")
        self.btn_apply = ttk.Button(
            buttons, text=self.s["apply"], command=lambda: self._start("apply")
        )
        self.btn_apply.pack(side="left", padx=6)
        self.btn_revert = ttk.Button(
            buttons, text=self.s["revert"], command=lambda: self._start("revert")
        )
        self.btn_revert.pack(side="left")
        self.btn_doctor = ttk.Button(
            buttons, text=self.s["doctor"], command=lambda: self._start("doctor")
        )
        self.btn_doctor.pack(side="left", padx=6)

        self.status = ttk.Label(frame, text=self.s["ready"], foreground="#0a6")
        self.status.pack(anchor="w", pady=(0, 6))

        self.output = tk.Text(frame, wrap="none", height=20)
        self.output.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(self.output, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    # -- 线程与消息 --------------------------------------------------------
    def _write(self, text: str) -> None:
        self.output.insert("end", text + "\n")
        self.output.see("end")

    def _start(self, action: str) -> None:
        if self.busy:
            return
        if action in ("apply", "revert"):
            from tkinter import messagebox

            prompt = self.s["ask_apply"] if action == "apply" else self.s["ask_revert"]
            if not messagebox.askyesno(self.s["title"], prompt):
                return
        self.busy = True
        for button in (self.btn_scan, self.btn_apply, self.btn_revert, self.btn_doctor):
            button.state(["disabled"])
        self.status.configure(text=self.s["busy"], foreground="#a60")
        self.output.delete("1.0", "end")
        threading.Thread(target=self._work, args=(action,), daemon=True).start()
        self.root.after(120, self._pump)

    def _work(self, action: str) -> None:
        """工作线程：只往队列里塞消息，绝不碰控件。"""
        put = self.queue.put
        try:
            put(("text", f"{APP_NAME} v{__version__}  [{action}]"))
            if action == "revert":
                self._do_revert(put)
            elif action == "doctor":
                self._do_doctor(put)
            else:
                self._do_scan(put, apply=action == "apply")
        except Exception:
            put(("text", traceback.format_exc()))
            put(("status", ("失败，详见输出", "#c00")))
        finally:
            put(("finish", None))

    def _do_scan(self, put, *, apply: bool) -> None:
        previous = hosts.HostsFile.read().pinned_ips()
        result = scan(
            previous_ips=previous,
            top=6,
            conns=10,
            log=lambda message: put(("text", message)),
        )
        self.result = result
        put(("text", "-" * 60))
        ok, message = result.pinned_status()
        if result.best:
            put(
                (
                    "text",
                    f"最快：{result.best.ip}  {result.best.mbps:.2f} MB/s  "
                    f"（{result.best.colo or '?'}）",
                )
            )
        if result.previous_ips:
            put(("text", ("✔ " if ok else "✘ ") + message))
        if not result.measured:
            put(("status", ("没有可用落点，未做修改", "#c00")))
            return
        if not apply:
            put(("status", (self.s["done"] + "（只读，未改系统）", "#0a6")))
            return
        ips = result.to_pin()
        plan = hosts.plan_apply(ips)
        if not plan.changed:
            put(("text", "hosts 已是目标状态，无需改动。"))
            put(("status", (self.s["done"], "#0a6")))
            return
        put(("text", f"写入：{' '.join(ips)} -> {result.host}"))
        outcome = elevate.run(plan)
        if not outcome.ok:
            put(("text", self.s["cancel"] if outcome.cancelled else f"失败：{outcome.stderr}"))
            put(("status", (self.s["cancel"] if outcome.cancelled else "写入失败", "#c00")))
            return
        ok2, message2 = elevate.verify(plan)
        put(("text", ("✔ " if ok2 else "✘ ") + message2))
        put(("text", "重启 EVE 启动器即可生效。"))
        put(("status", (message2, "#0a6" if ok2 else "#c00")))

    def _do_revert(self, put) -> None:
        plan = hosts.plan_revert()
        if not plan.changed:
            put(("text", "hosts 里没有本工具写入的内容，无需撤销。"))
            put(("status", ("无需撤销", "#0a6")))
            return
        outcome = elevate.run(plan)
        if not outcome.ok:
            put(("text", self.s["cancel"] if outcome.cancelled else f"失败：{outcome.stderr}"))
            put(("status", ("已取消" if outcome.cancelled else "撤销失败", "#c00")))
            return
        ok, message = elevate.verify(plan)
        put(("text", ("✔ " if ok else "✘ ") + message))
        put(("status", (message, "#0a6" if ok else "#c00")))

    def _do_doctor(self, put) -> None:
        """轻量诊断：只看现役落点还能不能下内容，不做全网扫描。"""
        from . import evindex
        from .scan import TARGET_HOST, probe_content, system_dns_ips

        target = evindex.bench_target(log=lambda m: put(("text", m)))
        pinned = hosts.HostsFile.read().pinned_ips()
        put(("text", f"基准资源：{target.path}  {target.size / 1e6:.1f} MB"))
        put(("text", f"当前钉住：{' '.join(pinned) if pinned else '（无）'}"))
        dns_ips = system_dns_ips(TARGET_HOST)
        put(("text", f"DNS 给出：{' '.join(dns_ips) if dns_ips else '（解析不到）'}"))
        worst = False
        for ip in list(dict.fromkeys(pinned + dns_ips))[:6]:
            content = probe_content(ip, target)
            tag = "  ← 现役" if ip in pinned else ""
            if content.ok:
                put(("text", f"  {ip:<16} OK  {content.mbps:5.2f} MB/s  边缘 {content.colo or '?'}{tag}"))
            else:
                put(("text", f"  {ip:<16} 取不到内容（{content.detail}）{tag}"))
                worst = worst or ip in pinned
        put(
            (
                "status",
                ("现役落点已取不到内容，建议撤销或重新生效" if worst else self.s["done"], "#c00" if worst else "#0a6"),
            )
        )

    def _pump(self) -> None:
        """主线程：把队列里的消息刷到界面。"""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "text":
                    self._write(str(payload))
                elif kind == "status":
                    text, colour = payload  # type: ignore[misc]
                    self.status.configure(text=text, foreground=colour)
                elif kind == "finish":
                    self.busy = False
                    for button in (
                        self.btn_scan, self.btn_apply, self.btn_revert, self.btn_doctor
                    ):
                        button.state(["!disabled"])
                    return
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(lang: str | None = None) -> int:
    """启动界面。tkinter 缺失时抛出 ImportError，由调用方给出命令行指引。"""
    return AcceleratorApp(lang).run()
