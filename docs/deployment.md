# 部署与迁移

交付三个独立项目：`Serve.tar.gz`、`Client.zip`，以及需要共享训练成果时单独提供的数据导出目录。程序包附带 SHA-256 文件；数据包包含训练结果，可能包含私人牌局和画像，不放入公开仓库。

## 接收方安装

后端要求 Ubuntu 24.04 x86_64、Python 3.12、python3-venv 和用户 systemd；Windows 使用 WSL2。先准备系统依赖，Serve 安装时从随包 wheels 离线安装 Python 依赖。

```bash
sudo apt-get install python3-venv
tar -xzf Serve.tar.gz
cd Serve
bash install.sh "$HOME/.local/share/texas-gto"
sudo loginctl enable-linger "$USER"
systemctl --user status texas-gto-local.service
```

服务在 `127.0.0.1:8765` 运行，API 与训练子进程合计限制为 1 GiB，空闲时自动训练。首次大库启动需要完整校验；接口恢复和训练恢复不是同一时刻。训练进度与随机策略测试页面位于 `http://127.0.0.1:8765/training`，随服务启动，无需 Node 或额外端口。

解压 Client.zip，在 Chrome 扩展管理页开启开发者模式，加载其中的 Client 目录。点击“开始对局”后，插件自动选择当前 WPK 页面并连接本机服务，断线自动重连。升级时覆盖原目录并重新加载，保留扩展 ID。进入牌桌后开始对局；缺历史、规则不支持或缺策略时不会显示建议。

WSL 需启用 systemd（`/etc/wsl.conf` 中 `[boot]` 下配置 `systemd=true`），并由 Windows 启动该发行版。仓库 `deploy/register-wsl-startup.ps1` 是 Windows 启动入口，源码开发仍全部在 WSL 内。

## 发送方导出训练结果

```bash
"$HOME/.local/share/texas-gto/current/bin/texas-state" export \
  --data-dir "$HOME/.local/share/texas-gto/data" \
  --output "$HOME/texas-state-export" --core-only
```

给别人共享训练结果推荐使用 `--core-only`，排除原始牌局和玩家画像。若明确需要迁移完整私人学习数据，去掉该参数。输出目录必须尚不存在。工具使用 SQLite backup 导出已提交的数据，包含 WAL 中已提交内容；不要直接复制运行中的 training.sqlite3。导出目录包含 manifest.json、core/training.sqlite3，以及存在时的 capture.sqlite3。核心保存累计遗憾、平均策略、随机状态、训练开局与迭代；采集库保存事件、画像、行为模型和学习游标。两个数据库分别一致，并非全局事务。

把整个导出目录通过私有渠道交给接收方。接收方首次安装时：

```bash
bash install.sh "$HOME/.local/share/texas-gto" /path/to/texas-state-export
```

安装器验证哈希并恢复，然后服务自动从已保存进度续训并查询模型。目标 data 已存在时拒绝覆盖；应使用新的安装目录，或先导出保留旧数据再安排迁移。导入不意味着策略覆盖充分，也不会让不同扩展 ID 的会话画像自动合并。

## 更新、日志与回退

更新程序：解压新 Serve 后对同一安装目录再次执行 install.sh，不带数据参数。现有 data 保留。查看日志：

```bash
journalctl --user -u texas-gto-local.service -f
systemctl --user stop texas-gto-local.service
systemctl --user start texas-gto-local.service
```

规则或开局分布升级会保存 before-* 备份。旧程序不一定支持新模型格式；回退需配套旧程序与对应备份，并先保留当前数据。不要仅切换 current 就假定模型已回退。

## 清理旧规则训练数据

停止服务后执行以下命令，仅保留当前 `ante4:` 信息集。保留尚无平均策略的当前节点，它们仍含可续训的遗憾反馈。此操作不重置轮数、随机状态或当前策略值。

```bash
systemctl --user stop texas-gto-local.service
"$HOME/.local/share/texas-gto/current/bin/texas-state" compact \
  --data-dir "$HOME/.local/share/texas-gto/data" \
  --backup "$HOME/training-before-compact.sqlite3"
systemctl --user start texas-gto-local.service
```

备份路径必须未存在，且与数据位于同一文件系统；工具使用硬链接保留独立回滚文件。旧库验证、当前记录复制和校验成功后才替换运行库。该备份不参与推荐，也不进入数据导出。后续规则变更须先明确兼容边界，不能承诺当前数据永远适用于未来规则。
