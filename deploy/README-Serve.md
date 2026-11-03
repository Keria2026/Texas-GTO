# Serve：Ubuntu 算法和训练端

这是 0.7.1 可部署预览，尚未完成八人实局策略质量验收。通过覆盖与最低反馈检查的八人节点可推送 Pluribus 平均策略，翻后可协商全范围限时搜索；空闲时自动训练同一套核心库。均匀平均且遗憾全零的节点暂不推荐，搜索缺覆盖／预算不足时的回退同样受此限制。该检查不证明策略成熟；画像学习已收集公开行为，但尚未用于调整行动，后续策略和实际局面仍有覆盖缺口。

目标环境为 Ubuntu 24.04 x86_64（包括 WSL2）、Python 3.12、`python3-venv`、用户 systemd。安装包包含锁定且带哈希的全部 Python 依赖轮子；安装过程不访问包仓库。缺少系统 venv 时先执行 `sudo apt-get install python3-venv`。

解压后在 Serve 目录执行：

```bash
bash install.sh "$HOME/.local/share/texas-gto"
systemctl --user status texas-gto-local.service
```

安装器先校验文件、在新的 release 目录安装并检查依赖，再切换 current 并重启服务。后续更新执行相同命令，保留 data 和以前的 release；已有服务属于另一目录时要求明确迁移数据，避免静默另建空模型。服务绑定 localhost 8765、免令牌，API 与训练进程合计 `MemoryMax=1G`。Ubuntu 用户服务随登录启动；需要未登录也启动时使用 `sudo loginctl enable-linger "$USER"`。WSL 还需要 Windows 启动 WSL，现有启动入口见仓库 `docs/local-startup.md`。

## 迁移训练结果及 WPK 学习数据

在旧机器导出。源码部署也可运行 `.venv/bin/texas-state`，`--data-dir` 指向现有 `artifacts/local`：

```bash
~/.local/share/texas-gto/current/bin/texas-state export \
  --data-dir "$HOME/.local/share/texas-gto/data" --output "$HOME/texas-state-export"
```

工具以 SQLite backup API 导出已提交的核心累计值／轮数／随机状态，以及存在时的 capture.sqlite3（真实事件、学习样本及画像）；包含 WAL 中已提交内容。每个数据库各自保持一致，两个数据库不是同一全局事务。manifest 记录文件大小与 SHA-256。程序代码、凭据、原始浏览器载荷和历史备份不属于这个数据包。

将导出目录复制到新机器，然后在一个新的安装目录恢复：

```bash
bash install.sh "$HOME/.local/share/texas-gto" /path/to/texas-state-export
```

已有 data 时拒绝覆盖。迁移到另一目录可保留旧安装用于回退。单独校验／恢复到新目录：

```bash
current/bin/texas-state restore --bundle /path/to/texas-state-export --data-dir /new/path/data
```

恢复先在临时目录复制并校验哈希和 SQLite，再发布目录；错误不覆盖已有数据。核心在启动时继续核查规则／抽象、累计数值和 RNG，完整核查大库可能需要数分钟。默认首次安装没有训练数据时才建立新库，导入结果后从已保存迭代续训。数据包包含私人牌局及画像，请通过自己的存储渠道迁移；公开 GitHub 构建产物仅含程序。

无 systemd 的安装验证可设 `TEXAS_INSTALL_NO_SERVICE=1`；此模式明确跳过服务注册，运行端须自行落实同等总内存上限。停止／日志分别使用 `systemctl --user stop texas-gto-local.service`、`journalctl --user -u texas-gto-local.service`。
