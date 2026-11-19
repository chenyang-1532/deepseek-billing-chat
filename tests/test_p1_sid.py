# -*- coding: utf-8 -*-
"""审查 R2/P1-12 归档路径安全回归：sid 白名单、文件名/JSON id/主键三处一致、Zip Slip、图片文件名白名单"""
import io, json, os, sys, zipfile, uuid

sys.path.insert(0, "/tmp/chatsite-test")
import app as A  # noqa: E402

results = []


def ck(name, cond, extra=None):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % (extra,)))


def mkzip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in entries:
            z.writestr(n, b)
    return buf.getvalue()


S = "chat_sessions/"
sid = str(uuid.uuid4())
sess = json.dumps({"id": sid, "title": "t", "updated_at": "1", "messages": []}).encode()

# 1. 正常会话进入拆包
out = A._split_archive(mkzip([(S + sid + ".json", sess)]))
ck("正常会话进入拆包", sid in out, list(out))
# 2. JSON id 与文件名不一致 → 拒绝
out = A._split_archive(mkzip([
    (S + sid + ".json", json.dumps({"id": sid + "x", "messages": []}).encode())]))
ck("JSON id 与文件名不一致被拒", sid not in out, list(out))
# 3. 文件名路径穿越（内嵌 id 合法）→ 拒绝
out = A._split_archive(mkzip([
    ("chat_sessions/../../escape.json", json.dumps({"id": "escape", "messages": []}).encode())]))
ck("文件名 ../ 被拒", len(out) == 0, list(out))
# 4. JSON id 路径穿越（文件名合法）→ 一致性拦截
out = A._split_archive(mkzip([
    (S + sid + ".json", json.dumps({"id": "../../escape", "messages": []}).encode())]))
ck("JSON id 含 ../ 被拒", len(out) == 0, list(out))
# 5. 子路径文件名 → 拒绝
out = A._split_archive(mkzip([
    ("chat_sessions/a/b.json", json.dumps({"id": "b", "messages": []}).encode())]))
ck("子路径文件名被拒", len(out) == 0, list(out))
# 6. 空字节 sid → 拒绝
out = A._split_archive(mkzip([
    (S + "a\x00b.json", json.dumps({"id": "a\x00b", "messages": []}).encode())]))
ck("空字节 sid 被拒", len(out) == 0, list(out))
# 7. 超长 sid → 拒绝
long_sid = "a" * 200
out = A._split_archive(mkzip([
    (S + long_sid + ".json", json.dumps({"id": long_sid, "messages": []}).encode())]))
ck("超长 sid 被拒", len(out) == 0, list(out))
# 8. 图片条目双重白名单
v = A._valid_img_entry("chat_sessions/%s_files/img.png" % sid)
ck("合法图片条目通过", bool(v) and v[0] == sid, v)
v = A._valid_img_entry("chat_sessions/%s_files/../../x.png" % sid)
ck("图片文件名穿越被拒", v is None, v)
v = A._valid_img_entry("chat_sessions/../../evil_files/a.png")
ck("图片 sid 穿越被拒", v is None, v)
v = A._valid_img_entry("chat_sessions/%s_files/.hidden" % sid)
ck("隐藏/点开头文件名被拒", v is None, v)
v = A._valid_img_entry("chat_sessions/%s_files/sub/x.png" % sid)
ck("图片嵌套子目录被拒", v is None, v)
# 9. sid 白名单正则单测
for s, dangerous in (("../../escape", True), ("a/b", True), ("a\\b", True), ("a\x00b", True),
                     ("a..b", True), ("a b", True), (str(uuid.uuid4()), False), ("a" * 64, False)):
    got = A._safe_sid(s)
    ck("sid 白名单: %s%s" % (repr(s[:16]), "→拒" if dangerous else "→过"),
       (got is None) if dangerous else (got == s), got)

fails = [n for n, ok in results if not ok]
print("\n════ P1-12 归档安全: ✅%d ❌%d ════" % (len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
