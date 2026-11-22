# Miandning AI（deepseek-billing-chat）技术文档

> 更新日期：2026-11-21 · 代码版本：`c34c913`（v23 卡片化面板后）
> 本文档与生产部署状态同步，测试站开发新功能前请先阅读第 2、5 节。

## 1. 系统概述

DeepSeek 风格的 AI 聊天站（仿 DeepSeek 官网 UI），采用**余额制计费**：
用户注册即开通 → 扫码充值（站长人工审批）→ 对话按 token 实时扣费 → 余额归零拦截（402）。

```
用户浏览器 (SPA, main.fe540c09.js)
    │  Anthropic 协议 /anthropic/v1/messages
    ▼
chat-site (FastAPI/uvicorn :8300, systemd)
    │  网关代理：余额预检 → 改写模型/参数 → 计费 Key 注入
    ▼
new-api (127.0.0.1:13001, 仅本机可访问)
    │  OpenAI 格式转发，quota 结算落库 one-api.db
    ▼
DeepSeek 官方 API（渠道 Key，模型 deepseek-flash）
```

- 前端：DeepSeek 官网复刻 SPA（React 压缩 bundle），内容变更必须**改名 bundle**（fe540c06→07→08→09）绕过 30 天 immutable 缓存；v25 起另有独立群聊模块 `static/js/group-chat.v2.js`（同样按文件名版本绕缓存）
- 后端：单文件 `app.py`（~4400 行），SQLite `data.db`（用户/充值单/流水/云备份）+ `one-api.db`（new-api 计费库）

## 2. 生产环境（勿改动，仅供参照）

| 项目 | 值 |
|---|---|
| 服务器 | `ssh admin@120.24.229.230`（密钥 `/tmp/prod_diag_key3`） |
| 站点目录 | `/home/admin/chat-site/`（app.py + static/ + data.db） |
| 服务 | `systemctl restart chat-site`（uvicorn :8300，nginx HTTPS 反代 chat.miandning.xyz） |
| new-api | `127.0.0.1:13001`（3001 被 Gitea docker 占用；iptables 限本机） |
| 计费库 | `/home/admin/new-api/one-api.db` |
| 管理后台 | `/admin`，密码 `Wcy0511.` |
| 最新全量备份 | `~/chat-site-backup-v23-full-20261121.tar.gz`（42MB，含 app.py/static/data.db/one-api.db） |
| 历史备份 | `~/chat-site-backup-20261121/`、`~/chat-site-backup-v20/`（各次改动前的单文件备份） |

## 3. 模型与计费（当前生产配置）

- **站点对话模型**：`deepseek-flash`（API 名，背后是 DeepSeek-V4.1-Flash-1017）。网关把所有请求的 model 统一改写为此值，用户无感知也不可选
- **上游链路**：chat-site → new-api「DeepSeek官方渠道」→ 官方 API，**原名直传无 model_mapping**
- **计费口径**：官方 Flash 档实时价 × 倍率 **1.2**（四个模型名均 1.2）；峰谷自动切换（峰时=UTC 周一至周五 01:00-04:00 与 06:00-10:00，中国节假日全天谷时；CN_HOLIDAYS 已录入至 2027 年）
- **模型名映射**：`deepseek-chat`→Flash 档、`deepseek-reasoner`→Pro 档（官方 legacy 规则）；`deepseek-v4-pro` 对应 DeepSeek-V4-Pro-0813
- 切换站点模型：后台「定价」页签 →「站点对话模型」（倍率自动随档位匹配）
- 站长赠送额度：环境变量 `GW_DEFAULT_GIFT_CNY`（当前 0，不赠送）

## 4. 关键机制（改代码前必读）

1. **充值审批 saga**（`/admin/wallet/pay`）：pending →(原子占单)→ processing →(幂等加款 `quota_ops` 唯一操作号)→ approved；崩溃/重试绝不重复加款。**页面表单必须携带 `action=approve/reject`**（v21 曾因漏字段导致点击无效）
2. **余额调账**：`/admin/wallet/adjust` 带 `dir=add/deduct`，金额恒取绝对值、按按钮定方向（v20 修复扣减变充值）
3. **幂等充值申请**：`/api/wallet/pay-request` 强制 `client_request_id`，重复提交返回同一订单
4. **402 拦截**：余额 < ¥0.005 拒绝对话；前端「充值」链接原地打开钱包弹窗（`window.__gwOpenWallet`），不跳外部
5. **SSE 兼容回退**：无 ReadableStream 的魔改内核（QQ 浏览器等）自动整包解析，避免原始 SSE 文本当错误显示
6. **收款码上传**：≤10MB（大图 Pillow 自动压缩），成功 303 带 `#sec-pay` 锚点直达区块；用户端点击可全屏放大
7. **admin 面板**：页签式切换（body.tabbed，仅显示当前 sec；锚点/303 重定向直达对应页签并记忆上次页签）；顶部总览条（待审充值/用户数/总余额/今日消耗/未用充值码）；移动端表格卡片化（`table.cardable` + `data-l` 标签）；已拒绝/禁用用户可「删除」（级联清理 gateway_map、cloud_sessions、wallet_tx、pay_orders、new-api 用户/token，正常用户须先禁用）
8. **注册**：免审批即 `approved`；登录页无任何审核文案
9. **文件上传绕过 new-api**（v24 修复）：`/api/upload` 在网关模式下**不走 new-api**——new-api 对 `/v1/files` 按对话中转强制要求 model 参数（Files API 无 model 字段）→ 必 400「Model name not specified」。改用 `gw_files_key()` 读 new-api 渠道表里启用的 DeepSeek 官方 Key，直连 `/anthropic/v1/files`（x-api-key + anthropic-beta: files-api-2025-04-14）。与聊天渠道同一 DeepSeek 账号，file_id 照常引用；上传不计费，解析 token 在聊天时仍走 new-api 扣费。**若未来 new-api 增加多渠道轮询，此处需改为按渠道亲和上传**（单渠道部署无此问题）
10. **purpose 白名单必须与前端对齐**（v24.1 修复）：前端对 pdf/doc/docx/ppt/pptx 传 `purpose=rich_layout_recognition`（DeepSeek 文档版式解析），其余传 `user_data`；服务端白名单漏掉 `rich_layout_recognition` 会导致 purpose 被静默丢弃、上游 400——表现为「图片传得上、Word 传不上」（2026-11-22 生产实测）。后续前端若新增 purpose 取值，必须同步扩服务端白名单
11. **角色群聊**（v25 新增）：把几个单聊角色拉进一个群，用户发一条消息后所有角色**按加入顺序各发言一次**（固定一轮、轮内可接话，硬上限=成员数×1 次调用，绝不自增轮次防烧钱）。要点：
    - 数据模型：`chat_groups`（uid/name/members_json）、`chat_group_messages`（sender_type=user/persona/system）、`persona_group_memory`（uid+角色名 主键），全部 `CREATE TABLE IF NOT EXISTS` 平滑升级
    - 编排端点 `POST /api/groups/{gid}/messages` 返回 SSE（user/reply_start/delta/reply_end/reply_error/done）；单角色失败发 `reply_error(kind=persona_failed)` 不阻塞他人；余额不足（<¥0.005 或预估成本超余额）发 `kind=insufficient_balance` 并终止整轮；发消息前还有 402 第一道闸与 10 条/分钟限流
    - 计费：每个角色一次独立 LLM 调用，直连 `NEWAPI_BASE/v1/messages`（Bearer=发起用户 token），走 new-api 正常扣费；done 事件带回合总消耗（logs 表落账有延迟，最多等 10s，未落账则提示以钱包流水为准）
    - 记忆双写：每条角色回复完成后立即把「本轮用户消息+截至该角色的全部发言」追加进该角色的 `persona_group_memory`（单条目封顶 600 字/角色、总量 12000 字滚动裁剪，BEGIN IMMEDIATE 防并发写）；**单聊时**由 `_gw_proxy` 解析 system 里的`【角色名】`自动把记忆注入 system（`GET /api/persona-memory` 可查，`GC_SINGLE_INJECT_CHARS=3000` 控制注入成本）
    - 前端：独立 overlay 模块 `static/js/group-chat.v2.js`（**主 bundle 未改动，无需改名**；本文件自身变更需改名版本号绕 immutable 缓存），index.html 追加引用。选人数据源是 `GET /api/personas`（服务端解析 cloud_sessions 里的 persona，头像服务端缩到 96px）——云端会话是权威数据源，本机 IndexedDB 只作回退；侧栏尝试注入「👥角色群聊」入口（中英文案都匹配），另有右下角 FAB 兜底；群面板自带群会话列表/消息渲染（角色名+头像）/费用提示与回合消耗条；402 时尝试原地打开钱包弹窗（window.__gwOpenWallet）
    - 选人要求：群名 1-30 字；成员 2-8 个、角色名唯一≤24 字（来自已有单聊角色的 persona.name）
    - 已知取舍（待站长决策）：一轮制编排（不做 LLM 选人/多轮自动互聊）；群定义与消息存服务端、换设备一致；群成员头像取建群时快照，之后改单聊人设不会回写

## 5. 测试站（新功能开发环境）

- 目录：服务器（生产同机）`/tmp/chatsite-test`（git 仓库，与 GitHub `chenyang-1532/deepseek-billing-chat` main 同步；本地沙箱同名目录为开发副本）。⚠️ 服务器 `/tmp` 重启会被清空，重建方法：解压 `~/chat-site-backup-v23-full-20261121.tar.gz` → 覆盖为最新 GitHub 代码 → touch gateway.on
- **当前状态**（2026-11-21 v24）：app.py = 生产版（md5 `7d70dc7ed94d65d294a8223549206663`），监听 `127.0.0.1:3100`（3000 被开源雷达 node 服务占用）；data.db / one-api.db 均为 v23 备份的**独立副本**，与生产库完全隔离
- 启动（服务器上）：
  ```bash
  cd /tmp/chatsite-test && RT_ALLOWED_DB_DIRS=/tmp ADMIN_PASSWORD='WIyWKVcl5Ey5sM4K' \
    GATEWAY_ENABLED=1 NEWAPI_DB=/tmp/chatsite-test/one-api.db NEWAPI_BASE=http://127.0.0.1:13001 \
    nohup /home/admin/chat-site/venv/bin/uvicorn app:app --host 127.0.0.1 --port 3100 > /tmp/test3100.log 2>&1 &
  ```
- 管理密码 `WIyWKVcl5Ey5sM4K`；上传链路测试直连真实 DeepSeek（用渠道 Key，测试上传会在官方账号留下小文件，无计费影响）

### 测试纪律

- 全套件入口：`RT_TAG=xx ADMIN_PW='WIyWKVcl5Ey5sM4K' RT_ALLOWED_DB_DIRS=/opt/newapi RT_SITE_DIR=/tmp/chatsite-test RT_NAPI_DB=/opt/newapi/one-api.db bash tests/run_all.sh`（18 套件）
- 资金类套件必须指向**运行中**的库路径（静态副本探测必失败）
- 注册接口限流 5/分：套件间用不同 `RT_TAG`，撞限流时等 65s 再跑
- 改动后最低回归：`regression_3000.sh`(58) + `test_pay_model.py`(62) + `test_ui_smoke.py`(35) 三件套
- 推送 GitHub 偶发 TLS 断连：循环重试至 `git ls-remote` 确认远端 HEAD 为本地提交

## 6. 部署流程（测试站 → 生产）

1. 测试站全套件绿 → commit → push GitHub
2. 备份生产对应文件（现已有 v23 全量包，单文件改动可 cp 到 `~/chat-site-backup-v20/`）
3. `scp` 变更文件到 `/home/admin/chat-site/`；**前端 bundle 必须改名**并同步改 `static/index.html` 引用
4. `sudo systemctl restart chat-site`；验证：`systemctl is-active` + 首页 303 + bundle 引用名
5. 真浏览器冒烟（注册→登录→发消息→钱包→后台），测试号清理

## 7. 环境变量（chat-site）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 必设 | 管理后台密码 |
| `NEWAPI_DB` | 必设 | one-api.db 路径 |
| `NEWAPI_BASE` | 必设 | new-api 地址（生产 `http://127.0.0.1:13001`） |
| `GW_DEFAULT_GIFT_CNY` | 0 | 新用户赠送金额（元） |
| `GW_UPSTREAM_BASE` | https://api.deepseek.com | 网关上游 |
| `SITE_TZ` | Asia/Shanghai | 时段倍率/今日消费/时间戳基准 |

## 8. 常见运维

- 看生产日志：`sudo journalctl -u chat-site --since 'HH:MM' --no-pager`
- 计费落账查询：one-api.db `logs` 表（type=2 为消费），`users.quota` 为余额（quota/1000000×7.2=人民币元；换算基准 `cny_to_quota = cny×1000000/7.2`）
- 官方价每 10 分钟自动抓取（api-docs.deepseek.com/quick_start/pricing），抓取失败>24h 后台顶部会出现红色告警条
- 用户反馈「看不到新功能」：先让用户强刷——bundle 改名策略下不存在缓存问题，若未改名则会命中 30 天缓存
