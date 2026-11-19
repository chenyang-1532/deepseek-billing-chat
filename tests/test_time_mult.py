#!/usr/bin/env python3.11
"""时段倍率专项测试（在站点进程外直接 import app.py 逻辑 + HTTP 集成验证 :3000）
测试项：时段匹配逻辑、recompute 组合数学、管理路由校验、持久化、生效链路
"""
import json, re, sys, time, urllib.request, urllib.parse, http.cookiejar, sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, "/tmp/chatsite-test")
os_env_admin = "ADMIN_PW"
PASS = FAIL = 0
def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  ✓ %s" % name)
    else: FAIL += 1; print("  ✗ %s %s" % (name, detail))

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def form(d):
    return urllib.parse.urlencode(d).encode()

def req(path, data=None, method=None):
    if isinstance(data, str):
        data = data.encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        resp = op.open(r, timeout=15)
        return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

# ── A. 逻辑单元测试（直接 import app 模块）──
print("── A. 时段倍率核心逻辑 ──")
import app as A

# A1 无规则默认
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": []}))
m, name = A._active_time_mult(datetime(2026, 1, 5, 12, 0))
ok("A1 无规则时用默认倍数", (m, name) == (1.0, "默认"), (m, name))

# A2 白天规则命中/不命中
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": [
    {"name": "白天", "start": "09:00", "end": "18:00", "mult": 1.5}]}))
ok("A2 白天时段内命中", A._active_time_mult(datetime(2026, 1, 5, 12, 0))[0] == 1.5)
ok("A3 时段外用默认", A._active_time_mult(datetime(2026, 1, 5, 8, 59))[0] == 1.0)
ok("A4 边界 18:00 不含端点", A._active_time_mult(datetime(2026, 1, 5, 18, 0))[0] == 1.0)
ok("A5 起点 09:00 含端点", A._active_time_mult(datetime(2026, 1, 5, 9, 0))[0] == 1.5)

# A6 跨零点窗口
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.2, "rules": [
    {"name": "深夜", "start": "22:00", "end": "06:00", "mult": 0.8}]}))
ok("A6 跨零点 23:30 命中", A._active_time_mult(datetime(2026, 1, 5, 23, 30))[0] == 0.8)
ok("A7 跨零点 05:59 命中", A._active_time_mult(datetime(2026, 1, 5, 5, 59))[0] == 0.8)
ok("A8 跨零点 06:00 不命中", A._active_time_mult(datetime(2026, 1, 5, 6, 0))[0] == 1.2)
ok("A9 跨零点 21:59 不命中", A._active_time_mult(datetime(2026, 1, 5, 21, 59))[0] == 1.2)

# A10 start==end 全天
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": [
    {"name": "全档", "start": "00:00", "end": "00:00", "mult": 2.0}]}))
ok("A10 start==end 视为全天", A._active_time_mult(datetime(2026, 1, 5, 3, 0))[0] == 2.0)

# A11 多规则按顺序首条命中优先
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": [
    {"name": "早", "start": "08:00", "end": "12:00", "mult": 1.1},
    {"name": "午", "start": "10:00", "end": "14:00", "mult": 1.9}]}))
m, nm = A._active_time_mult(datetime(2026, 1, 5, 11, 0))
ok("A11 重叠时段首条优先", (m, nm) == (1.1, "早"), (m, nm))

# A12 非法配置容错（坏 JSON/坏条目被丢弃，不抛异常）
A.set_setting(A.GW_TIME_MULT_KEY, "{bad json")
ok("A12 坏JSON容错", A.get_time_mults()["default"] == 1.0)
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": "x", "rules": [
    {"name": "bad", "start": "9", "end": "18", "mult": "abc"},
    {"name": "good", "start": "01:00", "end": "02:00", "mult": 3.0}]}))
tmc = A.get_time_mults()
ok("A13 坏条目丢弃/默认回落", tmc["default"] == 1.0 and len(tmc["rules"]) == 1, tmc)

# A14 倍数越界被夹紧
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 999, "rules": [
    {"name": "x", "start": "01:00", "end": "02:00", "mult": 0.001}]}))
tmc = A.get_time_mults()
ok("A14 越界夹紧到允许范围", tmc["default"] == 100.0 and tmc["rules"][0]["mult"] == 0.1, tmc)

# ── B. recompute_ratios 组合数学 ──
print("── B. 官方价×模型倍数×时段倍数 组合数学 ──")
official = A._load_official_prices()
tier = 1 if A._is_peak_now() else 0
src = "deepseek-flash"
base = (official.get("models") or {}).get(src) or A.OFFICIAL_PRICES_DEFAULT[src]

# B1 恢复干净配置：无模型倍数、无时段规则 → ModelRatio = 官方价×1
A.set_setting(A.GW_MULTIPLIER_KEY, "{}")
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": []}))
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
expect = A._ratio_from_price(base["in_miss"][tier], 1.0)
ok("B1 干净配置下 flash 倍率=官方谷/峰价", abs(mr["deepseek-flash"] - expect) < 1e-6,
   (mr["deepseek-flash"], expect, "tier=%d" % tier))

# B2 时段倍数生效
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 2.5, "rules": []}))
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr2 = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
expect2 = A._ratio_from_price(base["in_miss"][tier], 2.5)
ok("B2 时段倍数×2.5 生效", abs(mr2["deepseek-flash"] - expect2) < 1e-6, (mr2["deepseek-flash"], expect2))

# B3 模型倍数×时段倍数叠加
A.set_setting(A.GW_MULTIPLIER_KEY, json.dumps({"deepseek-flash": 2.0}))
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.5, "rules": []}))
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr3 = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
expect3 = A._ratio_from_price(base["in_miss"][tier], 2.0 * 1.5)
ok("B3 模型×时段倍数叠加=×3", abs(mr3["deepseek-flash"] - expect3) < 1e-6, (mr3["deepseek-flash"], expect3))

# B4 pro 模型按 pro 官方档同步（pro 未设模型倍数 → ×1×时段1.5）
base_p = (official.get("models") or {}).get("deepseek-v4-pro") or A.OFFICIAL_PRICES_DEFAULT["deepseek-v4-pro"]
expect_p = A._ratio_from_price(base_p["in_miss"][tier], 1.0 * 1.5)
ok("B4 v4-pro 按自身档位同步", abs(mr3["deepseek-v4-pro"] - expect_p) < 1e-6, (mr3["deepseek-v4-pro"], expect_p))

# B5 CompletionRatio 同步
with A.napi_connect() as nc:
    cr3 = json.loads(nc.execute("SELECT value FROM options WHERE key='CompletionRatio'").fetchone()[0])
expect_cr = A._ratio_from_price(base["out"][tier], 2.0 * 1.5)
ok("B5 CompletionRatio 同步", abs(cr3["deepseek-flash"] - expect_cr) < 1e-6, (cr3["deepseek-flash"], expect_cr))

# B6 值无变化不重复写库（幂等）
with A.napi_connect() as nc:
    ts0 = nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0]
changed = A.recompute_ratios(official)
ok("B6 幂等：无变化返回 unchanged 且不写库", changed == A.RATIOS_UNCHANGED and ts0 == json.dumps(mr3))

# ── C. HTTP 管理路由集成 ──
print("── C. 管理路由 HTTP 集成（:3000）──")
st, body = req("/admin/login", form({"pw": "ADMIN_PW"}), "POST")
ok("C1 管理员登录", st == 200, st)

# C2 面板出现时段倍率区块
st, body = req("/admin")
ok("C2 面板含时段倍率区块", "时段倍率" in body and "time-mult" in body)

# C3 添加规则
st, _ = req("/admin/wallet/time-mult",
            form({"name": "晚高峰", "start": "19:00", "end": "23:00", "mult": "1.8"}), "POST")
st2, body2 = req("/admin")
ok("C3 添加规则成功并回显", "晚高峰" in body2 and "×1.8" in body2)

# C4 规则即时重算（若当前时刻落在 19:00-23:00，倍率=1.5×1.8；否则=1.5）
now = datetime.now()
tmc = A.get_time_mults()
m_now, _ = A._active_time_mult(now)
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr4 = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
expect4 = A._ratio_from_price(base["in_miss"][tier], 2.0 * m_now)
ok("C4 添加规则后 new-api 倍率与当前生效组合一致",
   abs(mr4["deepseek-flash"] - expect4) < 1e-6, (mr4["deepseek-flash"], expect4, "m_now=%g" % m_now))

# C5 非法时间格式拒绝
st, body5 = req("/admin/wallet/time-mult", form({"name": "bad", "start": "25:00", "end": "23:00", "mult": "1.5"}), "POST")
ok("C5 非法 HH:MM 拒绝并提示", st == 200 and "时段倍率参数非法" in body5, (st,))

# C6 倍数越界拒绝
st, _ = req("/admin/wallet/time-mult", form({"name": "bad", "start": "10:00", "end": "11:00", "mult": "500"}), "POST")
tmc = A.get_time_mults()
ok("C6 倍数越界拒绝且未入库", len(tmc["rules"]) == 1, tmc["rules"])

# C7 非管理员拒绝
jar2 = http.cookiejar.CookieJar()
op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar2))
r = urllib.request.Request(BASE + "/admin/wallet/time-mult", b"name=x&start=01:00&end=02:00&mult=1", method="POST")
try:
    st = op2.open(r, timeout=10).status
except urllib.error.HTTPError as e:
    st = e.code
ok("C7 未登录调用被拒(重定向到登录)", st == 200, st)
tmc = A.get_time_mults()
ok("C7b 未登录未改变数据", len(tmc["rules"]) == 1, tmc["rules"])

# C8 默认倍数修改
st, _ = req("/admin/wallet/time-mult/default", form({"mult": "1.3"}), "POST")
m_now, nm_now = A._active_time_mult()
ok("C8 默认倍数修改生效", (m_now == 1.8 if nm_now == "晚高峰" else m_now == 1.3),
   (m_now, nm_now))

# C9 删除规则
st, _ = req("/admin/wallet/time-mult/del", form({"idx": "0"}), "POST")
tmc = A.get_time_mults()
ok("C9 删除规则生效", len(tmc["rules"]) == 0, tmc)

# C10 删除越界 idx 拒绝
st, _ = req("/admin/wallet/time-mult/del", form({"idx": "5"}), "POST")
ok("C10 删除越界idx拒绝", A.get_time_mults()["rules"] == [])

# C11 规则上限 full 拒绝
for i in range(A._TIME_MAX_RULES):
    req("/admin/wallet/time-mult", form({"name": "r%d" % i, "start": "%02d:00" % (i % 24), "end": "%02d:00" % ((i + 1) % 24), "mult": "1.1"}), "POST")
st, _ = req("/admin/wallet/time-mult", form({"name": "over", "start": "01:00", "end": "02:00", "mult": "1.1"}), "POST")
ok("C11 超过10条规则拒绝", len(A.get_time_mults()["rules"]) == A._TIME_MAX_RULES)
for i in reversed(range(A._TIME_MAX_RULES)):
    req("/admin/wallet/time-mult/del", form({"idx": "%d" % i}), "POST")

# ── D. 时段切换链路（模拟分钟级生效）──
print("── D. 时段切换生效链路 ──")
# D1 用「当前时刻往前1分钟到往后1分钟」的窄窗口模拟：规则命中时 recompute 应切换到规则倍数
now = datetime.now()
lo = (now - timedelta(minutes=1)).strftime("%H:%M")
hi = (now + timedelta(minutes=1)).strftime("%H:%M")
A.set_setting(A.GW_MULTIPLIER_KEY, "{}")
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": [
    {"name": "窄窗", "start": lo, "end": hi, "mult": 3.0}]}))
m_hit, nm_hit = A._active_time_mult()
ok("D1 窄窗口命中当前时刻", nm_hit == "窄窗" and m_hit == 3.0, (m_hit, nm_hit, lo, hi))
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr5 = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
ok("D2 命中后 new-api 倍率=官方价×3", abs(mr5["deepseek-flash"] - A._ratio_from_price(base["in_miss"][tier], 3.0)) < 1e-6)

# D3 窗口过期后回落默认（把窗口移到昨天之外时刻——直接用不含当前时刻的规则）
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": [
    {"name": "错开", "start": "03:00", "end": "03:30", "mult": 3.0}]}))
m_miss, _ = A._active_time_mult()
cur_hm = now.strftime("%H:%M")
if not (lo <= cur_hm <= hi):  # 窄窗口刚跨过当前时刻才需要此断言
    pass
ok("D3 错开时段回落默认×1", m_miss == 1.0, (m_miss, cur_hm))
A.recompute_ratios(official)
with A.napi_connect() as nc:
    mr6 = json.loads(nc.execute("SELECT value FROM options WHERE key='ModelRatio'").fetchone()[0])
ok("D4 回落后 new-api 倍率=官方价×1", abs(mr6["deepseek-flash"] - A._ratio_from_price(base["in_miss"][tier], 1.0)) < 1e-6)

# ── E. 持久化 ──
print("── E. 持久化 ──")
with sqlite3.connect("/tmp/chatsite-test/data.db") as c:
    row = c.execute("SELECT value FROM settings WHERE key=?", (A.GW_TIME_MULT_KEY,)).fetchone()
ok("E1 时段倍率配置持久化于 settings 表", row is not None and json.loads(row[0])["default"] == 1.0)

# ── 清理：恢复干净配置 + new-api 倍率按官方价×1 ──
A.set_setting(A.GW_MULTIPLIER_KEY, "{}")
A.set_setting(A.GW_TIME_MULT_KEY, json.dumps({"default": 1.0, "rules": []}))
A.recompute_ratios(official)

print("\n========== 时段倍率专项：%d 过 / %d 挂 ==========" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
