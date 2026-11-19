# tests/ 运行守则（审查 P0-测试安全）

## 禁止在生产机执行

本目录全部脚本会：写数据库、删除测试用户、（显式允许时）重启 new-api 进程。

**三重防护**（缺一即拒绝执行，退出码 2）：

1. `_guard.py guard(...)`：所有脚本启动时断言数据库路径位于 `/tmp/` 下，
   或在 `RT_ALLOWED_DB_DIRS` 环境变量中显式声明（如沙箱测试实例 `/opt/newapi`）。
2. `RT_ALLOW_RESTART=1`：涉及重启真实 new-api 进程的用例（test_reconciliation 重启恢复段）
   必须显式开启，否则整段 skip 并打印原因。
3. `run_all.sh` 默认导出声明；直接单跑某脚本时需自行 `export RT_ALLOWED_DB_DIRS=/opt/newapi`。

生产机特征（任一命中即不要跑）：`RT_NAPI_DB` 指向真实计费库；:3001 是线上网关；
无 `RT_ALLOWED_DB_DIRS` 声明。

## 统一门禁

```bash
export RT_TAG=<唯一标签> ADMIN_PW=<管理密码>
export RT_ALLOWED_DB_DIRS=/opt/newapi RT_ALLOW_RESTART=1
bash tests/run_all.sh
```

串行 12 套件 + 65s 限流冷却；任一失败总退出码 1。

## 数据铁律

- 测试用户一律 `rttest` 前缀，测完按 id 精确删除；
- 真实用户 chenyanggg、用户名 "1"（uid=17）、admin 不可触碰；
- 管理密码经 `ADMIN_PW` 环境变量传入，任何脚本不得硬编码。
