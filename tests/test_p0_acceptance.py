# -*- coding: utf-8 -*-
"""审查 R3 验收脚本（按审查评论的 A/C/D/E/F 断言落地，全部必须通过）

A. secret.key 空/短自愈 + 空 key 伪造的 admin 会话被拒
C. CacheRatio 已写入且值 = in_hit/in_miss
D. 峰谷窗口按官方口径（UTC 周一至五 01-04/06-10，节假日全天谷）——含国庆用例
E. 充值幂等键：新 crid 建新单；前端 bundle 收到 ok 响应即清键
F. run_all.sh 登记全部测试套件，缺登记即失败
"""
import ast
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import urllib.error
import urllib.request
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/tmp/chatsite-test")
import app as A  # noqa: E402
from _guard import guard  # noqa: E402

guard(A.DB_PATH, "/opt/newapi/one-api.db")

APP = os.path.join("/tmp/chatsite-test", "app.py")
PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅", name)
    else:
        FAIL += 1
        print("  ❌", name, extra)


def _extract(name):
    tree = ast.parse(open(APP, encoding="utf-8").read())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in name]
    return fns


print("═══ A. secret.key 自愈 + 空 key 伪造被拒 ═══")
for bad in (b"", b"A", b"x" * 31):
    with tempfile.TemporaryDirectory() as td:
        sk = os.path.join(td, "secret.key")
        open(sk, "wb").write(bad)
        import hashlib as _h, hmac as _hm, json as _js, base64 as _b64, time as _t
        mod = types.ModuleType("m")
        mod.os = os
        mod.secrets = __import__("secrets")
        mod.BASE = td
        mod.base64 = _b64
        mod.hashlib = _h
        mod.hmac = _hm
        mod.json = _js
        mod.time = _t
        # 注入 SECRET 后执行 sign/unsign/_load_secret
        for fn in _extract({"_load_secret", "sign", "unsign"}):
            exec(compile(ast.Module([fn], []), "<t>", "exec"), mod.__dict__)
        mod.SECRET = mod._load_secret()
        ck("len=%d 的 key → 自愈 >=32" % len(bad), len(mod.SECRET) >= 32)
        # 用空 key 伪造 admin 会话
        body = base64.urlsafe_b64encode(
            json.dumps({"admin": True, "exp": 9999999999}).encode()).decode().rstrip("=")
        forged = body + "." + hmac.new(b"", body.encode(), hashlib.sha256).hexdigest()
        ck("空 key 伪造的 admin 会话被拒（len=%d）" % len(bad), mod.unsign(forged) is None)
        # 真实签发可验回
        ok_tok = mod.sign({"admin": True, "exp": 9999999999})
        ck("轮换后真实签发可验回", (mod.unsign(ok_tok) or {}).get("admin") is True)

print("═══ C. CacheRatio 写入 ═══")
st = A.recompute_ratios(None)
ck("recompute 成功/无变化", st in (A.RATIOS_SUCCESS, A.RATIOS_UNCHANGED), st)
with A.napi_connect() as nc:
    row = nc.execute("SELECT value FROM options WHERE key='CacheRatio'").fetchone()
ck("options 含 CacheRatio", row is not None)
cr = json.loads(row[0]) if row else {}
base = A.OFFICIAL_PRICES_DEFAULT["deepseek-flash"]
ck("flash 比值 ≈ in_hit/in_miss", abs(float(cr.get("deepseek-flash", 0)) -
   base["in_hit"][0] / base["in_miss"][0]) < 0.01, cr.get("deepseek-flash"))

print("═══ D. 峰谷窗口（官方口径 + 节假日）═══")
cases = [((2026, 11, 16, 1, 30), True),    # 周一 UTC 01:30 峰
         ((2026, 11, 16, 5, 0), False),    # 周一 UTC 05:00 谷（两窗口之间）
         ((2026, 11, 16, 9, 59), True),    # 周一 UTC 09:59 峰
         ((2026, 11, 16, 10, 0), False),   # 边界：10:00 起谷
         ((2026, 11, 21, 2, 0), False),    # 周六 谷
         ((2026, 10, 1, 2, 0), False)]     # 国庆（周四）→ 谷（唯一真实缺陷，已修）
for dt, want in cases:
    got = A._is_peak_now(datetime(*dt))
    ck("UTC %s → %s" % (dt, "峰" if want else "谷"), got == want, "实际=%s" % got)
# 边界动态：00:30（UTC，工作日非节假日）= 谷
ck("UTC 周一 00:30 → 谷", A._is_peak_now(datetime(2026, 11, 16, 0, 30)) is False)

print("═══ E. 充值幂等键语义 ═══")
BASE_URL = "http://127.0.0.1:3000"
U = "rttest_acc" + uuid.uuid4().hex[:6]
PWD = "pw123456"


def api(method, path, data=None, cookie=None):
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(BASE_URL + path,
                                 data=json.dumps(data).encode() if data else None,
                                 method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


s, _ = api("POST", "/api/register", {"u": U, "p": PWD})
s, r = api("POST", "/api/login", {"u": U, "p": PWD})
site = sqlite3.connect(A.DB_PATH, timeout=15)
site.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
site.commit()
CK = None
s, r = api("POST", "/api/login", {"u": U, "p": PWD})
# 取 cookie
import http.cookiejar
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
try:
    op.open(urllib.request.Request(BASE_URL + "/api/login",
        data=json.dumps({"u": U, "p": PWD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=15)
except urllib.error.HTTPError:
    pass
CK = "; ".join("%s=%s" % (c.name, c.value) for c in jar)

crid1 = uuid.uuid4().hex
s1, r1 = api("POST", "/api/wallet/pay-request", {"cny": 5.5, "client_request_id": crid1}, CK)
crid2 = uuid.uuid4().hex
s2, r2 = api("POST", "/api/wallet/pay-request", {"cny": 6, "client_request_id": crid2}, CK)
j1, j2 = json.loads(r1), json.loads(r2)
ck("第一笔 crid 建单成功", s1 == 200 and j1.get("ok"), (s1, r1[:80]))
ck("第二笔新 crid 建新单（不再被 pending 旧单锁死）",
   s2 == 200 and j2.get("ok") and j2.get("order_id") != j1.get("order_id"), (s2, r2[:120]))
# 同 crid 重放 → 幂等返回原单
s3, r3 = api("POST", "/api/wallet/pay-request", {"cny": 5.5, "client_request_id": crid1}, CK)
j3 = json.loads(r3)
ck("同 crid 重放幂等返回原单", j3.get("order_id") == j1.get("order_id"), r3[:120])
# bundle 清理语义：收到 ok 即清键
bundle = open("/tmp/chatsite-test/static/static/js/main.fe540c06.js", encoding="utf-8").read()
ck("bundle 收到 ok 响应即清 crid（不再等终态）",
   "if(o.j&&o.j.ok){localStorage.removeItem('ps_pay_crid')}" in bundle)
# 清理测试用户与订单
uid = site.execute("SELECT id FROM users WHERE username=?", (U,)).fetchone()[0]
site.execute("DELETE FROM pay_orders WHERE uid=?", (uid,))
site.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
site.execute("DELETE FROM users WHERE id=?", (uid,))
site.commit(); site.close()

print("═══ F. run_all.sh 登记完整性 ═══")
runner = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_all.sh"),
              encoding="utf-8").read()
tdir = os.path.dirname(os.path.abspath(__file__))
missing = []
for f in os.listdir(tdir):
    if f.startswith("test_") and f.endswith(".py"):
        if "tests/%s" % f not in runner and "tests\\%s" % f not in runner:
            missing.append(f)
ck("run_all.sh 登记了全部套件（缺: %s）" % (missing or "无"), not missing, missing)

print("\n总结果: 过 %d / 挂 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
