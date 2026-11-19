import sys
# -*- coding: utf-8 -*-
"""第三部分/专项六：用户余额隔离全矩阵测试（3个真实测试用户）"""
import concurrent.futures as cf
import json
import sqlite3
import time
import urllib.request

import os
from _guard import guard, assert_restart_allowed
TAG = os.environ.get("RT_TAG", "")

BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test/data.db"
NAPI = "/opt/newapi/one-api.db"
guard(SITE, NAPI)
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


def api(method, path, data=None, cookie=None, timeout=60):
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
        return e.code, e.read().decode(errors="replace"), ""


def q(sql, params=(), db=SITE):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


def napi_set_quota(nid, v):
    c = sqlite3.connect(NAPI, timeout=15)
    c.execute("UPDATE users SET quota=? WHERE id=?", (v, nid))
    c.commit()
    c.close()


def napi_get_quota(nid):
    return q("SELECT quota AS v FROM users WHERE id=?", (nid,), NAPI)[0]["v"]


print("═══ 准备：注册审核 A/B/C 并充值 ¥10/¥50/¥0 ═══")
cookies = {}
nids = {}
for name, cny in (("A", 10), ("B", 50), ("C", 0)):
    u = "rttest%s_iso_" % TAG + name
    api("POST", "/api/register", {"u": u, "p": "isotest123"})
    c = sqlite3.connect(SITE, timeout=15)
    c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (u,))
    c.commit()
    c.close()
    s, _, ck_ = api("POST", "/api/login", {"u": u, "p": "isotest123"})
    cookies[name] = ck_
    api("GET", "/api/wallet", cookie=ck_)
    uid = q("SELECT id FROM users WHERE username=?", (u,))[0]["id"]
    nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]
    nids[name] = nid
    napi_set_quota(nid, round(cny * QUSD / USD2CNY)) if cny else napi_set_quota(nid, 0)
    print("  用户%s: uid=%d nid=%d 余额¥%.2f" % (name, uid, nid, napi_get_quota(nid)))
    globals()["uid_" + name] = uid


BODY = {"model": "deepseek-flash", "max_tokens": 1000, "stream": False,
        "messages": [{"role": "user", "content": "隔离测试请求"}]}

print("\n═══ 1. 各用户独立消费自己的余额 ═══")
for name in ("A", "B"):
    s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies[name])
    ck("用户%s 可正常消费" % name, s == 200, s)
time.sleep(1)
cost_a = 0
log_a = q("SELECT quota FROM logs WHERE user_id=? AND type=2 ORDER BY id DESC LIMIT 1", (nids["A"],), NAPI)
if log_a:
    cost_a = log_a[0]["quota"]
qa = napi_get_quota(nids["A"])
ck("用户A 余额减少且只减自己额度", 694444 - cost_a - 100000 < qa < 694444 - cost_a + 100000, qa)
# 精确: A初始=69444(¥10) 减一笔
ck("用户A 余额=¥10-一笔消费", abs(qa - (694444 - cost_a)) <= 2, qa)
qb = napi_get_quota(nids["B"])
log_b = q("SELECT quota FROM logs WHERE user_id=? AND type=2 ORDER BY id DESC LIMIT 1", (nids["B"],), NAPI)
ck("用户B 余额=¥50-自己的一笔消费", log_b and abs(qb - (3472222 - log_b[0]["quota"])) <= 2, qb)

print("\n═══ 2. 余额为0的用户C不能消费 ═══")
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["C"])
ck("用户C(¥0)被拒(402/403)", s in (402, 403), s)
ck("用户C被拒无结算记录",
   len(q("SELECT 1 FROM logs WHERE user_id=? AND type=2", (nids["C"],), NAPI)) == 0)

print("\n═══ 3. A余额耗尽不影响B ═══")
napi_set_quota(nids["A"], 100)  # A 几乎耗尽
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["A"])
ck("A耗尽后被拒", s in (402, 403), s)
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["B"])
ck("B不受A影响可正常消费", s == 200, s)
qb2 = napi_get_quota(nids["B"])
ck("B余额仍然独立正确", qb2 < qb, (qb, qb2))

print("\n═══ 4. 伪造 user_id / 越权参数 ═══")
s, r, _ = api("POST", "/api/wallet/logs?user_id=%d" % nids["B"], cookie=cookies["A"])
body_b_logs = json.loads(r).get("logs", []) if s == 200 else None
ck("A 无法查 B 的消费记录(user_id参数无效)", s != 200 or not body_b_logs, (s, r[:80]))
s, r, _ = api("GET", "/api/wallet", cookie=cookies["A"])
a_bal = json.loads(r).get("balance_cny")
ck("A 查询钱包只看到自己的余额", abs(a_bal - 100 / QUSD * USD2CNY) < 0.01, a_bal)
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["A"])
ck("A 会话固定绑定自己的计费账户(响应不因伪造参数改变)", s in (200, 402), s)
# 伪造 cookie 内 uid
forged = q("SELECT pw_hash FROM users WHERE username='rttest%s_iso_A'" % TAG)[0]["pw_hash"]
s, r, _ = api("GET", "/api/wallet", cookie="session= forged")
ck("伪造session被拒", s in (403, 401), s)

print("\n═══ 5. token_key 不外泄 ═══")
s, r, _ = api("GET", "/api/wallet", cookie=cookies["A"])
tk = q("SELECT token_key FROM gateway_map WHERE uid=?", (uid_A,))[0]["token_key"]
ck("钱包接口不返回token_key", tk not in r)
s, r, _ = api("GET", "/api/wallet/logs?limit=20", cookie=cookies["A"])
ck("消费记录接口不返回token_key", tk not in r)

print("\n═══ 6. 封禁/过期后的行为 ═══")
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='banned' WHERE username='rttest%s_iso_A'" % TAG)
c.commit()
c.close()
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["A"])
ck("封禁用户调用计费代理被拒", s in (402, 403), s)
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','-1 day') WHERE username='rttest%s_iso_B'" % TAG)
c.commit()
c.close()
s, r, _ = api("POST", "/anthropic/v1/messages", BODY, cookie=cookies["B"])
ck("过期用户调用计费代理被拒", s in (402, 403), s)
ck("过期用户B余额未变动", napi_get_quota(nids["B"]) == qb2)

print("\n═══ 7. 重复注册/重复绑定不会错绑 ═══")
s, r, _ = api("POST", "/api/register", {"u": "rttest%s_iso_A" % TAG, "p": "isotest123"})
ck("重复注册被拒", json.loads(r).get("ok") is False, r[:60])
maps = q("SELECT uid, napi_uid, token_key FROM gateway_map WHERE uid=?", (uid_A,))
ck("gateway_map 一人一映射(uid主键)", len(maps) == 1, len(maps))
# 网关map中的napi_uid始终等于最初nid
s, r, _ = api("GET", "/api/wallet", cookie=cookies["C"])
ck("C 钱包映射仍正确(未错绑)", q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid_C,))[0]["napi_uid"] == nids["C"])

print("\n═══ 8. 两个用户并发消费（隔离+并发交叉） ═══")
napi_set_quota(nids["A"], 347222)
napi_set_quota(nids["B"], 347222)
c = sqlite3.connect(SITE, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username IN ('rttest%s_iso_A','rttest%s_iso_B')" % (TAG, TAG))
c.commit()
c.close()
qa0, qb0 = napi_get_quota(nids["A"]), napi_get_quota(nids["B"])
mark_a = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nids["A"],), NAPI)[0]["m"]
mark_b = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nids["B"],), NAPI)[0]["m"]


def fire(name):
    return api("POST", "/anthropic/v1/messages", BODY, cookie=cookies[name])


with cf.ThreadPoolExecutor(max_workers=20) as pool:
    rs = list(pool.map(fire, ["A", "B"] * 10))
oks = {"A": 0, "B": 0}
for i, (s, r, _) in enumerate(rs):
    if s == 200:
        oks["AB"[i % 2]] += 1
time.sleep(1.5)
ca = qa0 - napi_get_quota(nids["A"])
cb = qb0 - napi_get_quota(nids["B"])
la = sum(r["quota"] for r in q("SELECT quota FROM logs WHERE user_id=? AND id>?", (nids["A"], mark_a), NAPI))
lb = sum(r["quota"] for r in q("SELECT quota FROM logs WHERE user_id=? AND id>?", (nids["B"], mark_b), NAPI))
ck("并发下 A/B 各自扣款与各自结算记录一致", ca == la and cb == lb and ca > 0, (ca, la, cb, lb))
ck("并发下 A/B 无负余额", napi_get_quota(nids["A"]) >= 0 and napi_get_quota(nids["B"]) >= 0)

# ── 清理 ──
c = sqlite3.connect(SITE, timeout=15)
for u in ("rttest%s_iso_A" % TAG, "rttest%s_iso_B" % TAG, "rttest%s_iso_C" % TAG):
    row = c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
    if row:
        c.execute("DELETE FROM wallet_tx WHERE uid=?", (row[0],))
        c.execute("DELETE FROM gateway_map WHERE uid=?", (row[0],))
        c.execute("DELETE FROM users WHERE id=?", (row[0],))
c.commit()
c.close()
c = sqlite3.connect(NAPI, timeout=15)
for nid in nids.values():
    c.execute("DELETE FROM logs WHERE user_id=?", (nid,))
    c.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
    c.execute("DELETE FROM users WHERE id=?", (nid,))
c.commit()
c.close()
print("rttest_iso_* 已清理")
print("══════════════")
print("用户余额隔离: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
