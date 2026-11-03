# Product
<!-- impeccable:product-schema 1 -->

## Platform
web

## Users
蘑菇玩法玩家使用 Chrome 侧栏读取局面与建议；模型维护者在本地页面查看训练积累并测试策略覆盖。

## Capabilities and Constraints
插件只有“开始对局”主入口，展示连接状态、行动建议与已观察牌局，不执行下注。算法与训练部署在 WSL/Linux，合计内存上限 1 GiB。模拟测试不写入训练库。真实策略缺失必须明确显示，不用随机建议掩盖。
