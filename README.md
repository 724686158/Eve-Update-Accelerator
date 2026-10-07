# EVE Update Accelerator

**给 EVE Online 的更新下载挑一条真正走得通的路。**

国内直连时，EVE 启动器的补丁下载常年停在几十 kB/s，甚至一晚上下不完一个 260 MB 的补丁。
原因几乎从不是"带宽不够"——启动器自己就是 10 并发——而是 `binaries.eveonline.com`
解析到的那几个 CDN 地址，在你这条线路上**要么丢包严重，要么后端已经失效（HTTP 530）**。

这个工具的做法很直接：**实测**。它从启动器自己的本地索引里取一个真实存在的资源，
对一批候选地址逐个"真的下一段数据"，量出速度，然后把可用且最快的那些写进 hosts。
不改启动器、不装驱动、不需要代理。

> 实测效果（同一网络、同一资源）：修复前 **5–67 kB/s** → 修复后 **4.2 MB/s**（本机 10 并发），
> 用户启动器实测 **12.91 MB/s**。

![EVE 启动器实测 12.91 MB/s](assets/screenshot-12.9MBps.png)

![速度对比](assets/speed-comparison.svg)

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [快速开始](#快速开始)
- [图形界面](#图形界面)
- [命令一览](#命令一览)
- [系统设计概述](#系统设计概述)
  - [模块划分](#模块划分)
  - [一次扫描的数据流](#一次扫描的数据流)
  - [为什么这样分层](#为什么这样分层)
- [几个踩出来的设计决定](#几个踩出来的设计决定)
- [常见问题](#常见问题)
- [安全与回滚](#安全与回滚)
- [开发](#开发)
- [边界与免责](#边界与免责)

---

## 它解决什么问题

EVE 的客户端补丁走 `https://binaries.eveonline.com`。这个域名背后换过 CDN：

| 时间 | 后端 | 现象 |
|---|---|---|
| 2026-10 之前 | Cloudflare | 部分地址 60–100% 丢包、TLS 握手 8 秒 |
| 2026-10 起 | CloudFront | 旧 Cloudflare 地址全部 `530 Origin DNS error` |

两次故障表现相同（更新卡住），根因完全不同。所以**把 CDN 写死的工具早晚会失效**——
这个工具不假设 CDN 是谁，只相信实测结果。

## 快速开始

### 方式一：从源码跑（现在就能用，推荐）

需要 Python 3.9+，**无第三方依赖**，连 pip install 都不需要：

```bash
git clone https://github.com/724686158/Eve-Update-Accelerator.git
cd Eve-Update-Accelerator

python -m eveupdate check      # 只测速，不动系统 —— 先看看情况
python -m eveupdate apply      # 测速并写入（需要管理员权限）
python -m eveupdate revert     # 撤销，恢复 hosts 原样
```

macOS 上可以让工具自己弹授权框，不用开管理员终端：

```bash
python -m eveupdate apply --gui
```

> **如果 `git clone` 卡住或报 `HTTP2 framing layer` / 连接超时**：那是 `github.com`
> 的 git 端点在你这条线路上不通（国内常见，与 EVE 下载慢同源）。此时改用 GitHub API
> 走 HTTPS 拉取源码即可（`api.github.com` 通常可用）：
>
> ```bash
> python3 - <<'EOF'
> import base64, json, os, urllib.request
> REPO, REF = "724686158/Eve-Update-Accelerator", "main"
> def get(url):
>     req = urllib.request.Request(url, headers={"User-Agent": "fetch", "Accept": "application/vnd.github+json"})
>     return json.load(urllib.request.urlopen(req, timeout=60))
> tree = get(f"https://api.github.com/repos/{REPO}/git/trees/{REF}?recursive=1")
> for node in tree["tree"]:
>     if node["type"] != "blob":
>         continue
>     path = node["path"]
>     blob = get(f"https://api.github.com/repos/{REPO}/contents/{path}?ref={REF}")
>     os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
>     open(path, "wb").write(base64.b64decode(blob["content"]))
>     print("  ", path)
> EOF
> ```
>
> 拉下来后照上面的命令跑即可（仓库零第三方依赖，不需要 `pip install`）。

想装成命令（可选）：

```bash
pip install -e .
eve-update-accelerator check
```

#### Windows 10 上的权限

写 hosts 需要管理员权限。两种方式都行：

```powershell
# 以管理员身份打开 PowerShell，然后
python -m eveupdate apply

# 或者普通窗口里跑，让它自己弹 UAC（走 ShellExecuteExW 提权）
python -m eveupdate apply --gui
```

`revert` 同理。hosts 路径是 `%SystemRoot%\System32\drivers\etc\hosts`，
备份文件在同目录（`.eve-update-accelerator.bak`）。

> ⚠️ 少数安全软件（"主页保护"/"hosts 防护"之类）会把 hosts 锁定或改回。
> 这种情况下写入后的回读验证会**明确报错**而不是假装成功，`doctor` 也能查出来。

**建议先跑 `check`**：它会告诉你现在的落点还行不行，以及有没有更快的。如果
DNS 给的地址本来就又快又稳，工具会明确说"无需改动"。

#### Windows 上的索引路径未经验证

工具的测速基准取自启动器本地的 `SharedCache/index_tranquility.txt`。macOS 上这个
路径是实测确认的；**Windows 上的候选路径没有在真机验证过**（开发机是 macOS），
所以给了一批常见位置并会在找不到时退回内置基准、明确标注"可能已过期"。
在 Windows 上跑 `doctor` 会打印它查过的每个路径，若都不对，用 `--index` 指定即可。

### 方式二：免安装的可执行文件（尚未发布）

[Releases](https://github.com/724686158/Eve-Update-Accelerator/releases) 里**现在是空的** ——
打包用的 GitHub Actions 工作流已经写进仓库（`.github/workflows/ci.yml`），但它还没被推送上去：
写 `.github/workflows/` 需要带 `workflow` 权限的令牌，而当前网络到 `github.com` 的 git 端点不可用
（细节与补推命令见 [PUSH-STATUS.md](PUSH-STATUS.md)）。工作流生效后，Releases 里会出现：

- **Windows 10**：`eve-update-accelerator-x.y.z-win.exe`
- **macOS**：`eve-update-accelerator-x.y.z-macos.zip`

在那之前请用方式一。这处说明是后补的：最初写文档时把"工作流将来会构建产物"
当成了"产物已经有了"，这是文档错误，不是产品缺功能——功能本身已经能跑（见下方验证方式）。

## 图形界面

不带子命令直接运行就是图形界面（Windows 的官方 Python 自带 tkinter；macOS 用系统 Python）：

```bash
python -m eveupdate
```

三个按钮，对应三件事：**测速**（只读）、**生效**、**撤销**。

## 命令一览

| 命令 | 作用 | 改系统？ |
|---|---|---|
| `check` | 测速并报告现役落点是否还可用 | 否 |
| `apply` | 测速 → 写入 hosts（自动备份 + 回读验证） | **是** |
| `revert` | 只摘掉本工具写入的标记块 | **是** |
| `bench` | 对比"默认解析路径"与"已钉住地址"的吞吐 | 否 |
| `doctor` | 诊断：基准资源、DNS 链、现役落点还能不能下东西 | 否 |

常用参数：`--top N`（测几个候选）、`--conns N`（并发数，默认 10，与启动器对齐）、
`--index PATH`（启动器 SharedCache 目录，默认自动查找）、`--dry-run`。

---

## 系统设计概述

设计目标有三个，按优先级排列：

1. **不能骗人**：宁可说"没法加速"，也不能把用户指到一个下不动东西的地址上；
2. **不写死任何 CDN**：后端会换，只有实测结果算数；
3. **说走就能走**：任何改动都能一键撤销，且不依赖工具本身（有备份文件）。

### 模块划分

```
                    ┌──────────────────────────────┐
   用户 ──────────► │  cli.py   /   gui.py         │   入口层：只负责交互
                    └──────────────┬───────────────┘
                                   │ 调用
                    ┌──────────────▼───────────────┐
                    │  scan.py                     │   决策层：谁可用、谁最快
                    │  collect → 校验 → 吞吐 → 排序 │
                    └───┬──────────────────────┬───┘
        取基准资源       │                      │  读写目标
                    ┌───▼──────────┐   ┌───────▼────────────┐
                    │  evindex.py  │   │  hosts.py          │   数据层
                    │  本地索引解析 │   │  标记块/备份/幂等   │
                    └──────────────┘   └───────┬────────────┘
                                               │ 需要提权时
                                       ┌───────▼────────────┐
                                       │  elevate.py        │   权限层
                                       │  UAC / osascript / │
                                       │  pkexec | sudo     │
                                       └────────────────────┘
```

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `evindex.py` | 从启动器 `SharedCache/index_tranquility.txt` 里挑一个真实存在的大文件做基准 | 流式解析（索引可达几十万行）；解析不了的行**跳过而不是猜** |
| `scan.py` | 候选收集、内容校验、并发吞吐、排序 | 只用真实 HTTP；所有网络调用可注入以便离线测试 |
| `hosts.py` | 跨平台 hosts 文本操作：标记块、备份、幂等、干净撤销 | 保留原文件 EOL 与 BOM；备份只在首次写入时创建 |
| `elevate.py` | 提权写入 | `build_command()` 是纯函数（可跨平台测试），执行才分平台 |
| `cli.py` / `gui.py` | 入口 | 默认只读；GUI 的所有耗时操作在工作线程，跨线程只走队列 |

### 一次扫描的数据流

```
[1] 选基准资源
    SharedCache/index_tranquility.txt ──► 最大的一个真实资源（例如 44.9 MB）
    （找不到就退回内置路径，并明确标注"可能已过期"）

[2] 收集候选
    公共 DoH ─┐
    系统解析器 ┼─► 按 CNAME 链过滤 ─┐
    网段样本 ─┘                    ├─► 候选地址池
    现役落点 ──────────────────────┘

[3] 内容校验（这一步就是探活）
    对每个候选：Range 请求基准资源的前 128 KiB
      拿到字节 → 可用（顺带得到初测速率）
      530/403/超时 → 淘汰        ←── 关键：不看握手，只看数据

[4] 真实吞吐
    对初测最快的 N 个：10 并发 × 1 MiB 分片，算总吞吐

[5] 排序与决策
    最优在前 + 已知死地址在后 ──► 写 hosts（若用户要求）
    现役落点取不到内容 ────────► 明确警告，建议换掉或撤销
```

### 为什么这样分层

- **决策层与权限层分离**：`scan.py` 不做任何提权，`elevate.py` 不做任何测量。
  这样测速可以在普通用户下跑，写入才需要授权 —— 用户可以先看清楚再决定。
- **文本层与执行层分离**：`hosts.py` 只处理字符串（纯函数、可单测），
  `elevate.py` 才碰系统。平台差异被压缩到"一条命令长什么样"和"怎么提权"。
- **一切网络调用可注入**：`doh_fetch` / `bench` / `resolvers` / `dns_ips` / `extra_ips`
  都是参数。所以 41 个单元测试**不打一次网络、不碰一次系统 hosts**，0.04 秒跑完。

---

## 几个踩出来的设计决定

这一节是这个项目最有价值的部分——每条都对应一次真实的误判。

### 1. 基准资源必须来自启动器本地索引，不能硬编码

第一版把基准写成 `/bundles/3542233/eveonlinemacOS_3542233.txt.bundle.6`。
两天后 CCP 发了新 build，旧文件从 CDN 上消失，于是**所有落点测出来都是 530**，
看起来像"全网故障"。现在改成读启动器自己的索引——那是它**此刻真的会去下载**的东西，
不会腐烂。

### 2. "能连上"不是判据，"能下到数据"才是

曾经用 Cloudflare 专有的 `/cdn-cgi/trace` 当探活端点。CCP 换成 CloudFront 后：

- 该端点返回 **403** → 7 个完全可用的地址被判成"连不上"；
- 反过来，同一个 IP 上 trace 返回 **200**，而资源路径返回 **530** → 死落点被判成"可用"。

两个方向都错过一次。现在只认一个判据：**Range 请求真实资源，拿到字节才算可用**。

### 3. 候选地址必须按 DNS 解析链过滤

`binaries.eveonline.com` 的 DoH 响应里有 CNAME 链
（`→ dk486zhrb40ft.cloudfront.net → 18.65.x.x`）。早期版本把响应里所有 A 记录都收进候选池，
结果混进一堆**不属于这个域名**的地址——用本域名的 SNI 去打它们只会得到 403。

### 4. 现役落点失效要能被主动发现

这是用户最容易遇到、也最难自己诊断的情况：hosts 里钉着一个地址，握手正常、
但内容全部 530，启动器表现就是"更新卡住"。`doctor` 命令和 `scan` 的
`pinned_failing()` 专门检测这种状态，并明确建议撤销或更换。

### 5. hosts 只能"换掉坏地址"，不能"优先使用"

实测（macOS）：即使 hosts 里写了一条记录，系统仍会把 DNS 结果一起放进候选集，
实际选谁由系统决定。所以：

- 不要承诺"钉了就一定走它"——README 与工具输出里都写明这一点；
- 但"把已知不可达的地址替换成可用地址"本身就是有效优化：实测默认路径 2.33 MB/s，
  换掉之后 4.20 MB/s。

---

## 常见问题

**Q：会不会把 hosts 改坏？**
A：只动自己标记块之间的内容，写入前备份到 `hosts.eve-update-accelerator.bak`，
写完回读验证；`revert` 只摘掉标记块，其余各行原样保留（有单测覆盖）。

**Q：为什么 `check` 说"没有任何落点能取到内容"？**
A：这通常不是你机器的问题，而是 CDN 侧故障（例如 CCP 的源站配置出错）。
工具在这种情况会**拒绝写入任何东西**——比乱钉一个地址更安全。可以过几小时再试。

**Q：跑完之后启动器还是慢？**
A：先 `doctor` 看现役落点是否还能下载；如果被安全软件还原了 hosts，`doctor` 会报出来。
另外注意：速度随时段波动，晚高峰国际出口劣化是常态。

**Q：需要装 Python 吗？**
A：用 Releases 里的可执行文件就不需要。源码方式需要 Python 3.9+，无第三方依赖。

**Q：为什么不做代理/VPN 那种方案？**
A：那需要改流量走向、装证书或驱动，风险和侵入性都高得多。这个工具只改一条 hosts 记录，
出问题一条命令就能撤销。

## 安全与回滚

- 写入前自动备份：`<hosts 同目录>/hosts.eve-update-accelerator.bak`（仅首次创建，
  不会被后续写入覆盖）；
- 写入使用「临时文件 + 一次性覆盖」，原文件的"坏窗口"最短；
- 写入后**回读验证**，不一致就明确报错（而不是假装成功）；
- 撤销不依赖备份，也不依赖工具：删掉标记块之间的几行即可；
- 彻底还原：

  ```bash
  # macOS / Linux
  sudo cp /etc/hosts.eve-update-accelerator.bak /etc/hosts && sudo dscacheutil -flushcache
  # Windows（管理员 PowerShell）
  Copy-Item "$env:SystemRoot\System32\drivers\etc\hosts.eve-update-accelerator.bak" `
    "$env:SystemRoot\System32\drivers\etc\hosts" -Force; ipconfig /flushdns
  ```

## 开发

```bash
git clone https://github.com/724686158/Eve-Update-Accelerator.git
cd Eve-Update-Accelerator
python -m unittest discover -s tests -t .      # 41 个用例，离线，约 0.04 秒
```

代码风格：零第三方依赖；所有网络与系统调用可注入；注释解释"为什么"而不是"是什么"。

打可执行文件：

```bash
pip install -e ".[build]"
pyinstaller --onefile --name eve-update-accelerator --paths src \
  --collect-submodules eveupdate src/eveupdate/__main__.py
```

CI（GitHub Actions）会在 Windows 与 macOS 上跑测试并构建产物，打 tag 时自动发布到 Releases。

## 边界与免责

- 本工具不是 CCP 官方产品，与 CCP hf. 无关联；EVE Online 是 CCP hf. 的商标。
- 它只做**网络路径选择**：不改游戏文件、不修改客户端、不注入进程、不绕过任何认证。
- 它不保证一定能加速。当 CDN 侧本身故障、或你的线路对此 CDN 整体不友好时，
  它会明确说"没有可用落点"并拒绝改动，而不是给你一个看起来像成功的假象。
- 使用 hosts 重定向属于常规网络配置手段；请自行确认符合你所在网络环境的管理规定。

## 许可

[MIT](LICENSE)
