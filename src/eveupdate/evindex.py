"""从 EVE 启动器的本地索引里取**真实的、当前有效**的资源来做基准。

为什么不用硬编码 URL：第一版工具把基准写成
``/bundles/3542233/eveonlinemacOS_3542233.txt.bundle.6``。两天后 CCP 发了新
build，旧 bundle 从 CDN 上消失 —— 于是"测速"测到的是 **530 Origin DNS error**，
看起来像"所有落点都死了"。**基准 URL 会腐烂**，这是设计缺陷，不是 bug。

启动器自己在本地维护一份资源索引（``index_tranquility.txt``）：每行是

    本地路径,CDN 资源路径,校验和,压缩后大小,……

这些路径就是它**此刻真的会去下载**的东西，所以拿它当基准既不会腐烂，也天然
和用户实际走的链路一致。索引缺失时退回内置路径，并明确告知「基准可能已过期」。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

#: 索引文件名与它所在目录名（各平台一致）。
INDEX_NAME = "index_tranquility.txt"
CACHE_DIR_NAME = "SharedCache"

#: 候选索引路径。用户也可以用 ``--index`` 直接指定。
#:
#: ⚠️ Windows 这几个路径**没有在真实 Windows 上验证过**（开发机是 macOS）：
#: 共享缓存位置随安装方式（默认 / 自定义目录 / 便携版）变化。所以：
#: 1. 这里给多个候选尽力覆盖；
#: 2. 找不到索引时会**退回内置基准并明确标注「可能已过期」**，不会静默用错；
#: 3. ``doctor`` 会打印实际查过哪些路径，找不到时请把它贴到 issue 里。
WINDOWS_CANDIDATES = (
    r"%LOCALAPPDATA%\CCP\EVE\SharedCache",
    r"%LOCALAPPDATA%\CCP\EVE\SharedCache\tq",
    r"%PROGRAMDATA%\CCP\EVE\SharedCache",
    r"%PROGRAMFILES%\CCP\EVE\SharedCache",
    r"%PROGRAMFILES(X86)%\CCP\EVE\SharedCache",
    r"%USERPROFILE%\Documents\EVE\SharedCache",
    r"%USERPROFILE%\EVE\SharedCache",
)
MACOS_CANDIDATES = (
    "~/Library/Application Support/EVE Online/SharedCache",
)
LINUX_CANDIDATES = (
    "~/.eve/SharedCache",
    "~/EVE/SharedCache",
)

#: 基准资源的最小体积。太小的文件测不出带宽（一个 RTT 就拉完了）。
MIN_BENCH_BYTES = 2 << 20  # 2 MiB


@dataclass
class BenchTarget:
    """一个基准资源。"""

    path: str          #: CDN 上的路径，例如 /42/426a084d…_319ba304…
    size: int          #: 压缩后大小（索引里记录的字节数）
    source: str        #: 来源说明：本地索引路径 / builtin

    @property
    def stale_risk(self) -> bool:
        return self.source == "builtin"


def cache_dirs(platform: str | None = None) -> list[str]:
    """各平台可能的共享缓存目录（已展开环境变量，未去重）。"""
    tag = (platform or sys.platform).lower()
    if tag.startswith("win"):
        raw = WINDOWS_CANDIDATES
    elif tag == "darwin":
        raw = MACOS_CANDIDATES
    else:
        raw = LINUX_CANDIDATES
    out: list[str] = []
    for item in raw:
        expanded = os.path.expandvars(os.path.expanduser(item))
        if expanded not in out:
            out.append(expanded)
    return out


def candidate_paths(
    explicit: str | None = None, *, platform: str | None = None
) -> list[str]:
    """会去查的所有索引路径（含用户指定的那个）。

    单独暴露出来是为了让诊断能说清「我查过哪儿」—— 找不到索引时这是用户
    唯一能提供给我们、且我们无法在自己机器上复现的信息（尤其 Windows）。
    """
    if explicit:
        if os.path.isdir(explicit):
            return [os.path.join(explicit, INDEX_NAME)]
        return [explicit]
    return [os.path.join(directory, INDEX_NAME) for directory in cache_dirs(platform)]


def find_index(explicit: str | None = None, *, platform: str | None = None) -> str | None:
    """找到索引文件。``explicit`` 可以是文件或目录。"""
    for candidate in candidate_paths(explicit, platform=platform):
        if os.path.exists(candidate):
            return candidate
    return None


def parse_index_line(line: str) -> tuple[str, int] | None:
    """解析一行索引，返回 ``(CDN 路径, 大小)``。

    格式（实测）::

        app:/EVE.app/Contents/Info.plist,a3/a374381be814f746_…,<sha>,841,420,33188

    第 2 列是 CDN 相对路径（无前导斜杠），第 4 列是压缩后大小。
    解析不了的行**跳过而不是猜** —— 猜错会让基准指向 404。
    """
    parts = line.rstrip("\n").split(",")
    if len(parts) < 4:
        return None
    rel = parts[1].strip()
    if not rel or rel.startswith("#"):
        return None
    # CDN 路径形如 ab/abcdef…_…，只认这种两段式，避免把本地路径当资源
    if rel.count("/") != 1:
        return None
    try:
        size = int(parts[3])
    except ValueError:
        return None
    if size <= 0:
        return None
    return "/" + rel, size


def biggest_resource(
    index_path: str, *, min_bytes: int = MIN_BENCH_BYTES
) -> BenchTarget | None:
    """索引里最大的那个资源 —— 用它测吞吐最能反映真实带宽。

    流式读取：索引在完整客户端上可以有几十万行、几十 MB，不能整体读进内存。
    """
    best: tuple[int, str] | None = None
    try:
        with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parsed = parse_index_line(line)
                if not parsed:
                    continue
                path, size = parsed
                if best is None or size > best[0]:
                    best = (size, path)
    except OSError:
        return None
    if best is None or best[0] < min_bytes:
        return None
    return BenchTarget(path=best[1], size=best[0], source=index_path)


#: 兜底基准：索引找不到时用。**可能已过期**，所以会明确标注。
BUILTIN_BENCH = BenchTarget(
    path="/bundles/3542233/eveonlinemacOS_3542233.txt.bundle.6",
    size=10 << 20,
    source="builtin",
)


def bench_target(
    explicit_index: str | None = None, *, platform: str | None = None, log=None
) -> BenchTarget:
    """取基准资源：优先本地索引，其次内置兜底。"""
    index = find_index(explicit_index, platform=platform)
    if index:
        target = biggest_resource(index)
        if target:
            if log:
                log(
                    f"  基准资源取自启动器索引：{target.size / 1e6:.1f} MB "
                    f"（{index}）"
                )
            return target
        if log:
            log(f"  索引存在但没找到够大的资源，退回内置基准：{index}")
    else:
        if log:
            log(
                "  没找到启动器索引（SharedCache/index_tranquility.txt）——"
                "退回内置基准，它可能已过期"
            )
    return BUILTIN_BENCH
