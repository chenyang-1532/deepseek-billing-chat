import sys
# -*- coding: utf-8 -*-
"""第四部分/专项二：并发与超额消费真实测试（场景A-E）
每请求实际成本可预测：input=5011 token；成本 = round((5011 + 22*CompletionRatio) * ModelRatio)
v17.5 起倍率来自「官方价×倍数」定价引擎（随峰谷时段浮动），故运行时从 new-api options 读取
当前倍率并独立套用公式计算预期成本，而非硬编码。
验收：不超额消费、不负余额、每笔唯一结算记录、失败请求不扣费
"""
import concurrent.futures as cf
import json
import sqlite3
import time
import urllib.request

import os
TAG = os.environ.get("RT_TAG", "")

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


def api(method, path, data=None, cookie=None, timeout=90):
    body = json.dumps(data).encode() if isinstance(data, dict) else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if isinstance(data, dict):
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = OP.open(req, timeout=timeout)
        sc = r.headers.get_all("Set-Cookie") or []
        sess = next((s_.split(";")[0] for s_ in sc if s_.startswith("session=") and len(s_) > 20), "")
        return r.status, r.read().decode(errors="replace"), sess
    except urllib.error.HTTPError as e:
        sc = e.headers.get_all("Set-Cookie") or []
        sess = next((s_.split(";")[0] for s_ in sc if s_.startswith("session=") and len(s_) > 20), "")
        return e.code, e.read().decode(errors="replace"), sess


def q(sql, params=(), db=SITE):
    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


def napi_set_quota(nid, quota):
    c = sqlite3.connect(NAPI, timeout=30)
    c.execute("UPDATE users SET quota=? WHERE id=?", (quota, nid))
    c.commit()
    c.close()


def napi_get(nid, col="quota"):
    return q("SELECT %s AS v FROM users WHERE id=?" % col, (nid,), NAPI)[0]["v"]


# ── 准备 ──
U = "rttest%s_conc" % TAG
api("POST", "/api/register", {"u": U, "p": "conctest123"})
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit()
c.close()
_, _, CKS = api("POST", "/api/login", {"u": U, "p": "conctest123"})
assert CKS, "登录失败"
api("GET", "/api/wallet", cookie=CKS)
uid = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]
nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]

BIG = "测" * 10000          # in=5011 token（5000 内容 + 1 空 system + 10 固定开销）
BODY = {"model": "deepseek-flash", "max_tokens": 1000, "stream": False,
        "messages": [{"role": "user", "content": BIG}]}
# 从当前定价配置独立推导每请求成本（官方价×倍数引擎产物，峰谷时段不同则数值不同）
_r = {k["key"]: k["value"] for k in q("SELECT key, value FROM options WHERE key IN ('ModelRatio','CompletionRatio')", (), NAPI)}
MR_FLASH = json.loads(_r["ModelRatio"])["deepseek-flash"]
CR_FLASH = json.loads(_r["CompletionRatio"])["deepseek-flash"]
COST = round((5011 + 22 * CR_FLASH) * MR_FLASH)
print("当前倍率(官方价×倍数): mr=%s cr=%s" % (MR_FLASH, CR_FLASH))
print("每请求实际成本(独立计算)=%d quota ≈ ¥%.4f" % (COST, COST / QUSD * USD2CNY))


def fire(n):
    return api("POST", "/anthropic/v1/messages", BODY, cookie=CKS)


def run_scenario(name, quota_cny, n_req, max_success=None):
    print("\n─── 场景 %s ───" % name)
    init_q = round(quota_cny * QUSD / USD2CNY)
    if max_success is None:
        max_success = init_q // COST   # 理论成功上限：初始余额够几笔
    napi_set_quota(nid, init_q)
    before = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nid,), NAPI)[0]["m"]
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n_req) as pool:
        results = list(pool.map(fire, range(n_req)))
    dt = time.time() - t0
    time.sleep(1.0)  # 等 new-api 结算落库
    codes = [r[0] for r in results]
    ok = sum(1 for c_ in codes if c_ == 200)
    logs = q("SELECT id, quota FROM logs WHERE user_id=? AND id>?", (nid, before), NAPI)
    final = napi_get(nid)
    consumed = sum(r["quota"] for r in logs)
    print("  并发%d笔: 成功%d 失败%d 耗时%.1fs 结算记录%d笔 总扣%dquota 最终余额%d(¥%.4f)"
          % (n_req, ok, n_req - ok, dt, len(logs), consumed, final, final / QUSD * USD2CNY))
    ck("%s: 无负余额" % name, final >= 0, final)
    ck("%s: 未超额消费(总扣≤初始余额)" % name, consumed <= init_q, "%d>%d" % (consumed, init_q))
    ck("%s: 成功数≤理论上限%d" % (name, max_success), ok <= max_success, ok)
    ck("%s: 每笔成功请求恰有一条结算记录" % name, len(logs) == ok, "logs=%d ok=%d" % (len(logs), ok))
    ck("%s: 失败请求未扣费(Σlogs=users.quota减量)" % name,
       init_q - final == consumed, "Δquota=%s Σlogs=%s" % (init_q - final, consumed))
    return ok, final


print("═══ 场景A: 余额¥0.10, 并发10请求 ═══")
run_scenario("A", 0.10, 10)

print("═══ 场景B: 余额¥0.05, 并发20请求 ═══")
run_scenario("B", 0.05, 20)

print("═══ 场景C: 余额¥0.99, 并发100请求 ═══")
run_scenario("C", 0.99, 100)

print("═══ 场景D: 余额¥0.001, 并发10请求 ═══")
run_scenario("D", 0.001, 10)

print("═══ 场景E: 余额恰差1quota不够两笔, 并发2请求 ═══")
run_scenario("E", (2 * COST - 1) / QUSD * USD2CNY, 2)

print("\n═══ 中途停止(客户端断连)结算归属测试 ═══")
napi_set_quota(nid, 100000)
before = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nid,), NAPI)[0]["m"]
import http.client
conn = http.client.HTTPConnection("127.0.0.1", 3000, timeout=30)
conn.request("POST", "/anthropic/v1/messages", json.dumps(BODY),
             {"Content-Type": "application/json", "Cookie": CKS})
resp = conn.getresponse()
resp.read(100)  # 读一小段后立即断开
conn.close()
time.sleep(2.0)
logs = q("SELECT id, quota FROM logs WHERE user_id=? AND id>?", (nid, before), NAPI)
final = napi_get(nid)
ck("客户端中途断连后结算仍完成(有记录)", len(logs) >= 1, logs)
ck("断连结算扣款正确(COST±2)", len(logs) >= 1 and abs(logs[0]["quota"] - COST) <= 2,
   logs[0]["quota"] if logs else "无")
ck("断连后余额≥0", final >= 0, final)

# ── 清理 ──
c = sqlite3.connect(SITE, timeout=15)
c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
c.execute("DELETE FROM users WHERE username=?", (U,))
c.commit()
c.close()
c = sqlite3.connect(NAPI, timeout=30)
c.execute("DELETE FROM logs WHERE user_id=?", (nid,))
c.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
c.execute("DELETE FROM users WHERE id=?", (nid,))
c.commit()
c.close()
print("rttest_conc 已清理")
print("══════════════")
print("并发扣费安全: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
