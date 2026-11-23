# Miandning AI 代码审查指引（REVIEW GUIDE）

> 更新日期：2026-11-23 · 对应版本：v25（commit `8566e64`）
> 供审查 agent / 人工审查者使用。行号基于 v25 的 `app.py`（约 5000 行），后续版本会漂移，建议先用文中 grep 关键词重新定位。

## 一、入口文件与推荐审查顺序

- **唯一后端入口**：`app.py`（单文件 FastAPI，按区块从上到下组织；先读文头常量区与 `init_db()` 再看业务区）
- **前端**：`static/index.html`（bundle 引用名）→ `static/static/js/main.fe540c09.js`（压缩主 bundle，改内容必须改名）→ `static/static/js/group-chat.v2.js`（v25 群聊独立模块）
- **推荐顺序**（资金安全 → 资金正确性 → 上传/外部输入 → 群聊 → 前端纪律 → 测试）：
  1. 网关代理与扣费闸（`_gw_proxy`，grep `def _gw_proxy`）
  2. 充值/调账/卡密资金面（`admin_wallet_pay` / `admin_wallet_adjust` / `api_wallet_redeem`）
  3. new-api 幂等加款（`gw_add_quota`，grep `quota_ops`）
  4. 文件上传（`api_upload`，grep `ALLOWED\|purpose`）
  5. 群聊编排（`_gc_round` / `api_group_chat`）
  6. 请求体上限与限流（grep `_BODY_LIMIT\|_body_limit_middleware\|_prune_attempts`）
  7. 静态文件/缓存纪律（`static/index.html`）
  8. 跑测试（见第四节）

## 二、关键审查点清单（对照 TECHNICAL.md 第 4 节）

| # | 审查点 | 代码位置（app.py） | 审查要点 |
|---|---|---|---|
| 1 | **充值审批 saga 幂等** | `admin_wallet_pay()`（~L4168，grep `def admin_wallet_pay`）+ `gw_add_quota()`（~L3143，grep `quota_ops`） | 状态机 pending→processing→approved 原子占单；加款必须带 `op_id` 走 `quota_ops` 唯一表，`BEGIN IMMEDIATE`（~L3159）串行化；**禁止 MAX(0,...) 截断负 quota**；op_id 绑定不同账户/金额必须拒绝；页面表单必须带 `action=approve/reject`（v21 教训） |
| 2 | **余额调账方向** | `admin_wallet_adjust()`（~L3948，grep `def admin_wallet_adjust`） | 金额恒取绝对值，方向只由 `dir=add/deduct` 决定（v20 修复「扣减变充值」）；确认两个按钮的 value 与后端解析一一对应 |
| 3 | **充值申请幂等** | `api_wallet_pay_request()`（~L3828） | 强制 `client_request_id`，`(uid, client_request_id)` 唯一索引（grep `idx_pay_orders_crid`），重复提交返回同一订单 |
| 4 | **purpose 白名单与前端对齐** | `api_upload()`（~L2355），白名单 ~L2383-2387（grep `rich_layout_recognition`） | 白名单 = `assistants/batch/fine-tune/vision/user_data/rich_layout_recognition`；前端对 pdf/doc/docx/ppt/pptx 传 `rich_layout_recognition`，漏掉即上游 400（2026-11-22 生产实测）；purpose 拼入自建 multipart，白名单兼防 CRLF（审查 R12）；**前端新增 purpose 必须同步扩白名单** |
| 5 | **群聊一轮制硬上限** | `_gc_round()`（~L4525）+ `api_group_chat()`（~L4822）+ 成员校验 ~L4746（grep `GC_MAX_MEMBERS\|GC_MIN_MEMBERS`） | 每轮调用次数硬上限 = 成员数 × 1，循环必须按 members 遍历一次、绝不自增轮次（防烧钱）；余额不足发 `reply_error(kind=insufficient_balance)` 并终止整轮（~L4560-4596）；单角色失败 `kind=persona_failed` 不阻塞他人；发消息前 402 第一道闸（~L4860）+ `_gc_rate_limited`（~L4299，10 条/分钟） |
| 6 | **群聊记忆双写并发保护** | `_gc_memory_append()`（~L4345，`BEGIN IMMEDIATE` ~L4351） | 每条回复完成即追加 `persona_group_memory`；必须 `BEGIN IMMEDIATE` 串行化并发写；单条目封顶 600 字、总量 12000 字滚动裁剪（从头部裁）；单聊注入走 `_gw_proxy` 解析 system `【角色名】`（grep `_gc_single_inject_block`），`GC_SINGLE_INJECT_CHARS` 控制注入成本 |
| 7 | **402 拦截** | `_gw_proxy()`（~L3509）：余额预检 ~L3542（<¥0.005）、估算预检 ~L3606（预估成本超余额） | 两道闸都要在**改写模型之后**按站点默认模型估值；前端 402 时原地打开钱包弹窗（`window.__gwOpenWallet`），不跳外部；群聊端点另有独立 402 闸（~L4860） |
| 8 | **网关统一改写与 max_tokens 钳制** | `_gw_proxy()`，常量 ~L2457-2526（grep `GW_DEFAULT_MODEL_KEY\|GW_MAX_TOKENS_CAP`） | 请求体 model 强制改写为 `gw_default_model`；`max_tokens` 钳制到 `GW_MAX_TOKENS_CAP`（8192），**预检估值与实际转发共用同一上限**（审查 P1-01），防止「估值按小值、转发按大值」透支 |
| 9 | **并发/限流护栏** | `_gw_inflight_acquire()`（~L2969，grep `_gw_inflight`）、`_pay_rate_limited()`（~L3011）、`_gc_rate_limited()`（~L4299） | 单用户在途请求上限缓解 new-api pre-consume 并发透支窗口； acquired 必须在所有失败路径 release（对账 finally） |
| 10 | **上传绕过 new-api 直连官方** | `api_upload()` + `gw_files_key()`（~L3061） | Files API 不走 new-api（new-api 对 `/v1/files` 强制要 model → 400）；key 从 new-api 渠道表读启用渠道（grep `gw_files_key`）；**单渠道部署正确；若未来多渠道轮询需改渠道亲和** |
| 11 | **请求体上限「读之前拦」** | `_body_limit_middleware()`（grep `R8/R9\|_body_limit_for`，~L688-810） | 上限拦截必须发生在读取 body 之前；chunked 分块丢弃内存有界；导出/恢复另有 200MB 单账号上限（grep `BACKUP_MAX_BYTES`）与 zip bomb 防护（~L83-88：条目数/单文件/总解压/压缩比四道） |
| 12 | **bundle 缓存改名纪律** | `static/index.html`（引用 `main.fe540c09.js` / `group-chat.v2.js`） | 主 bundle 内容变更必须改名版本号并同步改 index.html（30 天 immutable）；**群聊 v25 未改主 bundle，只动 group-chat.v2.js 时该文件自身需改名**；改 bundle 忘改名 = 用户 30 天看不到新功能 |
| 13 | **数据库迁移平滑性** | `init_db()`（grep `def init_db` / `_add_column`） | 所有新表/新列必须 `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN` 式增量升级，老库（生产 data.db）直接热启动不丢数据 |

## 三、测试如何运行

**禁止在生产机执行**（tests/ 有 `_guard.py` 三重防护，详见 [tests/README.md](../tests/README.md)）。

- **全套件门禁**（18 套件串行 + 65s 限流冷却）：
  ```bash
  export RT_TAG=<唯一标签> ADMIN_PW=<管理密码>
  export RT_ALLOWED_DB_DIRS=/opt/newapi RT_ALLOW_RESTART=1
  bash tests/run_all.sh
  ```
  退出码：失败=1；全 PASS 有 SKIP=4（不完整轮次，不得声称全绿）；全 PASS 无 SKIP=0
- **改动后最低回归三件套**：`tests/regression_3000.sh` + `tests/test_pay_model.py` + `tests/test_ui_smoke.py`
- **v25 群聊 e2e**：`tests/test_group_chat_e2e.py`（31 项断言，需测试站 uvicorn 运行在 :3100）：
  ```bash
  RT_TAG=<唯一标签> python3.11 tests/test_group_chat_e2e.py <tag>
  ```
  覆盖：注册→充值→建群→SSE 编排→记忆双写→单聊注入→余额扣减→402/边界
- **环境变量纪律**：
  - `RT_TAG`：套件间必须不同（注册接口限流 5/分，撞限流等 65s），测试用户一律 `rttest` 前缀
  - `RT_ALLOWED_DB_DIRS`：声明允许的数据库目录（缺一即拒绝执行，退出码 2）
  - `RT_ALLOW_RESTART=1`：允许重启 new-api 的用例（test_reconciliation）必须显式开启
  - `ADMIN_PW`：管理密码只经环境变量传入，任何脚本不得硬编码
- **数据铁律**（[docs/执行铁律-数据保护.md](执行铁律-数据保护.md)）：只删 `rttest_` 前缀的自己创建的测试账号；真实用户数据永不触碰

## 四、已知取舍与风险区（审查时知悉，勿当 bug 报）

1. **测试站 `/tmp` 重启清空**：服务器 `/tmp/chatsite-test` 重启即失，重建 = 解压 v23 全量备份包 + 覆盖最新 GitHub 代码 + touch gateway.on（见 TECHNICAL.md 第 5 节）
2. **单 worker 部署**：uvicorn 单 worker，SQLite 并发写依赖 `BEGIN IMMEDIATE` 串行化——多 worker 部署会破坏 in-flight 计数与内存锁语义，属已知架构约束
3. **群聊头像快照**：群成员头像取建群时快照，之后用户改单聊人设不会回写群头像（待站长决策）
4. **群聊一轮制**：不做 LLM 选人/多轮自动互聊，固定按加入顺序每人发言一次（防烧钱的主动取舍）
5. **群定义与消息存服务端**：换设备一致，但聊天记录上服务器（敏感数据，拖库即泄露）
6. **mock 上游验收局限**：mock 的 token 统计/缓存行为不能替代真实计费；上线前必须用真实 DeepSeek Key 完成带缓存命中的计费验收
7. **new-api 落账延迟**：群聊 done 事件的回合消耗最多等 logs 表落账 10s，未落账提示以钱包流水为准
8. **上传链路直连官方**：测试上传会在官方账号留小文件（无计费影响）；多渠道轮询时 `gw_files_key()` 需改渠道亲和

## 五、历史审查记录

R10–R19 复验轮次记录见仓库 Issue #1（安全收口：请求体上限/chunked、ClientDisconnect、RecursionError、multipart、版本兼容均已实测）。新审查发现建议先对照历史记录避免重复报告。
