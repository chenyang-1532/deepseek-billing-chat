# -*- coding: utf-8 -*-
"""群聊 v26 E2E 自测（测试站 3100，独立 RT_TAG）：
互聊轮数（判定降级全员）/ @点名必答 / 402 中途终止 / 计费数字与 new-api logs 核对 /
群设置 API / 闲聊泡泡（冷却+成功）/ UI 入口（bundle 改名 fe540c10 + group-chat.v5）
前置：3100 测试站 + new-api(3001, /opt/newapi/one-api.db) + mock 上游(3002) 已启动。
说明：插科打诨为 15% 随机路径，为保断言确定性本套件群设置统一 quip=false。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:3100"
SITE_DB = os.environ.get("RT_SITE_DB", "/tmp/chatsite-test/data.db")
NAPI_DB = os.environ.get("RT_NAPI_DB", "/opt/newapi/one-api.db")
STATIC_DIR = "/tmp/chatsite-test/static"
ADMIN_PW = os.environ.get("ADMIN_PW", "")
if not ADMIN_PW:
    print("[H-1] 缺少 ADMIN_PW 环境变量：export ADMIN_PW=<管理密码> 后重试")
    sys.exit(2)
TAG = sys.argv[1] if len(sys.argv) > 1 else "gcv26"
U1 = "rttest%s_u1" % TAG
U2 = "rttest%s_u2" % TAG
PW = "probetest12345"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if (detail and not cond) else ""))


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def mkop():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NR())


def req(op, method, path, data=None, headers=None, form=False, timeout=300):
    if form:
        body = urllib.parse.urlencode(data or {}).encode()
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs = {"Content-Type": "application/json"}
    else:
        body, hdrs = None, {}
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={**hdrs, **(headers or {})})
    try:
        resp = op.open(r, timeout=timeout)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def uid_of(username):
    c = sqlite3.connect(SITE_DB)
    rows = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchall()
    c.close()
    return rows[0][0] if rows else None


def napi_uid(uid):
    c = sqlite3.connect(SITE_DB)
    row = c.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
    c.close()
    return row[0] if row else None


def napi_state(nid):
    c = sqlite3.connect(NAPI_DB)
    quota = c.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()[0]
    consumed = c.execute(
        "SELECT COALESCE(SUM(quota),0) FROM logs WHERE user_id=? AND type=2", (nid,)).fetchone()[0]
    c.close()
    return quota, consumed


def sse_events(raw):
    evs = []
    for blk in raw.split("\n\n"):
        ev, data = None, None
        for line in blk.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if ev and data:
            try:
                evs.append((ev, json.loads(data)))
            except Exception:
                evs.append((ev, {"_raw": data}))
    return evs


# ── 建号/充值 ──
op1 = mkop()
req(op1, "POST", "/api/register", {"u": U1, "p": PW})
st, b = req(op1, "POST", "/api/login", {"u": U1, "p": PW})
check("u1 登录", st == 200, b)
uid1 = uid_of(U1)
check("u1 有 uid", uid1 is not None, uid1)

op2 = mkop()
req(op2, "POST", "/api/register", {"u": U2, "p": PW})
req(op2, "POST", "/api/login", {"u": U2, "p": PW})
uid2 = uid_of(U2)

adm = mkop()
req(adm, "POST", "/admin/login", {"pw": ADMIN_PW}, form=True)
st, b = req(adm, "POST", "/admin/wallet/adjust", {"uid": str(uid1), "amount": "10", "dir": "add"}, form=True)
check("u1 充值 ¥10", st in (200, 303), (st, b[:100]))

nid1 = napi_uid(uid1)
q0, c0 = napi_state(nid1)
print("napi u1: quota=%s consumed=%s" % (q0, c0))

P_A = {"name": "诸葛明", "avatar": "", "persona": {
    "description": "一位谨慎的军师型谋士，说话条理分明", "personality": "冷静、缜密",
    "style": "简洁专业", "example": "", "firstMes": ""}}
P_B = {"name": "李小花", "avatar": "", "persona": {
    "description": "一位热情的美食博主", "personality": "活泼", "style": "口语化、热情",
    "example": "", "firstMes": ""}}

# ── 群设置 API ──
st, b = req(op1, "POST", "/api/groups", {"name": "v26默认群", "members": [P_A, P_B]})
gid_d = json.loads(b).get("id") if st == 200 else None
check("默认群创建", st == 200 and gid_d, (st, b[:120]))
st, b = req(op1, "GET", "/api/groups/%d" % gid_d)
s = json.loads(b)["group"].get("settings")
check("新群默认设置 rounds=1 quip=true", s == {"rounds": 1, "quip": True}, s)
st, b = req(op1, "PUT", "/api/groups/%d/settings" % gid_d, {"rounds": 4, "quip": True})
check("设置 rounds=4 被拒 400", st == 400, (st, b[:100]))
st, b = req(op1, "PUT", "/api/groups/%d/settings" % gid_d, {"rounds": 2, "quip": "yes"})
check("设置 rounds=2 成功（quip 非布尔被忽略）", st == 200 and json.loads(b)["settings"] == {"rounds": 2, "quip": True}, (st, b[:120]))
st, b = req(op1, "PUT", "/api/groups/%d/settings" % gid_d, {"rounds": 3, "quip": False})
check("设置 rounds=3 成功（站长规则：quip 强制开启，客户端关不掉）",
      st == 200 and json.loads(b)["settings"] == {"rounds": 3, "quip": True}, (st, b[:120]))
st, b = req(op2, "PUT", "/api/groups/%d/settings" % gid_d, {"rounds": 1})
check("非群主改设置 404", st == 404, (st, b[:80]))

# ── 互聊轮数 + 编排结构不变式 ──
# 判定实现有两种：mock 判定（固定文案无合法名单 → 降级全员发言）与
# 真实 LLM 判定（GCV26_REAL_JUDGE=1，服务器官方透传，选人随机）。
# 两种判定都须满足：done.rounds 正确、replies ≥ 轮数、calls ≤ 成员×轮数×2。
# mock 专属的「降级全员 2+1+1」断言仅在 GCV26_REAL_JUDGE 未设时执行。
REAL_JUDGE = os.environ.get("GCV26_REAL_JUDGE") == "1"
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid_d,
              {"content": "我们讨论一下周末去哪玩？"}, timeout=600)
evs = sse_events(raw)
kinds = [e for e, _ in evs]
ends = [d.get("sender") for e, d in evs if e == "reply_end"]
dones = [d for e, d in evs if e == "done"]
d = dones[0] if dones else {}
check("rounds=3 回合 done", bool(dones), kinds)
check("done.rounds=3", d.get("rounds") == 3, d)
check("多轮编排：replies(%s) ≥ 轮数 3" % d.get("replies"),
      d.get("replies", 0) >= 3 and len(ends) == d.get("replies"), (d, ends))
cap = 2 * 3 * 2
check("结构上限不变式：calls(%s) ≤ 成员×轮数×2(%d)" % (d.get("calls"), cap),
      d.get("calls", 999) <= cap, d)
check("cap_hit 未触发", d.get("cap_hit") is False, d)
if REAL_JUDGE:
    check("真实判定：每轮均有发言（replies≥轮数已验）", len(ends) >= 3, ends)
else:
    check("判定降级：第 1 轮全员发言（mock 判定无合法名单）",
          ends[:2] == ["诸葛明", "李小花"], ends)
    check("互聊续聊：后续轮次已回复角色再被选中（2+1+1=4 条，不连续重复）",
          len(ends) == 4 and ends[1] != ends[2] and ends[2] != ends[3], ends)
    check("done.replies=4（3 轮编排，2+1+1）", d.get("replies") == 4, d)

# ── 计费数字 mock 核对：done.total_quota 为落账时点快照（部分调用可能稍后落账），
#    钱包实扣与 new-api logs 求和才是权威口径 ──
time.sleep(2)
q1, c1 = napi_state(nid1)
check("total_quota 已落账且不超过最终流水",
      0 < d.get("total_quota", 0) <= c1 - c0, (d.get("total_quota"), c0, c1))
check("钱包实扣 = logs 消费", q0 - q1 == c1 - c0, (q0, q1, c0, c1))
check("total_cny 换算非空", d.get("total_cny") is not None and d.get("total_cny") > 0, d.get("total_cny"))
print("round cost:", d.get("total_cny"), "quota:", d.get("total_quota"), "final logs:", c1 - c0)

# ── @点名必答（rounds=1 独立群，创建时带 settings） ──
st, b = req(op1, "POST", "/api/groups",
            {"name": "点名群", "members": [P_A, P_B], "settings": {"rounds": 1, "quip": False}})
gid_m = json.loads(b).get("id") if st == 200 else None
check("点名群创建（带 settings）", st == 200 and gid_m, (st, b[:120]))
st, b = req(op1, "GET", "/api/groups/%d" % gid_m)
check("点名群 settings 生效（quip 强制开启）",
      json.loads(b)["group"]["settings"] == {"rounds": 1, "quip": True},
      b[:150])
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid_m,
              {"content": "@李小花 你拿主意吧"}, timeout=600)
evs2 = sse_events(raw)
ends2 = [dd.get("sender") for e, dd in evs2 if e == "reply_end"]
d2 = [dd for e, dd in evs2 if e == "done"][0]
check("@点名：只有被点名者回复（插科打诨触发时至多补一条）",
      ends2 and ends2[0] == "李小花" and len(ends2) <= 2, ends2)
check("@点名：done.replies=1（插科打诨触发时为 2）", d2.get("replies") in (1, 2), d2)
check("@点名：点名轮无判定调用（calls ≤ 1+可选插科打诨）", d2.get("calls") in (1, 2), d2)

# ── 闲聊泡泡：冷却窗 + 成功路径 ──
st, b = req(op1, "POST", "/api/groups/%d/idle" % gid_m, {})
jd = json.loads(b)
check("刚聊完 → 闲聊冷却拒绝", st == 200 and jd.get("ok") is False and jd.get("reason") == "cooldown",
      (st, b[:120]))
# 把最后一条消息时间改老（绕过冷却窗，验证成功路径）
c = sqlite3.connect(SITE_DB, timeout=15)
c.execute("UPDATE chat_group_messages SET created_at='2026-11-24 00:00:00' WHERE group_id=?", (gid_m,))
c.commit()
c.close()
st, b = req(op1, "POST", "/api/groups/%d/idle" % gid_m, {})
jd = json.loads(b)
check("闲聊泡泡成功返回 persona 消息",
      st == 200 and jd.get("ok") is True and jd["message"]["sender_type"] == "persona"
      and len(jd["message"]["content"]) > 0,
      (st, b[:160]))
st, b = req(op1, "GET", "/api/groups/%d/messages?limit=200" % gid_m)
lastm = json.loads(b)["messages"][-1]
check("闲聊消息已落库", lastm["sender_type"] == "persona" and lastm["id"] == jd["message"]["id"], lastm)
st, b = req(op2, "POST", "/api/groups/%d/idle" % gid_m, {})
check("他人闲聊 404", st == 404, (st, b[:80]))

# ── 402 中途终止（两轮式：正常轮先产出，抽干轮验证没钱即停） ──
# 触发器放用户消息而非角色描述：判定/发言调用携带用户消息历史 → 抽干确定生效，
# 对 mock 判定降级与真实 LLM 判定（选人随机、被选角色可能不含触发词）都成立。
req(adm, "POST", "/admin/wallet/adjust", {"uid": str(uid2), "amount": "1", "dir": "add"}, form=True)
P_D = {"name": "钱烧光", "avatar": "", "persona": {
    "description": "一位回复前把钱包烧光的测试角色", "personality": "挥霍",
    "style": "", "example": "", "firstMes": ""}}
st, b = req(op2, "POST", "/api/groups",
            {"name": "中途耗尽群", "members": [P_D, P_B], "settings": {"rounds": 2, "quip": False}})
gid_dr = json.loads(b).get("id") if st == 200 else None
check("中途耗尽群创建", st == 200 and gid_dr, (st, b[:120]))
st, raw = req(op2, "POST", "/api/groups/%d/messages" % gid_dr, {"content": "先热热身"}, timeout=600)
evs3 = sse_events(raw)
ends3 = [dd.get("sender") for e, dd in evs3 if e == "reply_end"]
check("正常轮至少一名角色照常回复（钱已花有产出）", len(ends3) >= 1, ends3)
st, raw = req(op2, "POST", "/api/groups/%d/messages" % gid_dr,
              {"content": "DRAIN_402 烧钱环节开始"}, timeout=600)
evs3 = sse_events(raw)
ends3 = [dd.get("sender") for e, dd in evs3 if e == "reply_end"]
ib = [dd for e, dd in evs3 if e == "reply_error" and dd.get("kind") == "insufficient_balance"]
d3 = [dd for e, dd in evs3 if e == "done"][0]
check("抽干轮至多一名角色发言（没钱即停）", len(ends3) <= 1, ends3)
check("insufficient_balance 事件已推送", len(ib) >= 1, ib)
check("done.aborted=true", d3.get("aborted") is True, d3)
check("无超额消费（calls≤3）", d3.get("calls", 0) <= 3, d3)

# ── 零余额第一道闸 402 ──
st, b = req(op2, "POST", "/api/groups/%d/messages" % gid_dr, {"content": "再来"}, timeout=60)
check("余额耗尽后再发 → 402", st == 402, (st, b[:120]))
# 闲聊冷却窗先于余额闸（省费优先）；回拨消息时间绕过冷却后应得 402
c = sqlite3.connect(SITE_DB, timeout=15)
c.execute("UPDATE chat_group_messages SET created_at='2026-11-24 00:00:00' WHERE group_id=?", (gid_dr,))
c.commit()
c.close()
st, b = req(op2, "POST", "/api/groups/%d/idle" % gid_dr, {})
check("零余额闲聊 → 402", st == 402, (st, b[:120]))

# ── 失败对话不入云端备份（站长规则 2026-11-24） ──
import io
import zipfile

# 激活 u2（备份接口拒绝未审核/过期账号）
c = sqlite3.connect(SITE_DB, timeout=15)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U2,))
c.commit(); c.close()

SID_FAIL = "fail0000-0000-0000-0000-000000000001"
SID_OK = "okok0000-0000-0000-0000-000000000002"
sess_fail = {"id": SID_FAIL, "title": None, "updated_at": "2026-11-24T15:00:00.000Z", "messages": [
    {"id": 1, "role": "USER", "status": "FINISHED", "fragments": [{"type": "REQUEST", "content": ""}]},
    {"id": 2, "role": "ASSISTANT", "status": "FAILED",
     "fragments": [{"type": "TEMPLATE_RESPONSE", "content": "账户余额不足，请充值后重试"}]},
]}
sess_ok = {"id": SID_OK, "title": "正常对话", "updated_at": "2026-11-24T15:01:00.000Z", "messages": [
    {"id": 1, "role": "USER", "status": "FINISHED", "fragments": [{"type": "REQUEST", "content": "你好"}]},
    {"id": 2, "role": "ASSISTANT", "status": "FINISHED",
     "fragments": [{"type": "TEMPLATE_RESPONSE", "content": "你好！"}]},
]}
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps({"export_format_version": 3}))
    for sid, sess in ((SID_FAIL, sess_fail), (SID_OK, sess_ok)):
        z.writestr("chat_sessions/%s.json" % sid, json.dumps(sess))
r2 = urllib.request.Request(BASE + "/api/backup", data=buf.getvalue(), method="POST",
                            headers={"Content-Type": "application/zip"})
try:
    resp2 = op2.open(r2, timeout=120); st, raw = resp2.status, resp2.read()
except urllib.error.HTTPError as e:
    st, raw = e.code, e.read()
check("备份上传成功", st == 200, (st, raw[:120]))
r3 = urllib.request.Request(BASE + "/api/backup", method="GET")
try:
    resp3 = op2.open(r3, timeout=120); st3, zraw = resp3.status, resp3.read()
except urllib.error.HTTPError as e:
    st3, zraw = e.code, b""
stored = set()
if st3 == 200:
    with zipfile.ZipFile(io.BytesIO(zraw)) as zdl:
        stored = {n[len("chat_sessions/"):-len(".json")] for n in zdl.namelist()
                  if n.startswith("chat_sessions/") and n.endswith(".json")}
check("全失败会话不入云端备份", st3 == 200 and SID_FAIL not in stored, (st3, SID_FAIL in stored))
check("正常会话照常备份", st3 == 200 and SID_OK in stored, (st3, SID_OK in stored))

# ── UI 入口：bundle fe540c12 + group-chat.v8 + index.html 同步（v27 UI 改造） ──
b11 = os.path.join(STATIC_DIR, "static/js/main.fe540c12.js")
v5 = os.path.join(STATIC_DIR, "static/js/group-chat.v8.js")
idx = open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
check("main.fe540c12.js 存在", os.path.exists(b11), b11)
check("group-chat.v8.js 存在", os.path.exists(v5), v5)
check("index.html 引用 fe540c12 + v5",
      "main.fe540c12.js" in idx and "group-chat.v8.js" in idx, idx[:200])
check("index.html 不再引用 c10/v4",
      "main.fe540c10.js" not in idx and "group-chat.v5.js" not in idx, idx[:200])
b11src = open(b11, encoding="utf-8").read()
v5src = open(v5, encoding="utf-8").read()
check("bundle 群聊按钮直达创建表单（系统同款样式 sK.dp）",
      'className:sK.dp,onClick:()=>{window.__gcOpenGroupChat&&window.__gcOpenGroupChat("new")}' in b11src,
      "")
check("bundle 注入工单按钮（同款样式，跳 /tickets）",
      'className:sK.dp,onClick:()=>{window.location.href="/tickets"}' in b11src, "")
check("bundle 批量管理按钮统一同款样式（不再用 manageBtn 旧样式）",
      'className:sK.dp,onClick:()=>y(!0)' in b11src and 'sK.aX,onClick:()=>y(!0)' not in b11src, "")
check("bundle i18n：新建群聊/工单/批量管理",
      'groupChat:"新建群聊"' in b11src and 'tickets:"工单"' in b11src
      and 'batchManage:"批量管理对话"' in b11src, "")
check("v5 暴露 __gcOpenGroupChat hook（支持 new/群号直达）",
      "window.__gcOpenGroupChat = openPanel" in v5src and 'mode === "new"' in v5src
      and 'typeof mode === "number"' in v5src, "")
check("v5 会话列表混入（群聊同栏 + 群聊徽标）",
      'data-gc-block' in v5src and '"群聊"' in v5src and 'list-ac14ac' in v5src
      and 'section-ac14ac' in v5src, "")
check("v5 @ 弹层 + 长按头像",
      'gc-mention' in v5src and 'bindMentionAvatar' in v5src and 'mentionQuery' in v5src, "")
check("v5 无插科打诨开关（前端不展示，后端强制开启）",
      "gc-quip" not in v5src and "插科打诨开关" not in v5src, "")
check("v5 含群设置/闲聊泡泡/@点名能力",
      all(k in v5src for k in ("/settings", "/idle", "@点名必答")), "")
check("v5 保留 402 原地开钱包链路", "__gwOpenWallet" in v5src, "")
check("v6 面板全屏化（inset:0，无浮窗尺寸限制）",
      "position:fixed;inset:0" in v5src and "min(860px" not in v5src, "")
check("v7 无面板内群列表栏（聊天区全宽，入口走会话列表）",
      all(k not in v5src for k in ("gc-glist", "gc-groups", "gc-new", "renderGroups")), "")
check("v7 右上角关闭按钮存在", 'id="gc-close"' in v5src and "gcRefreshSidebarGroups();" in v5src, "")
check("v8 返回箭头 + 点标题切群 + 返回手势接管",
      'id="gc-back"' in v5src and "toggleSwi" in v5src
      and "history.pushState({ gc: 1 }" in v5src and "popstate" in v5src, "")
check("v6 无计费小字（前端文案清理）",
      all(k not in v5src for k in ("计费落账", "费用以钱包流水", "本轮群聊消耗")), "")
app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
appsrc = open(app_path, encoding="utf-8").read()
check("服务端已移除计费延迟小字",
      "计费落账有延迟" not in appsrc, "")
st, raw = req(op1, "GET", "/static/js/main.fe540c12.js")
check("站点可服务 fe540c12 bundle", st == 200 and len(raw) > 400000, (st, len(raw)))
st, raw = req(op1, "GET", "/static/js/group-chat.v8.js")
check("站点可服务 group-chat.v8.js", st == 200 and "__gcOpenGroupChat" in raw, (st, len(raw)))

# ── 清理（双侧库零残留） ──
def cleanup_pair(uid):
    cs = sqlite3.connect(SITE_DB, timeout=15)
    row = cs.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
    gids = [r[0] for r in cs.execute("SELECT id FROM chat_groups WHERE uid=?", (uid,)).fetchall()]
    for g in gids:
        cs.execute("DELETE FROM chat_group_messages WHERE group_id=?", (g,))
    cs.execute("DELETE FROM chat_groups WHERE uid=?", (uid,))
    cs.execute("DELETE FROM persona_group_memory WHERE uid=?", (uid,))
    cs.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
    cs.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
    cs.execute("DELETE FROM users WHERE id=?", (uid,))
    cs.commit()
    cs.close()
    nid = row[0] if row else None
    if nid is not None:
        cn = sqlite3.connect(NAPI_DB, timeout=15)
        cn.execute("DELETE FROM logs WHERE user_id=?", (nid,))
        cn.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
        cn.execute("DELETE FROM users WHERE id=?", (nid,))
        cn.commit()
        cn.close()
    print("已清理测试号 uid=%s (napi=%s)" % (uid, nid))


cleanup_pair(uid1)
cleanup_pair(uid2)
_left_site = sqlite3.connect(SITE_DB).execute(
    "SELECT count(*) FROM users WHERE username IN (?,?)", (U1, U2)).fetchone()[0]
check("双侧库测试号零残留", _left_site == 0, (_left_site,))

print("\n===== 结果: %d PASS / %d FAIL =====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
