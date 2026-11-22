# -*- coding: utf-8 -*-
"""群聊功能 E2E 自测（测试站 3100，独立 RT_TAG）：
注册→充值→建群(2角色)→发消息→SSE 编排→记忆双写→单聊注入→余额扣减→边界（402/校验）
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:3100"
SITE_DB = "/tmp/chatsite-test/data.db"
NAPI_DB = "/tmp/chatsite-test/one-api.db"
ADMIN_PW = "WIyWKVcl5Ey5sM4K"
TAG = sys.argv[1] if len(sys.argv) > 1 else "gcp1"
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


def req(op, method, path, data=None, headers=None, form=False, timeout=180):
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


def napi_quota_for_site_uid(uid):
    c = sqlite3.connect(SITE_DB)
    row = c.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
    c.close()
    if not row:
        return None
    n = sqlite3.connect(NAPI_DB)
    r = n.execute("SELECT quota, used_quota FROM users WHERE id=?", (row[0],)).fetchone()
    n.close()
    return r


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


# ── 建号 ──
op1 = mkop()
st, b = req(op1, "POST", "/api/register", {"u": U1, "p": PW})
print("register u1", st)
st, b = req(op1, "POST", "/api/login", {"u": U1, "p": PW})
check("u1 登录", st == 200, b)
uid1 = uid_of(U1)
check("u1 有 uid", uid1 is not None, uid1)

op2 = mkop()
req(op2, "POST", "/api/register", {"u": U2, "p": PW})
req(op2, "POST", "/api/login", {"u": U2, "p": PW})
uid2 = uid_of(U2)

# ── 充值 ──
adm = mkop()
req(adm, "POST", "/admin/login", {"pw": ADMIN_PW}, form=True)
st, b = req(adm, "POST", "/admin/wallet/adjust", {"uid": str(uid1), "amount": "5", "dir": "add"}, form=True)
check("u1 充值 ¥5", st in (200, 303), (st, b[:100]))

q_before = napi_quota_for_site_uid(uid1)
print("napi quota before:", q_before)

# ── 建群 ──
P_A = {"name": "诸葛明", "avatar": "", "persona": {
    "description": "一位谨慎的军师型谋士，说话条理分明", "personality": "冷静、缜密",
    "style": "简洁专业", "example": "", "firstMes": ""}}
P_B = {"name": "李小花", "avatar": "", "persona": {
    "description": "一位热情的美食博主", "personality": "活泼", "style": "口语化、热情",
    "example": "", "firstMes": ""}}
st, b = req(op1, "POST", "/api/groups", {"name": "探店讨论群", "members": [P_A]})
check("建群校验：1 个角色被拒 400", st == 400, (st, b))
st, b = req(op1, "POST", "/api/groups", {"name": "探店讨论群", "members": [P_A, dict(P_B, name="诸葛明")]})
check("建群校验：重名被拒 400", st == 400, (st, b))
st, b = req(op1, "POST", "/api/groups", {"name": "", "members": [P_A, P_B]})
check("建群校验：空群名被拒 400", st == 400, (st, b))
st, b = req(op1, "POST", "/api/groups", {"name": "探店讨论群", "members": [P_A, P_B]})
ok = st == 200
gid = json.loads(b).get("id") if ok else None
check("建群成功", ok, (st, b[:150]))
st, b = req(op1, "GET", "/api/groups")
gl = json.loads(b)["groups"]
check("群列表含新群", any(g["id"] == gid for g in gl), b[:200])
st, b = req(op1, "GET", "/api/groups/%d" % gid)
d = json.loads(b)
check("群详情含 2 成员", d["group"]["member_count"] == 2, b[:200])
st, b = req(op2, "GET", "/api/groups/%d" % gid)
check("他人访问群详情 404", st == 404, (st, b[:80]))

# ── 发消息（SSE 编排） ──
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid, {"content": "大家好，我们周末去哪家店探店？"}, timeout=300)
evs = sse_events(raw)
kinds = [e for e, _ in evs]
check("HTTP 200 且 SSE", st == 200 and kinds, (st, raw[:200]))
check("事件含 user", "user" in kinds, kinds)
starts = [d["sender"] for e, d in evs if e == "reply_start"]
ends = [d["sender"] for e, d in evs if e == "reply_end"]
check("reply_start 含两个角色", set(starts) == {"诸葛明", "李小花"}, starts)
check("reply_end 含两个角色", set(ends) == {"诸葛明", "李小花"}, ends)
deltas_A = "".join(d.get("text", "") for e, d in evs if e == "delta")
check("delta 有实际内容", len(deltas_A) > 10, deltas_A[:50])
dones = [d for e, d in evs if e == "done"]
check("done 事件且 replies=2", dones and dones[0].get("replies") == 2, dones)
check("done 含计费 quota>0", dones and (dones[0].get("total_quota") or 0) > 0, dones)
round_cny = dones[0].get("total_cny") if dones else None
print("round cost:", round_cny, "quota:", dones[0].get("total_quota") if dones else None)

# 第二条消息：验证共享上下文（后发言者能看到先发言者）+ 记忆累积
time.sleep(1)
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid,
              {"content": "诸葛明，你倾向哪家？"}, timeout=300)
evs2 = sse_events(raw)
check("第二条消息编排正常", any(e == "done" for e, _ in evs2), [e for e, _ in evs2])

# ── 群消息持久化 ──
st, b = req(op1, "GET", "/api/groups/%d/messages?limit=200" % gid)
msgs = json.loads(b)["messages"]
types = [m["sender_type"] for m in msgs]
check("消息持久化：1 user + 2 persona/轮", types.count("user") == 2 and types.count("persona") >= 4, types)
check("消息带角色名", any(m["sender_name"] == "诸葛明" for m in msgs), [m["sender_name"] for m in msgs])

# ── 记忆双写 ──
st, b = req(op1, "GET", "/api/persona-memory?name=" + urllib.parse.quote("诸葛明"))
memA = json.loads(b)["content"]
st, b = req(op1, "GET", "/api/persona-memory?name=" + urllib.parse.quote("李小花"))
memB = json.loads(b)["content"]
check("诸葛明记忆含群名与用户消息", "探店讨论群" in memA and "周末去哪家店探店" in memA, memA[:200])
check("李小花记忆含诸葛明的发言", "诸葛明：" in memB, memB[:200])
check("记忆按序累积（两轮）", memA.count("［群聊") >= 2, memA.count("［群聊"))

# ── 余额扣减 ──
q_after = napi_quota_for_site_uid(uid1)
check("钱包余额已扣减", q_after and q_before and q_after[1] > q_before[1] and q_after[0] < q_before[0],
      (q_before, q_after))

# ── 单聊记忆注入 ──
sys_prompt = "该助手为DeepSeek，由深度求索公司创造。 【角色名】诸葛明，始终以该角色的身份、语气、知识范围与用户对话：\n【身份与背景】一位谨慎的军师型谋士，说话条理分明"
st, b = req(op1, "POST", "/anthropic/v1/messages",
            {"model": "deepseek-flash", "max_tokens": 100, "thinking": {"type": "disabled"},
             "system": sys_prompt,
             "messages": [{"role": "user", "content": "我们之前在群聊里讨论的群名是什么？只回答群名三个字。"}]},
            headers={"x-api-key": "x", "anthropic-version": "2023-06-01"}, timeout=120)
reply_txt = ""
try:
    reply_txt = "".join(x.get("text", "") for x in json.loads(b)["content"] if x.get("type") == "text")
except Exception:
    pass
check("单聊注入后角色能答出群名", "探店" in reply_txt, (st, reply_txt[:120], b[:200] if st != 200 else ""))
q_after2 = napi_quota_for_site_uid(uid1)
check("单聊调用也有扣费", q_after2 and q_after2[1] > q_after[1], (q_after, q_after2))

# ── 402 边界：u2 无余额 ──
st, b = req(op2, "POST", "/api/groups", {"name": "穷人群", "members": [P_A, P_B]})
gid2 = json.loads(b).get("id") if st == 200 else None
check("u2 无余额仍可建群", st == 200 and gid2, (st, b[:120]))
st, b = req(op2, "POST", "/api/groups/%d/messages" % gid2, {"content": "嗨"}, timeout=60)
check("余额为 0 发群消息 402", st == 402, (st, b[:120]))

# ── 契约：消息为空 400 / 群删除 ──
st, b = req(op1, "POST", "/api/groups/%d/messages" % gid, {"content": ""})
check("空消息 400", st == 400, (st, b[:80]))
st, b = req(op2, "DELETE", "/api/groups/%d" % gid2)
check("删群成功", st == 200, (st, b[:80]))
st, b = req(op2, "GET", "/api/groups/%d/messages" % gid2)
check("删群后消息不可见", st == 404, (st, b[:80]))

print("\n===== 结果: %d PASS / %d FAIL =====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
