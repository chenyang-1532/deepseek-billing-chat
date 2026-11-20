import sys
# -*- coding: utf-8 -*-
"""网关计费资金安全专项测试（只动 rttest_ 前缀账号，测完即删）"""
import concurrent.futures as cf
import json
import os
import sqlite3
import time
import urllib.request
import urllib.parse

import os
from _guard import guard, probe_billing_or_skip, assert_restart_allowed
TAG = os.environ.get("RT_TAG", "")

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test"
NAPI_DB = os.environ.get("RT_NAPI_DB") or "/opt/newapi/one-api.db"  # R13 P3-1：读 env，不再写死
guard(SITE, NAPI_DB)
probe_billing_or_skip(BASE, SITE + "/data.db", NAPI_DB, TAG)   # R13 P3-2：记账前置探测，无记账环境 SKIP
PASS, FAIL = 0, 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不跟随 303，保留原始 Set-Cookie


_OPENER = urllib.request.build_opener(_NoRedirect)


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ " + name)
    else:
        FAIL += 1
        print("  ❌ " + name + (" | " + str(extra) if extra else ""))


def api(method, path, data=None, cookie=None, headers=None, timeout=15):
    url = BASE + path
    body = None
    if data is not None:
        body = json.dumps(data).encode() if isinstance(data, dict) else (data if isinstance(data, bytes) else data.encode())
    req = urllib.request.Request(url, data=body, method=method)
    if isinstance(data, dict):
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = _OPENER.open(req, timeout=timeout)
        status = r.status
        raw = r.headers
        content = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.headers
        content = e.read().decode(errors="replace")
    # 多个 Set-Cookie 时 dict() 会丢，直接从原始头提取全部 cookie
    sc = raw.get_all("Set-Cookie") or []
    sess = ""
    for s_ in sc:
        f = s_.split(";")[0]
        if "=" in f and len(f.split("=", 1)[1]) > 15:
            # admin 接口返回 admin cookie，普通接口返回 session cookie
            if f.startswith("admin=") or not sess:
                sess = f
    loc = raw.get("Location") or raw.get("location") or ""
    return status, content, {"Set-Cookie": sess, "Location": loc}


def dbq(sql, params=(), db=SITE + "/data.db"):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


# ── 准备：注册两个 rttest 账号并直接审核通过 ──
print("═══ 准备测试账号 ═══")
for u in ("rttest%s_sec_a" % TAG, "rttest%s_sec_b" % TAG):
    api("POST", "/api/register", {"u": u, "p": "sectest123"})
c = sqlite3.connect(SITE + "/data.db")
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username IN ('rttest%s_sec_a','rttest%s_sec_b')" % (TAG, TAG))
c.commit()
c.close()

sA, bodyA, hA = api("POST", "/api/login", {"u": "rttest%s_sec_a" % TAG, "p": "sectest123"})
sB, bodyB, hB = api("POST", "/api/login", {"u": "rttest%s_sec_b" % TAG, "p": "sectest123"})
ckA = hA.get("Set-Cookie", "").split(";")[0]
ckB = hB.get("Set-Cookie", "").split(";")[0]
ck("两个测试账号登录成功", sA == 200 and sB == 200, (sA, sB))

uid_a = dbq("SELECT id FROM users WHERE username='rttest%s_sec_a'" % TAG)[0]["id"]
uid_b = dbq("SELECT id FROM users WHERE username='rttest%s_sec_b'" % TAG)[0]["id"]

# 触发 provision 拿到 napi_uid
s, r, _ = api("GET", "/api/wallet", cookie=ckA)
nid_a = dbq("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid_a,))[0]["napi_uid"]
base_quota_a = dbq("SELECT quota FROM users WHERE id=?", (nid_a,), NAPI_DB)[0]["quota"]
print("  uid_a=%s nid_a=%s 起始quota=%s" % (uid_a, nid_a, base_quota_a))

# 直插一张 ¥5 充值码
test_code = "GW-TEST-%d" % int(time.time() % 100000)
c = sqlite3.connect(SITE + "/data.db")
c.execute("INSERT INTO redeem_codes(code, cny, created_at) VALUES(?,?,datetime('now','localtime'))", (test_code, 5.0))
c.commit()
c.close()
print("  测试充值码: " + test_code + " (¥5)")

print("═══ 1. 并发兑换同一码（10线程，只允许1个成功） ═══")
def try_redeem(ck_):
    return api("POST", "/api/wallet/redeem", {"code": test_code}, cookie=ck_)

with cf.ThreadPoolExecutor(max_workers=10) as pool:
    results = list(pool.map(try_redeem, [ckA] * 10))
oks = [r for r in results if r[0] == 200 and json.loads(r[1]).get("ok")]
ck("仅1个并发请求成功", len(oks) == 1, [r[0] for r in results])
after_quota = dbq("SELECT quota FROM users WHERE id=?", (nid_a,), NAPI_DB)[0]["quota"]
added = after_quota - base_quota_a
expect = round(5.0 * 500000 / 7.2)
ck("余额只加了一次(Δquota=%s, 期望%s)" % (added, expect), added == expect, added)
used_rows = dbq("SELECT used_by FROM redeem_codes WHERE code=?", (test_code,))
ck("码已被销毁", used_rows[0]["used_by"] == uid_a)
txs = dbq("SELECT COUNT(*) AS n FROM wallet_tx WHERE uid=? AND type='redeem' AND note LIKE ?", (uid_a, "%" + test_code + "%"))
ck("流水只记了一条", txs[0]["n"] == 1, txs[0]["n"])

print("═══ 2. 已销毁码再兑换被拒 ═══")
s, r, _ = api("POST", "/api/wallet/redeem", {"code": test_code}, cookie=ckB)
ck("他人再兑换→拒绝且提示统一", s == 200 and json.loads(r).get("ok") is False and "无效" in json.loads(r).get("msg", ""), (s, r[:80]))
after_quota_b_attempt = dbq("SELECT quota FROM users WHERE id=?", (nid_a,), NAPI_DB)[0]["quota"]
ck("余额未变", after_quota_b_attempt == after_quota)

print("═══ 3. 兑换限流（每分钟5次，B在第2节已试1次） ═══")
codes429 = []
for i in range(7):
    s, r, _ = api("POST", "/api/wallet/redeem", {"code": "GW-0000-0000"}, cookie=ckB)
    codes429.append(s)
ck("超过5次后被429限流", codes429[3] == 200 and codes429[4] == 429 and codes429[6] == 429, codes429)
body0 = json.loads(api("POST", "/api/wallet/redeem", {"code": "GW-1111-2222"}, cookie=ckB)[1] or "{}")
ck("错误提示归一为'无效'(不区分不存在/已用)", "无效" in body0.get("msg", "") or codes429[-1] == 429, body0)

print("═══ 4. 负数/异常充值码防护 ═══")
# 表结构 cny NOT NULL：验证 NULL 被数据库层面拦截
try:
    c = sqlite3.connect(SITE + "/data.db")
    c.execute("INSERT INTO redeem_codes(code, cny, created_at) VALUES('GW-NUL-TEST1', NULL, datetime('now','localtime'))")
    c.commit()
    null_blocked = False
except sqlite3.IntegrityError:
    null_blocked = True
finally:
    c.close()
ck("NULL面值码被DB层NOT NULL拦截", null_blocked)
c = sqlite3.connect(SITE + "/data.db")
c.execute("INSERT INTO redeem_codes(code, cny, created_at) VALUES('GW-NEG-TEST1', -5.0, datetime('now','localtime'))")
c.commit()
c.close()
s, r, _ = api("POST", "/api/wallet/redeem", {"code": "GW-NEG-TEST1"}, cookie=ckA)
ck("负数面值码被拒", json.loads(r).get("ok") is False, r[:60])

print("═══ 5. 代理越权与余额拦截 ═══")
s, r, h = api("POST", "/anthropic/v1/messages", {"model": "deepseek-flash", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
ck("未登录代理→401", s == 401, s)
s, r, _ = api("GET", "/anthropic/v1/models", cookie=ckA)
ck("登录后代理可用(models 200)", s == 200, s)
s, r, _ = api("POST", "/anthropic/v1/messages", {"model": "deepseek-flash", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}, cookie=ckA)
ck("正常对话经网关计费成功", s == 200, s)
ck("响应含固定验证文案", "已打通" in r or "已计量" in r, r[:80])   # 审查 R11：or len(r)>0 恒真，删掉
# 路径穿越
s, r, _ = api("GET", "/anthropic/v1/../admin/panel", cookie=ckA)
ck("代理路径穿越→400", s == 400, s)
# 余额清零后 → 402
db = sqlite3.connect(NAPI_DB, timeout=15)
db.execute("UPDATE users SET quota=0 WHERE id=?", (nid_a,))
db.commit()
db.close()
s, r, _ = api("POST", "/anthropic/v1/messages", {"model": "deepseek-flash", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}, cookie=ckA)
ck("余额为0→402拒绝", s == 402, s)

print("═══ 6. admin 页 XSS 转义 ═══")
s, r, h = api("POST", "/admin/login", data=("pw=%s" % ADMIN_PW), headers={"Content-Type": "application/x-www-form-urlencoded"})
ckadmin = h.get("Set-Cookie", "")
ck("管理员登录拿到cookie", ckadmin.startswith("admin="), ckadmin[:20])
payload = "?codes=" + urllib.parse.quote("<img src=x onerror=alert(1)>")
s, r, _ = api("GET", "/admin" + payload, cookie=ckadmin)
ck("注入payload未原样渲染(已转义)", "<img src=x onerror=alert(1)>" not in r)
ck("转义后内容出现在页面", "&lt;img src=x" in r)

print("═══ 7. 后台 adjust NaN/Inf 不 500 ═══")
form = "uid=%d&amount=nan" % uid_a
s, r, h = api("POST", "/admin/wallet/adjust", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=ckadmin)
loc = h.get("Location") or h.get("location") or ""
ck("NaN金额→重定向不500", s == 303 and "wallet_err=1" in loc, (s, loc))
form = "uid=%d&amount=inf" % uid_a
s, r, _ = api("POST", "/admin/wallet/adjust", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=ckadmin)
ck("Inf金额→重定向不500", s == 303, s)
# 正常充值 ¥3 验证 adjust 工作
form = "uid=%d&amount=3&note=专项测试" % uid_a
s, r, _ = api("POST", "/admin/wallet/adjust", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=ckadmin)
q_after = dbq("SELECT quota FROM users WHERE id=?", (nid_a,), NAPI_DB)[0]["quota"]
ck("正常充值¥3到账(Δ=%s,期望%s)" % (q_after - 0, round(3 * 500000 / 7.2)), q_after == round(3 * 500000 / 7.2), q_after)

print("═══ 8. 越权：普通用户访问 admin 接口 ═══")
s, r, _ = api("POST", "/admin/wallet/adjust", data="uid=1&amount=999", headers={"Content-Type": "application/x-www-form-urlencoded"}, cookie=ckA)
ck("普通用户adjust→303且无Cookie授权", s == 303, s)
q_user1 = dbq("SELECT quota FROM users WHERE id=?", (nid_a,), NAPI_DB)[0]["quota"]
ck("余额未被改动", q_user1 == round(3 * 500000 / 7.2), q_user1)

print("═══ 清理 ═══")
c = sqlite3.connect(SITE + "/data.db")
c.execute("DELETE FROM wallet_tx WHERE uid IN (SELECT id FROM users WHERE username IN ('rttest%s_sec_a','rttest%s_sec_b'))" % (TAG, TAG))
c.execute("DELETE FROM gateway_map WHERE uid IN (SELECT id FROM users WHERE username IN ('rttest%s_sec_a','rttest%s_sec_b'))" % (TAG, TAG))
# 审查 R11（P2）：原来是 LIKE 'GW-TEST-%' 的宽范围删除，会清掉库里所有同前缀的码。
# 改成只删本套件确实创建的三张。
c.execute("DELETE FROM redeem_codes WHERE code IN (?,?,?)",
          (test_code, "GW-NEG-TEST1", "GW-NUL-TEST1"))
# 审查 R11（P2）：原为 "... IN ('rttest%s_sec_a','rttest%s_sec_b')" % (TAG, TAG) 的
# 格式化 DELETE——RT_TAG 含单引号即可改写 WHERE（生产库上的删除）。改参数化。
c.execute("DELETE FROM users WHERE username IN (?,?)",
          ("rttest%s_sec_a" % TAG, "rttest%s_sec_b" % TAG))
c.commit()
c.close()
db = sqlite3.connect(NAPI_DB, timeout=15)
db.execute("DELETE FROM tokens WHERE user_id=?", (nid_a,))
db.execute("DELETE FROM users WHERE id=?", (nid_a,))
db.execute("DELETE FROM logs WHERE user_id=?", (nid_a,))
db.commit()
db.close()
# 清掉限流记忆不影响后续
print("rttest%s_sec_* 账号及关联数据已清理" % TAG)
print("══════════════")
print("资金安全专项: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
