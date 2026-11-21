# DeepSeek 计费聊天站（MiandningAI）

网关计费制聊天站点：所有用户请求统一经网关改写为站点默认模型计费，用户无感知；站长可在后台切换默认模型。定价引擎 = **官方实时价 × 模型倍数 × 时段倍数**，每 10 分钟自动抓取官方文档价格并按峰/谷时段换档。

## 仓库结构

```
├── app.py                # 站点后端（单文件 FastAPI，约 3900 行；版本演进见 git log 与 docs/运维维护手册）
├── static/               # 前端 bundle（含计费改版补丁：钱包流水/扫码充值/时段倍率）
├── docs/
│   ├── 运维维护手册-MiandningAI.md   # 运维、守护进程、防火墙、数据铁律
│   ├── 部署指南-DeepSeek聊天界面.md  # 部署步骤
│   └── 执行铁律-数据保护.md          # 测试数据纪律（rttest 前缀、真实用户保护）
└── tests/                # 全量回归测试套件（18 个套件 = 1 bash 回归 + 17 Python，425+ 项断言）
    ├── regression_3000.sh                  # 57 项基础回归（含客户端中途断开收口断言）
    ├── test_money_special_3000.py          # 资金专项
    ├── test_money_security.py              # 资金安全
    ├── test_pay_model.py                   # 新功能专项（扫码充值/实时定价/时段倍率）
    ├── test_billing_accuracy.py            # 计费逐笔对账
    ├── test_concurrency.py                 # 并发扣费安全
    ├── test_isolation.py                   # 用户数据隔离
    ├── test_reconciliation.py              # 账务对账（含 new-api 重启恢复）
    ├── test_gw_e2e.py                      # 网关端到端
    ├── test_ui_smoke.py                    # 前端真浏览器 UI 冒烟（playwright；无浏览器环境 SKIP 退出码 3）
    ├── test_performance.py                 # 性能
    ├── test_time_mult.py                   # 时段倍率专项
    ├── test_p0_saga.py / p0_r3 / p0_acceptance   # P0 故障注入专项
    ├── test_p1_sid.py / p1_cloud / p1_price      # P1 归档安全 / 云同步冲突 / 官方价加固
    ├── run_all.sh                          # 统一门禁（退出码：失败=1 / SKIP>0=4 / 全过=0）
    └── _guard.py                           # 防误跑生产机守卫
```

## 核心设计（供审查参考）

1. **网关统一改写**：`_gw_proxy` 强制把请求体 `model` 改写为站点默认模型（设置键 `gw_default_model`，默认 deepseek-flash），用户无感知，后台可切换、切换即时生效。
2. **实时定价引擎**：`fetch_official_prices()` 每 10 分钟抓官方价 → `_peak_window()` 判峰/谷 → `recompute_ratios()` 按「官方价 × 模型倍数 × 时段倍数」重算 ModelRatio/CompletionRatio/**CacheRatio**（缓存命中比=命中价/未命中价，取两时段较小值）写入 new-api options；值无变化不写库，写库后新连接回读全模型确认。
3. **时段倍率**：面板可配置时段规则（支持跨零点、最多 10 条、按序匹配），`get_time_mults()` 提供运行时查询，规则变更即时重算。
4. **充值双通道**：26 位卡密（无 I/O/0/1）+ 扫码支付（¥5 起充、站长上传二维码、用户提交→站长审批到账，`pay_orders` 表 pending→approved/rejected 幂等审批，二维码魔数校验；上传后重定向带 `#sec-pay` 锚点直达区块，>2MB 自动压缩）。
5. **三道扣费闸**：余额预检 / 估算预检 / 并发上限，防透支、防并发竞态。
6. **余额制（v19）**：注册即开通（免审批，占位有效期 10 年仅兼容），前后端不再展示会员到期；前端角标/侧栏显示实时账户余额（`/api/me` 返回 `balance_cny`），侧栏品牌位显示用户网站 ID；用户自带 API Key 在启动时清除并禁用入口，统一走网站计费渠道；注册赠送默认 0（`GW_DEFAULT_GIFT_CNY` 环境变量可调）。

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

- 最新完整轮次 gate24：**11 PASS / 6 SKIP / 0 FAIL**（SKIP 为依赖真实记账环境未设置，未实际执行，不声称 17/17 全绿）、进程退出码 4、regression 57/0
- 审查闭环：R10–R19 复验轮次记录见 Issue #1（安全收口：请求体上限/chunked、ClientDisconnect、RecursionError、multipart、版本兼容均已实测）
- 待办：接入真实 DeepSeek Key（OpenAI 协议渠道）、正式站部署
