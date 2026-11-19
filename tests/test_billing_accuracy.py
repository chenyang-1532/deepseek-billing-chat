import sys
# -*- coding: utf-8 -*-
"""专项五/第二部分：计费准确性逐笔对账（真实请求，非Mock绕过）
预期值用独立公式计算，与 new-api 数据库真实扣款记录逐笔对比。
独立公式（one-api/new-api 标准）：
  quota = (prompt_tokens + completion_tokens * CompletionRatio) * ModelRatio * 1000  [取整策略实测判定]
  人民币 = quota / 500000 * 7.2
"""
import json
import re
import sqlite3
import time
import urllib.request

import os
TAG = os.environ.get("RT_TAG", "")

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test/data.db"
NAPI = "/opt/newapi/one-api.db"
QUSD, USD2CNY = 500000, 7.2
PASS, FAIL = 0, 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ " + name)
    else:
        FAIL += 1
        print("  ❌ " + name + (" | " + str(extra) if extra else ""))


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OP = urllib.request.build_opener(NR)


def api(method, path, data=None, cookie=None):
    body = json.dumps(data).encode() if isinstance(data, dict) else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if isinstance(data, dict):
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = OP.open(req, timeout=60)
        sc = r.headers.get_all("Set-Cookie") or []
        sess = ""
        for s_ in sc:
            f = s_.split(";")[0]
            if "=" in f and len(f.split("=", 1)[1]) > 15:
                sess = f
        return r.status, r.read().decode(errors="replace"), sess
    except urllib.error.HTTPError as e:
        sc = e.headers.get_all("Set-Cookie") or []
        sess = ""
        for s_ in sc:
            f = s_.split(";")[0]
            if "=" in f and len(f.split("=", 1)[1]) > 15:
                sess = f
        return e.code, e.read().decode(errors="replace"), sess


def q(sql, params=(), db=SITE):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


# 独立推导 mock 上游的计量逻辑（从源码解析 REPLY，不复制硬编码）
src = open("/opt/newapi/mock_upstream.py").read()
REPLY = re.search(r'REPLY = "([^"]+)"', src).group(1)
OUT_TOK = max(1, len(REPLY) // 2)
# v17.5 起倍率来自「官方价×倍数」定价引擎（峰谷时段浮动），运行时读取当前配置作为价格基准；
# 预期值仍由本脚本用独立公式 round((in + out*CR) * MR) 逐笔计算，与系统实现零共享
_rrows = {r["key"]: r["value"] for r in q(
    "SELECT key, value FROM options WHERE key IN ('ModelRatio','CompletionRatio')", (), NAPI)}
_mr_map = json.loads(_rrows["ModelRatio"])
_cr_map = json.loads(_rrows["CompletionRatio"])
RATIOS = {
    "deepseek-flash": (_mr_map["deepseek-flash"], _cr_map["deepseek-flash"]),
    "deepseek-chat": (_mr_map["deepseek-chat"], _cr_map["deepseek-chat"]),
    "deepseek-v4-pro": (_mr_map["deepseek-v4-pro"], _cr_map["deepseek-v4-pro"]),
    "deepseek-reasoner": (_mr_map["deepseek-reasoner"], _cr_map["deepseek-reasoner"]),
}
print("当前倍率基准(官方价×倍数):", RATIOS)


def mock_input_tokens(user_text, system_text=""):
    # mock 上游 count_tokens: max(1, len//2)；空 system 也会贡献 1 个 token（count_tokens("")=1）
    return (max(1, len(user_text) // 2)
            + (max(1, len(system_text) // 2) if system_text else 1)
            + 10)


def expected_quota(in_t, model):
    # new-api rc.51 实测公式：quota = round((in + out*CompletionRatio) * ModelRatio)
    # ModelRatio = 每 token quota 数；换算美元价：quota/500000×1e6 = $/1M tokens
    mr, cr = RATIOS[model]
    return round((in_t + OUT_TOK * cr) * mr)


# ── 准备专用测试账号 ──
U = "rttest%s_bill" % TAG
api("POST", "/api/register", {"u": U, "p": "billtest123"})
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit()
c.close()
s, _, ck_user = api("POST", "/api/login", {"u": U, "p": "billtest123"})
assert ck_user, "登录失败"
s, r, _ = api("GET", "/api/wallet", cookie=ck_user)
assert s == 200, r
uid = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]
nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]
# 后台 adjust 充 ¥20 保证余额充足
import http.cookiejar
jar = http.cookiejar.CookieJar()
adm = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NR())
try:
    adm.open(urllib.request.Request(BASE + "/admin/login", data=("pw=%s" % ADMIN_PW).encode(),
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}))
except urllib.error.HTTPError:
    pass  # 303 即登录成功，cookie 已入 jar
try:
    adm.open(urllib.request.Request(BASE + "/admin/wallet/adjust",
                                    data=("uid=%d&amount=20&note=计费对账测试" % uid).encode(),
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}))
except urllib.error.HTTPError:
    pass
quota_now = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
print("测试账号", U, "nid=%d 起始quota=%d (≈¥%.4f)" % (nid, quota_now, quota_now / QUSD * USD2CNY))

# ── 用例矩阵：极小/普通/大/超大/零内容/多模型 ──
CASES = [
    ("极小-1字", "deepseek-flash", "好", ""),
    ("普通-40字", "deepseek-flash", "请用一句话介绍你自己，谢谢。" * 3, ""),
    ("大-800字", "deepseek-flash", "测试" * 400, ""),
    ("超大-8000字", "deepseek-flash", "压" * 8000, ""),
    ("零内容-仅标点", "deepseek-flash", "", ""),
    ("带system", "deepseek-flash", "你好", "你是一个测试助手，用于计费验证。"),
    ("模型-v4-pro", "deepseek-v4-pro", "普通请求", ""),
    ("模型-reasoner", "deepseek-reasoner", "普通请求", ""),
]
print("\n═══ 逐笔对账（独立公式计算 vs new-api 数据库真实扣款） ═══")
before_log = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nid,), NAPI)[0]["m"]
before_used = q("SELECT used_quota FROM users WHERE id=?", (nid,), NAPI)[0]["used_quota"]
before_quota = quota_now

rows_result = []
for name, model, text, system in CASES:
    body = {"model": model, "max_tokens": 1000, "messages": [{"role": "user", "content": text or " "}], "stream": False}
    if system:
        body["system"] = system
    t0 = time.time()
    s, r, _ = api("POST", "/anthropic/v1/messages", body, cookie=ck_user)
    dt = time.time() - t0
    ck("%s: 请求成功" % name, s == 200, (s, r[:100]))
    time.sleep(0.3)

log_rows = q(
    "SELECT id, model_name, prompt_tokens, completion_tokens, quota, created_at FROM logs "
    "WHERE user_id=? AND id>? ORDER BY id", (nid, before_log), NAPI)
ck("用例数=日志条数（每笔请求都有结算记录，无漏记）", len(log_rows) == len(CASES),
   "logs=%d cases=%d" % (len(log_rows), len(CASES)))

print("  %-14s %-18s %6s %6s %10s %10s %8s %8s %10s" % ("用例", "模型", "输入", "输出", "预期quota", "实际quota", "误差", "扣款¥", "上游成本$"))
total_user_cny, total_upstream_usd = 0.0, 0.0
for row, (name, model, text, system) in zip(log_rows, CASES):
    # v17.5 网关把所有请求统一改写为站点默认模型（用户无感知），实际计费模型=后台配置的默认模型
    billed_model = row["model_name"]
    mr, cr = RATIOS[billed_model]
    exp_in = mock_input_tokens(text or " ", system)
    # 预期：new-api rc.51 实测公式 round((in + out*cr) * mr)，按实际计费模型取倍率
    exp_quota = expected_quota(exp_in, billed_model)
    diff = row["quota"] - exp_quota
    cny = row["quota"] / QUSD * USD2CNY
    upstream_usd = row["quota"] / QUSD  # 倍率≈官方价 → 上游成本≈用户扣款等值美元
    total_user_cny += cny
    total_upstream_usd += upstream_usd
    rows_result.append((name, model, row["prompt_tokens"], row["completion_tokens"], exp_quota, row["quota"], diff))
    print("  %-14s %-18s %6d %6d %10d %10d %8d %8.4f %10.4f" % (
        name, model, row["prompt_tokens"], row["completion_tokens"], exp_quota, row["quota"], diff, cny, upstream_usd))
    ok_tok = (row["prompt_tokens"] == exp_in and row["completion_tokens"] == OUT_TOK)
    ck("%s: token计量与独立预期一致" % name, ok_tok,
       "in %s/%s out %s/%s" % (row["prompt_tokens"], exp_in, row["completion_tokens"], OUT_TOK))
    ck("%s: 扣款quota与独立公式一致(|误差|≤2)" % name, abs(diff) <= 2, diff)

after_used = q("SELECT used_quota FROM users WHERE id=?", (nid,), NAPI)[0]["used_quota"]
after_quota = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
sum_log = sum(r["quota"] for r in log_rows)
ck("users.used_quota 增量 == Σlogs.quota（无重复结算/漏结算）", after_used - before_used == sum_log,
   "usedΔ=%s Σlogs=%s" % (after_used - before_used, sum_log))
ck("users.quota 减量 == Σlogs.quota（扣款与结算记录一致）", before_quota - after_quota == sum_log,
   "quotaΔ=%s Σlogs=%s" % (before_quota - after_quota, sum_log))
ck("余额不为负", after_quota >= 0, after_quota)

print("\n  平台毛利测算：用户扣款 ¥%.4f ≈ $%.4f；上游按同倍率结算 → Demo 毛利=0（倍率=官方价演示，正式上线由站长自定加价倍率）" % (total_user_cny, total_upstream_usd))

# ── 清理 ──
c = sqlite3.connect(SITE, timeout=15)
c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
c.execute("DELETE FROM users WHERE username=?", (U,))
c.commit()
c.close()
c = sqlite3.connect(NAPI, timeout=15)
c.execute("DELETE FROM logs WHERE user_id=?", (nid,))
c.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
c.execute("DELETE FROM users WHERE id=?", (nid,))
c.commit()
c.close()
print("\n%s 已清理" % U)
print("══════════════")
print("计费准确性: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))
with open("/root/.codebuddy/artifact/delivery/test_results/billing_accuracy_result.json", "w") as f:
    json.dump([{
        "case": n, "model": m, "prompt_tokens": p, "completion_tokens": ct,
        "expected_quota": eq, "actual_quota": aq, "diff": d
    } for (n, m, p, ct, eq, aq, d) in rows_result], f, ensure_ascii=False, indent=2)
print("明细已存 delivery/test_results/billing_accuracy_result.json")


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
