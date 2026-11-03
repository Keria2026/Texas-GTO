# WSL 本地算法服务自启动

本机使用 Ubuntu-24.04、Linux 用户 `ubuntu`（UID 1000），源码位于 `/home/ubuntu/Project/Texas-GTO`。2026-11-03 起，实际服务使用 Serve 安装目录 `/home/ubuntu/.local/share/texas-gto`，不再从开发 checkout 导入代码。Windows 当前用户登录后，原计划任务 `Texas-GTO-Local-WSL` 唤起 WSL，并保留一个 `sleep infinity` 前台进程，使关闭终端后 WSL 仍保持运行。算法进程由同名 systemd 用户服务 `texas-gto-local.service` 管理，退出后等待 3 秒自动重启。

服务使用安装目录的 `current/bin/python`；`current` 指向已安装的 release，启动时不下载依赖。本机 Python 3.12 运行时位于安装目录的 `runtime/`。API 仅监听 `127.0.0.1:8765`；本地模式免令牌，允许 5173/5174 工作台和合法 Chrome 扩展 Origin，保留 Host、Origin 与 HTTP 客户端标记校验。当前数据位于安装目录的 `data/`，日志由 journal 管理。原 `artifacts/local` 保留为迁移前副本，后续进度以新目录为准。Vite 开发界面不属于此自启动服务。

用户指定内存上限为 1G：此 unit 设置 `MemoryAccounting=yes`、`MemoryMax=1G`，限制 API 与训练子进程合计为 1 GiB。训练进程自己的地址空间限制仍保留。该设置不限制 Chrome、Vite 或其它 WSL 服务，旧 JSON 导入的 64 MiB 限额独立保留，SQLite 训练库可继续增长。

服务同时启动独立的核心训练进程：空闲 30 秒后自博弈，牌局或计算请求到来时暂停，完成迭代后事务提交 `data/core/training.sqlite3`，重启自动恢复。工作台会显示实际进度；不需要点击“运行算法验证训练”。当前核心跨 489 个明确公开开局共享策略，使用固定 4 分前注和两手视野；实局推荐仍须通过输入、当前规则覆盖与反馈检查。旧无前注条目保留但不代表新规则覆盖，迁移及当前规则轮数见 [前注接入](ante-migration.md)。资源上限与导入导出见 [训练说明](../training/README.md)。

## Linux 服务

本机已启用 WSL systemd 与 `ubuntu` 的 linger。安装包自带锁定依赖，以下命令在解压后的 Serve 目录执行；本机更新沿用已经安装的 Python 运行时：

```sh
PATH="$HOME/.local/share/texas-gto/runtime/bin:$PATH" \
  bash install.sh "$HOME/.local/share/texas-gto"
```

安装器保留 `data/` 和以前的 release，更新后重启同一服务。其他机器的 Python／venv 前提、首次安装及数据导入导出见 [Serve 说明](../deploy/README-Serve.md)。不要用源码示例 `deploy/texas-gto-local.service` 覆盖本机安装器生成的 unit，否则会切回旧数据目录。

查看状态、最近日志或手动重启：

```sh
systemctl --user status texas-gto-local.service
journalctl --user -u texas-gto-local.service -n 50 --no-pager
systemctl --user restart texas-gto-local.service
```

## Windows 登录任务

注册脚本仅管理 Windows 的 WSL 启动入口，不在 Windows 开发或运行项目源码。在 Windows PowerShell 中执行一次：

```powershell
& '\\wsl.localhost\Ubuntu-24.04\home\ubuntu\Project\Texas-GTO\deploy\register-wsl-startup.ps1'
Start-ScheduledTask -TaskName 'Texas-GTO-Local-WSL'
Get-ScheduledTask -TaskName 'Texas-GTO-Local-WSL'
```

任务按当前用户的交互登录触发，无需管理员权限或保存密码；运行时间无限制，失败后每分钟重试一次，最多三次，不启动重复实例。任务显示 `Running` 是正常状态，因为它需要保持 WSL 前台进程。重复运行注册脚本会更新此任务。

## 关闭自启动

先在 Windows PowerShell 停用并停止入口：

```powershell
Disable-ScheduledTask -TaskName 'Texas-GTO-Local-WSL'
Stop-ScheduledTask -TaskName 'Texas-GTO-Local-WSL'
```

再在 WSL 停用算法服务：

```sh
systemctl --user disable --now texas-gto-local.service
```

无需关闭整个 WSL，其他项目可继续运行。
