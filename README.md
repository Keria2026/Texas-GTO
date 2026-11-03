# Texas GTO — Pre1.0

内部版本 **0.8.3**。Chrome 插件采集 WPK 牌局，本地 Ubuntu 服务重建状态、查询策略、限时搜索并推送建议，同时进行自博弈和真实样本学习。

当前为内部预览：采集、通信、训练、迁移与推荐链路已实现，真实八人局的策略覆盖仍不足，不能视为已可稳定实用的推荐系统。树外下注、4–7 人实采推荐及 re-straddle 尚未完整支持。

- [开发与目录说明](docs/development.md)
- [部署、交付与训练数据迁移](docs/deployment.md)
- [AI 技术手册：模块、数据流和算法](docs/ai/architecture.md)
- [后续工作](ROADMAP.md)

唯一正式客户端为 Client；Serve 是后端安装包。GitHub Actions 每次推送及 PR 检查并构建两个工件。训练库与私人采集数据单独导出，不包含在程序包中。
