# Miandning AI 聊天站 · 运维维护手册

> 最后更新：2026-11-18 · 由部署助手生成
> 本文档同时存放于服务器 `/home/admin/chat-site-backup-20261118/` 内

---

## ⛔ 第〇章 · 数据保护铁律（任何操作前必读，优先级高于一切）

**任何人（包括 AI 助手、自动化脚本、维护人员）在测试、维护、升级本站时，必须遵守以下铁律，违反即视为事故：**

1. **测试账号只动自己的**。测试需要账号时，**只允许新建**以 `rttest_` 前缀命名的专用测试账号；测试结束后**只允许删除这些自己创建的 `rttest_` 测试账号**（及其产生的备份记录），**其余任何真实用户一律不得删除、修改、禁用**。
2. **禁止触碰真实用户的对话与备份数据**。`cloud_backups` 表中真实用户的记录、备份 ZIP 文件、用户浏览器端的聊天记录（IndexedDB）——**任何情况下不得删除、清空、覆盖、截断**。哪怕只有一条，也不行。
3. **删除操作必须带 `rttest_` 前缀限定**：用户只允许 `WHERE username LIKE 'rttest_%'` 条件删除；备份只允许 `WHERE uid IN (SELECT id FROM users WHERE username LIKE 'rttest_%')`。禁止执行不带此限定的 `DELETE FROM users` / `DELETE FROM cloud_backups` 类 SQL。
4. **测试只针对功能本身**：接口返回值、页面渲染、缓存头、门禁逻辑等，全部只读验证 + 使用新建的测试账号，不依赖、不修改、不清理任何已有用户数据。
5. **升级部署时只允许增量变更**：覆盖代码/静态文件可以，但 `data.db`、`secret.key` 在任何升级操作中必须原样保留，操作前先备份（§七）。
6. 如果某个操作**不确定是否会影响用户数据，先停下**，备份 `data.db` 并征得站长确认后再做。

> 历史说明：v1~v3 开发期的测试账号在站长明确要求下清理过。自本铁律生效之日（2026-11-18）起：**助手自己的 `rttest_` 测试账号测完可自行清理，真实用户的一切数据永不触碰**。

---

## 一、系统速查卡（最重要）

| 项目 | 值 |
|---|---|
| 正式地址 | https://chat.miandning.xyz |
| 管理面板 | https://chat.miandning.xyz/admin |
| 管理密码 | **不在此记录**（存于 systemd 服务文件 `ADMIN_PASSWORD=`，改法见 §5；按已泄露处理已轮换） |
| 服务器 | 阿里云 ECS · 深圳 · 公网IP `***.***.***.***` · 2核2G · 到期 2027-07-31 |
| SSH | `ssh admin@***.***.***.***` 端口22 · 密钥 `workbuddy-deploy-20261118` |
| 应用目录 | `/home/admin/chat-site/` |
| 系统环境 | Alibaba Cloud Linux 4 · Python 3.11.6 · Nginx 1.30.2 |
| 技术栈 | FastAPI + SQLite（后端单文件 app.py）+ chat-demo v0.1.1 定制构建（前端静态） |

⚠️ **SSH 部署公钥收回方法**（部署助手用的临时钥匙，确认不再需要远程协助后执行）：
```bash
sed -i '/workbuddy-deploy-20261118/d' ~/.ssh/authorized_keys
```

## 二、目录结构

```
/home/admin/chat-site/
├── app.py          # 后端全部逻辑（用户/审核/备份/公告/门禁），单文件
├── static/         # 前端构建产物（chat-demo 定制版：品牌名+云端同步+会员倒计时）
│   ├── index.html
│   └── static/js|css|media/   # 文件名带内容哈希
├── data.db         # SQLite 数据库：用户/云端备份/公告设置 ★核心数据
├── secret.key      # 登录Cookie签名密钥 ★丢失则所有用户需重新登录
├── venv/           # Python 虚拟环境（fastapi/uvicorn/python-multipart）
├── regression.sh   # 全量回归测试脚本（49项）
└── docs/           # 本手册与备份文件
```

## 三、功能与数据流

- **用户隔离**：API Key 存用户浏览器 localStorage，聊天记录存 IndexedDB；服务器只存"账号 + 云端备份快照 ZIP"
- **云端同步（v5 起为会话级合并）**：前端防抖2秒推送整包 + 删除墓碑；服务端按会话合并（同一会话谁新用谁），多设备互不覆盖；响应 changed=true 时客户端自动拉取合并结果；服务端墓碑下发给各设备同步删除。表 `cloud_sessions(uid,sid,session_json,images,updated_at,deleted)`
- **门禁**：所有请求经 FastAPI 门禁（session Cookie，30天有效），未登录→/login，到期/待审→状态页；静态文件带哈希的可长缓存，index.html 永不缓存（保证升级即时生效）
- **文件上传中转（v16 新增）**：`POST /api/upload?name=<文件名>&purpose=<user_data|rich_layout_recognition>`，body 为原始二进制流，`x-api-key` 头透传用户自己的 DeepSeek Key。服务器在机房网络重建标准 multipart 转发 `https://api.deepseek.com/anthropic/v1/files`（可用环境变量 `DEEPSEEK_BASE` 覆盖，供测试）。**为什么要中转**：手机浏览器直连 DeepSeek 上传 multipart 会被运营商/夸克等代理链路改坏导致"上传失败"（云同步 multipart 也踩过同一坑）；聊天 JSON 请求小不受影响，仍为直连
- **缓存策略**：`static/` 下哈希文件 `immutable 30天`；升级=换文件名，用户自动拿到新版，无需清缓存，且不影响本地聊天记录

## 四、日常运营操作

均在管理面板 `/admin` 完成（手机已适配）：
- **审核**：待审核卡片橙色置顶 → 填天数 → 点「通过」
- **续期**：用户卡片点「续期」填天数（原有效期未过则顺延）；用户可在站内申请续期，卡片会显示 ⚡
- **禁用/启用**：违规用户点「禁用」（立即生效）
- **公告**：顶部「登录页公告」编辑框，保存后立即显示在登录页；留空则不显示
- **备份用量监控**：「云端备份用量」区块显示总量与每账号占用；账号达 160MB(80%) 自动 ⚠️ 标红

## 五、服务管理命令

```bash
sudo systemctl status|restart|stop chat-site   # 服务管理
sudo tail -f /var/log/messages | grep chat-site # 或 journalctl -u chat-site 看日志
sudo nginx -t && sudo systemctl reload nginx    # nginx 配置生效
```

**改管理密码**：
```bash
sudo nano /etc/systemd/system/chat-site.service   # 改 ADMIN_PASSWORD= 行
sudo systemctl daemon-reload && sudo systemctl restart chat-site
```

## 六、升级前端版本（chat-demo 新版发布时）

1. 本地/沙箱获取官方新源码，重新应用三处定制（见 §九·定制清单），`pnpm type && pnpm build`
2. 上传新 dist 覆盖 `/home/admin/chat-site/static/`（旧哈希文件可留可删，index.html 必须是新的）
3. **无需重启服务**（静态文件实时读取），用户下次打开自动获得新版
4. 跑一遍 `/home/admin/chat-site/regression.sh` 回归确认

⚠️ 官方大版本可能调整本地存储格式，升级前先小范围验证云端备份的导入兼容性。

## 七、备份与恢复

**自动定时备份（2026-11-18 起已配置，每 2 小时）**：cron 每 2 小时执行 `~/chat-site/backup_cron.py`，把 `data.db` + `secret.key` + 每个用户的云端聊天快照 ZIP 备份到 `/home/admin/chat-site-backups/backup-YYYYmmdd-HH/`（含 `chats/用户名.zip` 会话包 + `snapshots/用户名.zip` 最新完整导出原包），保留最近 14 天自动清理，日志在 `~/chat-site-backups/cron.log`。备份目录权限 `700`、全部文件 `600`（仅 admin 可读），脚本内置 `umask 077`。

**手动备份（可随时执行）：**
```bash
cd /home/admin/chat-site
./venv/bin/python backup_cron.py   # 立即做一次完整备份
```

**恢复**：停服务 → 用备份覆盖 `data.db` 和 `secret.key` → 起服务。两个文件必须成对恢复（Cookie签名才不会失效）。

**已有备份位置**：`/home/admin/chat-site-backup-20261118/`（含手册、代码快照、当日数据库、nginx/systemd 配置）

## 八、故障排查

| 症状 | 排查 |
|---|---|
| 网站 502/打不开 | `systemctl status chat-site`；`journalctl -u chat-site -n 50` |
| 登录后无限跳登录页 | `secret.key` 是否被换过/重启丢失（文件不在=随机生成） |
| 用户说聊天记录没同步 | 让其刷新页面；查 `journalctl` 是否有 413（超200MB）；管理面板看该用户备份是否存在 |
| 429 报错 | 用户自己的 DeepSeek 账号并发超限，非本站问题 |
| 402 报错 | 用户 API Key 余额不足 |
| 证书过期告警 | `sudo certbot renew` 手动跑；自动续期已配置（certbot.timer） |
| 全站服务不可用且本站无恙 | DeepSeek 官方故障，等官方恢复 |

## 九、定制清单（重建前端时必须重新应用）

源码仓库：https://github.com/deepseek-ai/chat-demo （v0.1.1，MIT）

1. `src/index.html`：`<title>Miandning AI</title>`
2. `src/i18n/zh.ts` + `en.ts`：`appName: 'Miandning AI'`
3. 新增 `src/services/chat/cloudSync.ts`：云端同步模块（防抖推送/启动比对拉取）
4. `src/app/App.tsx`：useEffect 中调用 `initCloudSync()`
5. 新增 `src/components/sider/MembershipBadge.tsx(.module.css)`：会员剩余时间条（侧边栏底部，导出 `useMembershipInfo` 共享数据钩子）
6. `src/components/sider/Sider.tsx`：底部 `<MembershipBadge />`
7. 新增 `src/components/session/MembershipChip.tsx(.module.css)`：全页漂浮「会员剩 N 天」徽章（手机右上/电脑右下，≤3天变红闪烁）；`src/app/App.tsx` 根布局渲染 `<MembershipChip />`
8. 新增 `src/components/announcement/AnnouncementBanner.tsx(.module.css)`：登录后顶部弹出公告横幅（可关闭，公告内容变更后重新弹出，避让高度经 `--announce-h` 变量传给徽章）；`src/app/App.tsx` 根布局渲染 `<AnnouncementBanner />`
9. **批量管理对话**：`src/components/sider/Sider.tsx` 新增「批量管理对话」模式（多选/全选/批量删除，带确认弹窗）；`SessionItem.tsx(.module.css)` 增加 `selectMode/checked/onToggleSelect` 多选形态
10. **移除导出功能**（站长指令）：设置对话框的「导出所有历史对话」已删除；批量管理只有删除、无导出。`backup.ts` 中 `exportHistory` 仅保留供内部云端同步使用
11. **同步可靠性**：`cloudSync.ts`：变化停止 2 秒推送；导入完成立即推送；失败自动重试3次；关页走 keepalive+sendBeacon 保底通道；`pagehide`/切后台时强制推送未保存的变化，防止用户导入记录后立刻关闭页面导致丢失
12. **定时备份**：服务器 `~/chat-site/backup_cron.py` + crontab 每 2 小时（见 §七）
13. 后端 `app.py`（本站自研）与前端定制版快照都保存在 `chat-site-backup-20261118/`

## 十、版本历史

- **v1** 2026-11-18：基础部署（注册/人工审核/续期/门禁 + chat-demo 原版）
- **v2** 2026-11-18：品牌更名 Miandning AI；注册提示修复；缓存策略（自动升级）；30天登录态；云端聊天同步；公告系统；管理面板手机适配
- **v3** 2026-11-18：管理面板云端备份用量监控（80%预警）；用户侧会员剩余时间提示（≤3天变橙色）
- **v4** 2026-11-18：漂浮徽章版剩余时间；登录用户弹公告横幅；批量管理对话（批量删除）；移除全部导出功能；同步防丢；服务器每6小时定时备份
- **v5** 2026-11-18：**同步架构重做**——整包覆盖改为会话级合并+墓碑删除，根治多浏览器交替使用时互相覆盖/清空数据的问题（此前发现云端被推送空档案、用户数据被清的事故）；定时备份脚本适配会话表
- **v6** 2026-11-18：**同步误删双 bug 修复**（真实双浏览器自动化实测抓出）——
  1. 前端：导入是替换式操作（replaceHistory 一次性换掉全部会话），旧版把"导入期间消失的会话"误判为用户删除、整批发墓碑上传，导致云端与其它设备的这些会话被误标删除。修复：`importingHistory` 期间的会话移除一律不记墓碑（cloudSync.ts）
  2. 前端：墓碑存储 key 由 `cloudSync.deletedIds` 改为 `cloudSync.deletedIds.v2`，作废 bug 版本残留在浏览器里的假墓碑名单，防止旧误删名单再次被推送
  3. 服务端：`_migrate_legacy` 迁移失败/拆不出会话时不再清空旧 blob（此前该缺陷叠加旧版空包覆盖，导致 chenyanggg 的旧云端整包被清；其数据已从 03:47 定时备份恢复，会话「用户发来数字1」已重新入库）
  4. 实测场景全通过：A导入→B自动拉取；B替换导入→不误杀且自动合并；B真删→A刷新同步消失
- **v7** 2026-11-18：登录即双向同步 + 公告弹窗化——
  1. 前端同步再加强：每次推送成功后**无条件**拉取一次云端合并（不再依赖 changed 标记），登录进去立即完成「云端 ∪ 本地」双向对齐，哪个新用哪个
  2. 公告从顶部黄色横幅改为**居中弹窗**（标题「公告」+ 正文 + 蓝色「好的」按钮），风格与 DeepSeek 官方弹窗一致；公告内容变更后会重新弹出
  3. HTML 缓存升级为 `no-store`，杜绝浏览器缓存旧版前端导致"改了没效果"的假象（此前用户"还是不行"很可能是在旧缓存 JS 上测试）
  4. 强制登录：未登录访问任何页面均 303 跳转 /login（原有门禁，本次核实）；登录页输入框已带 autocomplete 属性，浏览器可记住密码一键登录
  5. 用户主动删除的对话：各设备同步删除，且不进入云端备份（墓碑行不导出、blob 置空）
  6. 实测（双浏览器+真实账号 rttest_sync307，测毕已删）：A导入→B自动拉取；B替换导入→不误杀且合并；B真删→A同步消失；公告弹窗正常弹出与关闭
- **v8** 2026-11-18：新增**「同步聊天记录」一键同步按钮**（用户要求的手动同步入口）——
  1. 位置：设置 → 数据管理（Data）页签第一项；描述文案告知用户"换浏览器后点这里找回全部记录"
  2. 行为：一键完成 本机→云端→本机 双向合并，服务端按会话 `updated_at` 时间戳新者胜；全程页面内调 API 完成，用户不接触任何文件包
  3. 完成后显示"同步完成，本机共 N 条对话"，失败显示重试提示
  4. 自动备份不变：本地记录变化 2 秒内自动上传云端（防抖），服务器每 6 小时定时快照；用户删除的对话不进备份
  5. 站长恢复用户数据：`~/chat-site-backups/备份目录/chats/<用户名>.zip` 或 `cloud_sessions` 表（chenyanggg 曾借此恢复）；导入方法见 §七
  6. 实测：新浏览器空账号点「同步」→ 1 条对话完整拉回；测毕测试账号已删
- **v9** 2026-11-18：**修复手机端"同步失败"（真实用户日志确诊）**——
  1. 服务端日志发现手机用户所有 `POST /api/backup` 返回 400/422：手机浏览器或其网络链路（夸克云加速/运营商代理）会改坏 multipart 表单上传，桌面环境无此问题
  2. 修复：前端上传改用**原始二进制流**（application/octet-stream，纯字节流代理无法改写），删除墓碑改放 URL `?deleted=` 参数；服务端同时兼容旧 multipart 格式（灰度平滑）
  3. WebKit（Safari 内核）+ Chromium 双内核实测全 200，curl 验证 raw+墓碑链路正确
  4. 教训：排查线上问题优先看服务端日志状态码，不要只依赖桌面端复现
- **v10** 2026-11-18：**用站长授权的真实账号 chenyanggg 全流程实测后定版**——
  1. 修复手动同步后的 POST→GET 无限循环（拉取引发的导入事件又触发推送；现拉取期间完全静默，推送前取消挂着的防抖计时）
  2. 实测矩阵（Chromium + WebKit/Safari 内核交叉）：登录自动同步 ✓；手动同步"已同步 N 条" ✓；A 端导入 → B 端同步出现 ✓；B 端删除 → A 端刷新消失 ✓；每次手动同步仅 1 组请求（无循环）✓
  3. 该账号手机端此前上传一直 400/422（见 v9），v9 上线后其手机已自动推送成功 4 条真实对话——确认 raw 二进制方案彻底解决手机链路问题
  4. 测试产生的数据已清理，仅保留用户真实对话
- **v15** 2026-11-18：**登录体验与到期策略**——①已登录用户访问任何页面直接进对话页；②已登录访问 /login 自动跳回对话页；③登录态滑动续期（每次打开自动续 30 天，活跃用户永不重登）；④**账号过期立马踢出到登录页**（清登录态 + 跳 /login?expired=1 + 登录页显示过期提示），登录接口拒绝过期账号防止"踢出→登录→又踢出"死循环；⑤全量回归脚本更新为 v14/v15 行为（53 项，改为服务器本地执行，不再依赖 ssh 回环）
- **v16** 2026-11-18：**文件上传走自家服务器中转**——修复手机端（运营商/夸克代理链路）上传文件必失败的问题：浏览器直连 DeepSeek 的 multipart 表单会被链路改坏。新增 `POST /api/upload`（原始二进制流 + URL 传 name/purpose + 透传 x-api-key），服务器重建 multipart 转发 DeepSeek Files API；前端 `uploadFile` 改打同源中转接口，聊天/模型列表仍直连。线上用假 key 验证中转可达 DeepSeek（返回真实 401），回归 53/53 通过。**排障提示**：中转生效后看 `journalctl -u chat-site | grep api/upload` 即可看到每次上传的状态码——402=用户的 DeepSeek Key 欠费（换浏览器没用，充值才能解决）、401=Key 无效、200=成功。前端上传失败提示已细化：402→「API Key 余额不足，请充值后重试」、401→「API Key 无效」
- **v16.1** 2026-11-18：**修复"点开对话被弹回新对话窗口"**——根因：云同步推送成功后无条件拉取，拉取走 importHistory→replaceHistory，该函数把 activeId 硬编码重置为 null，导致任何状态变化（点开对话/发消息/重命名）后 2 秒被踢回新对话。修复：replaceHistory 替换完成后若原 activeId 会话仍存在则保持打开。验证：chenyanggg 真实账号端到端——点开对话等 8 秒（覆盖防抖推送+拉取全程）仍停留在原对话；回归 53/53
- **v16.2** 2026-11-18：**修复"不断闪到对话最底部"**——v16.1 修了 activeId，但"推送成功后无条件整包拉取"仍在：每次拉取导入都会清空 sessionCache → 正在看的会话卸载重挂 → 滚动位置砸回底部，多设备用户每 2 秒一次。修复两处：① 服务端 changed 判定精确化——墓碑情形不再置 changed=True（客户端已通过响应内 tombstones 自行删除，无需整包拉取），只保留"云端有本机缺的会话 / 云端某条更新"两种真缺数据情形；② 前端只在 changed=true 时拉取（手动同步按钮 forcePull 强制双向合并）。本地 mock 验证 6 种场景全过；chenyanggg 移动端模拟（同款 UA）：点开对话滚到中间，等 12 秒滚动位置零漂移，未被弹回；回归 53/53
- **v16.4（2026-11-18 19:00 上线正式站）**：**服务端时间戳比较修复（推送-拉取死循环）**——`api_backup_post` 里云端与客户端会话 `updated_at` 用字符串比较，秒精度 `…16Z` 与毫秒精度 `…16.000Z` 逐字符比较时 `'Z' > '.'`，云端永远被判"比本地新"，客户端每次推送都触发 changed=true → 整包拉取 → 会话重挂载回底部，2 秒一次无限循环（导入第三方秒精度归档的用户必踩）。修复：新增 `_parse_ts`/`_cloud_newer`，按解析后的时间比较，解析失败退回字符串比较。已同步部署测试环境。正式站复验：推送仅 2 次、第二次 changed=false、滚动 12 秒零漂移
- **v16.3（2026-11-18 上线正式站）**：**角色人设系统（结构化角色卡 + 模板库 + SillyTavern 角色卡导入）**——① persona 从单字符串升级为结构化对象：角色名/头像/性格/说话风格/身份背景/开场白/示例对话，system 提示词分节注入（「完全扮演{角色名}」+ 各分区 + 优先级声明），旧版字符串人设按身份设定兼容；② 内置 8 个成品角色模板（教练/日语老师/深夜电台/面试官/监督学姐/古风诗友/侦探/喵管家），欢迎屏一键开局；③ SillyTavern 角色卡导入：PNG tEXt 块（chara/ccv3，V1/V2/V3 全兼容）或 JSON，scenario 并入背景、卡片 PNG 压缩为头像；④ 开场白为**虚拟展示**（空会话时显示，不进消息树/不进请求上下文）——归档格式要求消息树根必须为用户消息，assistant 根会炸掉云端导入（此坑已踩并修复）；⑤ 归档三道白名单（format/schema/toLocal）全部放行 persona 对象，随会话云同步。端到端 18 项全过（模板开局/表单/卡导入/双设备同步）。**已于 2026-11-18 18:36 上线正式站**（v16.3：index.html + main.f438cbe4.js + main.4044c2a7.css + 全部 async 分块）
- **v14** 2026-11-18：**导入同步连环 bug 根治**——① Nginx HTTPS 块补 `client_max_body_size 200m`（配置早已写入但一直未 reload，实际生效默认 1MB，>1MB 的导入包全部 413 被拒，这是"导入的对话同步不上去"的直接原因）；② 服务端墓碑只对云端真实存在的会话生效，从未上传过的 sid 忽略，杜绝垃圾墓碑产生与回传扩散（此前 113 条 NULL 墓碑把所有设备上的导入对话反复误删）；③ 客户端只为"成功上传过的会话"记墓碑（`cloudSync.pushedIds`）；④ 新增"恢复云端备份"按钮：服务器每次推送都原样存档完整导出包（`~/chat-site/snapshots/<uid>/`，保留 30 份），`GET /api/restore` 取回最新包原样导入，等同手动导出/导入的全自动版；⑤ 每 3 分钟定时兜底推送
- **回归**：49项自动化测试全绿（脚本 `~/chat-site/regression.sh`；自铁律生效后，测试账号保留不清理，见 §〇）

---

## v17.1 资金安全加固（2026-11-19，代码审查子代理全量审计后修复）

### 审计结论
代码审查子代理对网关计费全链路（app.py 计费段 / 兑换 / 代理 / 后台 / 前端钱包）做了逐项审计，
发现 🔴严重 4 项、🟠中 6 项、🟡低 18 项。已修复全部 🔴/🟠 项与关键 🟡 项。

### 已修复（按风险从高到低）
| 级别 | 问题 | 修复 |
|---|---|---|
| 🔴 | 兑换码并发竞态：读后判断无锁、"先加钱后销码"，同一码可被并发兑换多次到账 | 两阶段兑换：阶段一原子销码（`WHERE used_by IS NULL`+rowcount 校验+独立事务提交）→ 阶段二跨库加钱 → 失败补偿回滚销码（used_by 置回 NULL） |
| 🔴 | gw_add_quota 读-改-写丢失更新 | 单语句 `UPDATE users SET quota=MAX(0,COALESCE(quota,0)+?)` + BEGIN IMMEDIATE |
| 🔴 | admin 页 `?codes=` 参数反射型 XSS（可诱导管理员点链接刷钱） | `html.escape` codes/流水 note/用户名/created_at 全部转义 |
| 🔴 | 兑换接口零限流，码可被暴力枚举 | 每账号每分钟 5 次（进程内限流表），6 次起 429；错误信息统一为"兑换码无效"不区分不存在/已用 |
| 🟠 | gw_provision 并发重复发 token、gift 流水重复 | token 按 name=site-<uid> 幂等复用；gift 流水按 wallet_tx 是否已有 gift 去重；gateway_map upsert |
| 🟠 | 代理无余额预检，余额为 0 也可打上游 | 代理入口本地查 new-api quota，< ¥0.005 直接 402 |
| 🟠 | 代理缺 DELETE/PUT 路由、丢 query string | 补 DELETE/PUT 路由；透传 query string；拒绝含 `..` 的路径（400）；body 限 10MB（413） |
| 🟠 | adjust 异常被 `except: pass` 静默吞掉、NaN/Inf 未捕获 500 | 全部进 try；失败重定向 `/admin?wallet_err=1` 页面红字提示+写日志；NaN/Inf/超限(>10万)拒绝 |
| 🟠 | gen-codes 面值无上限、NaN 插 NULL 导致兑换 500 | 面值校验 `0<face≤10000` 且 isfinite；INSERT OR IGNORE + 撞码重试；失败提示 wallet_err=code |
| 🟠 | SSE 流中断客户端只收到被掐断的连接 | gen() 捕获异常发 `event: error` SSE 帧；加 X-Accel-Buffering: no / Cache-Control: no-cache；超时改 (10,300) |
| 🟡 | 未登录代理 303→401；/api/wallet u 判空；wallet_tx 加 (uid,id) 索引；时间统一本地时间；wallet_err 不再泄内部细节；前端钱包弹窗关闭后停止 30s 轮询；明细 key 加固 | 均已修复 |

### 审计确认为安全的项
- SQL 全参数化无注入；/api/wallet*、代理、/admin/wallet* 登录与管理员校验无遗漏；token_key 无任何外泄路径；
- 用户请求头无法伪造计费身份（Authorization/x-api-key 全覆盖注入）；路由顺序正确（网关段在 catch-all 门禁前）。

### 验证
- 53/53 服务端回归全过（`/tmp/regression_testenv.sh`）
- 资金安全专项 25/25（`/tmp/prod_e2e/test_money_security.py`）：
  10 线程并发兑同一码仅 1 成功且只加一次钱、销码/流水/限流/负数/NULL 面值/未登录代理 401/余额 0→402/路径穿越 400/XSS 转义/NaN-Inf 不 500/普通用户越权被拒 全部通过
- 注意：测试 uvicorn 管理密码为 ***已轮换（不入库）***（勿用正式站密码重启测试环境，会连锁导致回归误报）
- 备份：/workspace/代码备份-v17.1-安全加固-20261119.zip（163 文件，1.9MB）
- 正式站 chat.miandning.xyz 未部署网关计费与本次加固（等验收后一起上线）

---

## v17.2 交付前全面质量审查（2026-11-19）

流程：审查员甲（计费/并发/对账）+ 审查员乙（隔离/安全/恢复）独立深审 → 双向交叉验证（乙纠正甲的 provision 竞态机制描述：new-api users.username 无 UNIQUE，并发后果是重复用户行而非 500）→ 主控亲自真实验证 → 打回整改循环 ×3 → 180 项测试全过。

本轮修复（详见交付包 07_问题与修复记录.md）：
- P1×4：并发透支三道闸缓解（估算成本预检/new-api pre-consume/单用户并发≤3）、默认弱管理密码改随机生成+0600、iptables 封 3001 外网、兑换两阶段+redeem_pending 意图流水
- P2×6：upload 网关化（Bearer 头，交叉验证揪出 x-api-key 不被认）、登录/注册限流（20/10 每分）、adjust 超扣拒绝、代理预检加强等
- P3×10：公告 XSS、serve_static 前缀、provision 全局锁、面值下限、前端轮询等

最终测试：回归 54/54、资金 25/25、计费对账 28/28（误差0）、隔离 24/24、幂等+对账 17/17、并发 A-E 28/28、性能 4/4（100 并发 0% 错误率、无负余额）。
交付包：/workspace/DeepSeek计费Demo-交付审查材料包.zip（审查报告/测试日志/截图/测试源码/完整源码/部署说明）。
整体评级：B（核心全过；真实 DeepSeek 上游与支付网关属未实现/未验证项）。

⚠️ 待站长决定：演示账号"1"在审查期间被审查员甲实测污染（余额约¥144、5笔审计日志、used_quota=47 vs Σlogs=1284 漂移已登记）。如需恢复 ¥50 演示态：UPDATE new-api users SET quota=3472222 WHERE id=3; 并可选删除 logs 中 user_id=3 的 5 条审计记录（id 229-234）。
