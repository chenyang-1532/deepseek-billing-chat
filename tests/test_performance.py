import sys
# -*- coding: utf-8 -*-
"""第十一部分：性能测试（真实请求打到完整计费链路）
会话 cookie 按站点 sign() 算法本地构造（仅用于压测登录环节，计费链路完全真实）。
指标：成功率 / 平均 / P95 / P99 延迟 / 错误率 / 服务进程 CPU·内存 / 余额正确性

R5（审查 R3/P0-测试安全）：
- 数据库路径不再硬编码，必须 RT_SITE_DB / RT_NAPI_DB 显式传入，缺失即 SKIP；
- 任何 sqlite3.connect 之前先过 guard()，生产机直接退出码 2 拒绝；
- 清理段用 try/finally 包住用例体——中途异常也必须删掉直插的压测账号/token，
  不留孤儿 unlimited_quota Key 在库中。
"""
import concurrent.futures as cf
import json
import os
import sqlite3
import statistics
import subprocess
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _guard import guard  # noqa: E402

TAG = os.environ.get("RT_TAG", "")

BASE = os.environ.get("RT_BASE", "http://127.0.0.1:3000")
SITE = os.environ.get("RT_SITE_DIR", "")
SITE_DB = os.environ.get("RT_SITE_DB", "")
NAPI = os.environ.get("RT_NAPI_DB", "")
if not (SITE_DB and NAPI):
    print("[SKIP] RT_SITE_DB / RT_NAPI_DB 未设置，跳过性能测试（防止误写生产库）")
    sys.exit(3)   # 审查 R11/R12：SKIP 用专用退出码，run_all 单列 SKIP，防止「17/17」名不副实
guard(SITE_DB, NAPI)          # 必须在任何 connect 之前
SITE = SITE or os.path.dirname(SITE_DB)
SECRET = open(os.path.join(SITE, "secret.key"), "rb").read()
QUSD, USD2CNY = 500000, 7.2
PASS, FAIL = 0, 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ " + name)
    else:
        FAIL += 1
        print("  ❌ " + name + (" | " + str(extra) if extra else ""))


def make_cookie(uid):
    import base64, hashlib, hmac
    body = base64.urlsafe_b64encode(json.dumps({"uid": uid, "exp": time.time() + 86400}).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return "session=%s.%s" % (body, sig)


def api(method, path, data=None, cookie=None, timeout=90):
    body = json.dumps(data).encode() if isinstance(data, dict) else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if isinstance(data, dict):
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return 0, b""


def proc_stats(pattern):
    out = subprocess.run(["ps", "-o", "pid,%cpu,rss,args", "-e"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if pattern in line and "grep" not in line:
            parts = line.split()
            return float(parts[1]), int(parts[2]) / 1024.0
    return 0.0, 0.0


print("═══ 准备 N 个压测账号（DB直插，绕过注册限流；计费链路全真实） ═══")
import hashlib
PW = hashlib.sha256(b"perftest123").hexdigest()  # 与站点 hash 算法一致即可（不登录，仅占位）


def prep_users(n):
    site = sqlite3.connect(SITE_DB, timeout=30)
    napi = sqlite3.connect(NAPI, timeout=30)
    users = []
    today = time.strftime("%Y-%m-%d")
    for i in range(n):
        u = "rttest%s_perf_%d" % (TAG, i)
        row = site.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
        if row:
            uid = row[0]
        else:
            site.execute("INSERT INTO users(username,pw_hash,status,expiry,created_at) VALUES(?,?,?,?,?)",
                         (u, PW, "approved", today, today))
            uid = site.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
        site.commit()
        mrow = site.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,)).fetchone()
        if mrow:
            nid = mrow[0]
        else:
            uname = "site%d_%s" % (uid, u[:16])
            napi.execute("INSERT INTO users(username,password,display_name,role,status,quota) VALUES(?,?,?,1,1,?)",
                         (uname[:32], os.urandom(8).hex(), u[:20], 69444))
            nid = napi.execute("SELECT id FROM users WHERE username=?", (uname[:32],)).fetchone()[0]
            key = os.urandom(24).hex()
            napi.execute("INSERT INTO tokens(user_id,key,status,name,created_time,accessed_time,expired_time,remain_quota,unlimited_quota) "
                         "VALUES(?,?,1,?,?,?,-1,0,1)", (nid, key, "site-%d" % uid, int(time.time()), int(time.time())))
            site.execute("INSERT INTO gateway_map(uid,napi_uid,token_key) VALUES(?,?,?)", (uid, nid, key))
            site.commit()
        napi.execute("UPDATE users SET quota=69444 WHERE id=?", (nid,))
        napi.commit()
        users.append((uid, nid))
    site.close()
    napi.close()
    return users


BODY = {"model": "deepseek-flash", "max_tokens": 1000, "stream": False,
        "messages": [{"role": "user", "content": "性能测试请求" * 20}]}


def run_load(n_users, req_per_user):
    users = prep_users(n_users)
    lat = []
    ok = 0
    t0 = time.time()

    def one(idx):
        uid, _ = users[idx % len(users)]
        t1 = time.time()
        s, _b = api("POST", "/anthropic/v1/messages", BODY, cookie=make_cookie(uid))
        return s, (time.time() - t1) * 1000

    with cf.ThreadPoolExecutor(max_workers=n_users) as pool:
        results = list(pool.map(one, range(n_users * req_per_user)))
    wall = time.time() - t0
    time.sleep(1.5)
    for s, ms in results:
        if s == 200:
            ok += 1
        lat.append(ms)
    # 每用户余额复核
    site = sqlite3.connect(SITE_DB, timeout=30)
    napi = sqlite3.connect(NAPI, timeout=30)
    bad_bal = 0
    for uid, nid in users:
        q = napi.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()[0]
        if q < 0:
            bad_bal += 1
    napi.close()
    site.close()
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0
    p99 = lat[min(int(len(lat) * 0.99), len(lat) - 1)] if lat else 0
    return {
        "n_users": n_users, "requests": len(lat), "success": ok,
        "error_rate": round(100 * (1 - ok / len(lat)), 2),
        "avg_ms": round(statistics.mean(lat), 1),
        "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
        "wall_s": round(wall, 2), "throughput_rps": round(len(lat) / wall, 2),
        "neg_balance_users": bad_bal,
    }


# R5：try/finally 包住整个用例体——中途异常也必须执行清理段，
# 不留直插账号 / unlimited_quota 孤儿 Key 在库中（审查 P0-测试安全）
results = {}
q_ms = 0.0
cpu0, mem0 = proc_stats("uvicorn app:app")
try:
    for n_users, rpu in ((10, 2), (50, 2), (100, 2)):
        r = run_load(n_users, rpu)
        results["users%d" % n_users] = r
        print("  %3d用户×%d请求: 成功%d/%d 错误率%.1f%% 平均%.0fms P95=%.0fms P99=%.0fms 墙钟%.1fs 吞吐%.1frps 负余额用户%d"
              % (n_users, rpu, r["success"], r["requests"], r["error_rate"], r["avg_ms"], r["p95_ms"],
                 r["p99_ms"], r["wall_s"], r["throughput_rps"], r["neg_balance_users"]))
    cpu1, mem1 = proc_stats("uvicorn app:app")

    print("\n─── 服务资源占用 ───")
    print("  uvicorn: CPU %.1f%%→%.1f%%  内存 %.0fMB→%.0fMB" % (cpu0, cpu1, mem0, mem1))
    cpu_na, mem_na = proc_stats("./new-api")
    print("  new-api: CPU %.1f%%  内存 %.0fMB" % (cpu_na, mem_na))

    ck("100并发下无负余额", results["users100"]["neg_balance_users"] == 0)
    ck("10并发错误率<5%", results["users10"]["error_rate"] < 5, results["users10"]["error_rate"])
    ck("50并发错误率<10%", results["users50"]["error_rate"] < 10, results["users50"]["error_rate"])
    ck("100并发错误率<20%", results["users100"]["error_rate"] < 20, results["users100"]["error_rate"])

    # ── 大量记录查询性能 ──
    t1 = time.time()
    c = sqlite3.connect(NAPI, timeout=15)
    c.execute("SELECT COUNT(*) FROM logs WHERE type=2").fetchone()
    q_ms = (time.time() - t1) * 1000
    print("  流水表聚合查询: %.1fms（当前 logs %d 行）" % (q_ms, c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]))
    c.close()
except Exception as e:
    FAIL += 1
    ck("性能测试主体未异常", False, repr(e))
finally:
    # ── 清理（保证执行） ──
    try:
        site = sqlite3.connect(SITE_DB, timeout=30)
        napi = sqlite3.connect(NAPI, timeout=30)
        # 审查 R11（P2）：原来是 "LIKE 'rttest%s_perf_'" % TAG 的格式化 SQL——RT_TAG
        # 含单引号即可改写 WHERE（生产库上的 DELETE），且 TAG 里的 _ 会被 LIKE 当通配符。
        _pat = "rttest" + TAG + r"\_perf\_"
        uids = [r[0] for r in site.execute(
            "SELECT id FROM users WHERE username LIKE ? ESCAPE '\\'", (_pat,)).fetchall()]
        nids = [r[0] for r in site.execute("SELECT napi_uid FROM gateway_map WHERE uid IN (%s)" % ",".join(map(str, uids))).fetchall()] if uids else []
        site.execute("DELETE FROM gateway_map WHERE uid IN (%s)" % ",".join(map(str, uids or [0])))
        site.execute("DELETE FROM users WHERE username LIKE ? ESCAPE '\\'", (_pat,))
        site.commit()
        for nid in nids:
            napi.execute("DELETE FROM logs WHERE user_id=?", (nid,))
            napi.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
            napi.execute("DELETE FROM users WHERE id=?", (nid,))
        napi.commit()
        site.close()
        napi.close()
        print("rttest_perf_* 已清理")
    except Exception as _ce:
        print("⚠️ 清理段异常（可能残留 rttest%s_perf_* 直插账号，需手工核查！）: %r" % (TAG, _ce))
print("══════════════")
print("性能测试: ✅通过 %d  ❌失败 %d" % (PASS, FAIL))

# ── 并发上传内存闸（审查 R12/P2：_UPLOAD_SEM=2，超限 503 快速失败）──
print("\n═══ 并发上传内存闸 ═══")
try:
    import threading as _th, io as _io, zipfile as _zf, json as _js, sqlite3 as _sq3
    _db = _sq3.connect(SITE_DB, timeout=15)
    _row = _db.execute("SELECT id FROM users WHERE status='approved' LIMIT 1").fetchone()
    _db.close()
    if not _row:
        print("  ⏭️ 无已审核用户，跳过并发上传闸测试")
    else:
        _ck = make_cookie(_row[0])
        _buf = _io.BytesIO()
        with _zf.ZipFile(_buf, "w", _zf.ZIP_STORED) as _z:
            _z.writestr("manifest.json", _js.dumps({"export_format_version": 3}))
            for _i in range(4):
                _z.writestr("chat_sessions/%s.json" % ("sem%02d" % _i),
                            _js.dumps({"id": "sem%02d" % _i, "title": "t", "updated_at": "2026-05-01T00:00:00Z",
                                       "messages": [{"role": "user", "content": "y" * 8_000_000}]}))
        _payload = _buf.getvalue()
        _codes, _lock, _bar = [], _th.Lock(), _th.Barrier(4)
        def _up4():
            try:
                _bar.wait()
                _req = urllib.request.Request(
                    BASE + "/api/backup?deleted=%5B%5D", data=_payload, method="POST",
                    headers={"Content-Type": "application/octet-stream", "Cookie": _ck})
                _sc = urllib.request.urlopen(_req, timeout=120).status
                with _lock: _codes.append(_sc)
            except urllib.error.HTTPError as _he:
                with _lock: _codes.append(_he.code)
            except Exception as _e2:
                with _lock: _codes.append(str(_e2)[:40])
        _ts2 = [_th.Thread(target=_up4) for _ in range(4)]
        [_t.start() for _t in _ts2]; [_t.join() for _t in _ts2]
        _n200, _n503 = _codes.count(200), _codes.count(503)
        ck("并发 4 次上传 → 2×200 + 2×503（内存闸生效）", _n200 == 2 and _n503 == 2,
           "codes=%s" % _codes)
        # 清理本次上传产生的会话
        _db = _sq3.connect(SITE_DB, timeout=15)
        _uid2 = _row[0]
        _db.execute("DELETE FROM cloud_sessions WHERE uid=?", (_uid2,))
        _db.execute("DELETE FROM cloud_backups WHERE uid=?", (_uid2,))
        _db.commit(); _db.close()
        import shutil as _sh
        _sh.rmtree(os.path.join(SITE, "snapshots", str(_uid2)), ignore_errors=True)  # R13 P3-1：随 RT_SITE_DIR
except Exception as _sem_e:
    ck("并发上传内存闸（异常: %s）" % str(_sem_e)[:60], False)

os.makedirs("/root/.codebuddy/artifact/delivery/test_results", exist_ok=True)
try:
    with open("/root/.codebuddy/artifact/delivery/test_results/perf_result.json", "w") as f:
        json.dump({"results": results, "uvicorn_cpu": [cpu0, cpu1], "uvicorn_mem_mb": [mem0, mem1],
                   "newapi_cpu": cpu_na, "newapi_mem_mb": mem_na, "logs_query_ms": round(q_ms, 1)}, f, ensure_ascii=False, indent=2)
    print("结果已存 delivery/test_results/perf_result.json")
except Exception as _je:
    print("结果文件未写出（主体中途异常时中间变量可能缺失）: %r" % _je)


# P1-13：失败必须非零退出
sys.exit(1 if FAIL else 0)
