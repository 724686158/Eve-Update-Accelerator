"""测速引擎：找出当前网络下**可达、能下内容、而且最快**的落点。

EVE 的补丁下载走 ``https://binaries.eveonline.com``，背后是哪家 CDN **会变**：
2026-10 之前实测在 Cloudflare 上（证书 ``*.eveonline.com`` / DigiCert），
10-06 实测已切到 CloudFront（证书 ``CN=binaries.eveonline.com`` / Amazon），
同一批 Cloudflare 地址全部返回 ``530 Origin DNS error``。所以这个工具**不假设
CDN 是谁**，只用 DNS 解析链给出的地址 + 一批通用种子去实测。

三步，全部用真实 HTTP：

1. **收集候选**：公共 DoH + 系统解析器（按解析链过滤，避免混入无关后端）+
   一批常见 CDN 网段样本；
2. **连通性探活**：带真实 SNI/Host 请求 ``/cdn-cgi/trace``（Cloudflare 端点，
   只用来判断"连得上"），记录耗时与边缘机房代号；
3. **内容校验 + 真实吞吐**：从启动器本地索引取一个**真实存在的资源**，先下
   一小段确认真的能拿到（这一步拦住了「握手正常但内容 530」的坑，实测踩过），
   再并发拉 10 × 1MiB 算吞吐。**不用 ping、也不拿几十字节的 trace 当基准**。

排序与汇总逻辑全部可注入：``doh_fetch``（DNS 层）与 ``bench``（HTTP 层，
内容校验与吞吐共用），所以没有网络也能把决策逻辑测死。
"""

from __future__ import annotations

import concurrent.futures as futures
import http.client
import json
import random
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .evindex import BUILTIN_BENCH, BenchTarget, bench_target

TARGET_HOST = "binaries.eveonline.com"

#: 每片 1 MiB。并发分片既是为了压满带宽，也是为了对齐启动器的 10 并发。
BENCH_CHUNK = 1 << 20
BENCH_SLOTS = 10

USER_AGENT = (
    "eve-update-accelerator/1.2 "
    "(+https://github.com/724686158/Eve-Update-Accelerator)"
)

DOH_ENDPOINTS = (
    "https://223.5.5.5/resolve",
    "https://1.1.1.1/dns-query",
    "https://dns.google/resolve",
)

UDP_RESOLVERS = ("223.5.5.5", "119.29.29.29", "180.76.76.76", "114.114.114.114")

#: 常见 CDN 网段样本（Cloudflare 各段 + CloudFront 常见段）。
#: 存在的理由：DNS 有时只给一两个地址（甚至给的是坏地址），而这些段里往往
#: 有更快或更可用的落点。它不是完整列表 —— DNS 结果同样会补进来，两边都不可省。
SEED_ADDRESSES = (
    "173.245.48.1", "103.21.244.1", "103.22.200.1", "103.31.4.1",
    "141.101.64.1", "108.162.192.1", "190.93.240.1", "188.114.96.1",
    "197.234.240.1", "198.41.128.1", "162.158.0.1", "104.16.0.1",
    "104.17.0.1", "104.18.0.1", "104.19.0.1", "104.20.0.1", "104.21.0.1",
    "104.22.0.1", "104.23.0.1", "104.24.0.1", "104.25.0.1", "104.26.0.1",
    "104.27.0.1", "172.64.0.1", "172.65.0.1", "172.66.0.1", "172.67.0.1",
    "162.159.128.1", "162.159.140.1", "173.245.58.1",
    # CloudFront（EVE 2026-10 起实际使用的后端）
    "18.65.14.1", "18.65.168.1", "108.138.246.1", "13.226.69.1",
    "13.249.74.1", "3.165.39.1", "18.173.121.1",
)

IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


# --------------------------------------------------------------------------
# HTTP：能把域名钉到指定 IP，同时保持 SNI 与 Host 不变
# --------------------------------------------------------------------------
class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到 ``connect_ip``，但握手用原域名做 SNI、Host 头也保持原样。

    这一点是整个工具的地基：探活必须走**真实的 CDN 路径**（SNI + Host 都要对），
    只是把「解析到哪个 IP」换掉。直接把 URL 改成 IP 会连不上 Cloudflare，
    也测不出真东西。
    """

    def __init__(self, host: str, connect_ip: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._connect_ip = connect_ip

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:  # pragma: no cover - 不使用代理
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def http_request(
    url: str,
    *,
    timeout: float = 8.0,
    headers: dict[str, str] | None = None,
    resolve_ip: str | None = None,
    method: str = "GET",
    read_limit: int | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """发一次 HTTP(S) 请求，返回 ``(状态码, 正文, 响应头)``。

    只用标准库，且不用 ``urlopen`` 的全局 opener —— 它没有「指定解析结果」
    的口子，想做到就得改全局 socket，那会污染同进程里的其它请求（本项目是
    多线程并发探测，绝不能这么干）。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"不支持的协议：{parts.scheme}")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query

    send_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"}
    send_headers.update(headers or {})
    # Host 要显式给：我们连的是 IP，但对端必须看到域名。
    send_headers.setdefault("Host", host if port in (80, 443) else f"{host}:{port}")

    if parts.scheme == "https":
        context = ssl.create_default_context()
        if resolve_ip:
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host, resolve_ip, port=port, timeout=timeout, context=context
            )
        else:
            conn = http.client.HTTPSConnection(
                host, port=port, timeout=timeout, context=context
            )
    else:
        conn = http.client.HTTPConnection(
            resolve_ip or host, port=port, timeout=timeout
        )

    try:
        conn.request(method, target, headers=send_headers)
        resp = conn.getresponse()
        body = resp.read(read_limit) if read_limit else resp.read()
        return resp.status, body, {k.lower(): v for k, v in resp.getheaders()}
    except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
        raise exc
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# DNS：DoH + 系统解析器
# --------------------------------------------------------------------------
def parse_doh_addresses(
    payload: bytes | str, *, host: str | None = None
) -> list[str]:
    """从 DoH JSON 里取 A 记录。

    ``host`` 给定时**按名字过滤**：DoH 返回的是一整条解析链
    （``binaries.eveonline.com`` → CNAME → ``xxx.cloudfront.net`` → A），
    不过滤就会把 CDN 后端的地址混进候选池 —— 那些地址用本域名的 SNI 去打是
    打不通的（实测过：混进来的 CloudFront 地址全部 403）。所以只认
    「主机名等于原域名或它的别名（CNAME 目标）」的那些 A 记录。
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    # DoH 返回的顶层必须是对象；有的解析器出错时会回一个数组，那属于异常响应。
    if not isinstance(data, dict):
        return []
    answers = data.get("Answer") or []
    if not isinstance(answers, list):
        return []
    allowed: set[str] = set()
    if host:
        canonical = host.rstrip(".")
        allowed.add(canonical.lower())
        for answer in answers:
            if str(answer.get("type")) == "5":  # CNAME
                target = str(answer.get("data") or "").strip().rstrip(".").lower()
                if target:
                    allowed.add(target)
    out: list[str] = []
    for answer in answers:
        if str(answer.get("type")) != "1":
            continue
        if host and str(answer.get("name") or "").rstrip(".").lower() not in allowed:
            continue
        addr = str(answer.get("data") or "").strip()
        if IPV4_RE.fullmatch(addr):
            out.append(addr)
    return out


def resolver_command(
    server: str, host: str, *, platform: str | None = None
) -> list[str]:
    """系统解析器查询命令（纯函数，跨平台可测）。

    Windows 只有 ``nslookup``；POSIX 优先 ``dig``，没有就退回 ``nslookup``。
    DoH 才是主力，这条只是补充 —— 命令不存在不算错误。
    """
    tag = (platform or _platform_name()).lower()
    if tag.startswith("win"):
        return ["nslookup", "-type=A", host, server]
    dig = shutil.which("dig")
    if dig:
        return [dig, "+short", "+time=3", "+tries=1", "@" + server, host]
    return ["nslookup", "-type=A", host, server]


def _platform_name() -> str:
    import platform as _p

    return _p.system()


def system_dns_ips(host: str = TARGET_HOST) -> list[str]:
    """系统解析器现在会给的 IPv4（用于识别「死地址」）。"""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    return _dedupe([info[4][0] for info in infos])


def collect_candidates(
    host: str = TARGET_HOST,
    *,
    doh_fetch=None,
    resolvers: tuple[str, ...] = UDP_RESOLVERS,
    seeds: tuple[str, ...] = SEED_ADDRESSES,
    extra: tuple[str, ...] | list[str] = (),
    timeout: float = 8.0,
    log=None,
) -> list[str]:
    """收集候选：DoH + 系统解析器 + 网段样本。

    顺序有意义：DNS 结果在前（更可能是本网络真正会走的地址），网段样本打散
    在后。实测最优落点经常**不在** DNS 返回的那几个里，所以两路都不能省。
    """
    fetch = doh_fetch or http_request

    def doh(url: str) -> list[str]:
        try:
            code, body, _ = fetch(
                f"{url}?name={host}&type=A",
                timeout=timeout,
                headers={"accept": "application/dns-json"},
            )
        except Exception:
            return []
        if code != 200:
            return []
        return parse_doh_addresses(body, host=host)

    def udp(server: str) -> list[str]:
        cmd = resolver_command(server, host)
        if not shutil.which(cmd[0]):
            return []
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return []
        return IPV4_RE.findall(proc.stdout.decode("utf-8", "replace"))

    found: list[str] = []
    workers = len(DOH_ENDPOINTS) + len(resolvers)
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        jobs = [ex.submit(doh, url) for url in DOH_ENDPOINTS]
        jobs += [ex.submit(udp, srv) for srv in resolvers]
        for job in jobs:
            try:
                found.extend(job.result())
            except Exception:
                pass

    dns_ips = _dedupe(found)
    shuffled = list(seeds)
    random.shuffle(shuffled)
    # extra 排在最前：那是调用方明确指定的候选（--seed / 企业内网地址 /
    # 测试注入），比随机网段样本更该被优先测到。
    ordered = _dedupe(list(extra)) + dns_ips + [
        ip for ip in shuffled if ip not in set(dns_ips)
    ]
    ordered = _dedupe(ordered)
    if log:
        log(f"  候选 {len(ordered)} 个（DNS/解析器结果 {len(dns_ips)} + 网段样本）")
    return ordered


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------
# 内容校验与吞吐
# --------------------------------------------------------------------------
@dataclass
class ContentProbe:
    """一个落点能不能真的下到内容 —— 工具里唯一有意义的判据。"""

    ip: str
    ok: bool = False
    status: int = 0
    recv_bytes: int = 0
    seconds: float = 0.0
    #: 边缘机房代号（CloudFront 的 x-amz-cf-pop / Cloudflare 的 cf-ray 后缀）
    colo: str = ""
    detail: str = ""

    @property
    def mbps(self) -> float:
        """MB/s（十进制，与启动器进度条口径一致）。"""
        if self.seconds <= 0:
            return 0.0
        return self.recv_bytes / self.seconds / 1_000_000


def bench_fetch(
    ip: str,
    start: int,
    end: int,
    *,
    host: str = TARGET_HOST,
    timeout: float = 25.0,
    slot: int = 0,
    target: BenchTarget | None = None,
) -> tuple[int, int, dict[str, str]]:
    """拉基准资源的一段 Range，返回 ``(字节数, 状态码, 响应头)``。

    基准来自启动器的本地索引（:mod:`evindex`），所以路径是**当前真实存在**的。
    ``slot`` 只在退回内置兜底基准时有意义：那是一组分片文件，按 slot 换文件名，
    免得 10 个并发连接读同一个文件的同一段（那样测的是缓存而不是带宽）。
    """
    if target is not None:
        path = target.path
    else:
        path = BUILTIN_BENCH.path
        if slot:
            path = f"{path.rsplit('.', 1)[0]}.{slot % BENCH_SLOTS}"
    code, body, headers = http_request(
        f"https://{host}{path}",
        timeout=timeout,
        resolve_ip=ip,
        headers={"Range": f"bytes={start}-{end}"},
    )
    return len(body), code, headers


def probe_content(
    ip: str,
    target,
    *,
    host: str = TARGET_HOST,
    timeout: float = 20.0,
    bench=None,
    probe_bytes: int = 128 << 10,
) -> ContentProbe:
    """校验这个落点**真的能下内容**。

    这是整个工具里唯一有意义的判据，理由是两个实测教训：

    * 曾经用 Cloudflare 专有的 ``/cdn-cgi/trace`` 当探活端点，CCP 把后端换成
      CloudFront 后它返回 403 —— 7 个完全可用的地址被判成「连不上」；
    * 反过来，同一个 IP 上 ``trace`` 返回 200，而资源路径返回
      ``530 Origin DNS error`` —— 只测 trace 又会把死落点判成「可用」。

    所以这里是**先下 128 KiB 真实数据**，拿到字节才算可用。顺带还能量出速率。

    ``bench`` 注入点与 :func:`measure_throughput` 完全一致：一个返回
    ``(字节数, 状态码, 响应头)`` 的 HTTP 层函数。**这一点必须说清** ——
    曾经把「返回 ContentProbe 的高层假函数」注进来，结果在
    ``len(result)`` 上炸成 TypeError，查了很久。
    """
    fetch = bench or bench_fetch
    started = time.perf_counter()
    try:
        got = fetch(
            ip, 0, probe_bytes - 1, host=host, timeout=timeout, target=target
        )
    except Exception as exc:
        return ContentProbe(ip=ip, ok=False, detail=f"{type(exc).__name__}")
    elapsed = max(time.perf_counter() - started, 1e-6)
    size, code, headers = _unpack_fetch(got)
    ok = 200 <= code < 300 and size > 0
    detail = "" if ok else (f"HTTP {code}" if code else "无响应")
    return ContentProbe(
        ip=ip,
        ok=ok,
        status=code,
        recv_bytes=size,
        seconds=elapsed,
        colo=edge_from_headers(headers),
        detail=detail,
    )


def _unpack_fetch(result):
    """兼容 ``(bytes, code)`` 与 ``(bytes, code, headers)`` 两种返回。"""
    if len(result) == 3:
        return result[0], result[1], result[2] or {}
    return result[0], result[1], {}


def edge_from_headers(headers: dict[str, str] | None) -> str:
    """从响应头里读边缘机房代号。

    CloudFront 给 ``x-amz-cf-pop``（如 ``LAX50``），Cloudflare 给 ``cf-ray``
    后缀（如 ``8f3a...-LAX``）。两家命名不同，但都能告诉我们"走的是哪个口"，
    这正是用户排障时最想知道的信息。
    """
    if not headers:
        return ""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    pop = lowered.get("x-amz-cf-pop")
    if pop:
        return "".join(ch for ch in pop if ch.isalpha())[:3] or pop
    ray = lowered.get("cf-ray")
    if ray and "-" in ray:
        return ray.rsplit("-", 1)[-1]
    return ""


@dataclass
class Throughput:
    """一轮并发分片测试的结果。"""

    ip: str
    recv_bytes: int = 0
    seconds: float = 0.0
    colo: str = ""
    ok_parts: int = 0
    parts: int = 0

    @property
    def mbps(self) -> float:
        """MB/s（十进制，跟启动器进度条的口径一致）。"""
        if self.seconds <= 0:
            return 0.0
        return self.recv_bytes / self.seconds / 1_000_000

    @property
    def complete(self) -> bool:
        return self.parts > 0 and self.ok_parts == self.parts


def measure_throughput(
    ip: str,
    *,
    host: str = TARGET_HOST,
    conns: int = 8,
    timeout: float = 25.0,
    fetch=None,
    colo: str = "",
    target=None,
) -> Throughput:
    """并发拉 ``conns`` 片 1MiB，算总吞吐。

    为什么要并发：实测同一落点上 8 并发比单连接快 4–6 倍（单连接受 TCP
    拥塞窗口限制）。EVE 启动器自己就是 10 并发，这里跟它对齐才有可比性。
    """
    fetch = fetch or bench_fetch
    conns = max(1, conns)
    started = time.perf_counter()
    results: list[tuple[int, int]] = []
    with futures.ThreadPoolExecutor(max_workers=conns) as ex:
        jobs = [
            ex.submit(
                fetch,
                ip,
                index * BENCH_CHUNK,
                (index + 1) * BENCH_CHUNK - 1,
                host=host,
                timeout=timeout,
                slot=index,
                target=target,
            )
            for index in range(conns)
        ]
        for job in jobs:
            try:
                # 兼容 (字节, 码) 与 (字节, 码, 响应头) 两种返回 —— 注入的假
                # fetch 常常只给前两个（早期版本就是这样，measure_throughput
                # 曾因此直接抛 ValueError，被测试抓住了）。
                size, code, _headers = _unpack_fetch(job.result())
                results.append((size, code))
            except Exception:
                results.append((0, 0))
    elapsed = max(time.perf_counter() - started, 1e-6)
    ok = [size for size, code in results if 200 <= code < 300]
    return Throughput(
        ip=ip,
        recv_bytes=sum(ok),
        seconds=elapsed,
        colo=colo,
        ok_parts=len(ok),
        parts=len(results),
    )


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
@dataclass
class ScanResult:
    host: str
    bench: BenchTarget | None = None
    candidates: list[str] = field(default_factory=list)
    #: 真的下到过内容的落点（这就是探活结果）。
    content_ok: list[ContentProbe] = field(default_factory=list)
    #: 探活通过但内容拿不到（例如 530 Origin DNS error）的落点 IP。
    content_failed: list[str] = field(default_factory=list)
    measured: list[Throughput] = field(default_factory=list)
    #: 系统 DNS 现在返回、但实测不可达的地址 —— 写 hosts 时要一并覆盖。
    dead_ips: list[str] = field(default_factory=list)
    #: hosts 里当前钉住的落点（用来判断「还需不需要换」）。
    previous_ips: list[str] = field(default_factory=list)

    @property
    def best(self) -> Throughput | None:
        return self.measured[0] if self.measured else None

    @property
    def best_ip(self) -> str | None:
        best = self.best
        return best.ip if best else None

    def pinned_failing(self) -> str | None:
        """现役落点是否**已经不能下内容**了（若有，返回它的 IP）。

        这是本工具最该主动发现的一种故障：落点握手正常、trace 正常，但资源
        路径返回 530 —— 用户看到的是「更新卡住」，而工具若只看握手会误判为
        「一切正常」。命中时应当建议撤销而不是继续钉着。
        """
        for ip in self.previous_ips:
            if ip in self.content_failed:
                return ip
        return None

    def to_pin(self) -> list[str]:
        """写进 hosts 的顺序：实测最优在前，其后跟已知死地址。

        钉住死地址不是多余：macOS 与 Windows 都把 hosts 记录和 DNS 结果放进
        同一个候选集，把死地址也指到同一个名字上，客户端就不会再选中它们 ——
        等于对「解析器返回什么」免疫。
        """
        ordered: list[str] = []
        if self.best_ip:
            ordered.append(self.best_ip)
        for ip in list(self.dead_ips) + list(self.previous_ips):
            if ip not in ordered:
                ordered.append(ip)
        return ordered

    def pinned_status(self) -> tuple[bool, str]:
        """现役落点还快不快 —— 与最佳候选在同一轮里比过，所以可比。"""
        if not self.previous_ips:
            return False, "尚未钉住任何落点"
        failing = self.pinned_failing()
        if failing:
            return False, (
                f"★ 现役落点 {failing} 已无法下载内容（探活正常但资源取不到）——"
                "建议立即 revert，或重跑 apply 换一个"
            )
        current = next((t for t in self.measured if t.ip == self.previous_ips[0]), None)
        best = self.best
        if best is None:
            return False, "本轮没有拿到任何吞吐结果"
        if current is None:
            return False, f"现役落点 {self.previous_ips[0]} 本轮没测出速度，建议重跑 apply"
        if current.ip == best.ip:
            return True, f"现役落点 {current.ip} 就是当前最优（{current.mbps:.2f} MB/s）"
        if current.mbps >= best.mbps * 0.8:
            return True, (
                f"现役落点 {current.ip} 仍有 {current.mbps:.2f} MB/s，无需更换"
                f"（最优 {best.ip} 为 {best.mbps:.2f} MB/s）"
            )
        ratio = best.mbps / max(current.mbps, 1e-6)
        return False, (
            f"现役落点 {current.ip} 只剩 {current.mbps:.2f} MB/s，"
            f"比最优慢 {ratio:.1f} 倍 —— 建议重跑 apply"
        )


def scan(
    host: str = TARGET_HOST,
    *,
    top: int = 8,
    conns: int = 8,
    previous_ips: list[str] | None = None,
    index: str | None = None,
    doh_fetch=None,
    bench=None,
    extra_ips: tuple[str, ...] | list[str] = (),
    seeds: tuple[str, ...] = SEED_ADDRESSES,
    resolvers: tuple[str, ...] = UDP_RESOLVERS,
    dns_ips: list[str] | None = None,
    log=None,
    content_workers: int = 8,
    max_probe: int = 24,
) -> ScanResult:
    """完整流程：基准 → 收集候选 → **内容校验（即探活）** → 真实吞吐 → 排序。

    这里**没有**单独的「连通性探活」步骤，这是刻意的：曾经用 Cloudflare 专有的
    ``/cdn-cgi/trace`` 判可达，结果 CCP 把后端换成 CloudFront 后该端点返回 403，
    7 个完全可用的地址被判成「连不上」。教训是 ——「能连上」没有跨 CDN 的统一
    含义，**「真的下到一个字节」才是唯一有意义的判据**。所以直接用内容校验当探活：
    它同时给出「能不能用」和「有多快」，少一层就少一次误判。
    """
    log = log or (lambda _msg: None)
    result = ScanResult(host=host, previous_ips=list(previous_ips or []))

    log("[1/4] 选择基准资源…")
    result.bench = bench_target(index, log=log)

    log("[2/4] 收集候选 IP…")
    candidates = collect_candidates(
        host,
        doh_fetch=doh_fetch,
        resolvers=resolvers,
        seeds=seeds,
        extra=extra_ips,
        log=log,
    )
    # 现役落点必须进候选：它要是还行就不该乱换，否则每次重跑都在抖。
    candidates = _dedupe(result.previous_ips + candidates)
    result.candidates = candidates
    if len(candidates) > max_probe:
        # 候选很多时优先测「DNS 给的 + 现役的」，它们最可能是当前实际路径。
        head = _dedupe(result.previous_ips + (dns_ips if dns_ips is not None else system_dns_ips(host)))
        rest = [ip for ip in candidates if ip not in head]
        candidates = _dedupe(head + rest)[:max_probe]
        log(f"  候选较多，取前 {len(candidates)} 个做内容校验")

    log(f"[3/4] 内容校验 + 初测（真的下一个分片，对 {len(candidates)} 个候选）…")
    probes: list[ContentProbe] = []
    failures: list[str] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, content_workers)) as ex:
        jobs = {
            ex.submit(probe_content, ip, result.bench, host=host, bench=bench): ip
            for ip in candidates
        }
        for job in futures.as_completed(jobs):
            ip = jobs[job]
            try:
                probes.append(job.result())
            except Exception as exc:
                # 不静默吞咽：注入的 fetch 签名对不上时，这里曾把所有候选都变成
                # 「TypeError」而日志一片空白，白排查了很久。失败要说得出来。
                failures.append(f"{ip}: {type(exc).__name__}: {exc}")
    if failures:
        log(f"  ⚠ {len(failures)} 个候选在校验时抛异常，前 3 条：")
        for line in failures[:3]:
            log(f"      {line}")
    content_ok = [p for p in probes if p.ok]
    content_ok.sort(key=lambda p: -p.mbps)
    result.content_ok = content_ok
    result.content_failed = [p.ip for p in probes if not p.ok]
    for probe in sorted(probes, key=lambda p: (not p.ok, -p.mbps)):
        mark = "  ← 现役" if probe.ip in result.previous_ips else ""
        state = f"OK {probe.mbps:5.2f} MB/s" if probe.ok else f"✗ {probe.detail}"
        log(f"    {probe.ip:<16} {state}{mark}")
    if not content_ok:
        log("  没有任何落点能取到内容 —— 这不是本机设置能修的（多半是 CDN 侧故障）")
        return result

    if result.previous_ips and result.previous_ips[0] in result.content_failed:
        log(f"  ★ 现役落点 {result.previous_ips[0]} 已取不到内容，本轮会换掉它")

    ok_ips = [p.ip for p in content_ok]
    # 初测速度已经给了排序依据，取前 top 个做更重的并发测试即可。
    pool = content_ok[: max(1, top)]
    if result.previous_ips and result.previous_ips[0] in ok_ips:
        current_ip = result.previous_ips[0]
        if not any(p.ip == current_ip for p in pool):
            # 现役落点还能用但没进前列时，也要实测它 ——「是否变快」需要同一把尺子。
            pool = pool + [next(p for p in content_ok if p.ip == current_ip)]

    log(f"[4/4] 真实吞吐测试（并发 {conns}，对 {len(pool)} 个候选）…")
    measured: list[Throughput] = []
    with futures.ThreadPoolExecutor(max_workers=3) as ex:
        jobs = {
            ex.submit(
                measure_throughput,
                probe.ip,
                host=host,
                conns=conns,
                fetch=bench,
                colo=probe.colo,
                target=result.bench,
            ): probe
            for probe in pool
        }
        for job in futures.as_completed(jobs):
            try:
                measured.append(job.result())
            except Exception:
                continue
    measured.sort(key=lambda t: -t.mbps)
    result.measured = measured
    for item in measured:
        mark = "  ← 现役" if item.ip in result.previous_ips else ""
        partial = "" if item.complete else f"（{item.ok_parts}/{item.parts} 片成功）"
        log(f"    {item.ip:<16} {item.mbps:6.2f} MB/s  落点 {item.colo or '?'}{mark}{partial}")
    return result
