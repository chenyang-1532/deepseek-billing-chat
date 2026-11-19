# -*- coding: utf-8 -*-
"""R3 P0 专项：审查要求的 4 个失败注入用例（b134e9d 复审收口清单验收项）

1. 空 secret.key 自愈（P0-A）：0 字节 key 必须自动轮换，绝不接受空/短密钥
2. 兑换码幂等（P0-B）：gw_add_quota 成功后响应丢失 → 重试 → 只到账一次
3. CacheRatio 单价（P0-C）：recompute_ratios 必须写 CacheRatio，flash=0.02 / pro≈0.033333
4. 并发上传存档（低成本修正2）：同 ms 并发 _save_snapshot 不互相覆盖
"""
import ast
import concurrent.futures
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/tmp/chatsite-test")
import app as A  # noqa: E402
from _guard import guard  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import urllib.request  # noqa: E402

guard(A.DB_PATH, "/opt/newapi/one-api.db")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅", name)
    else:
        FAIL += 1
        print("  ❌", name, extra)


APP = os.path.join("/tmp/chatsite-test", "app.py")


def _extract_fn(name):
    """从 app.py 抽出单个函数 AST（避免 import app 触发后台线程/建库副作用）"""
    tree = ast.parse(open(APP, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.Module([fn], [])


print("═══ 1. P0-A 空 secret.key 自愈 ═══")
# 先证明旧实现的危害（红）：空 key 会被原样接受
tmpd = tempfile.mkdtemp(prefix="rt_secret_")
try:
    sk = os.path.join(tmpd, "secret.key")
    open(sk, "wb").close()  # 0 字节
    mod = types.ModuleType("m")
    mod.os = os
    mod.secrets = __import__("secrets")
    mod.fcntl = __import__("fcntl")   # R6：_load_secret 改 flock 串行化，需要 fcntl
    mod.BASE = tmpd
    exec(compile(_extract_fn("_load_secret"), "<t>", "exec"), mod.__dict__)
    key = mod._load_secret()
    ck("空 key 启动 → 自动轮换为 >=32 字节", len(key) >= 32, "len=%d" % len(key))
    ck("文件已覆写为新 key", open(sk, "rb").read() == key)
    ck("新文件权限 0600", oct(os.stat(sk).st_mode & 0o777) == "0o600")
    # 再调用一次：已 >=32 字节，不得再次轮换
    key2 = mod._load_secret()
    ck("正常 key 不被重复轮换", key2 == key)
    # 短 key（如运维误 echo 短串）同样自愈
    open(sk, "wb").write(b"123")
    key3 = mod._load_secret()
    ck("3 字节短 key → 同样自动轮换", len(key3) >= 32 and open(sk, "rb").read() == key3)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("═══ 2. P0-B 兑换码幂等（响应丢失重试只到账一次） ═══")
import urllib.error
NR = urllib.request.HTTPCookieProcessor()
BASE = "http://127.0.0.1:3000"
U = "rttest_r3" + os.environ.get("RT_TAG", "x")[:6].lower()
PWD = "pw123456"
SITE = A.DB_PATH


def api(method, path, data=None):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode() if data else None,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


api("POST", "/api/register", {"u": U, "p": PWD})
c = sqlite3.connect(SITE)
c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
c.commit(); c.close()
uid = c.execute("SELECT id FROM users WHERE username=?", (U,)).fetchone()[0] if False else sqlite3.connect(SITE).execute("SELECT id FROM users WHERE username=?", (U,)).fetchone()[0]
A.gw_provision({"id": uid, "username": U})
nid = sqlite3.connect(SITE).execute(
    "SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()[0]


def nq():
    with A.napi_connect() as nc:
        return nc.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()[0]


q0 = nq()
q1 = A.cny_to_quota(1.0)
op = "redeem:R3-TEST-CODE"
try:
    A.gw_add_quota(nid, q1, op_id=op)  # 第一次：真实到账
    # 注入「到账成功但响应丢失」：重试同一 op_id —— 幂等命中必须跳过
    A.gw_add_quota(nid, q1, op_id=op)
    q_after = nq()
    ck("重试后余额只增加一次面额", q_after - q0 == q1, (q_after - q0, q1))
    with A.napi_connect() as nc:
        n = nc.execute("SELECT COUNT(*) FROM quota_ops WHERE op_id=?", (op,)).fetchone()[0]
    ck("quota_ops 只有 1 条 redeem:<code>", n == 1, n)
    # 反向：同 op_id 不同金额 → 必须报错（语义校验，R2 已修，此处确认未被破坏）
    try:
        A.gw_add_quota(nid, q1 * 2, op_id=op)
        ck("同 op_id 不同金额必须报错", False)
    except RuntimeError:
        ck("同 op_id 不同金额必须报错", True)
finally:
    with A.napi_connect() as nc:
        nc.execute("DELETE FROM quota_ops WHERE op_id=?", (op,))
        nc.commit()

print("═══ 3. P0-C CacheRatio 单价 ═══")
st = A.recompute_ratios(None)
ck("recompute_ratios 成功/无变化", st in (A.RATIOS_SUCCESS, A.RATIOS_UNCHANGED), st)
with A.napi_connect() as nc:
    row = nc.execute("SELECT value FROM options WHERE key='CacheRatio'").fetchone()
ck("options 存在 CacheRatio", row is not None)
cache = json.loads(row[0]) if row else {}
d = A.OFFICIAL_PRICES_DEFAULT
exp_flash = round(min(d["deepseek-flash"]["in_hit"][0] / d["deepseek-flash"]["in_miss"][0],
                      d["deepseek-flash"]["in_hit"][1] / d["deepseek-flash"]["in_miss"][1]), 6)
exp_pro = round(min(d["deepseek-v4-pro"]["in_hit"][0] / d["deepseek-v4-pro"]["in_miss"][0],
                    d["deepseek-v4-pro"]["in_hit"][1] / d["deepseek-v4-pro"]["in_miss"][1]), 6)
ck("flash cache 比值 = %s（0.02，缺省时为 50 倍多收根因）" % exp_flash,
   cache.get("deepseek-flash") == exp_flash, cache.get("deepseek-flash"))
ck("pro cache 比值 = %s" % exp_pro, cache.get("deepseek-v4-pro") == exp_pro,
   cache.get("deepseek-v4-pro"))
ck("比值 ≤ 0.05（宁可少收不多收）", all(v <= 0.05 for v in cache.values() if v), cache)

# R5（审查 R3）：CacheRatio 0.02 vs new-api 内置 0.25 之争，以官方页原始响应为据。
# tests/fixtures/official_prices_snapshot.html 是 fetch_official_prices() 抓到的原始页面存档，
# 站点运行时每次成功抓取也会落盘 BASE/official_price_snapshot.html。断言：
# ① 存档快照用 _parse_official_page 解析出的价格与 OFFICIAL_PRICES_DEFAULT 一致；
# ② 命中价/未命中价 = CacheRatio 写入值（0.02 / 0.033333），官方页原文背书。
_snap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "official_prices_snapshot.html")
if os.path.exists(_snap_path):
    _parsed = A._parse_official_page(open(_snap_path, encoding="utf-8").read())
    ck("官方价快照可解析且与默认价一致", bool(_parsed) and all(
        _parsed.get(m, {}).get(k) == d.get(m, {}).get(k)
        for m in d for k in ("in_hit", "in_miss", "out")),
        {m: {k: (_parsed or {}).get(m, {}).get(k), "默认": d[m][k]} for m in d for k in ("in_hit", "in_miss", "out")})
    ck("快照比值反推 = CacheRatio 写入值（官方页原文背书 0.02）", bool(_parsed) and all(
        round(min(_parsed[m]["in_hit"][i] / _parsed[m]["in_miss"][i] for i in (0, 1)), 6) == cache.get(m)
        for m in cache if m in _parsed and _parsed[m].get("in_hit") and _parsed[m].get("in_miss")),
        {m: cache.get(m) for m in cache})
else:
    ck("官方价快照存档存在（tests/fixtures/）", False, _snap_path)

print("═══ 4. 并发上传存档不互相覆盖 ═══")
udir = tempfile.mkdtemp(prefix="rt_snap_")
try:
    orig = A.SNAP_DIR
    A.SNAP_DIR = udir
    A._save_snapshot(99999901, b"Z" * 10, 1700000000000)
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        fs = [ex.submit(A._save_snapshot, 99999901, bytes([65 + i]) * 10, 1700000000001)
              for i in range(8)]
        for f in fs:
            f.result()
    A.SNAP_DIR = orig
    # 结构：SNAP_DIR/<uid>/<ts>-<rand>.zip（并发时每个快照独立文件名，互不覆盖）
    ufiles = sorted(os.listdir(os.path.join(udir, "99999901")))
    # R5（审查 R3）：断言收紧——同毫秒 8 次并发上传必须全部留存（1 份基线 + 8 份并发），
    # 原断言 `1 <= len(ufiles)` 把同 ms 覆盖 bug（8 份只剩 1 份）判成了绿
    ck("并发 8 次同 ms 上传全部留存（1 基线 + 8 并发 = 9 份，SNAP_KEEP=%d 内）" % A.SNAP_KEEP,
       len(ufiles) == 9, (len(ufiles), ufiles))
    ck("全部为独立 .zip（无固定 .tmp 覆盖）", all(f.endswith(".zip") for f in ufiles), ufiles)
    # 每个快照内容完整（10 字节），未串写；且 8 份并发内容互不相同
    contents = [open(os.path.join(udir, "99999901", f), "rb").read() for f in ufiles]
    ok = all(len(c) == 10 for c in contents)
    ck("全部快照内容完整无串写", ok)
    ck("8 份并发快照内容互不相同（真唯一而非覆盖后同内容）",
       len(set(contents)) == 9, sorted(set(contents)))
finally:
    shutil.rmtree(udir, ignore_errors=True)

print("═══ 5. inflight 僵尸额度 TTL 自愈（审查 R5/P2） ═══")
# 根因：StreamingResponse 的 gen() 是惰性生成器，从未被消费则 finally 不执行
# （未启动的生成器 close()/GC 都不跑 finally），release 永不调用；
# 原实现无 TTL → CAP=3 漏 3 次该 uid 永久 429，只能重启进程恢复。
_test_uid = 7770001
_orig_ent = A._GW_INFLIGHT.pop(_test_uid, None)
try:
    r = [A._gw_inflight_acquire(_test_uid) for _ in range(4)]
    ck("连续泄漏 3 次后第 4 次被拒", r == [True, True, True, False], r)
    ck("泄漏后 inflight=[3, ts] 且无自愈路径前的再拒", A._gw_inflight_acquire(_test_uid) is False)
    # 模拟僵尸：last_ts 拨回 400s 前（> ZOMBIE 330s）
    A._GW_INFLIGHT[_test_uid][1] = time.time() - 400
    r5 = A._gw_inflight_acquire(_test_uid)
    ck("超 330s 僵尸额度回收 → 第 5 次自愈成功且归 1",
       r5 is True and A._GW_INFLIGHT[_test_uid][0] == 1, A._GW_INFLIGHT.get(_test_uid))
    # 正常路径：release ×4 归 0，之后可正常 acquire
    A._GW_INFLIGHT[_test_uid] = [3, time.time()]
    for _ in range(4):
        A._gw_inflight_release(_test_uid)
    ck("正常 release ×4 → count 归 0", A._GW_INFLIGHT[_test_uid][0] == 0, A._GW_INFLIGHT[_test_uid])
    ck("归 0 后可正常 acquire", A._gw_inflight_acquire(_test_uid) is True)
    # 未超 TTL 不回收（防误杀正常长回答）
    A._GW_INFLIGHT[_test_uid] = [1, time.time() - 100]
    ck("100s < 330s 不回收（不误杀长回答）", A._gw_inflight_acquire(_test_uid) is True
       and A._GW_INFLIGHT[_test_uid][0] == 2, A._GW_INFLIGHT[_test_uid])
    # Python 语义根因证据：未启动生成器的 finally 确实不执行
    def _unstarted_gen():
        try:
            yield 1
        finally:
            raise AssertionError("未消费生成器不应执行 finally")
    _unstarted_gen().close()
    ck("未启动生成器 close() 不触发 finally（根因语义确认）", True)
finally:
    if _orig_ent is not None:
        A._GW_INFLIGHT[_test_uid] = _orig_ent
    else:
        A._GW_INFLIGHT.pop(_test_uid, None)

print("\n总结果: 过 %d / 挂 %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
