"""跨平台的 hosts 文件读写：标记块、备份、幂等更新。

这个模块只关心**文本**，不负责提权 —— 提权在 :mod:`eveupdate.elevate`。
分开的理由：文本逻辑（块识别、幂等、撤销、EOL 保留）是纯函数，可以在任何
平台上被单元测试钉死；提权才是真正跟操作系统绑定的那部分。

几个刻意为之的细节：

* **只在首次写入时建备份**。如果每次 apply 都覆盖备份，那第二次之后备份里
  存的就是「已经改过」的内容，等于没有备份。
* **保留原有 EOL**。Windows 的 hosts 常年是 CRLF，Linux/macOS 是 LF。
  用 ``newline=""`` 读、按检测到的风格写，免得整份文件被无声改写。
* **撤销要留白得体**。摘掉块之后不能留下三个连续空行 —— 那是「被工具改过」
  的痕迹，用户会以为文件坏了。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

#: 标记块起止。撤销只依赖这两行，所以它们必须稳定 —— 一旦改动，
#: 老用户文件里的块会认不出来（因此这里不做任何「智能兼容」）。
MARK_BEGIN = "# >>> eve-update-accelerator >>>"
MARK_END = "# <<< eve-update-accelerator <<<"

#: 被加速的域名。只钉补丁下载那一条：启动器页面/登录不走它。
TARGET_HOST = "binaries.eveonline.com"

#: 备份文件名后缀。放在 hosts 同目录，用户一眼能找到。
BACKUP_SUFFIX = ".eve-update-accelerator.bak"

WINDOWS_HOSTS = r"C:\Windows\System32\drivers\etc\hosts"


class HostsError(RuntimeError):
    """读写 hosts 失败（权限、路径不存在、内容异常）。"""


def default_hosts_path(platform: str | None = None) -> str:
    """当前平台的 hosts 路径。``platform`` 可注入，便于在测试里模拟 Windows。"""
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.join(system_root, "System32", "drivers", "etc", "hosts")
    return "/etc/hosts"


@dataclass
class HostsFile:
    """一份 hosts 的内容，连同它的风格（EOL、编码）。"""

    path: str
    raw: str
    eol: str = "\n"
    encoding: str = "utf-8"
    #: 读到过 BOM 的话，写回时也带上 —— 有些 Windows 工具会读它。
    had_bom: bool = False
    exists: bool = True

    # -- 读 ---------------------------------------------------------------
    @classmethod
    def read(cls, path: str | None = None) -> "HostsFile":
        target = path or default_hosts_path()
        if not os.path.exists(target):
            # 不存在不是错误：给出一个可写的空骨架，apply 会创建它。
            return cls(path=target, raw="", eol="\n", exists=False)

        # BOM 必须在**解码前**从字节层判断。用 utf-8-sig 解码虽然也能吃 BOM，
        # 但那样字符串里就再也看不到它了 —— 曾经的写法是「用 utf-8-sig 读，
        # 再 startswith('\ufeff') 检测」，这个条件永远为假，had_bom 从来没过；
        # 结果写回时 BOM 被静默丢掉（Windows 上有些老工具会因此读不了）。
        try:
            with open(target, "rb") as fh:
                head = fh.read(3)
        except OSError as exc:  # pragma: no cover - 权限等
            raise HostsError(f"读不了 {target}：{exc}") from exc
        had_bom = head == b"\xef\xbb\xbf"
        encoding = "utf-8-sig" if had_bom else "utf-8"

        try:
            with open(target, "r", encoding=encoding, newline="") as fh:
                raw = fh.read()
        except UnicodeDecodeError:
            # latin-1 兜底：Windows 上历史遗留的 hosts 可能是本地代码页。
            # 它不会抛异常，所以「读得出来」优先，不做猜测性转换。
            with open(target, "r", encoding="latin-1", newline="") as fh:
                raw = fh.read()
            encoding = "latin-1"
        except OSError as exc:  # pragma: no cover
            raise HostsError(f"读不了 {target}：{exc}") from exc

        eol = "\r\n" if "\r\n" in raw else "\n"
        return cls(
            path=target,
            raw=raw,
            eol=eol,
            encoding=encoding,
            had_bom=had_bom,
        )

    # -- 规范化 -----------------------------------------------------------
    @property
    def normalized(self) -> str:
        """把内容按 ``\\n`` 统一，方便做行级处理。"""
        return self.raw.replace("\r\n", "\n").replace("\r", "\n")

    def render(self, text_lf: str) -> str:
        """把 LF 文本按本文件的 EOL 风格还原，并处理 BOM。"""
        body = text_lf.replace("\r\n", "\n")
        if self.eol != "\n":
            body = body.replace("\n", self.eol)
        return ("\ufeff" if self.had_bom else "") + body

    # -- 块 ---------------------------------------------------------------
    def block(self) -> str | None:
        """取出标记块（不含首尾换行的语义，原样返回）。"""
        m = re.search(
            re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END),
            self.normalized,
            re.S,
        )
        return m.group(0) if m else None

    def pinned_ips(self) -> list[str]:
        """块里钉住的 IP，按出现顺序。宽容解析：手改过也算认。"""
        block = self.block()
        if not block:
            return []
        for line in block.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) >= 2 and any(
                p.lower().rstrip(".") == TARGET_HOST for p in parts[1:]
            ):
                return [p for p in parts[:-1] if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p)]
        return []

    def without_block(self) -> str:
        """摘掉标记块，不留多余空行。"""
        block = self.block()
        if not block:
            return self.normalized
        text = self.normalized.replace(block, "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.rstrip("\n") + "\n"

    def with_block(self, block_text: str) -> str:
        """替换或追加标记块（幂等：替换后再写一次结果不变）。"""
        existing = self.block()
        if existing:
            return self.normalized.replace(existing, block_text)
        body = self.normalized.rstrip("\n")
        if not body:
            return block_text + "\n"
        return body + "\n\n" + block_text + "\n"


def build_block(
    ips: list[str],
    *,
    host: str = TARGET_HOST,
    note: str = "",
    tool_hint: str = "eve-update-accelerator revert",
    now: float | None = None,
) -> str:
    """生成标记块。第一个 IP 是实测最优，其余作为附加候选。

    为什么要把多个 IP 写在一行：hosts 里一条记录可以给多个地址，客户端会把
    它们都当作候选。实测（macOS）**hosts 并不保证优先**，它只是把「已知不可达
    的地址」替换成「实测可用且更快的地址」—— 这才是它的真实作用，所以把几个
    好地址一起挂上比只挂一个更划算。
    """
    good, rest = ips[0], [ip for ip in ips[1:] if ip != ips[0]]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [MARK_BEGIN]
    detail = f" (+{len(rest)} more measured-good)" if rest else ""
    lines.append(
        f"# EVE update accelerator: prefer measured-good endpoints for {host}{detail}"
    )
    lines.append(
        "# hosts only *adds candidates*; it cannot outrank DNS. Revert with the tool below."
    )
    if note:
        lines.append(f"# {note}")
    lines.append(f"# generated {stamp}; revert with: {tool_hint}")
    lines.append(" ".join([good] + rest) + f" {host}")
    lines.append(MARK_END)
    return "\n".join(lines)


def backup_path(hosts_path: str) -> str:
    return hosts_path + BACKUP_SUFFIX


def ensure_backup(hosts_path: str, *, dry_run: bool = False) -> str | None:
    """首次写入前建备份。已存在就**不动它**，返回 None。"""
    target = backup_path(hosts_path)
    if os.path.exists(target) or dry_run:
        return None
    if not os.path.exists(hosts_path):
        return None
    shutil.copy2(hosts_path, target)
    return target


@dataclass
class WritePlan:
    """一次 hosts 修改的完整计划。可以在真正落盘前被检查、被测试。"""

    path: str
    new_text: str
    hosts: HostsFile
    backup: str | None = None
    dry_run: bool = False
    changed: bool = True
    #: 落盘之后，标记块里**应当**出现的 IP（撤销时为空）。验证阶段只用它比对，
    #: 不再二次解析计划文本 —— 期望值只有一个来源。
    expected_ips: list[str] = field(default_factory=list)
    #: 计划之前块里已有的 IP。空表示「之前没有块」。
    previous_ips: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def action(self) -> str:
        if not self.changed:
            return "noop"
        if not self.expected_ips:
            return "revert"
        return "update" if self.previous_ips else "install"

    def summary(self) -> str:
        label = {"install": "install", "update": "update", "revert": "revert",
                 "noop": "no change"}[self.action]
        prefix = "[dry-run] would " if self.dry_run else ""
        if self.action == "noop":
            return f"{prefix}no change needed for {self.path}"
        return f"{prefix}{label} {self.path} (backup: {self.backup or 'n/a'})"


def plan_apply(
    ips: list[str], *, hosts_path: str | None = None, dry_run: bool = False
) -> WritePlan:
    """算出「把 ips 钉进去」之后的完整文件内容，不落盘。"""
    cleaned: list[str] = []
    for ip in ips:
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) and ip not in cleaned:
            cleaned.append(ip)
    if not cleaned:
        raise HostsError("没有合法的 IPv4 地址可写入")
    hosts = HostsFile.read(hosts_path)
    previous = hosts.pinned_ips()
    new_lf = hosts.with_block(build_block(cleaned))
    return WritePlan(
        path=hosts.path,
        new_text=hosts.render(new_lf),
        hosts=hosts,
        backup=backup_path(hosts.path),
        dry_run=dry_run,
        changed=previous != cleaned,
        expected_ips=list(cleaned),
        previous_ips=previous,
    )


def plan_revert(*, hosts_path: str | None = None, dry_run: bool = False) -> WritePlan:
    """算出「撤销加速块」之后的完整文件内容，不落盘。"""
    hosts = HostsFile.read(hosts_path)
    previous = hosts.pinned_ips()
    if hosts.block() is None:
        return WritePlan(
            path=hosts.path,
            new_text=hosts.render(hosts.normalized),
            hosts=hosts,
            backup=None,
            dry_run=dry_run,
            changed=False,
            expected_ips=[],
            previous_ips=[],
            notes=["no accelerator block found; nothing to revert"],
        )
    return WritePlan(
        path=hosts.path,
        new_text=hosts.render(hosts.without_block()),
        hosts=hosts,
        backup=None,
        dry_run=dry_run,
        changed=True,
        expected_ips=[],
        previous_ips=previous,
    )


def stage_file(text: str) -> str:
    """把内容写到临时文件，返回路径。由提权后的命令负责搬过去。

    先落临时文件再 `cat > hosts`，而不是直接以 root 打开 hosts ——
    这样「写坏原文件」的窗口最短（原文件只经历一次覆盖，不做原地改）。
    """
    fd, path = tempfile.mkstemp(prefix="eve-update-accel-", suffix=".hosts")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.chmod(path, 0o644)
    return path


def apply_plan_as_root(plan: WritePlan) -> None:
    """已经是 root 时的落盘路径（提权由调用方负责）。"""
    if plan.dry_run:
        return
    ensure_backup(plan.path)
    directory = os.path.dirname(plan.path) or "."
    tmp = tempfile.NamedTemporaryFile(
        "w", delete=False, dir=directory, encoding=plan.hosts.encoding,
        newline="", prefix=".eve-update-accel-",
    )
    try:
        tmp.write(plan.new_text)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        os.replace(tmp.name, plan.path)
    finally:
        if os.path.exists(tmp.name):  # pragma: no cover - 失败清理
            os.unlink(tmp.name)
