"""命令行入口：``eve-update-accelerator <命令>``。

命令一览::

    check      只测速并报告（只读，不改系统）
    apply      测速 → 写入 hosts（需要管理员/root，或 macOS 用 --gui 弹窗）
    revert     撤销写入（可加 --gui）
    bench      只测「当前默认路径」与「已钉住的地址」，用来确认现状
    doctor     诊断：基准资源、DNS 链、现役落点是否还能下内容

设计约定：**默认不动系统**。``check``/``doctor``/``bench`` 全是只读的，
只有 ``apply``/``revert`` 会写文件，而且写入前一定先备份、写入后一定回读验证。
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

from . import APP_NAME, __version__, evindex, hosts
from .elevate import describe_channel, platform_tag
from .scan import scan

BAR = "=" * 66


def _log_factory(quiet: bool):
    def log(message: str) -> None:
        if not quiet:
            print(message, flush=True)

    return log


def cmd_check(args: argparse.Namespace) -> int:
    """只测速：找出现在最快、并且真的能下内容的落点。"""
    log = _log_factory(args.quiet)
    previous = hosts.HostsFile.read().pinned_ips() if args.consider_pinned else []
    print(f"{APP_NAME} v{__version__}  只读检查（不会修改任何文件）")
    result = scan(
        top=args.top,
        conns=args.conns,
        previous_ips=previous,
        index=args.index,
        extra_ips=_parse_seeds(getattr(args, "seed", "")),
        log=log,
    )
    print(BAR)
    if result.bench:
        stale = "（内置兜底，可能已过期）" if result.bench.stale_risk else ""
        print(f"基准资源：{result.bench.path}  {result.bench.size / 1e6:.1f} MB {stale}")
    if not result.measured:
        print("没有测出任何可用落点。")
        if result.content_failed:
            print(
                f"注意：{len(result.content_failed)} 个落点能连通但取不到内容 —— "
                "这通常是 CDN 侧的问题，不是本机设置能修的。"
            )
        return 2
    best = result.best
    assert best is not None
    print(f"最快落点：{best.ip}   {best.mbps:.2f} MB/s   边缘机房 {best.colo or '?'}")
    if len(result.measured) > 1:
        worst = result.measured[-1]
        if worst.mbps > 0:
            print(f"对比最慢可用落点：{worst.ip} {worst.mbps:.2f} MB/s")
    ok, message = result.pinned_status()
    if result.previous_ips:
        print(("✔ " if ok else "✘ ") + message)
    print(BAR)
    print("要落盘生效：")
    print(f"  sudo {_prog()} apply          # 或用管理员/终端")
    if platform_tag() == "macos":
        print(f"  {_prog()} apply --gui        # 弹系统授权框，不用开终端")
    print("要撤销：")
    print(f"  {_prog()} revert" + (" --gui" if platform_tag() == "macos" else ""))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """测速 → 写入 hosts（先备份，写完回读验证）。"""
    log = _log_factory(args.quiet)
    previous = hosts.HostsFile.read().pinned_ips()
    print(f"{APP_NAME} v{__version__}  将修改系统 hosts")
    print(f"授权通道：{describe_channel()}")
    result = scan(
        top=args.top,
        conns=args.conns,
        previous_ips=previous,
        index=args.index,
        extra_ips=_parse_seeds(getattr(args, "seed", "")),
        log=log,
    )
    if not result.measured:
        print(BAR)
        print("没有测出可用落点，**不做任何修改**。")
        if result.content_failed:
            print(
                f"{len(result.content_failed)} 个落点可连通但取不到内容。"
                "这更像 CDN 侧故障；若你现在正被钉着，建议先 revert。"
            )
        return 2

    ips = result.to_pin()
    best = result.best
    assert best is not None
    print(BAR)
    print(f"将写入：{' '.join(ips)}  ->  {result.host}")
    print(f"实测最优：{best.ip}（{best.mbps:.2f} MB/s，{best.colo or '?'}）")

    plan = hosts.plan_apply(ips)
    if not plan.changed:
        print("hosts 已是目标状态，无需改动。")
        return 0
    if args.dry_run:
        print(plan.summary())
        print("（--dry-run：没有写入）")
        return 0

    from . import elevate

    outcome = elevate.run(plan)
    if not outcome.ok:
        if outcome.cancelled:
            print("已取消授权，未做任何修改。")
            return 1
        print(f"写入失败：{outcome.stderr}")
        return 1
    ok, message = elevate.verify(plan)
    print(("✔ " if ok else "✘ ") + message)
    if ok and plan.backup:
        print(f"原文件已备份到：{plan.backup}")
    print("\n重启 EVE 启动器即可生效（正在跑的话退出重开）。")
    print(f"撤销：{_prog()} revert" + (" --gui" if platform_tag() == "macos" else ""))
    return 0 if ok else 1


def cmd_revert(args: argparse.Namespace) -> int:
    """撤销：只摘掉标记块，其余内容原样保留。"""
    plan = hosts.plan_revert()
    if not plan.changed:
        print("hosts 里没有本工具写入的加速块，无需撤销。")
        return 0
    if args.dry_run:
        print(plan.summary())
        return 0
    from . import elevate

    print(f"授权通道：{describe_channel()}")
    outcome = elevate.run(plan)
    if not outcome.ok:
        if outcome.cancelled:
            print("已取消授权，未做任何修改。")
            return 1
        print(f"撤销失败：{outcome.stderr}")
        return 1
    ok, message = elevate.verify(plan)
    print(("✔ " if ok else "✘ ") + message)
    return 0 if ok else 1


def cmd_bench(args: argparse.Namespace) -> int:
    """对比「默认解析路径」与「已钉住的地址」—— 回答「到底有没有变快」。"""
    from .scan import bench_fetch, measure_throughput

    target = evindex.bench_target(args.index, log=print)
    pinned = hosts.HostsFile.read().pinned_ips()
    print(f"基准资源：{target.path}  {target.size / 1e6:.1f} MB\n")

    print("[默认路径] 让系统自己解析（含 hosts 影响）：")
    default_run = measure_throughput(
        "0.0.0.0", conns=args.conns, target=target, fetch=_default_fetch
    )
    print(f"  {default_run.mbps:.2f} MB/s（{default_run.ok_parts}/{default_run.parts} 片成功）")

    if pinned:
        print(f"\n[已钉住的地址] {' '.join(pinned)}：")
        for ip in pinned[:4]:
            run = measure_throughput(ip, conns=args.conns, target=target)
            print(f"  {ip:<16} {run.mbps:6.2f} MB/s（{run.ok_parts}/{run.parts} 片）")
    else:
        print("\nhosts 里没有钉任何地址（当前完全依赖 DNS 解析）。")
    del bench_fetch
    return 0


def _default_fetch(ip, start, end, *, host, timeout, slot, target=None):
    """不带 IP 覆盖的取数：让操作系统自己选地址。"""
    from .scan import BENCH_CHUNK, http_request

    del ip, slot, BENCH_CHUNK
    path = target.path if target is not None else "/"
    code, body, _ = http_request(
        f"https://{host}{path}",
        timeout=timeout,
        headers={"Range": f"bytes={start}-{end}"},
    )
    return len(body), code


def cmd_doctor(args: argparse.Namespace) -> int:
    """诊断：不测速，只回答「现在这套配置还能不能用」。"""
    from .scan import TARGET_HOST, probe_content, system_dns_ips

    print(f"{APP_NAME} v{__version__}  诊断")
    print(f"平台：{platform.platform()}")
    print(f"目标域名：{TARGET_HOST}")
    print(f"hosts 路径：{hosts.default_hosts_path()}")
    print(f"授权通道：{describe_channel()}")

    index = evindex.find_index(args.index)
    if index:
        print(f"启动器索引：{index}")
    else:
        print("启动器索引：未找到 —— 基准会退回内置（可能已过期）")
        print("  已查找以下位置（Windows 上的路径未经验证，若你知道实际位置请用 --index 指定）：")
        for candidate in evindex.candidate_paths(args.index):
            print(f"    {'存在' if os.path.exists(candidate) else '不存在'}  {candidate}")
    target = evindex.bench_target(args.index)
    print(f"基准资源：{target.path}  {target.size / 1e6:.1f} MB"
          f"{'（内置，可能过期）' if target.stale_risk else ''}")

    pinned = hosts.HostsFile.read().pinned_ips()
    print(f"当前钉住：{' '.join(pinned) if pinned else '（无）'}")
    dns_ips = system_dns_ips(TARGET_HOST)
    print(f"DNS 现在给出：{' '.join(dns_ips) if dns_ips else '（解析不到）'}")

    print("\n逐个地址实测（只认一个判据：能不能真的下到内容）：")
    for ip in _dedupe(pinned + dns_ips)[:8]:
        content = probe_content(ip, target)
        mark = "  ← 现役" if ip in pinned else ""
        if content.ok:
            print(
                f"  {ip:<16} OK  {content.mbps:5.2f} MB/s  "
                f"边缘 {content.colo or '?'}{mark}"
            )
        else:
            print(f"  {ip:<16} 取不到内容（{content.detail}）{mark}")

    ok = False
    if pinned:
        probe = probe_content(pinned[0], target)
        ok = bool(probe.ok)
        print(
            f"\n结论：现役落点 {pinned[0]} "
            + ("可以正常下载。" if ok else f"**已经取不到内容**（{probe.detail}）——建议 revert 或重跑 apply。")
        )
    else:
        probe = probe_content(dns_ips[0], target) if dns_ips else None
        if probe and probe.ok:
            print(f"\n结论：没钉任何地址，DNS 路径可用（{probe.mbps:.2f} MB/s）。")
        else:
            print("\n结论：没钉任何地址，DNS 路径也取不到内容 —— 更像 CDN 侧问题。")
    return 0 if ok or not pinned else 1


#: GUI 的显示名，Windows 上要 .pyw，所以入口要能区分。
def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _parse_seeds(text: str) -> list[str]:
    """把 ``--seed a,b`` 解析成合法 IPv4 列表（顺手丢掉空项与非法项）。"""
    import re as _re

    out = []
    for chunk in (text or "").replace(";", ",").split(","):
        item = chunk.strip()
        if item and _re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", item):
            out.append(item)
    return out


def _prog() -> str:
    """给用户复制的命令前缀。

    直接 ``python -m eveupdate`` 跑时 ``sys.argv[0]`` 是 ``…/__main__.py``，
    把那个长路径贴给用户既难看也没法直接用，所以这种情况统一回退到模块形式。
    """
    script = sys.argv[0] or ""
    if script.endswith("__main__.py") or not script:
        return "python -m eveupdate"
    if script.endswith(".py"):
        return f"python3 {script}"
    return "eve-update-accelerator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eve-update-accelerator",
        description="给 EVE 更新下载挑一条更好走的路（默认只读，apply 才改系统）",
        epilog="不带子命令时启动图形界面（需要 tkinter）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--lang", choices=("zh", "en"), default=None, help="界面语言（默认跟随系统）")
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--top", type=int, default=6, help="对前 N 个候选做吞吐测试（默认 6）")
        p.add_argument("--conns", type=int, default=10, help="吞吐测试并发数（默认 10，对齐启动器）")
        p.add_argument("--index", default=None, help="启动器索引路径或 SharedCache 目录")
        p.add_argument(
            "--seed",
            default="",
            help="额外候选地址，逗号分隔（企业内网/自建镜像等场景）",
        )
        p.add_argument("-q", "--quiet", action="store_true", help="少输出")

    p_check = sub.add_parser("check", help="只测速并报告（只读）")
    common(p_check)
    p_check.add_argument("--consider-pinned", action="store_true",
                         help="把 hosts 里已钉的地址也纳入对比（默认也纳入）")
    p_check.set_defaults(func=cmd_check, consider_pinned=True)

    p_apply = sub.add_parser("apply", help="测速并写入 hosts（需要管理员权限）")
    common(p_apply)
    p_apply.add_argument("--gui", action="store_true", help="macOS：弹系统授权框，不用 sudo")
    p_apply.add_argument("--dry-run", action="store_true", help="只显示将要写入什么")
    p_apply.set_defaults(func=cmd_apply)

    p_revert = sub.add_parser("revert", help="撤销写入")
    p_revert.add_argument("--gui", action="store_true", help="macOS：弹系统授权框")
    p_revert.add_argument("--dry-run", action="store_true", help="只显示将要做什么")
    p_revert.set_defaults(func=cmd_revert)

    p_bench = sub.add_parser("bench", help="对比默认路径与已钉地址的吞吐（只读）")
    common(p_bench)
    p_bench.set_defaults(func=cmd_bench)

    p_doctor = sub.add_parser("doctor", help="诊断现有配置还能不能用（只读）")
    p_doctor.add_argument("--index", default=None, help="启动器索引路径")
    p_doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 没有子命令 → 尝试 GUI；tkinter 缺失时给出明确指引而不是栈回溯。
        try:
            from .gui import run_gui
        except ImportError as exc:  # pragma: no cover - 取决于环境
            print(f"无法加载图形界面（{exc}）。请用子命令，例如：")
            print("  eve-update-accelerator check")
            print("  eve-update-accelerator apply --gui")
            return 1
        return run_gui()
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断。")
        return 130
    except hosts.HostsError as exc:
        print(f"hosts 操作失败：{exc}")
        return 1
