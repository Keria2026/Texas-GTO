# Serve：Ubuntu 算法和训练端

0.8.3 会从可信的八人实采前缀自动收集公开开局，在完整自博弈迭代之间加入同一核心库。只使用筹码、蘑菇权益和位置，私牌仍随机发出；首次扩展前备份，旧累计与 RNG 保留。工作台显示实采开局数和容量状态。收集开局不表示已具备该局面的策略覆盖，树外下注仍待支持；见项目 `docs/observed-openings.md`。

0.8.2 新增固定 4 分前注与最新补码规则。首次启动会先备份旧核心，再启用独立前注信息集；历史总轮数保留，`rule_iterations` 另计当前规则轮数。原条目不代表新规则覆盖，升级并不保证立即产生建议。大库备份和完整校验可能耗时数分钟，见项目 `docs/ante-migration.md`。

Pr1.0 支持当前下注轮起点搜索、画像条件八人推荐，以及真实样本的自动训练／时间留出评测／版本发布／回滚。每 30 秒检查新的完整采集结算；原始事件和核心训练库保留。`GET /v1/capture/learning` 返回本 Origin 的学习状态，`POST /v1/capture/learning/rollback` 以 `expected_version` 回滚；沿用 Origin、X-Texas-Local 和可选令牌边界。条件行为策略与报告随 capture.sqlite3 一起迁移。

动作建议仍须经过输入、平均策略覆盖和反馈检查；画像调整还须通过独立两手收益抽样。两手视野、有限动作和信息集覆盖、平台边界与真实八人质量仍有限制，Pr1.0 不表示完整 V1.0 验收或盈利保证。

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
