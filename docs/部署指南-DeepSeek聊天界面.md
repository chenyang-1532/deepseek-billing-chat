# MiandningAI 聊天站（v17.7）部署运维指南

> ⚠️ 本文档对应 **v17.7 服务端计费形态**（Python 后端 + SQLite + new-api 计费网关）。
> 早期「纯静态、无后端、用户自带 Key」版本已淘汰——如果任何文档/交接材料仍那样描述，以本文为准。

## 一、系统形态（先读这个）

| 组件 | 说明 |
|---|---|
| 主站点 `app.py` | 单文件 FastAPI 应用（uvicorn :3000），含注册/钱包/充值审批/云同步/管理面板 |
| `data.db`（SQLite） | **核心数据**：用户、余额流水（wallet_tx）、充值订单（pay_orders）、兑换码、云端会话（cloud_sessions） |
| `new-api`（:3001） | 计费网关：按 ModelRatio/CompletionRatio/CacheRatio 对 token 计费，库 `one-api.db` |
| `mock_upstream.py`（:3002） | 测试用 mock 上游；**接入真实 DeepSeek Key 后由 https://api.deepseek.com 替代** |
| `secret.key` | 会话签名密钥（0600）；空/过短自动轮换重建，**所有用户与管理员会话立即失效**（需重新登录） |
| `snapshots/` | 每次云同步上传的完整导出包存档（每用户保留最近 30 份） |
| `gateway.on` | 开关文件：存在才启用网关计费链路 |

**关键安全事实（运维必须知晓）**：

1. **API Key 是站长的共享计费 Key**（通过 new-api 网关注入），不是用户各自的 Key。泄露 = 全站被薅 + 账单打在站长头上。
2. **全部聊天内容都在服务器上**：`data.db` 的 `cloud_sessions` 表 + `snapshots/` 目录。服务器被拖库 = 全量聊天泄露。请按敏感数据对待。
3. 用户余额、订单、流水都在 `data.db`——**不备份 = 无法对账 + 数据全丢**。

## 二、必须备份的路径

```bash
# data.db 在线备份（wal 模式安全备份，不要直接 cp 运行中的文件）
sqlite3 /path/to/data.db ".backup '/backup/data-$(date +%F).db'"
sqlite3 /opt/newapi/one-api.db ".backup '/backup/newapi-$(date +%F).db'"
cp -a /path/to/snapshots/ /backup/snapshots-$(date +%F)/
cp -a /path/to/secret.key /backup/secret.key   # 0600 权限一并保留
```

建议每日 cron。恢复：停服 → 覆盖 → 启服。

## 三、环境变量与配置

| 变量/文件 | 含义 |
|---|---|
| `ADMIN_PASSWORD` | 管理面板密码（systemd 服务文件内，泄露按已泄露处理：轮换见 §6） |
| `SITE_TZ` | **站点时区（默认 Asia/Shanghai）**：时段倍率规则、"今日消费"起点、订单/流水时间戳的基准。Docker/云主机默认 UTC 会导致规则整体偏 8 小时——**务必显式设置** `SITE_TZ=Asia/Shanghai`（systemd: `Environment=SITE_TZ=Asia/Shanghai`；docker: `-e SITE_TZ=Asia/Shanghai`）。启动日志会打印生效时区。官方峰谷判定例外：固定按 UTC（DeepSeek 官方口径） |
| `NEWAPI_DB` | new-api 库路径（默认 `/opt/newapi/one-api.db`），站点直连其 options/users 表 |
| `GATEWAY_ENABLED` | 网关计费总开关（环境变量），配合 `gateway.on` 文件双保险 |
| `data.db` 设置表 | `gw_default_model`（强制改写的模型）、`gw_multipliers`（每模型倍数）、时段倍率规则等，管理面板可改 |

## 四、部署步骤

```bash
# 1. 依赖
apt install python3.11 python3-pip sqlite3
pip3 install fastapi uvicorn requests

# 2. new-api（计费网关）
#    部署于 :3001，库文件默认 /opt/newapi/one-api.db
#    建一个 DeepSeek 渠道（type 选 DeepSeek，填真实 Key）；测试环境用 mock 上游 :3002

# 3. 主站点
cd /opt/chatsite
ADMIN_PASSWORD='<强密码>' setsid python3.11 -m uvicorn app:app --host 0.0.0.0 --port 3000
touch /opt/chatsite/gateway.on    # 启用网关计费

# 4. Nginx 反代（必须，见下）
```

### Nginx 反代（含 X-Forwarded-Proto，Cookie Secure 依赖它）

```nginx
server {
    listen 443 ssl;
    server_name chat.miandning.xyz;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;   # 必须：站点据此发 Secure Cookie
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 360s;                       # 网关流式响应最长 300s
    }
}
```

`_req_secure()` 的 HTTPS 探测顺序：`X-Forwarded-Proto` 头 → `url.scheme`。只走反代时必须配置该头，否则登录 Cookie 不带 Secure。

## 五、日常运维

| 任务 | 操作 |
|---|---|
| 看余额对账 | 管理面板 → 状态页（`/admin`）；不变量：`new-api users.quota == Σ充值 − Σ消费` |
| 官方价同步 | 后台每 10 分钟自动抓取 api-docs.deepseek.com 并重算三组倍率；面板定价区显示数据时间与变动/stale 红色告警；**峰谷=UTC 周一至五 01:00-04:00 与 06:00-10:00，法定节假日全天谷时（`CN_HOLIDAYS` 每年按国务院通知更新一次，需指定负责人）** |
| 充值审批 | 面板审批 `pay_orders`（pending→processing 原子占单，可安全重复点） |
| 兑换码 | 面板生成（一次性响应页）；状态机 unused→processing→used，崩溃自动恢复 |
| 用户会话全失效 | 先停服务 → 删除/清空 `secret.key` → 再启动（自动轮换新密钥）。**不要在多 worker 运行中直接删**——各 worker 会各自轮换出不同密钥，登录随机掉线且难排查 |
| 管理登录被锁 | 连续 5 次失败锁 300 秒（内存计数），重启站点即清 |

## 六、共享计费 Key 泄露处置流程

1. 立即在 new-api 后台**禁用/删除该渠道**（先止血，全站暂停服务）；
2. 撤销/更换 DeepSeek 平台侧 Key，建新渠道；
3. 查 `new-api` `logs` 表按时间窗核对异常消费，估算损失；
4. 检查 `data.db` 管理登录审计日志与异常注册；
5. 若怀疑服务器被入侵：轮换 `ADMIN_PASSWORD`（见下）+ `secret.key`（**先停服务 → 删除 → 再启动**，自动重建；直接删会让多 worker 各持一把不同密钥）+ 服务器 SSH 凭据。

**正式站管理密码轮换**（泄露或定期）：

```bash
NEWPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
sudo sed -i "s|ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$NEWPW|" /etc/systemd/system/chat-site.service
sudo systemctl daemon-reload && sudo systemctl restart chat-site
echo "$NEWPW"   # 仅本次显示，立即转存密码管理器并清终端历史
```

## 七、测试与回归

```bash
export RT_TAG=<唯一标签> ADMIN_PW=<管理密码>
export RT_ALLOWED_DB_DIRS=/opt/newapi   # 显式声明测试实例目录（防误跑生产机）
export RT_ALLOW_RESTART=1               # 仅测试环境允许 new-api 重启类用例
bash tests/run_all.sh                   # 统一门禁：12 套件串行，任一失败总退出码 1
```

**禁止在生产机执行**：所有 `tests/` 脚本会写数据库、删除测试用户、（在显式允许时）重启 new-api 进程。`_guard()` 会在数据库路径不在 `/tmp/` 且未声明 `RT_ALLOWED_DB_DIRS` 时以退出码 2 拒绝执行。

## 八、已知边界

- `prompt_cache_hit_tokens` 按官方缓存命中价计费（`CacheRatio`：flash 0.02 / v4-pro ≈0.033333，取两时段较小值——宁可少收不错收）；**需确认 new-api 版本支持缓存分档**，且渠道级 `setting.cache_ratio` 未覆盖全局值（当前渠道为空 = 生效全局）。
- 接入真实 DeepSeek Key 前，token 统计与缓存命中行为由 mock 上游模拟，**不能替代真实验收**（最终验收须用真实 Key 跑一笔带缓存命中的长对话，核对 `logs.quota` 与分档手算值误差 ≤1%）。
