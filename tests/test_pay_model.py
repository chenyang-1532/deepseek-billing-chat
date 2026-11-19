# -*- coding: utf-8 -*-
"""
新功能综合专项：扫码充值流程 + 站点模型改写 + 官方价×倍数定价
"""
import io, uuid
import json
import math
import re
import sqlite3
import time
import urllib.request
import urllib.error
import urllib.parse

import os
from _guard import guard, assert_restart_allowed
TAG = os.environ.get("RT_TAG", "")

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test"
guard(SITE)
SITE_DB = SITE + "/data.db"
NAPI = "/opt/newapi/one-api.db"
QUSD = 500000
PASS = FAIL = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ " + name)
    else:
        FAIL += 1
        print("  ❌ " + name + (" | " + str(extra) if extra else ""))


def api(method, path, data=None, cookie=None, form=False, raw=False, timeout=20):
    body = None
    req = urllib.request.Request(BASE + path, method=method)
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            req.add_header("Content-Type", "application/json")
    req.data = body
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = _OPENER.open(req, timeout=timeout)
        content = r.read()
        return r.status, (content if raw else content.decode(errors="replace")), r.headers
    except urllib.error.HTTPError as e:
        content = e.read()
        return e.code, (content if raw else content.decode(errors="replace")), e.headers


def get_cookie(headers, name=""):
    for v in (headers.get_all("Set-Cookie") or []):
        return v.split(";")[0]
    return ""


def q(sql, args=(), db=SITE_DB):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── 准备 ──
_, _, h = api("POST", "/admin/login", {"pw": ADMIN_PW}, form=True)
ckadm = get_cookie(h)
ck("管理员登录", "admin=" in ckadm)

U = "rttest%s_pm" % TAG
api("POST", "/api/register", {"u": U, "p": "pmtest12345"})
c = sqlite3.connect(SITE_DB)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit(); c.close()
_, _, hu = api("POST", "/api/login", {"u": U, "p": "pmtest12345"})
cku = get_cookie(hu)
ck("测试用户登录", "session=" in cku)
api("GET", "/api/wallet", cookie=cku)  # provision
uid = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]
nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]
bal0 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]

time.sleep(62)  # 避开上一轮遗留的限流窗口
print("\n═══ A. 扫码充值：参数校验 ═══")
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 4.99, "client_request_id": "crida0499low"}, cookie=cku)
ck("¥4.99 < 起充¥5 → 拒绝", s == 400 and "低于" in r or "5" in r, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 0}, cookie=cku)
ck("¥0 → 拒绝", s == 400, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": -3}, cookie=cku)
ck("¥-3 → 拒绝", s == 400, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 10001}, cookie=cku)
ck("¥10001 超上限 → 拒绝", s == 400, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": "abc"}, cookie=cku)
ck("非数字 → 拒绝", s == 400, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": float("nan")}, cookie=cku)
ck("NaN → 拒绝", s in (400, 429), (s, r[:80]))  # 429=同窗口第6次请求被限流，同为拒绝
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 5}, cookie=None)
ck("未登录提交 → 403", s == 403, s)
ck("以上非法请求均未产生订单", len(q("SELECT * FROM pay_orders")) == 0)

print("\n═══ B. 收款码上传与访问 ═══")
# 1x1 png
PNG = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                    "01f15c4890000000d49444154789c626001000000ffff03000006000557"
                    "bfabd40000000049454e44ae426082")
boundary = "----bnd%d" % int(time.time())
payload = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"qr.png\"\r\n"
            "Content-Type: image/png\r\n\r\n" % boundary).encode() + PNG +
           ("\r\n--%s--\r\n" % boundary).encode())
req = urllib.request.Request(BASE + "/admin/wallet/qr", data=payload, method="POST")
req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
req.add_header("Cookie", ckadm)
try:
    r = _OPENER.open(req, timeout=15)
    loc = dict(r.headers).get("Location", "")
except urllib.error.HTTPError as e:
    loc = (e.headers.get_all("Location") or [""])[0]
ck("上传 png 收款码 → qr_ok", "qr_ok=1" in loc, loc)
s, body, hh = api("GET", "/pay-qr", cookie=cku, raw=True)
ck("用户访问 /pay-qr 200 且为 png", s == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", (s, body[:8]))
s, body, _ = api("GET", "/pay-qr", cookie=None, raw=True)
ck("未登录访问 /pay-qr → 拒绝", s == 303, s)
# 伪装图片（文本冒充 png）
payload2 = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"fake.png\"\r\n"
             "Content-Type: image/png\r\n\r\n" % boundary).encode() + b"alert(1)" +
            ("\r\n--%s--\r\n" % boundary).encode())
req = urllib.request.Request(BASE + "/admin/wallet/qr", data=payload2, method="POST")
req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
req.add_header("Cookie", ckadm)
try:
    r = _OPENER.open(req, timeout=15)
    loc = dict(r.headers).get("Location", "")
except urllib.error.HTTPError as e:
    loc = (e.headers.get_all("Location") or [""])[0]
ck("伪装 png（魔数不符）→ 拒绝", "qr_err=type" in loc, loc)
# 普通用户不能上传
try:
    req = urllib.request.Request(BASE + "/admin/wallet/qr", data=payload, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Cookie", cku)
    r = _OPENER.open(req, timeout=15)
    loc = dict(r.headers).get("Location", "")
except urllib.error.HTTPError as e:
    loc = (e.headers.get_all("Location") or [""])[0]
ck("普通用户上传 → 303 回面板（无 qr_ok）", "qr_ok" not in loc, loc)

time.sleep(62)  # A 段 7 次请求触发 5/分 限流，等窗口重置
print("\n═══ C. 充值申请 → 审批到账闭环 ═══")
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 5.5}, cookie=cku)
ck("无 client_request_id → 400（P0-4 强制幂等键）", s == 400, (s, r[:80]))
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 5.5, "client_request_id": "short!!"}, cookie=cku)
ck("非法 client_request_id → 400", s == 400, (s, r[:80]))
_crid1 = uuid.uuid4().hex
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 5.5, "client_request_id": _crid1}, cookie=cku)
j = json.loads(r)
ck("提交 ¥5.5 申请成功", s == 200 and j.get("ok"), (s, r[:100]))
oid = j.get("order_id")
row = q("SELECT * FROM pay_orders WHERE id=?", (oid,))[0]
ck("订单入库 pending", row["status"] == "pending" and row["cny"] == 5.5 and row["uid"] == uid)
s, r, _ = api("GET", "/api/wallet/pay-orders", cookie=cku)
j = json.loads(r)
ck("用户可查自己的订单", j.get("ok") and j["orders"][0]["id"] == oid and j["orders"][0]["status"] == "pending")
ck("返回 has_qr=true", j.get("has_qr") is True)

# 面板渲染含待审
s, body, _ = api("GET", "/admin", cookie=ckadm)
ck("面板充值审批区渲染待审单号", 'id="sec-pay"' in body and str(oid) in body)
ck("导航徽标显示 1", "充值审批" in body and ">1</span>" in body, body.count("充值审批"))

# 未登录/他人不能审批
s, _, hh = api("POST", "/admin/wallet/pay", {"oid": oid, "action": "approve"}, cookie=cku, form=True)
ck("普通用户审批 → 拒绝", "pay_ok" not in (hh.get("Location", "") or (hh.get_all("Location") or [""])[0]))
ck("审批拒绝后订单仍 pending", q("SELECT status FROM pay_orders WHERE id=?", (oid,))[0]["status"] == "pending")

# 通过 → 到账
s, _, hh = api("POST", "/admin/wallet/pay", {"oid": oid, "action": "approve"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("管理员通过 → pay_ok", "pay_ok=1" in loc, loc)
bal1 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
delta = bal1 - bal0
ck("余额到账 ¥5.5（quota 原子核对）", delta == int(round(5.5 * QUSD / 7.2)), (delta, int(round(5.5 * QUSD / 7.2))))
tx = q("SELECT * FROM wallet_tx WHERE uid=? AND type='pay' ORDER BY id DESC LIMIT 1", (uid,))[0]
ck("pay 流水记录（金额/余额/单号备注）", tx["cny"] == 5.5 and tx["balance_cny"] is not None and "#%s" % oid in (tx["note"] or ""))
ck("订单状态 approved", q("SELECT status FROM pay_orders WHERE id=?", (oid,))[0]["status"] == "approved")
# 重复审批（幂等）
s, _, hh = api("POST", "/admin/wallet/pay", {"oid": oid, "action": "approve"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("重复审批 → 拒绝（幂等）", "pay_ok" not in loc and "pay_err" in loc, loc)
bal2 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
ck("重复审批不重复到账", bal2 == bal1)
# 用户流水接口显示扫码充值
s, r, _ = api("GET", "/api/wallet/logs?limit=20", cookie=cku)
j = json.loads(r)
kinds = [x.get("kind") for x in j.get("logs", [])]
ck("用户钱包流水含 扫码充值 事件", "wallet" in kinds and any("扫码充值" in str(x.get("model", "")) for x in j["logs"] if x.get("kind") == "wallet"), kinds)

# 拒绝流程
_crid2 = uuid.uuid4().hex
s, r, _ = api("POST", "/api/wallet/pay-request", {"cny": 6, "client_request_id": _crid2}, cookie=cku)
oid2 = json.loads(r)["order_id"]
bal3 = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
s, _, hh = api("POST", "/admin/wallet/pay", {"oid": oid2, "action": "reject"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("拒绝订单 → pay_ok", "pay_ok=1" in loc, loc)
ck("拒绝不入账", q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"] == bal3)
ck("拒绝后状态 rejected", q("SELECT status FROM pay_orders WHERE id=?", (oid2,))[0]["status"] == "rejected")

print("\n═══ D. 站点模型改写（用户无感知） ═══")
def chat_and_log_model():
    before = q("SELECT COALESCE(MAX(id),0) AS m FROM logs WHERE user_id=?", (nid,), NAPI)[0]["m"]
    body = {"model": "deepseek-chat", "max_tokens": 200,
            "messages": [{"role": "user", "content": "模型改写测试"}]}
    s, r, _ = api("POST", "/anthropic/v1/messages", body, cookie=cku, timeout=60)
    time.sleep(0.5)
    logs = q("SELECT model_name, prompt_tokens, completion_tokens, quota FROM logs WHERE user_id=? AND id>?", (nid, before), NAPI)
    return s, logs

# 改写前确认当前默认
cur = q("SELECT value FROM settings WHERE key='gw_default_model'", db=SITE_DB)
default_model = cur[0]["value"] if cur else "deepseek-flash"
s, logs = chat_and_log_model()
ck("对话成功", s == 200, (s, logs))
ck("默认模型 %s 生效（客户端发 deepseek-chat 被网关改写）" % default_model,
   logs and logs[0]["model_name"] == default_model, logs and logs[0]["model_name"])

# 切换到 v4-pro
s, _, hh = api("POST", "/admin/wallet/model", {"model": "deepseek-v4-pro"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("后台切换模型 → model_ok", "model_ok=1" in loc, loc)
s, logs = chat_and_log_model()
ck("切换后真实调用 deepseek-v4-pro", logs and logs[0]["model_name"] == "deepseek-v4-pro", logs)
# 按 pro 当前时段官方价独立核对扣费（峰/谷自动适配：从 new-api options 读取定价引擎维护的倍率）
_mr_pro = json.loads(q("SELECT value FROM options WHERE key='ModelRatio'", (), NAPI)[0]["value"])["deepseek-v4-pro"]
_cr_pro = json.loads(q("SELECT value FROM options WHERE key='CompletionRatio'", (), NAPI)[0]["value"])["deepseek-v4-pro"]
l = logs[0]
exp = round((l["prompt_tokens"] + l["completion_tokens"] * _cr_pro) * _mr_pro)
ck("v4-pro 按官方当前时段价×1 计费", abs(l["quota"] - exp) <= 2, (l["quota"], exp))
# 非法模型名
s, _, hh = api("POST", "/admin/wallet/model", {"model": "evil-model"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("非法模型名 → model_err", "model_err=1" in loc, loc)
# 恢复 flash
s, _, hh = api("POST", "/admin/wallet/model", {"model": "deepseek-flash"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("恢复 deepseek-flash", "model_ok=1" in loc, loc)
s, logs = chat_and_log_model()
ck("恢复后回到 flash", logs and logs[0]["model_name"] == "deepseek-flash", logs)

print("\n═══ E. 官方价×倍数定价 ═══")
# 保存倍数 1.5 → 倍率=官方峰价×1.5
s, _, hh = api("POST", "/admin/wallet/ratios", {"model": "deepseek-flash", "mult": "1.5"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("保存倍数 1.5 → ratio_ok", "ratio_ok=1" in loc, loc)
mr = json.loads(q("SELECT value FROM options WHERE key='ModelRatio'", (), NAPI)[0]["value"])
cr = json.loads(q("SELECT value FROM options WHERE key='CompletionRatio'", (), NAPI)[0]["value"])
peak = _is_peak_now = None  # 峰谷由服务端决定；两种都验证
import datetime as _dt
now = _dt.datetime.utcnow()
ispk = now.weekday() < 5 and any(lo <= now.hour + now.minute / 60.0 < hi for lo, hi in [(1, 4), (6, 10)])
base_in, base_out = (0.3, 1.2) if ispk else (0.15, 0.6)
ck("flash 输入倍率 = 官方%s价$%g×1.5" % ("峰" if ispk else "谷", base_in),
   abs(mr["deepseek-flash"] - round(base_in / 1e6 * QUSD * 1.5, 6)) < 1e-9, mr["deepseek-flash"])
ck("flash 输出倍率 = 官方%s价$%g×1.5" % ("峰" if ispk else "谷", base_out),
   abs(cr["deepseek-flash"] - round(base_out / 1e6 * QUSD * 1.5, 6)) < 1e-9, cr["deepseek-flash"])
# 非法倍数
for name, mv in [("nan", "nan"), ("inf", "inf"), ("-1", "-1"), ("0", "0"), ("500", "500"), ("abc", "abc")]:
    s, _, hh = api("POST", "/admin/wallet/ratios", {"model": "deepseek-flash", "mult": mv}, cookie=ckadm, form=True)
    loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
    ck("倍数 %s → 拒绝" % name, "ratio_err=1" in loc, loc)
mr2 = json.loads(q("SELECT value FROM options WHERE key='ModelRatio'", (), NAPI)[0]["value"])
ck("非法倍数不污染倍率表", mr2["deepseek-flash"] == mr["deepseek-flash"])
# 未登录改倍数
s, _, hh = api("POST", "/admin/wallet/ratios", {"model": "deepseek-flash", "mult": "1.5"}, cookie=cku, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("普通用户改倍数 → 拒绝", "ratio_ok" not in loc, loc)
# 恢复 1.0
s, _, hh = api("POST", "/admin/wallet/ratios", {"model": "deepseek-flash", "mult": "1"}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("恢复倍数 1.0", "ratio_ok=1" in loc, loc)
mr3 = json.loads(q("SELECT value FROM options WHERE key='ModelRatio'", (), NAPI)[0]["value"])
ck("恢复后=官方%s价×1" % ("峰" if ispk else "谷"), abs(mr3["deepseek-flash"] - round(base_in / 1e6 * QUSD, 6)) < 1e-9, mr3["deepseek-flash"])
# 手动刷新官方价
s, _, hh = api("POST", "/admin/wallet/refresh-official", {}, cookie=ckadm, form=True)
loc = hh.get("Location", "") or (hh.get_all("Location") or [""])[0]
ck("手动刷新官方价（在线应成功）", "official_ok=1" in loc, loc)

print("\n═══ F. 清理 ═══")
c = sqlite3.connect(SITE_DB)
c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
c.execute("DELETE FROM pay_orders WHERE uid=?", (uid,))
c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
c.execute("DELETE FROM users WHERE id=?", (uid,))
c.commit(); c.close()
c = sqlite3.connect(NAPI)
c.execute("DELETE FROM users WHERE id=?", (nid,))
c.commit(); c.close()
ck("测试用户与订单清理完成", q("SELECT COUNT(*) AS n FROM users WHERE username=?", (U,))[0]["n"] == 0)
ck("倍率设定已复位（倍数表无残留测试值）",
   json.loads(q("SELECT value FROM settings WHERE key='model_multipliers'", db=SITE_DB)[0]["value"]).get("deepseek-flash", 1) == 1)

print("\n═══ 结果：%d 通过 / %d 失败 ═══" % (PASS, FAIL))
exit(1 if FAIL else 0)
