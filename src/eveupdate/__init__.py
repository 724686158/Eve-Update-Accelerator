"""eve-update-accelerator：给 EVE 更新下载挑一条更好走的路。

模块划分（详见 README 的系统设计一节）：

===================  ==========================================================
:mod:`evindex`       从启动器本地索引里取**真实存在**的基准资源（避免基准腐烂）
:mod:`scan`          候选收集 → 连通性探活 → **内容校验** → 真实吞吐
:mod:`hosts`         跨平台 hosts 读写：标记块、备份、幂等、干净撤销
:mod:`elevate`       提权：Windows UAC / macOS 授权弹窗 / Linux pkexec|sudo
:mod:`cli`           命令行入口
:mod:`gui`           tkinter 图形界面
===================  ==========================================================
"""

__version__ = "1.3.0"

APP_NAME = "EVE Update Accelerator"
REPO_URL = "https://github.com/724686158/Eve-Update-Accelerator"

__all__ = ["__version__", "APP_NAME", "REPO_URL"]
