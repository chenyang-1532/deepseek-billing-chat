# DeepSeek 计费聊天站（MiandningAI）

网关计费制聊天站点：所有用户请求统一经网关改写为站点默认模型计费，用户无感知；站长可在后台切换默认模型。定价引擎 = **官方实时价 × 模型倍数 × 时段倍数**，每 10 分钟自动抓取官方文档价格并按峰/谷时段换档。

## 仓库结构

```
├── app.py                # 站点后端（v17.6，单文件 Python/HTTP，约 2600 行）
├── static/               # 前端 bundle（含计费改版补丁：钱包流水/扫码充值/时段倍率）
├── docs/
│   ├── 运维维护手册-MiandningAI.md   # 运维、守护进程、防火墙、数据铁律
│   ├── 部署指南-DeepSeek聊天界面.md  # 部署步骤
│   └── 执行铁律-数据保护.md          # 测试数据纪律（rttest 前缀、真实用户保护）
└── tests/                # 全量回归测试套件（10 个脚本，275+ 项）
    ├── regression_3000.sh                  # 54 项基础回归
    ├── test_money_special_3000.py          # 25 项资金专项
    ├── test_money_security.py              # 25 项资金安全
    ├── test_pay_model.py                   # 57 项新功能专项（扫码充值/实时定价/时段倍率）
    ├── test_billing_accuracy.py            # 28 项计费逐笔对账
    ├── test_concurrency.py                 # 28 项并发扣费安全
    ├── test_isolation.py                   # 24 项用户数据隔离
    ├── test_reconciliation.py              # 17 项账务对账（含 new-api 重启恢复）
    ├── test_gw_e2e.py                      # 13 项网关端到端
    ├── test_performance.py                 # 4 项性能
    └── test_time_mult.py                   # 37 项时段倍率专项
```

## 核心设计（供审查参考）

1. **网关统一改写**：`_gw_proxy` 强制把请求体 `model` 改写为站点默认模型（设置键 `gw_default_model`，默认 deepseek-flash），用户无感知，后台可切换、切换即时生效。
2. **实时定价引擎**：`fetch_official_prices()` 每 10 分钟抓官方价 → `_peak_window()` 判峰/谷 → `recompute_ratios()` 按「官方价 × 模型倍数 × 时段倍数」重算 ModelRatio/CompletionRatio/**CacheRatio**（缓存命中比=命中价/未命中价，取两时段较小值）写入 new-api options；值无变化不写库，写库后新连接回读全模型确认。
3. **时段倍率**：面板可配置时段规则（支持跨零点、最多 10 条、按序匹配），`get_time_mults()` 提供运行时查询，规则变更即时重算。
4. **充值双通道**：26 位卡密（无 I/O/0/1）+ 扫码支付（¥5 起充、站长上传二维码、用户提交→站长审批到账，`pay_orders` 表 pending→approved/rejected 幂等审批，二维码魔数校验）。
5. **三道扣费闸**：余额预检 / 估算预检 / 并发上限，防透支、防并发竞态。

## 运行形态

- 站点 `:3000`（app.py）· new-api `:3001`（计费网关）· mock 上游 `:3002`（**当前为 mock 验收环境**；接入真实 DeepSeek Key 后上游切换为 https://api.deepseek.com，上线前必须用真实 Key 完成带缓存命中请求的计费验收，mock 的 token 统计/缓存行为不能替代）
- 守护：`/tmp/supervisor.sh` setsid 常驻循环；网关开关 = 站点目录 `gateway.on` 文件
- 敏感/运行时文件（data.db、secret.key、.admin_password）不入库

## 测试运行方式

```bash
export RT_TAG=<唯一标签>          # 并行运行必须设置，隔离测试用户
bash tests/regression_3000.sh
RT_TAG=$RT_TAG python3.11 tests/test_pay_model.py
```

## 当前状态

- 全量回归 275+ 项 × 3 轮并行子代理验证，除环境瞬态外全过
- 待办：接入真实 DeepSeek Key（OpenAI 协议渠道）、正式站部署、放置 gateway.on
