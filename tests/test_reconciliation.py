import sys
# -*- coding: utf-8 -*-
"""第五/八部分：充值码幂等性 + 全库账务对账（独立可重复执行）
核心不变量：new-api users.quota == Σ充值额度(quota) − Σ消费(quota)，容忍每笔±1舍入
"""
import json
import random
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
        sess = next((s_.split(";")[0] for s_ in sc if s_.startswith("session=") and len(s_) > 20), "")
        return r.status, r.read().decode(errors="replace"), sess
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        return e.code, e.read().decode(errors="replace") or loc, ""


def q(sql, params=(), db=SITE):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


def c2q(cny):
    return round(cny * QUSD / USD2CNY)


# ═══ 第一段：充值码幂等性（真实操作流） ═══
print("═══ 1. 充值码幂等性真实操作流 ═══")
U = "rttest%s_rec" % TAG
api("POST", "/api/register", {"u": U, "p": "rectest123"})
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit()
c.close()
_, _, CKU = api("POST", "/api/login", {"u": U, "p": "rectest123"})
api("GET", "/api/wallet", cookie=CKU)
uid = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]
nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]

jar = sqlite3.connect(SITE, timeout=15)
codes = []
for face in (5, 10, 0.01):
    code = "GW-REC-%d-%d" % (int(time.time()), int(face * 100))
    jar.execute("INSERT INTO redeem_codes(code, cny, created_at) VALUES(?,?,datetime('now','localtime'))", (code, face))
    codes.append((code, face))
jar.commit()
jar.close()

quota0 = q("SELECT quota AS v FROM users WHERE id=?", (nid,), NAPI)[0]["v"]
expected = quota0
for ci, (code, face) in enumerate(codes):
    if ci:
        time.sleep(62)  # 尊重兑换限流(5次/分钟)：每张码独立窗口
    s, r, _ = api("POST", "/api/wallet/redeem", {"code": code}, cookie=CKU)
    ok1 = json.loads(r).get("ok") if r.startswith("{") else False
    ck("兑换 %s(¥%g) 第一次成功" % (code, face), ok1 is True, r[:60])
    expected += c2q(face)
    s, r, _ = api("POST", "/api/wallet/redeem", {"code": code}, cookie=CKU)
    ok2 = json.loads(r).get("ok") if r.startswith("{") else True
    ck("兑换 %s 第二次被拒(幂等)" % code, ok2 is False, r[:60])
    s, r, _ = api("POST", "/api/wallet/redeem", {"code": code}, cookie=CKU)
    ok3 = json.loads(r).get("ok") if r.startswith("{") else True
    ck("兑换 %s 第三次仍被拒" % code, ok3 is False)

# 重复提交请求体（模拟用户刷新页面重发）；先等限流窗口重置
time.sleep(62)
code0 = codes[0][0]
for _ in range(3):
    api("POST", "/api/wallet/redeem", {"code": code0}, cookie=CKU)
quota_now = q("SELECT quota AS v FROM users WHERE id=?", (nid,), NAPI)[0]["v"]
ck("重复提交后余额仍=预期值(只加一次)", quota_now == expected, "%s vs %s" % (quota_now, expected))

# ═══ 第二段：随机操作后全库对账 ═══
print("\n═══ 2. 混合操作（消费+兑换+调整）后全库对账 ═══")
# 再加一笔 adjust
c = sqlite3.connect(NAPI, timeout=15)
c.execute("UPDATE users SET quota=quota+? WHERE id=?", (c2q(3.33), nid))
c.commit()
c.close()
expected += c2q(3.33)
# 5 笔并发消费
BODY = {"model": "deepseek-flash", "max_tokens": 1000, "stream": False,
        "messages": [{"role": "user", "content": "对账测试" * 200}]}
import concurrent.futures as cf
with cf.ThreadPoolExecutor(max_workers=5) as pool:
    list(pool.map(lambda _: api("POST", "/anthropic/v1/messages", BODY, cookie=CKU), range(5)))
time.sleep(1)
consumed = q("SELECT COALESCE(SUM(quota),0) AS s FROM logs WHERE user_id=? AND type=2", (nid,), NAPI)[0]["s"]
quota_now = q("SELECT quota AS v FROM users WHERE id=?", (nid,), NAPI)[0]["v"]
ck("不变量: 当前余额 == Σ充值 − Σ消费", quota_now == expected - consumed,
   "now=%s expect=%s-%s" % (quota_now, expected, consumed))

# 全库层面：每个有 gateway_map 的用户都验证 used_quota==Σlogs
print("\n── 全库不变量扫描（所有映射用户） ──")
bad, known = [], []
KNOWN_DRIFT_NIDS = set()  # P1-13：漂移已对齐清理（nid=3 used_quota 已按 Σlogs 修正），不再永久豁免
for m in q("SELECT uid, napi_uid FROM gateway_map"):
    used = q("SELECT used_quota AS v FROM users WHERE id=?", (m["napi_uid"],), NAPI)
    if not used:
        continue
    lg = q("SELECT COALESCE(SUM(quota),0) AS s FROM logs WHERE user_id=? AND type=2", (m["napi_uid"],), NAPI)[0]["s"]
    if used[0]["v"] != lg:
        (known if m["napi_uid"] in KNOWN_DRIFT_NIDS else bad).append((m["napi_uid"], used[0]["v"], lg))
ck("所有用户 used_quota == Σlogs（无重复结算/漏结算）", not bad, bad)
if known:
    print("  ⚠️ 已知漂移(演示账号,已登记待人工核查):", known)

# wallet_tx 与 redeem_codes 一致性
unmatched = q("""SELECT rc.code FROM redeem_codes rc WHERE rc.used_by IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM wallet_tx wt WHERE wt.type='redeem' AND wt.note LIKE '%'||rc.code)""")
ck("每张已用充值码都有对应流水", not unmatched, unmatched[:5])

# 负余额扫描
neg = q("SELECT id, quota FROM users WHERE quota<0", db=NAPI)
ck("new-api 全库无负余额用户", not neg, neg[:5])

# ═══ 第三段：new-api 重启后映射与余额保持 ═══
print("\n═══ 3. new-api 重启恢复测试 ═══")
quota_before = quota_now
import subprocess
import fcntl
def port_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:3001/api/status", timeout=2)
        return True
    except Exception:
        return False

# 全局文件锁：并行全量测试时重启段全局串行，避免代理间互踩
lockf = open("/tmp/napi_restart.lock", "w")
fcntl.flock(lockf, fcntl.LOCK_EX)
try:
    # 1) 停止实例；由常驻守护循环统一拉起，避免双实例端口竞争
    subprocess.run("pkill -f 'new-api'", shell=True, executable="/bin/bash")
    deadline = time.time() + 20
    while time.time() < deadline and port_up():
        time.sleep(0.5)
    # 2) 等待新实例就绪（status 连续稳定 5 秒，最长 90 秒；守护循环 5 秒轮询拉起）
    deadline = time.time() + 90
    stable_since = None
    while time.time() < deadline:
        if port_up():
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 5:
                break
        else:
            stable_since = None
        time.sleep(0.5)
    # 3) 钱包/消费检查带瞬态重试（new-api 启动初始化期间 token/会话校验可能短暂 403/401）
    ok_wallet = False
    for _ in range(10):
        s, r, _ = api("GET", "/api/wallet", cookie=CKU)
        if s == 200:
            ok_wallet = True
            bal = json.loads(r).get("balance_quota")
            ck("重启后余额不变(映射持久)", bal == quota_before, (bal, quota_before))
            break
        time.sleep(4)
    ck("new-api 重启后钱包可查", ok_wallet, s)
    ok_consume = False
    for _ in range(10):
        s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=CKU)
        if s == 200:
            ok_consume = True
            break
        time.sleep(4)
    ck("重启后计费链路恢复可消费", ok_consume, s)
finally:
    fcntl.flock(lockf, fcntl.LOCK_UN)
    lockf.close()

# ── 清理 ──
c = sqlite3.connect(SITE, timeout=15)
c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
c.execute("DELETE FROM redeem_codes WHERE code LIKE 'GW-REC-%'")
c.execute("DELETE FROM users WHERE username=?", (U,))
c.commit()
c.close()
c = sqlite3.connect(NAPI, timeout=15)
c.execute("DELETE FROM logs WHERE user_id=?", (nid,))
c.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
c.execute("DELETE FROM users WHERE id=?", (nid,))
c.commit()
c.close()
print("rttest_rec 已清理")
print("══════════════")
print("充值幂等+账务对账: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
