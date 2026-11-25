# -*- coding: utf-8 -*-
"""R26 P3 专项验证 v2：回复限流 30/时 + 页数封顶（进程内构造 page_count=30000）"""
import json, sys, time, sqlite3, http.cookiejar, urllib.request, urllib.parse, urllib.error

sys.path.insert(0, "/tmp/chatsite-test")
BASE = "http://127.0.0.1:3100"
TAG = "p3chk%d" % (int(time.time()) % 100000)
U = "rttest%s_x" % TAG
PW = "probetest12345"
PASS, FAIL = [], []
def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if detail and not cond else ""))

class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NR())
def req(method, path, data=None, form=False, timeout=120):
    h = {"User-Agent": "p3chk"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    try:
        resp = op.open(r, timeout=timeout)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

# 1) 注册 + 登录（用户已存在则直接登录）
s, b, _ = req("POST", "/api/login", {"u": U, "p": PW})
if s != 200 or b'"ok":true' not in b:
    req("POST", "/api/register", {"u": U, "p": PW})
    s, b, _ = req("POST", "/api/login", {"u": U, "p": PW})
ck("登录成功", s == 200 and b'"ok":true' in b, (s, b[:120]))

c = sqlite3.connect("/tmp/chatsite-test/data.db")
uid = c.execute("SELECT id FROM users WHERE username=?", (U,)).fetchone()[0]
nid = c.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
if nid is None:   # 新注册用户尚未建档（首次走网关时才 provision），此处手动建档
    import importlib.util as _ilu
    spec0 = _ilu.spec_from_file_location("appmod0", "/tmp/chatsite-test/app.py")
    m0 = _ilu.module_from_spec(spec0)
    spec0.loader.exec_module(m0)
    urow = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    nid = m0.gw_provision({"id": uid, "username": U})[1]
else:
    nid = nid[0]

# 2) 找工单（复用上轮 P3验证 工单；无则新建）
row = c.execute("SELECT id FROM tickets WHERE subject='P3验证' ORDER BY id DESC LIMIT 1").fetchone()
if row:
    tid = row[0]
else:
    s, b, _ = req("POST", "/tickets/create", {"category": "other", "subject": "P3验证", "body": "x" * 20}, form=True)
    tid = c.execute("SELECT id FROM tickets WHERE subject='P3验证' ORDER BY id DESC LIMIT 1").fetchone()[0]

# 3) 清空限流窗口影响：直接再发 31 条（若上轮已计数，重启后窗口已清；用回复数判断）
n0 = c.execute("SELECT COUNT(*) FROM ticket_replies WHERE ticket_id=?", (tid,)).fetchone()[0]
codes = []
for i in range(31):
    s, b, h = req("POST", "/tickets/reply", {"tid": tid, "body": "r%d" % i}, form=True, timeout=60)
    codes.append((s, h.get("location", "")))
blocked = [x for x in codes if "err=rate" in x[1]]
ck("前 30 条正常 303", all(s == 303 and "err=rate" not in loc for s, loc in codes[:30]), codes[:5])
ck("第 31 条被限流(err=rate)", len(blocked) >= 1, codes[-3:])

# 4) 页数封顶：进程内调用 _ocr_fee_charge，构造上游 page_count=30000
import importlib.util
spec = importlib.util.spec_from_file_location("appmod", "/tmp/chatsite-test/app.py")
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)
FID = "p3chk-cap-%d" % (int(time.time()) % 1000000)
fake_resp = json.dumps({"id": FID, "page_count": 30000}).encode()
out = appmod._ocr_fee_charge(uid, nid, fake_resp, True, "big.pdf", b"PK")
ck("响应原样透传（静默扣费）", json.loads(out).get("id") == FID, out[:80])
c2 = sqlite3.connect("/tmp/chatsite-test/data.db")
row = c2.execute("SELECT cny, balance_cny, ref FROM wallet_tx WHERE uid=? AND ref=?", (uid, 'ocrfee:'+FID)).fetchone()
ck("扣费按封顶 5000 页 = -9.0 元", row is not None and abs(row[0] - (-9.0)) < 0.001, row)
print("wallet_tx:", row)

# 幂等复扣验证：同 file_id 再调一次不重复扣
out2 = appmod._ocr_fee_charge(uid, nid, fake_resp, True, "big.pdf", b"PK")
n = c2.execute("SELECT COUNT(*) FROM wallet_tx WHERE ref=?", ('ocrfee:'+FID,)).fetchone()[0]
ck("同 file_id 幂等不重扣", n == 1, n)

print("==== 结果: %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
