# -*- coding: utf-8 -*-
"""审查 R2/P1-15 云同步冲突处理回归：
服务端 rev 权威、base_revs 并发冲突检测、时钟回拨容忍、恢复一致快照"""
import io, json, os, sys, sqlite3, urllib.request, urllib.error, urllib.parse, uuid, zipfile
from _guard import guard, assert_restart_allowed

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE = "/tmp/chatsite-test/data.db"
guard(SITE)
TAG = os.environ.get("RT_TAG", "")
U = "rttest%s_cloud" % TAG
PWD = "cloud123456"
results = []


def ck(name, cond, extra=None):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % (extra,)))


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OP = urllib.request.build_opener(NR)


def api(method, path, body=None, hdrs=None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    hdr = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
    hdr.update(hdrs or {})
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=hdr)
    try:
        resp = OP.open(r, timeout=30)
        return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def mkzip(sessions):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"export_format_version": 3}))
        for sid, sess in sessions.items():
            z.writestr("chat_sessions/%s.json" % sid,
                       json.dumps({"id": sid, "title": sess.get("title", "t"),
                                   "updated_at": sess.get("updated_at", ""),
                                   "messages": sess.get("messages", [])}))
    return buf.getvalue()


def q(sql, p=(), db=SITE):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, p).fetchall()
    c.close()
    return rows


# ── 准备：注册 + 激活 + 登录 ──
api("POST", "/api/register", {"u": U, "p": PWD})
c = sqlite3.connect(SITE)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit(); c.close()
s, r, h = api("POST", "/api/login", {"u": U, "p": PWD})
setc = h.get("Set-Cookie") or ""
CK = setc.split(";")[0]
ck("准备:注册并登录", s == 200 and CK, (s, setc[:40]))

sid = str(uuid.uuid4())
BUP = {"Content-Type": "application/octet-stream", "Cookie": CK}

# 1. 首次上传 → rev=1
z1 = mkzip({sid: {"updated_at": "2026-01-01T00:00:00Z", "messages": [{"role": "user", "content": "v1"}]}})
s1, r1, _ = api("POST", "/api/backup?deleted=%5B%5D", z1, BUP)
j1 = json.loads(r1)
ck("首次上传成功", s1 == 200 and j1.get("ok"), (s1, r1[:120]))
ck("首次上传 rev=1", (j1.get("revs") or {}).get(sid) == 1, j1.get("revs"))

# 2. 相同内容重传 → rev 不虚增
s2, r2, _ = api("POST", "/api/backup?deleted=%5B%5D", z1, BUP)
j2 = json.loads(r2)
ck("相同内容重传 rev 不虚增", (j2.get("revs") or {}).get(sid) == 1, j2.get("revs"))

# 3. 正确 base_rev 更新 → 覆盖成功 rev=2
z2 = mkzip({sid: {"updated_at": "2026-01-02T00:00:00Z", "messages": [{"role": "user", "content": "v2"}]}})
s3, r3, _ = api("POST", "/api/backup?deleted=%5B%5D&base_revs=" + urllib.parse.quote(json.dumps({sid: 1})), z2, BUP)
j3 = json.loads(r3)
ck("base_rev 匹配时覆盖成功", j3.get("ok") and sid not in (j3.get("conflicts") or []), r3[:150])
ck("覆盖后 rev=2", (j3.get("revs") or {}).get(sid) == 2, j3.get("revs"))

# 4. 落后的 base_rev（双设备并发编辑）→ 冲突，云端保留
z3 = mkzip({sid: {"updated_at": "2026-01-03T00:00:00Z", "messages": [{"role": "user", "content": "stale-device"}]}})
s4, r4, _ = api("POST", "/api/backup?deleted=%5B%5D&base_revs=" + urllib.parse.quote(json.dumps({sid: 1})), z3, BUP)
j4 = json.loads(r4)
ck("落后 base_rev 触发冲突", sid in (j4.get("conflicts") or []), r4[:150])
uid_row = q("SELECT id FROM users WHERE username=?", (U,))[0]["id"]
row = q("SELECT session_json, rev FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid))
ck("冲突时云端保留 rev2 内容", b"v2" in row[0]["session_json"] and row[0]["rev"] == 2, row[0]["rev"] if row else None)

# 5. 时钟回拨（无 base_revs 旧客户端）：客户端 updated_at 更老 → 云端较新，不覆盖
z4 = mkzip({sid: {"updated_at": "2020-01-01T00:00:00Z", "messages": [{"role": "user", "content": "old-clock"}]}})
s5, r5, _ = api("POST", "/api/backup?deleted=%5B%5D", z4, BUP)
j5 = json.loads(r5)
row = q("SELECT session_json, rev FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid))
ck("时钟回拨不覆盖云端", b"old-clock" not in row[0]["session_json"], dict(row[0]) if row else None)

# 6. 毫秒精度差异不致死循环：相同时刻不同格式
z5 = mkzip({sid: {"updated_at": "2026-01-02T00:00:00.000Z", "messages": [{"role": "user", "content": "v2"}]}})
s6, r6, _ = api("POST", "/api/backup?deleted=%5B%5D", z5, BUP)
j6 = json.loads(r6)
ck("毫秒精度差异不触发反向覆盖", j6.get("ok"), r6[:120])

# 7. 恢复一致性：restore 返回服务端合并状态（含 v2，不含 stale-device）
s7, r7, _ = api("GET", "/api/restore", hdrs={"Cookie": CK})
ok_restore = s7 == 200 and r7[:2] == b"PK"
ck("restore 返回 zip", ok_restore, s7)
if ok_restore:
    zf = zipfile.ZipFile(io.BytesIO(r7))
    names = zf.namelist()
    body = zf.read("chat_sessions/%s.json" % sid).decode() if "chat_sessions/%s.json" % sid in names else ""
    ck("restore 含云端权威内容(v2)", "v2" in body, body[:120])
    ck("restore 不含被冲突拒绝的内容", "stale-device" not in body, body[:120])

# 8. 并发上传（两线程，无 base_revs）：rev 单调、库中单行
import threading
zA = mkzip({sid: {"updated_at": "2026-01-04T00:00:00Z", "messages": [{"role": "user", "content": "concA"}]}})
zB = mkzip({sid: {"updated_at": "2026-01-04T00:00:00Z", "messages": [{"role": "user", "content": "concB"}]}})
errs = []
def up(zx):
    try:
        s_, r_, _ = api("POST", "/api/backup?deleted=%5B%5D", zx, BUP)
        if s_ != 200:
            errs.append(s_)
    except Exception as e:
        errs.append(str(e))
ths = [threading.Thread(target=up, args=(z,)) for z in (zA, zB)]
[t.start() for t in ths]; [t.join() for t in ths]
rows = q("SELECT sid, rev FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid))
ck("并发上传库中单行 rev 单调", len(rows) == 1 and rows[0]["rev"] >= 2 and not errs, (len(rows), rows[0]["rev"] if rows else None, errs))

# 9. 图片保留语义（审查 R11/P1-a）：带图上传 → 无图上传 → restore 仍有图
def mkzip_img(sessions, with_img):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"export_format_version": 3}))
        for s_, sess in sessions.items():
            z.writestr("chat_sessions/%s.json" % s_,
                       json.dumps({"id": s_, "title": "t", "updated_at": sess["updated_at"],
                                   "messages": sess["messages"]}))
            if with_img:
                z.writestr("chat_sessions/%s_files/pic1.png" % s_, b"\x89PNG\r\n\x1a\nfake-image-bytes-r11")
    return buf.getvalue()

sid_img = str(uuid.uuid4())
def img_zip_has(blob):
    # 云端 images 是 _imgs_zip() 的 ZIP_DEFLATED 包（审查 R11 断言适配：解压后检查）
    if not blob:
        return False
    try:
        iz = zipfile.ZipFile(io.BytesIO(blob))
        names = iz.namelist()
        return any("pic1.png" in n for n in names) and any(
            b"fake-image-bytes-r11" in iz.read(n) for n in names)
    except Exception:
        return b"fake-image-bytes-r11" in blob
zi1 = mkzip_img({sid_img: {"updated_at": "2026-02-01T00:00:00Z", "messages": [{"role": "user", "content": "with-img"}]}}, True)
s9, r9, _ = api("POST", "/api/backup?deleted=%5B%5D", zi1, BUP)
j9 = json.loads(r9)
ck("带图上传成功", s9 == 200 and j9.get("ok"), (s9, r9[:120]))
img_row = q("SELECT images FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid_img))
ck("云端 images 已存图片", img_row and img_zip_has(img_row[0]["images"]),
   len(img_row[0]["images"]) if img_row and img_row[0]["images"] else None)
zi2 = mkzip_img({sid_img: {"updated_at": "2026-02-02T00:00:00Z", "messages": [{"role": "user", "content": "no-img-update"}]}}, False)
s10, r10, _ = api("POST", "/api/backup?deleted=%5B%5D&base_revs=" + urllib.parse.quote(json.dumps({sid_img: 1})), zi2, BUP)
j10 = json.loads(r10)
ck("无图更新推送成功", s10 == 200 and j10.get("ok") and sid_img not in (j10.get("conflicts") or []), r10[:150])
img_row2 = q("SELECT images FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid_img))
ck("无图上传不清空云端图片", img_row2 and img_zip_has(img_row2[0]["images"]),
   len(img_row2[0]["images"]) if img_row2 and img_row2[0]["images"] else None)
s11, r11, _ = api("GET", "/api/restore", hdrs={"Cookie": CK})
img_survives = False
if s11 == 200 and r11[:2] == b"PK":
    zf11 = zipfile.ZipFile(io.BytesIO(r11))
    img_survives = "chat_sessions/%s_files/pic1.png" % sid_img in zf11.namelist()
ck("restore 后图片仍幸存", img_survives, s11)

# 10. base_revs CAS 并发（审查 R11/P1-b）：两设备同 base_rev 并发推送 → 恰好一个 conflicts 非空
sid_race = str(uuid.uuid4())
zr1 = mkzip({sid_race: {"updated_at": "2026-03-01T00:00:00Z", "messages": [{"role": "user", "content": "race-base"}]}})
s12, r12, _ = api("POST", "/api/backup?deleted=%5B%5D", zr1, BUP)
j12 = json.loads(r12)
base_rev = (j12.get("revs") or {}).get(sid_race)
ck("CAS 准备: 初始 rev 就绪", s12 == 200 and base_rev == 1, (s12, j12.get("revs")))
zrA = mkzip({sid_race: {"updated_at": "2026-03-02T00:00:00Z", "messages": [{"role": "user", "content": "race-A"}]}})
zrB = mkzip({sid_race: {"updated_at": "2026-03-02T00:00:00Z", "messages": [{"role": "user", "content": "race-B"}]}})
race_conflicts, race_errs = [], []
barrier = threading.Barrier(2)
def push_race(zx):
    try:
        barrier.wait()
        s_, r_, _ = api("POST", "/api/backup?deleted=%5B%5D&base_revs=" + urllib.parse.quote(json.dumps({sid_race: base_rev})), zx, BUP)
        j_ = json.loads(r_)
        race_conflicts.append(sid_race in (j_.get("conflicts") or []))
    except Exception as e:
        race_errs.append(str(e))
ths2 = [threading.Thread(target=push_race, args=(z,)) for z in (zrA, zrB)]
[t.start() for t in ths2]; [t.join() for t in ths2]
ck("同 base_rev 并发推送恰好一个冲突", len(race_errs) == 0 and sum(race_conflicts) == 1,
   "conflicts=%s errs=%s" % (race_conflicts, race_errs))
race_row = q("SELECT rev, session_json FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid_race))
ck("CAS 后云端单行且 rev 只前进一次", len(race_row) == 1 and race_row[0]["rev"] == base_rev + 1,
   (len(race_row), race_row[0]["rev"] if race_row else None))

# 11. 墓碑清 blob（审查 R12/P1-c）：上传→删除→blob 必须释放，否则磁盘无限占用
sid_tomb = str(uuid.uuid4())
zt = mkzip({sid_tomb: {"updated_at": "2026-04-01T00:00:00Z",
                       "messages": [{"role": "user", "content": "tomb-" + "x" * 500000}]}})
s13, r13, _ = api("POST", "/api/backup?deleted=%5B%5D", zt, BUP)
j13 = json.loads(r13)
ck("墓碑用例: 上传成功", s13 == 200 and j13.get("ok"), (s13, r13[:120]))
sz_before = q("SELECT LENGTH(session_json) AS n FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid_tomb))
s14, r14, _ = api("POST", "/api/backup?deleted=" + urllib.parse.quote(json.dumps([sid_tomb])), zt, BUP)
ck("墓碑用例: 删除请求成功", s14 == 200 and json.loads(r14).get("ok"), (s14, r14[:120]))
sz_after = q("SELECT deleted, LENGTH(session_json) AS n, LENGTH(images) AS ni FROM cloud_sessions WHERE uid=? AND sid=?", (uid_row, sid_tomb))
ck("删除后墓碑 blob 已清空", sz_after and sz_after[0]["deleted"] == 1
   and (sz_after[0]["n"] is None or sz_after[0]["n"] == 0), dict(sz_after[0]) if sz_after else None)

# ── 清理 ──
c = sqlite3.connect(SITE)
c.execute("DELETE FROM cloud_sessions WHERE uid=?", (uid_row,))
c.execute("DELETE FROM cloud_backups WHERE uid=?", (uid_row,))
c.execute("DELETE FROM users WHERE username=?", (U,))
c.commit(); c.close()
import shutil
shutil.rmtree("/tmp/chatsite-test/snapshots/%s" % uid_row, ignore_errors=True)
print("清理完成: %s" % U)

fails = [n for n, ok in results if not ok]
print("\n════ P1-15 云同步冲突: ✅%d ❌%d ════" % (len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
