# -*- coding: utf-8 -*-
"""群聊功能 E2E 自测（测试站 3100，独立 RT_TAG）：
注册→充值→建群(2角色)→发消息→SSE 编排→记忆双写→单聊注入→余额扣减→边界（402/校验）
"""
import base64
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:3100"
SITE_DB = "/tmp/chatsite-test/data.db"
NAPI_DB = "/tmp/chatsite-test/one-api.db"
# H-1（审查 R-v25）：管理密码只经环境变量传入，代码库不得出现明文。
# 缺失时立即报错退出（而非静默降级——静默会让「忘了传」被误当成测试通过）。
ADMIN_PW = os.environ.get("ADMIN_PW", "")
if not ADMIN_PW:
    print("[H-1] 缺少 ADMIN_PW 环境变量：export ADMIN_PW=<管理密码> 后重试（密码不落代码库）")
    sys.exit(2)
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

# ── M-4：头像 data URL 强校验 ──
_bad_svg = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg onload='alert(1)'>").decode()
st, b = req(op1, "POST", "/api/groups",
            {"name": "坏头像群", "members": [dict(P_A, avatar=_bad_svg), P_B]})
check("M-4 SVG 头像被拒 400", st == 400, (st, b[:120]))
st, b = req(op1, "POST", "/api/groups",
            {"name": "坏头像群", "members": [dict(P_A, avatar="data:image/png;base64,!!!非法"), P_B]})
check("M-4 非法 base64 被拒 400", st == 400, (st, b[:120]))
st, b = req(op1, "POST", "/api/groups",
            {"name": "坏头像群", "members": [dict(P_A, avatar="data:image/png;base64,AAAA"), P_B]})
check("M-4 伪装魔数被拒 400", st == 400, (st, b[:120]))
PNG_1x1 = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
st, b = req(op1, "POST", "/api/groups",
            {"name": "好头像群", "members": [dict(P_A, avatar=PNG_1x1), P_B]})
check("M-4 合法 PNG 头像可建群", st == 200, (st, b[:120]))
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
check("消息持久化：user 2 条 + persona 每轮至少一人（判定选人）",
      types.count("user") == 2 and types.count("persona") >= 2, types)
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
             "stream": False,
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

# ── 测试 1：上游 500 → persona_failed，不写记忆，system 行落库 ──
# 用 @点名保证确定性：判定选人随机，只有点名才能确保张失败被要求发言。
P_T = {"name": "张失败", "avatar": "", "persona": {
    "description": "TRIGGER_500 一位专门触发上游故障的测试角色", "personality": "稳定失败",
    "style": "", "example": "", "firstMes": ""}}
st, b = req(op1, "POST", "/api/groups", {"name": "故障注入群", "members": [P_T, P_A]})
gid_t = json.loads(b).get("id") if st == 200 else None
check("故障注入群创建", st == 200 and gid_t, (st, b[:120]))
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid_t,
              {"content": "@张失败 请介绍一下自己"}, timeout=300)
evs_t = sse_events(raw)
fails = [d for e, d in evs_t if e == "reply_error" and d.get("kind") == "persona_failed"]
check("上游 500 → persona_failed", any(d.get("sender") == "张失败" for d in fails), fails)
dones_t = [d for e, d in evs_t if e == "done"]
check("失败即停本轮（aborted 不误报成功）",
      dones_t and dones_t[0].get("replies", 0) == 0, dones_t)
# 失败后群仍可用：点名诸葛明确认可正常回复（失败不损坏群状态）
st, raw = req(op1, "POST", "/api/groups/%d/messages" % gid_t,
              {"content": "@诸葛明 你在吗"}, timeout=300)
evs_t2 = sse_events(raw)
ends_t = [d.get("sender") for e, d in evs_t2 if e == "reply_end"]
check("单角色失败不阻塞群聊（诸葛明仍回复）", "诸葛明" in ends_t, ends_t)
c = sqlite3.connect(SITE_DB)
sys_rows = c.execute(
    "SELECT sender_name, content FROM chat_group_messages WHERE group_id=? AND sender_type='system'",
    (gid_t,)).fetchall()
mem_t = c.execute(
    "SELECT content FROM persona_group_memory WHERE uid=? AND persona_name='张失败'",
    (uid1,)).fetchall()
c.close()
check("失败角色 system 行落库", any(r[0] == "张失败" for r in sys_rows), sys_rows)
check("失败角色不写记忆", not mem_t, mem_t)

# ── 测试 2：402 中途耗尽（两轮式：触发器放用户消息，对 mock/真实判定都确定生效） ──
req(adm, "POST", "/admin/wallet/adjust", {"uid": str(uid2), "amount": "1", "dir": "add"}, form=True)
P_D = {"name": "钱烧光", "avatar": "", "persona": {
    "description": "一位回复前把钱包烧光的测试角色", "personality": "挥霍",
    "style": "", "example": "", "firstMes": ""}}
st, b = req(op2, "POST", "/api/groups", {"name": "中途耗尽群", "members": [P_D, P_B]})
gid_d = json.loads(b).get("id") if st == 200 else None
check("中途耗尽群创建", st == 200 and gid_d, (st, b[:120]))
st, raw = req(op2, "POST", "/api/groups/%d/messages" % gid_d,
              {"content": "先热热身"}, timeout=300)
evs_d = sse_events(raw)
ends_d = [d.get("sender") for e, d in evs_d if e == "reply_end"]
check("正常轮至少一名角色照常回复（钱已花有产出）", len(ends_d) >= 1, ends_d)
st, raw = req(op2, "POST", "/api/groups/%d/messages" % gid_d,
              {"content": "DRAIN_402 烧钱环节开始"}, timeout=300)
evs_d = sse_events(raw)
ends_d = [d.get("sender") for e, d in evs_d if e == "reply_end"]
ib = [d for e, d in evs_d if e == "reply_error" and d.get("kind") == "insufficient_balance"]
check("抽干轮至多一名角色发言（没钱即停）", len(ends_d) <= 1, ends_d)
check("insufficient_balance 事件已推送", len(ib) >= 1, ib)
dones_d = [d for e, d in evs_d if e == "done"]
check("done.aborted=true",
      dones_d and dones_d[0].get("aborted") is True, dones_d)

# ── 测试 3：并发回合 → 第二个请求 429 ──
op1b = mkop()
st, b = req(op1b, "POST", "/api/login", {"u": U1, "p": PW})
check("op1b 登录", st == 200, b[:100])
results_c = [None, None]  # 预分配：线程内 out[idx]=... 对空列表赋值会 IndexError


def _send_concurrent(op, out, idx):
    out[idx] = req(op, "POST", "/api/groups/%d/messages" % gid, {"content": "并发测试"}, timeout=300)


ths = [threading.Thread(target=_send_concurrent, args=(op1b, results_c, i)) for i in range(2)]
ths[0].start()
time.sleep(1.0)   # 确保第一个请求已拿到回合锁、进入回复中
ths[1].start()
ths[0].join()
ths[1].join()
codes = sorted(st for st, _ in results_c)
check("并发回合：一个 200 一个 429", codes == [200, 429], [st for st, _ in results_c])
check("429 提示上一轮回复中",
      any("上一轮仍在回复中" in b for st, b in results_c if st == 429),
      [b[:120] for st, b in results_c if st == 429])

# ── 契约：消息为空 400 / 群删除 ──
st, b = req(op1, "POST", "/api/groups/%d/messages" % gid, {"content": ""})
check("空消息 400", st == 400, (st, b[:80]))
st, b = req(op2, "DELETE", "/api/groups/%d" % gid2)
check("删群成功", st == 200, (st, b[:80]))
st, b = req(op2, "GET", "/api/groups/%d/messages" % gid2)
check("删群后消息不可见", st == 404, (st, b[:80]))

# ── 清理（双侧库零残留） ──
def cleanup_pair(uid):
    cs = sqlite3.connect(SITE_DB, timeout=15)
    row = cs.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
    gids = [r[0] for r in cs.execute(
        "SELECT id FROM chat_groups WHERE uid=?", (uid,)).fetchall()]
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
_left_napi = sqlite3.connect(NAPI_DB).execute(
    "SELECT count(*) FROM users WHERE username LIKE 'site%' AND username LIKE '%rttest%'").fetchone()[0]
check("双侧库测试号零残留", _left_site == 0, (_left_site,))

print("\n===== 结果: %d PASS / %d FAIL =====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
