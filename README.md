# Miandning AI 聊天站（deepseek-billing-chat）

DeepSeek 官网复刻风格的 AI 聊天站，采用**余额制计费**：用户注册即开通 → 扫码充值（站长人工审批）→ 对话按 token 实时扣费 → 余额归零拦截（402）。

**架构三件套**：

- **前端**：DeepSeek 官网复刻 SPA（React 压缩 bundle），内容变更必须**改名 bundle** 绕过 30 天 immutable 缓存；v25 起另有独立群聊模块 `static/js/group-chat.v2.js`
- **后端**：单文件 FastAPI `app.py`（约 5000 行），SQLite `data.db`（用户/充值单/流水/云备份）
- **计费网关**：new-api（本机内网端口），所有用户请求统一经网关改写为站点默认模型计费，用户无感知；定价 = 官方实时价 × 模型倍数 × 时段倍数，每 10 分钟自动抓取官方价

## 目录结构

```
├── app.py                 # 站点后端（单文件 FastAPI：注册/钱包/充值审批/云同步/网关代理/群聊编排/管理面板）
├── static/
│   ├── index.html         # SPA 入口（bundle 引用名需与实际文件一致）
│   ├── static/js/         # main.fe540c09.js（主 bundle）+ group-chat.v2.js（v25 群聊，独立模块）
│   └── static/css/        # main.56567ac0.css
├── tests/                 # 全量回归测试套件（运行方式见 tests/README.md）
│   ├── regression_3000.sh # 基础回归（bash）
│   ├── run_all.sh         # 统一门禁（18 套件串行；退出码：失败=1 / SKIP>0=4 / 全过=0）
│   ├── test_group_chat_e2e.py  # v25 群聊 e2e（31 项断言，依赖测试站 :3100）
│   ├── _guard.py          # 防误跑生产机守卫（数据库路径必须 /tmp 或 RT_ALLOWED_DB_DIRS 声明）
│   └── ...                # 资金专项/并发/隔离/对账/UI 冒烟/性能等
└── docs/
    ├── TECHNICAL.md                    # ★ 技术手册（系统概述/生产环境/计费/关键机制/部署流程）
    ├── 部署指南-DeepSeek聊天界面.md     # 部署形态与组件说明
    ├── 运维维护手册-MiandningAI.md     # 运维、守护进程、备份、数据铁律
    └── 执行铁律-数据保护.md            # 数据保护铁律（rttest 前缀纪律，最高优先级）
```

## 快速上手

### 测试站（新功能开发环境）

监听 `127.0.0.1:3100`，data.db / one-api.db 与生产完全隔离。服务器上启动：

```bash
cd /tmp/chatsite-test && RT_ALLOWED_DB_DIRS=/tmp ADMIN_PASSWORD='<管理密码>' \
  GATEWAY_ENABLED=1 NEWAPI_DB=/tmp/chatsite-test/one-api.db NEWAPI_BASE=http://127.0.0.1:13001 \
  nohup /home/admin/chat-site/venv/bin/uvicorn app:app --host 127.0.0.1 --port 3100 > /tmp/test3100.log 2>&1 &
```

⚠️ 服务器 `/tmp` 重启会被清空，重建方法见 `docs/TECHNICAL.md` 第 5 节。

### 生产部署（测试站 → 生产）

摘要流程（**完整步骤与验证清单见 `docs/TECHNICAL.md` 第 6 节**）：

1. 测试站全套件绿 → commit → push GitHub
2. 备份生产对应文件
3. `scp` 变更文件到 `/home/admin/chat-site/`；**前端 bundle 必须改名**并同步改 `static/index.html` 引用
4. `sudo systemctl restart chat-site` 并验证服务状态、bundle 引用名
5. 真浏览器冒烟（注册→登录→发消息→钱包→后台），测试号清理

## 技术手册入口

- **★ 技术手册**：[docs/TECHNICAL.md](docs/TECHNICAL.md) —— 系统概述、生产环境速查、模型与计费、关键机制（改代码前必读第 4 节）、部署流程、环境变量、常见运维
- **部署指南**：[docs/部署指南-DeepSeek聊天界面.md](docs/部署指南-DeepSeek聊天界面.md)
- **运维维护手册**：[docs/运维维护手册-MiandningAI.md](docs/运维维护手册-MiandningAI.md)（第〇章数据保护铁律对所有维护者生效）
- **执行铁律 · 数据保护**：[docs/执行铁律-数据保护.md](docs/执行铁律-数据保护.md)
- **审查指引**（供代码审查 agent 使用）：[docs/REVIEW-GUIDE.md](docs/REVIEW-GUIDE.md)
- **测试守则**：[tests/README.md](tests/README.md)（禁止在生产机执行、统一门禁、RT_TAG 纪律）

## 当前版本状态

- **v25（2026-11-23）**：角色群聊已开发完成——多角色拉群（2-8 人）、固定一轮编排（硬上限=成员数×1 次调用，绝不自增轮次防烧钱）、群聊记忆双写（`persona_group_memory`，BEGIN IMMEDIATE 防并发）、走 new-api 正常计费；群聊 e2e 31/31 PASS
- **待办**：v25 待部署到生产（按 `docs/TECHNICAL.md` 第 6 节流程）
- 版本演进详见 git log 与 `docs/TECHNICAL.md`
