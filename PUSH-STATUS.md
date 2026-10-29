# 构建与推送记录（维护者备注）

这份文件记录的是**环境层面踩过的坑**，不是待办列表。

## CI 工作流已经进仓库了

之前 `.github/workflows/ci.yml` 推不上去，是因为写 `.github/workflows/` 要求令牌
带 `workflow` scope。现在换成了
[`.github/workflows/build-win10-installer.yml`](.github/workflows/build-win10-installer.yml)，
已推送。维护者的令牌需要勾上 **repo + workflow** 两个 scope。

`api.github.com` 在这台机器上是不可用的（403、空响应），所以**不要指望用 API 建
Release 或上传资产**；打 `v*` tag 让 CI 去发布，是这里唯一走得通的路。

## 网络：git 必须手动配代理

系统代理在 WinINET 里（`127.0.0.1:7897`），PowerShell/.NET 会自动走它，
但 **git 用的 libcurl 不会**，表现是 `Connection was reset` 或连接超时——
和「被墙」的现象一模一样，很容易误判。解法：

```bash
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 <命令>
# 或者设 HTTP_PROXY / HTTPS_PROXY 环境变量
```

另外，`~/.gitconfig` 在这台机器上不可写，`git config --global` 会报
`could not lock config file`，所以用 `-c` 参数或环境变量传代理。

## 构建：两个「BOM 陷阱」

`build_win10.ps1` 和 `packaging/installer.nsi` **都必须带 UTF-8 BOM**：

- Windows PowerShell 5.1 读没有 BOM 的 `.ps1` 会按系统 ANSI 代码页解析，中文变乱码，
  严重时直接语法错误（`Missing closing '}'`）；
- NSIS 只有见到 BOM 才按 UTF-8 解析脚本，否则中文全是乱码。

改完这两个文件后，确认 BOM 还在（不少编辑器和脚本会悄悄丢掉它）。

## 构建：本机 %TEMP% 建不了大内存映射

这台机器的 `C:\Users\Administrator\AppData\Local\Temp` 看起来一切正常（可写、权限正常），
但**超过约 30 MB 的内存映射会失败**，表现为：

- PyInstaller `--onefile` 报 `Could not create temporary directory!`；
- NSIS 报 `Internal compiler error #12345: error creating mmap the size of ...`。

把 `TEMP`/`TMP` 指到别的普通目录就正常了：

```powershell
$env:TEMP='C:\Deepseek\_work\tmp'; $env:TMP=$env:TEMP
```

所以本项目坚持用 `--onedir`（安装包本来就要铺开文件，单文件模式纯属白亏）。

## 本机跑不了 GUI 子系统程序

这台机器上 NSIS / Inno Setup 安装包（以及任何 GUI 子系统 exe）启动后**返回 0 但什么
都不做**，所以安装/卸载的冒烟测试只能交给 CI 的 windows runner 去做
（`build-win10-installer.yml` 的 `installer` 任务）。本地能验证的是：exe 能跑
（命令行版 `--version`/`--help`）、以及安装包载荷与源目录**逐字节一致**。

## 待办

调试网络通道时建过一个私有空仓库 `724686158/eve-accel-probe`，
令牌缺 `delete_repo` scope 删不掉，需要在网页上手动删除：
https://github.com/724686158/eve-accel-probe → Settings → Delete。
