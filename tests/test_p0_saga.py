# -*- coding: utf-8 -*-
"""审查 P0 修复故障注入专项：P0-1 审批 saga / P0-3 调账幂等 / P0-4 支付幂等"""
import json, sys, time, sqlite3, hashlib, secrets, urllib.request, urllib.error, uuid, threading

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test/data.db"
NAPI = "/opt/newapi/one-api.db"
TAG = os.environ.get("RT_TAG", "") if (os := sys.modules.get("os")) or True else ""
import os
TAG = os.environ.get("RT_TAG", "")
U = "rttest%s_p0fix" % TAG
PWD = "p0fix123"
results = []

def ck(name, cond, extra=None):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % (extra,)))

class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

OP = urllib.request.build_opener(NR)

def api(method, path, body=None, cookie=None, form=False):
    if form and isinstance(body, dict):
        data = "&".join("%s=%s" % kv for kv in body.items()).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    elif isinstance(body, dict):
        data = json.dumps(body).encode()
        hdr = {"Content-Type": "application/json"}
    else:
        data, hdr = body, {}
    if cookie:
        hdr["Cookie"] = cookie
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=hdr)
    try:
        resp = OP.open(r, timeout=60)
        return resp.status, resp.read().decode(errors="replace"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers

def q(sql, p=(), db=SITE):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, p).fetchall()
    c.close()
    return rows

# ── 准备：注册并开通测试用户，管理员登录 ──
api("POST", "/api/register", {"u": U, "p": PWD})
c = sqlite3.connect(SITE)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit(); c.close()
s, r, h = api("POST", "/api/login", {"u": U, "p": PWD})
CK = [x for x in (h.get_all("Set-Cookie") or []) if "session" in x][0].split(";")[0]
uid = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]

import http.cookiejar
jar = http.cookiejar.CookieJar()
ADM = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NR)
try:
    ADM.open(urllib.request.Request(BASE + "/admin/login",
        data=("pw=%s" % ADMIN_PW).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}))
except urllib.error.HTTPError:
    pass
ACK = "; ".join("%s=%s" % (x.name, x.value) for x in jar)

# napi uid 与初始余额（未开通则先触发 provision）
row = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))
if not row:
    sys.path.insert(0, "/tmp/chatsite-test")
    import app as _A
    _A.gw_provision({"id": uid, "username": U})
    row = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))
nid = row[0]["napi_uid"]

# ── 幂等原语单测：gw_add_quota 同 op_id 重复调用只加一次 ──
sys.path.insert(0, "/tmp/chatsite-test")
import app as A
op = uuid.uuid4().hex
q0 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
r1 = A.gw_add_quota(nid, 1000, op_id="t:%s" % op)
r2 = A.gw_add_quota(nid, 1000, op_id="t:%s" % op)
q1 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
ck("gw_add_quota 同op_id重试不重复加款", (q1 - q0) == 1000, (q0, q1))
ck("第二次调用返回当前余额(幂等)", r1 - q0 == 1000 and r2 == q1, (r1, r2))

# 回滚测试余额
A.gw_add_quota(nid, -1000, op_id="rb:%s" % op)

# ── P0-3：调账 saga 故障注入（流水写入失败后重试不重复加款） ──
orig_tx = A._wallet_tx
op3 = uuid.uuid4().hex
# 注入：调账时流水抛异常（模拟上次跑到一半）
def broken_tx(c, uid_, *a, **k):
    raise RuntimeError("注入故障: 流水库不可写")
A._wallet_tx = broken_tx
# 直接调 saga 的前两步（模拟真实路由内部步骤）：幂等加款
# delta 必须与重试路由的 cny_to_quota(0.5) 完全一致（审查 R1：quota_ops 语义校验，
# 同 op_id 绑定不同 delta 会被拒绝——这正是需要验证的安全行为）
delta_real = A.cny_to_quota(0.5)
try:
    A.gw_add_quota(nid, delta_real, op_id="adjust:%s" % op3)
    A._wallet_tx(sqlite3.connect(SITE), uid, "adjust", 0.5, 0, "x", )  # 会抛
except RuntimeError as e:
    pass
A._wallet_tx = orig_tx

# 审查 R1 新增回归：quota_ops 幂等命中不校验语义 = 静默吞掉不同操作
# 1) 同 op_id 不同金额 → 必须报错，且余额不变
try:
    A.gw_add_quota(nid, delta_real + 999, op_id="adjust:%s" % op3)
    ck("P0-3 同op_id不同金额必须报错(拒绝语义冲突)", False, "未抛异常")
except RuntimeError:
    ck("P0-3 同op_id不同金额必须报错(拒绝语义冲突)", True)
# 2) 同 op_id 不同 uid → 必须报错
try:
    A.gw_add_quota(nid + 999999, delta_real, op_id="adjust:%s" % op3)
    ck("P0-3 同op_id不同uid必须报错(拒绝语义冲突)", False, "未抛异常")
except RuntimeError:
    ck("P0-3 同op_id不同uid必须报错(拒绝语义冲突)", True)
# 3) 同 op_id 语义一致重放 → 幂等返回当前余额（加款恰好一次）
try:
    A.gw_add_quota(nid, delta_real, op_id="adjust:%s" % op3)
    ck("P0-3 同op_id同语义重放幂等返回", True)
except RuntimeError as e:
    ck("P0-3 同op_id同语义重放幂等返回", False, str(e))
bal_after_fail = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
# 管理员重试（HTTP 实际路由，同一 op_id）——应只补流水不再加款
s, r, _ = api("POST", "/admin/wallet/adjust", {"uid": uid, "amount": "0.5", "note": "saga重试", "op_id": op3}, ACK, form=True)
bal_after_retry = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
ck("P0-3 流水失败后同op_id重试不重复加款", bal_after_retry == bal_after_fail, (bal_after_fail, bal_after_retry))
tx3 = q("SELECT * FROM wallet_tx WHERE ref=?", ("adjust:%s" % op3,))
ck("P0-3 重试后流水恰有一条(ref唯一凭据)", len(tx3) == 1, len(tx3))
ck("P0-3 流水余额快照已回填", tx3 and tx3[0]["balance_cny"] is not None, dict(tx3[0]) if tx3 else None)
# 回滚
A.gw_add_quota(nid, -delta_real, op_id="rb2:%s" % op3)

# ── 负向扣减原子性（审查 R1/P1-02：禁止 MAX(0,...) 截断） ──
bal0 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
# 1) 余额不足扣减 → 必须报错，余额分毫不变（不再被截断到 0）
try:
    A.gw_add_quota(nid, -(bal0 + 100), op_id="negtest:%s" % op3)
    ck("P0-3 余额不足扣减必须报错(不再截断为0)", False, "未抛异常")
except RuntimeError:
    ck("P0-3 余额不足扣减必须报错(不再截断为0)", True)
bal1 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
ck("P0-3 余额不足扣减失败后余额不变", bal1 == bal0, (bal0, bal1))
# 2) 并发两笔扣减总和超过余额 → 恰好一笔成功一笔失败，余额不为负、无丢失更新
_d = bal0 // 2 + 10
_errs = []
def _neg():
    try:
        A.gw_add_quota(nid, -_d, op_id="negc%s:%s" % (threading.get_ident(), op3))
    except RuntimeError:
        _errs.append(1)
_ths = [threading.Thread(target=_neg) for _ in range(2)]
[t.start() for t in _ths]; [t.join() for t in _ths]
bal2 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
ck("P0-3 并发超扣恰一笔成功一笔失败", len(_errs) == 1 and bal0 - _d == bal2, (len(_errs), bal0, bal2))
ck("P0-3 扣减后余额不为负", bal2 >= 0, bal2)
# 恢复余额供后续用例
A.gw_add_quota(nid, bal0 - bal2, op_id="restore:%s" % op3)

# ── P0-4：支付申请幂等（同 crid 重复提交返回原订单） ──
crid = uuid.uuid4().hex
s1, r1_, _ = api("POST", "/api/wallet/pay-request", {"cny": 5, "client_request_id": crid}, CK)
j1 = json.loads(r1_)
s2, r2_, _ = api("POST", "/api/wallet/pay-request", {"cny": 5, "client_request_id": crid}, CK)
j2 = json.loads(r2_)
ck("P0-4 首次提交创建订单", s1 == 200 and j1.get("ok"), (s1, r1_[:80]))
ck("P0-4 重复提交返回同一订单", j2.get("order_id") == j1.get("order_id"), (j1.get("order_id"), j2.get("order_id")))
orders = q("SELECT id FROM pay_orders WHERE uid=? AND client_request_id=?", (uid, crid))
ck("P0-4 库中只建一单(唯一约束)", len(orders) == 1, len(orders))

# 并发双击：两线程同时提交同 crid
import threading
crid2 = uuid.uuid4().hex
oids = []
def submit():
    s, r_, _ = api("POST", "/api/wallet/pay-request", {"cny": 5, "client_request_id": crid2}, CK)
    try:
        oids.append(json.loads(r_).get("order_id"))
    except Exception:
        oids.append(None)
t1, t2 = threading.Thread(target=submit), threading.Thread(target=submit)
t1.start(); t2.start(); t1.join(); t2.join()
ck("P0-4 并发双击订单ID一致或仅一单", len(set(oids)) == 1, oids)
ck("P0-4 并发后库中仅一单", len(q("SELECT id FROM pay_orders WHERE uid=? AND client_request_id=?", (uid, crid2))) == 1)

# ── P0-1：审批 saga（正常审批 + 加款幂等 + 重复审批幂等） ──
oid = j1["order_id"]
bal0 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
s, r, _ = api("POST", "/admin/wallet/pay", {"oid": oid, "action": "approve"}, ACK, form=True)
bal1 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
expect = A.cny_to_quota(5.0)
ck("P0-1 审批后到账 ¥5", bal1 - bal0 == expect, (bal0, bal1, expect))
st = q("SELECT status FROM pay_orders WHERE id=?", (oid,))[0]["status"]
ck("P0-1 saga 收尾 status=approved", st == "approved", st)
txp = q("SELECT * FROM wallet_tx WHERE ref=?", ("pay:%s" % oid,))
ck("P0-1 流水恰一条 ref=pay:<oid>", len(txp) == 1, len(txp))
# 重复审批（幂等/拒绝）
s, r, _ = api("POST", "/admin/wallet/pay", {"oid": oid, "action": "approve"}, ACK, form=True)
bal2 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] or 0
ck("P0-1 重复审批不再加款", bal2 == bal1, (bal1, bal2))
# 故障注入：加款失败（new-api 用户消失场景）→ 订单停在 processing 可续跑
oid2 = j2.get("order_id")
if oid2 == oid:
    # crid2 的订单用于续跑测试
    oid2 = oids[0]
# 模拟「加款成功但流水失败」崩溃：手工置 processing + 补做流水（模拟重启后续跑）
if oid2:
    c = sqlite3.connect(SITE)
    c.execute("UPDATE pay_orders SET status='processing' WHERE id=?", (oid2,))
    c.commit(); c.close()
    s, r, _ = api("POST", "/admin/wallet/pay", {"oid": oid2, "action": "approve"}, ACK, form=True)
    st2 = q("SELECT status FROM pay_orders WHERE id=?", (oid2,))[0]["status"]
    ck("P0-1 processing 订单可续跑到 approved", st2 == "approved", st2)

# ── quota_ops 对账一致性 ──
ops = q("SELECT op_id, delta FROM quota_ops WHERE op_id IN ('pay:%s','adjust:%s')" % (oid, op3), db=NAPI)
ck("quota_ops 与流水一一对应", len(ops) == 2, [dict(x) for x in ops])

# ── 清理 ──
c = sqlite3.connect(SITE)
c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
c.execute("DELETE FROM pay_orders WHERE uid=?", (uid,))
c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
c.execute("DELETE FROM users WHERE id=?", (uid,))
c.commit(); c.close()
n = sqlite3.connect(NAPI)
for t, col in (("logs", "user_id"), ("tokens", "user_id"), ("users", "id")):
    n.execute("DELETE FROM %s WHERE %s=?" % (t, col), (nid,))
n.execute("DELETE FROM quota_ops WHERE napi_uid=?", (nid,))
n.commit(); n.close()
print(U, "已清理")

fails = [n for n, ok in results if not ok]
print("\n════ P0 故障注入专项: ✅%d ❌%d ════" % (len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
