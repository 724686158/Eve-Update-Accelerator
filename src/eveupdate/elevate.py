"""提权：把一份新的 hosts 内容写进系统文件。

三个平台三条通道，都走**系统原生授权界面**，用户不必自己开管理员终端：

===========  ==========================================================
Windows 10   ``ShellExecuteExW(..., verb="runas")`` → 一次 UAC 对话框
macOS        ``osascript -e 'do shell script … with administrator privileges'``
Linux        ``pkexec``（桌面）或直接 ``sudo``（本来就在终端里跑）
===========  ==========================================================

刻意分成两层：

* :func:`build_command` 是**纯函数** —— 给定平台与路径，返回该跑什么命令。
  它可以在任何操作系统上被单元测试，Windows 分支也能在 macOS 上被测到。
* :func:`run` 才真正执行，且只在目标平台上走对应分支。

安全边界：只覆盖 hosts 一个文件；命令里所有路径都经过引号处理；临时文件用完即删。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from .hosts import TARGET_HOST, HostsError, HostsFile, WritePlan


class ElevationCancelled(HostsError):
    """用户在 UAC / 授权弹窗上主动取消。GUI 靠它区分「失败」与「用户取消」。"""


class ElevationUnavailable(HostsError):
    """这台机器上没有可用的提权通道。"""


def platform_tag(platform: str | None = None) -> str:
    """把 ``sys.platform`` 归一成 windows / macos / linux / other。"""
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "macos"
    if plat.startswith("linux"):
        return "linux"
    return "other"


def already_elevated(platform: str | None = None) -> bool:
    """当前进程是否已经是管理员（Windows）/ root（POSIX）。"""
    if platform_tag(platform) == "windows":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:  # pragma: no cover - 非 POSIX
        return False


def build_command(
    plan: WritePlan, staged_path: str, *, platform: str | None = None
) -> list[str]:
    """构造把 ``staged_path`` 搬到 ``plan.path`` 的命令（纯函数，不执行）。

    POSIX 上是一条 ``sh -c``：写文件 + 刷 DNS 缓存。刷缓存失败不算失败
    （``|| true``）—— 落盘才是目的，缓存只是让改动立刻生效。
    """
    tag = platform_tag(platform)
    hosts = plan.path

    if tag == "windows":
        # PowerShell 而非 cmd：路径里可能有空格，且需要正确的退出码。
        ps = (
            f"Copy-Item -LiteralPath '{staged_path}' -Destination '{hosts}' -Force; "
            "ipconfig /flushdns | Out-Null"
        )
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ]

    flush = {
        "macos": "/usr/bin/dscacheutil -flushcache; "
                 "/usr/bin/killall -HUP mDNSResponder 2>/dev/null || true",
        "linux": "(resolvectl flush-caches || systemd-resolve --flush-caches "
                 "|| true) 2>/dev/null",
    }.get(tag, "")

    shell = f"/bin/cat {shlex.quote(staged_path)} > {shlex.quote(hosts)}"
    if flush:
        shell += f"; {flush}"
    shell += "; true"
    return ["/bin/sh", "-c", shell]


class ElevationResult:
    """一次提权写入的结果。"""

    __slots__ = ("ok", "stdout", "stderr", "cancelled")

    def __init__(
        self, ok: bool, stdout: str = "", stderr: str = "", cancelled: bool = False
    ) -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.cancelled = cancelled

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ElevationResult(ok={self.ok}, cancelled={self.cancelled}, stderr={self.stderr!r})"


# --------------------------------------------------------------------------
# Windows：ShellExecuteExW + runas（只弹一次 UAC）
# --------------------------------------------------------------------------
def _windows_runas(argv: list[str]) -> ElevationResult:
    """以管理员身份同步执行 argv[0] + argv[1:]，等待其结束。

    用 ``ShellExecuteExW`` 而不是 ``ShellExecuteW``：后者拿不到可靠的进程句柄
    与退出码，实践里会写成「调两次」——那会让用户看到两个 UAC 弹窗。
    """
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    ERROR_CANCELLED = 1223
    WAIT_TIMEOUT = 0x00000102

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else None
    info.nShow = SW_HIDE

    ok = shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return ElevationResult(ok=False, cancelled=True, stderr="UAC 被取消")
        return ElevationResult(ok=False, stderr=f"UAC 提权失败（错误码 {err}）")

    handle = info.hProcess
    if not handle:
        return ElevationResult(ok=False, stderr="未能取得提权进程句柄")
    try:
        rc = kernel32.WaitForSingleObject(handle, 600_000)
        if rc == WAIT_TIMEOUT:
            return ElevationResult(ok=False, stderr="提权进程超时（10 分钟）")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if code.value != 0:
            return ElevationResult(ok=False, stderr=f"提权进程退出码 {code.value}")
        return ElevationResult(ok=True, stdout="UAC 授权完成")
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def _run(argv: list[str], timeout: float = 600.0) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def run(plan: WritePlan, *, platform: str | None = None) -> ElevationResult:
    """执行写入：已提权就直接写，否则走原生授权通道。"""
    if plan.dry_run:
        return ElevationResult(ok=True, stdout="dry-run：未改动任何文件")

    tag = platform_tag(platform)

    if already_elevated(platform):
        from .hosts import apply_plan_as_root

        apply_plan_as_root(plan)
        return ElevationResult(ok=True, stdout="已是管理员/root：直接写入")

    from .hosts import stage_file

    staged = stage_file(plan.new_text)
    try:
        argv = build_command(plan, staged, platform=platform)

        if tag == "windows":
            return _windows_runas(argv)

        if tag == "macos":
            if not shutil.which("osascript"):
                raise ElevationUnavailable("找不到 osascript，无法弹出授权框")
            import json

            inner = " ".join(shlex.quote(a) for a in argv)
            script = (
                "do shell script " + json.dumps(inner) + " with administrator privileges"
            )
            code, out, err = _run(["osascript", "-e", script])
            if code != 0:
                if "-128" in err or "cancel" in err.lower():
                    return ElevationResult(ok=False, cancelled=True, stderr="授权被取消")
                return ElevationResult(ok=False, stderr=err.strip())
            return ElevationResult(ok=True, stdout=out)

        if tag == "linux":
            if shutil.which("pkexec"):
                code, out, err = _run(["pkexec", *argv])
                if code != 0 and ("dismissed" in err.lower() or code == 126):
                    return ElevationResult(ok=False, cancelled=True, stderr="授权被取消")
                return ElevationResult(ok=code == 0, stdout=out, stderr=err.strip())
            if shutil.which("sudo"):
                code, out, err = _run(["sudo", *argv])
                return ElevationResult(ok=code == 0, stdout=out, stderr=err.strip())
            raise ElevationUnavailable("需要管理员权限：请用 sudo 或以 root 运行")

        raise ElevationUnavailable(f"不支持的平台：{tag}")
    finally:
        try:
            os.unlink(staged)
        except OSError:  # pragma: no cover
            pass


def describe_channel(platform: str | None = None) -> str:
    """给用户看的一句话：这次会走哪个授权界面。"""
    tag = platform_tag(platform)
    if already_elevated(platform):
        return "已是管理员/root：直接写入"
    return {
        "windows": "Windows UAC 对话框",
        "macos": "macOS 系统授权弹窗",
        "linux": "pkexec / sudo",
    }.get(tag, "未知平台")


def verify(plan: WritePlan, *, platform: str | None = None) -> tuple[bool, str]:
    """写完回读原文件确认，按「计划里的期望值」比对。

    提权通道在 Windows 上拿不到子进程 stdout，所以验证不能靠命令输出，
    只能靠回读 —— 这也更接近用户真正关心的东西（文件到底改了没）。

    期望值来自 :class:`WritePlan.expected_ips`（唯一来源），所以这里不会出现
    「验证逻辑与写入逻辑各写一遍、迟早漂移」的问题。

    注意 ``changed=False`` 有两种含义，必须分开处理 —— 把它们混在一起会
    误报（真实踩过：用户重复点一次按钮，看到的却是「被安全软件还原」）：

    * 已经就是目标状态（``expected_ips`` 非空且已存在）→ 成功；
    * 本来就没有块、无需撤销（``expected_ips`` 为空）→ 成功；
    * 撤销跑完（``previous_ips`` 非空、``expected_ips`` 为空）→ 块必须消失。
    """
    try:
        after_ips = HostsFile.read(plan.path).pinned_ips()
    except HostsError as exc:  # pragma: no cover - IO 异常
        return False, f"回读失败：{exc}"

    if not plan.changed:
        if plan.expected_ips:
            if after_ips == plan.expected_ips:
                return True, f"无需改动：已是目标状态（{' '.join(after_ips)} {TARGET_HOST}）"
            return False, (
                f"未改动，但回读到 {after_ips or '（无块）'}，"
                f"与预期的 {plan.expected_ips} 不符 —— 可能被安全软件还原"
            )
        if after_ips:
            return False, f"本该没有加速块，回读却得到 {after_ips} —— 可能被安全软件还原"
        return True, "无需改动：本来就没有加速块"

    if after_ips != plan.expected_ips:
        return False, (
            f"回读与预期不符：{after_ips or '（空）'} != "
            f"{plan.expected_ips or '（空）'} —— 可能被安全软件拦截或还原"
        )
    if not plan.expected_ips:
        return True, "已撤销加速块（其余内容未动）"
    action = "已更新" if plan.previous_ips else "已生效"
    return True, f"{action}：{' '.join(after_ips)} {TARGET_HOST}"
