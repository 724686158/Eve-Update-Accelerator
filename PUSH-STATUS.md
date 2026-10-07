# 推送状态（维护者备注）

远端 `main` 已经包含完整的源码、测试、README 与素材（17 个文件）。**唯一没上去的是**
`.github/workflows/ci.yml`：GitHub 的 API 要求 token 带 `workflow` scope 才能写
`.github/workflows/`，而 `gh` 的 token 没有这个 scope；同时当时 `github.com` 的
git 端点不可连（HTTP/2 framing 报错、上行 < 1 KB/s），所以 `git push` 也走不通。

## 网络恢复后，一条命令补上

```bash
cd ~/Eonline/Eve-Update-Accelerator
git fetch origin
git reset --hard origin/main     # 远端历史是重建的，本地的 SHA 与它不同
git push origin main
```

工作流文件已经在本地提交里了，`git push` 会把它一起带上去，之后 GitHub Actions 会
在 Windows / macOS / Linux 上跑测试并构建双平台可执行文件。

## 另外还有一个临时仓库要删

调试网络通道时建过一个私有空仓库 `724686158/eve-accel-probe`，
`gh` 的 token 缺 `delete_repo` scope 删不掉，请在网页上删除：
https://github.com/724686158/Eve-Update-Accelerator/settings 之外的那个仓库，
或访问 https://github.com/724686158/eve-accel-probe → Settings → Delete。
