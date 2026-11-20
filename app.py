# -*- coding: utf-8 -*-
"""
付费门禁版 DeepSeek Chat Demo 后端（v2）
功能：
 - 用户注册(待审核) -> 管理员人工审核 -> 访问聊天站 -> 到期申请续期 -> 管理员续期
 - 登录态 30 天有效（记住密码，免重复登录）
 - 聊天记录云端同步：按账号存云端快照，换设备登录自动恢复
 - 登录页公告栏，管理面板可编辑
 - 管理面板手机端适配
运行：uvicorn app:app --host 0.0.0.0 --port 8000
管理面板：/admin （密码用环境变量 ADMIN_PASSWORD 设置）
"""
import html
import math
import os
import fcntl
import threading
import re
import io
import sqlite3
import zipfile
import hashlib
import hmac
import base64
import json
import time
import secrets
import uuid
import mimetypes
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, Response, FileResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data.db")
STATIC_DIR = os.path.join(BASE, "static")

# ── 站点统一时区（审查 R3/P1-7）──
# 时段倍率规则、"今日消费"起点、订单/流水时间戳全部用 SITE_TZ，不依赖宿主环境
# （Docker/云主机默认 UTC，会把站长填的北京时间规则整体偏 8 小时）。
# 官方峰谷窗口例外：DeepSeek 官方口径就是 UTC，_is_peak_now 保持 utcnow。
SITE_TZ = ZoneInfo(os.environ.get("SITE_TZ", "Asia/Shanghai"))


def site_now() -> datetime:
    """站点统一时间（北京 wall clock，naive——保持既有落库/展示字符串格式不变）"""
    return datetime.now(SITE_TZ).replace(tzinfo=None)


print("[tz] SITE_TZ=%s（时段倍率/今日消费/时间戳基准；峰谷判定固定 UTC）" % SITE_TZ)
# 管理密码：必须通过环境变量显式设置；未设置时随机生成并落盘，绝不使用弱默认值
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    _pw_file = os.path.join(BASE, ".admin_password")
    if os.path.exists(_pw_file):
        ADMIN_PASSWORD = open(_pw_file).read().strip()
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(16)
        _fd = os.open(_pw_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 0600 权限创建
        with os.fdopen(_fd, "w") as f:
            f.write(ADMIN_PASSWORD)
        print("[安全] 未设置 ADMIN_PASSWORD，已随机生成管理密码并保存到 %s（首次启动请查看并妥善保管）" % _pw_file)
    try:
        os.chmod(_pw_file, 0o600)  # 历史文件权限兜底收紧
    except OSError:
        pass
SESSION_DAYS = 30        # 登录态有效期（天）：相当于自动“记住密码”
BACKUP_MAX_BYTES = 200 * 1024 * 1024  # 单账号云端快照上限 200MB
# P1-11：ZIP 解压资源限制
ZIP_MAX_ENTRIES = 5000                      # 归档条目数上限
ZIP_MAX_ENTRY_BYTES = 64 * 1024 * 1024      # 归档内单文件解压上限 64MB
ZIP_MAX_TOTAL_UNC = 512 * 1024 * 1024       # 归档总解压上限 512MB
ZIP_MAX_RATIO = 200                          # 压缩比上限（>200:1 且单文件>10MB 判为 bomb）
SNAP_DIR = os.path.join(BASE, "snapshots")   # 每次上传的完整导出包原样存档目录
SNAP_KEEP = 30                               # 每个账号保留最近 30 个包

def _load_secret() -> bytes:
    key_file = os.path.join(BASE, "secret.key")
    # R6（审查 A2/P0）：多 worker 场景下"replace 后回读"不是同步原语——回读与 replace
    # 之间没有任何互斥，谁后谁赢看调度：实测 3 worker 15/20 轮各持一把 key（登录随机掉线、
    # 日志无字）。用独立 .lock 文件 flock 串行化整个判定+轮换：持锁期间重读判定，
    # 后到 worker 看到的已是合法 key，直接复用，不再产生第二次 replace。
    lock_fd = os.open(key_file + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(key_file, "rb") as f:
                key = f.read()
        except FileNotFoundError:
            key = b""
        if len(key) >= 32:
            try:
                os.chmod(key_file, 0o600)   # 修历史遗留的宽松权限（不轮换，不踢用户）
            except OSError:
                pass
            return key
        # 空/过短密钥可被 HMAC 伪造会话（审查 P0-A）：自动轮换，旧会话全部失效。
        # 不直接 raise——服务起不来只会诱导运维 touch 一个空文件，把漏洞固化；
        # 但写入失败必须 fail-closed，绝不允许带着弱密钥对外服务。
        old_len = len(key)
        key = secrets.token_hex(32).encode()
        tmp = "%s.rotate.%d.%s" % (key_file, os.getpid(), secrets.token_hex(4))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, key_file)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError("secret.key 无法写入，拒绝以弱密钥启动: %s" % e)
        # 锁内回读：确认落盘内容与本次写入逐字节一致（防异常文件系统静默丢写）
        disk = open(key_file, "rb").read()
        if disk != key or len(disk) < 32:
            raise RuntimeError("secret.key 写入后校验失败，拒绝启动")
        print("[security] secret.key 长度=%d 不足 32，已自动轮换(新 key %d 字节)；旧会话已失效" % (old_len, len(disk)))
        return disk
    finally:
        os.close(lock_fd)

SECRET = _load_secret()

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _add_column(c, table: str, col: str, decl: str) -> None:
    """容错加列（审查 R11/P1）：多 worker 同时启动时两个进程可能同时通过 PRAGMA 检查，
    后到者 ALTER 撞 duplicate column 直接抛异常 → 该 worker 启动失败（实测 6 并发 1 崩）。
    表名/列名为本文件内硬编码常量，不接受外部输入。"""
    try:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pw_hash TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                expiry TEXT DEFAULT '',
                renewal_requested INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                created_at TEXT, approved_at TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS cloud_backups(
                uid INTEGER PRIMARY KEY,
                data BLOB,
                updated_at INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS gateway_map(
                uid INTEGER PRIMARY KEY,
                napi_uid INTEGER,
                token_key TEXT,
                created_at TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS redeem_codes(
                code TEXT PRIMARY KEY,
                cny REAL NOT NULL,
                created_at TEXT,
                used_by INTEGER,
                used_at TEXT,
                status TEXT
            )"""
        )
        # 迁移：老库补 status 列并回填（兑换 saga，审查 R2：unused→processing→used 持久状态机）
        _rc_cols = [r[1] for r in c.execute("PRAGMA table_info(redeem_codes)").fetchall()]
        if "status" not in _rc_cols:
            _add_column(c, "redeem_codes", "status", "TEXT")
        c.execute(
            "UPDATE redeem_codes SET status=CASE WHEN used_by IS NOT NULL THEN 'used' ELSE 'unused' END "
            "WHERE status IS NULL"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS wallet_tx(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                type TEXT NOT NULL,
                cny REAL NOT NULL,
                balance_cny REAL,
                note TEXT DEFAULT '',
                created_at TEXT
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_tx_uid ON wallet_tx(uid, id)"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS pay_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                username TEXT NOT NULL,
                cny REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                handled_at TEXT
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_pay_orders_status ON pay_orders(status, id)"
        )
        # ── 审查修复：跨库资金操作幂等（P0-1/P0-3/P0-4） ──
        # wallet_tx.ref：业务操作号唯一凭据（adjust:<op_id> / pay:<oid>），禁止再用备注 LIKE 对账
        cols = {r["name"] for r in c.execute("PRAGMA table_info(wallet_tx)")}
        if "ref" not in cols:
            _add_column(c, "wallet_tx", "ref", "TEXT")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_tx_ref ON wallet_tx(ref) "
            "WHERE ref IS NOT NULL"
        )
        cols2 = {r["name"] for r in c.execute("PRAGMA table_info(pay_orders)")}
        if "client_request_id" not in cols2:
            _add_column(c, "pay_orders", "client_request_id", "TEXT")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_orders_crid ON pay_orders(uid, client_request_id) "
            "WHERE client_request_id IS NOT NULL"
        )

def today() -> str:
    return site_now().date().isoformat()

def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    with db() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

# 这些账号登录后不显示"会员剩余天数"角标（宣传截图用）
HIDE_MEMBERSHIP_USERS = {"admin"}


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"

def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(
        hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex(), h
    )

def sign(obj: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"

def unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    good = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    pad = "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data

def _req_secure(request: Request | None) -> bool:
    """HTTPS 探测：反代场景看 X-Forwarded-Proto，直连看 url.scheme（审查 P1-10）"""
    if request is None:
        return False
    xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return xfp == "https" or request.url.scheme == "https"


def set_cookie(resp: Response, name: str, value: str, days: int, request: Request | None = None):
    resp.set_cookie(
        name, value, max_age=days * 86400, httponly=True,
        samesite="lax", path="/", secure=_req_secure(request),
    )

def get_user(request: Request):
    data = unsign(request.cookies.get("session"))
    if not data:
        return None
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (data.get("uid"),)).fetchone()
    return row

def is_expired(user) -> bool:
    if not user["expiry"]:
        return True
    return user["expiry"] < today()

def days_left(user) -> int:
    if not user["expiry"]:
        return 0
    try:
        d = (date.fromisoformat(user["expiry"]) - site_now().date()).days
        return max(d, 0)
    except ValueError:
        return 0

def valid_paid_user(user) -> bool:
    return user is not None and user["status"] == "approved" and not is_expired(user)

STYLE = """
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f1f5f9;color:#1e293b;
      display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px}
 .card{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.08);padding:36px;width:380px;max-width:100%}
 h1{font-size:20px;margin-bottom:6px}
 .sub{color:#64748b;font-size:13px;margin-bottom:22px;line-height:1.6}
 label{display:block;font-size:13px;color:#475569;margin:14px 0 6px}
 input{width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
 button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#2563eb;color:#fff;
        font-size:15px;cursor:pointer}
 button:hover{background:#1d4ed8}
 .msg{margin-top:14px;padding:10px;border-radius:8px;font-size:13px;display:none}
 .err{background:#fee2e2;color:#b91c1c}.ok{background:#dcfce7;color:#15803d}
 .tabs{display:flex;gap:8px;margin-bottom:8px}
 .tabs button{margin:0;padding:8px;background:#e2e8f0;color:#334155;font-size:13px;border-radius:6px}
 .tabs button.on{background:#2563eb;color:#fff}
 .tip{margin-top:16px;font-size:12px;color:#94a3b8;line-height:1.7}
 .notice{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;
         padding:12px 14px;font-size:13px;line-height:1.7;margin-bottom:18px;white-space:pre-wrap;word-break:break-word}
 .notice:empty{display:none}
</style>"""

LOGIN_PAGE_HEAD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · Miandning AI</title>""" + STYLE + """
</head><body><div class="card">
<h1>Miandning AI</h1>
<div class="notice" id="notice" style="display:none"></div>
<div class="sub">注册后需管理员审核通过方可使用。<br>按钱包余额计费（充值后即可使用）。聊天记录自动云端同步，换设备登录即恢复。</div>"""

LOGIN_PAGE_TAIL = """
<div id="msg" class="msg"></div>
<div class="tip">提示：注册提交后进入人工审核，审核通过并用注册的账号登录即可使用；会员到期后账号会立即被请出网站，联系管理员续费即可恢复。</div>
</div>
<script>
(async()=>{try{var r=await fetch('/api/announcement');var d=await r.json();
 if(d.ok&&d.text&&d.text.trim()){var n=document.getElementById('notice');
 n.textContent='📢 公告\\n'+d.text;n.style.display='block';}}catch(e){}
 if(location.search.indexOf('expired=1')>-1){show('会员已过期，请联系管理员续费后重新登录','err');}})();
function sw(i){document.getElementById('pane-login').style.display=i?'none':'block';
 document.getElementById('pane-reg').style.display=i?'block':'none';
 document.getElementById('tab-login').classList.toggle('on',!i);
 document.getElementById('tab-reg').classList.toggle('on',!!i);
 hide();}
function show(t,c){var m=document.getElementById('msg');m.className='msg '+c;m.textContent=t;m.style.display='block';}
function hide(){var m=document.getElementById('msg');m.style.display='none';}
async function doLogin(e){e.preventDefault();
 var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({u:document.getElementById('li-user').value.trim(),p:document.getElementById('li-pw').value})});
 var d=await r.json();
 if(d.ok){location.href='/';}else{show(d.msg,'err');}return false;}
async function doReg(e){e.preventDefault();
 var r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({u:document.getElementById('rg-user').value.trim(),p:document.getElementById('rg-pw').value})});
 var d=await r.json();
 show(d.msg,d.ok?'ok':'err');if(d.ok){sw(0);setTimeout(function(){show(d.msg,'ok')},50);}return false;}
</script></body></html>"""

LOGIN_FORMS = """
<div class="tabs">
  <button id="tab-login" class="on" onclick="sw(0)">登录</button>
  <button id="tab-reg" onclick="sw(1)">注册账号</button>
</div>
<div id="pane-login">
  <form onsubmit="return doLogin(event)">
    <label>用户名</label><input id="li-user" autocomplete="username">
    <label>密码</label><input id="li-pw" type="password" autocomplete="current-password">
    <button>登 录</button>
  </form>
</div>
<div id="pane-reg" style="display:none">
  <form onsubmit="return doReg(event)">
    <label>用户名（2-24位，字母数字下划线中文）</label><input id="rg-user" autocomplete="username">
    <label>密码（至少6位）</label><input id="rg-pw" type="password" autocomplete="new-password">
    <button>提交注册申请</button>
  </form>
</div>"""

STATUS_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>账号状态</title>""" + STYLE + """
</head><body><div class="card">
<h1>账号状态：{{status_text}}</h1>
<div class="sub">{{detail}}</div>
<form onsubmit="return renew(event)" id="rf" style="{{renew_display}}">
  <button id="rbtn">申请续期</button>
</form>
<form action="/api/logout" method="get"><button style="background:#64748b;margin-top:10px">退出登录</button></form>
<div id="msg" class="msg"></div>
<div class="tip">审核/续期由管理员人工处理，通过后重新登录或刷新本页即可进入。</div>
</div>
<script>
async function renew(e){e.preventDefault();
 var r=await fetch('/api/request-renewal',{method:'POST'});var d=await r.json();
 var m=document.getElementById('msg');m.className='msg '+(d.ok?'ok':'err');m.textContent=d.msg;m.style.display='block';
 if(d.ok){document.getElementById('rf').style.display='none';}return false;}
</script></body></html>"""

# ---------- 管理面板（KPI + 锚点导航 + 表格化响应式布局） ----------
ADMIN_PAGE_HEAD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>管理面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f7f9;color:#0f172a;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:22px 16px 60px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
h1{font-size:19px;font-weight:700}
.sub{color:#64748b;font-size:12px;margin:2px 0 12px}
.btn{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 16px;border:1px solid transparent;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;background:#4f46e5;color:#fff;white-space:nowrap;transition:background .15s,border-color .15s}
.btn:hover{background:#4338ca}
.btn.ghost{background:#fff;border-color:#d9dde3;color:#374151}
.btn.ghost:hover{background:#f5f6f8}
.btn.danger{background:#fff;border-color:#f1c7c7;color:#d92d20}
.btn.danger:hover{background:#fef3f2}
.btn.sm{height:30px;padding:0 11px;font-size:12px;border-radius:7px}
.btn:focus-visible{outline:2px solid #c7d2fe;outline-offset:1px}
.num{width:64px;height:30px;padding:0 8px;border:1px solid #d9dde3;border-radius:7px;font-size:13px;color:#0f172a;text-align:right;font-variant-numeric:tabular-nums}
.num:focus{outline:none;border-color:#4f46e5;box-shadow:0 0 0 3px #eef2ff}
.num.note{width:88px;text-align:left}
.nav{display:flex;gap:4px;position:sticky;top:0;z-index:20;background:rgba(246,247,249,.92);backdrop-filter:blur(8px);padding:8px 0;margin-bottom:12px;overflow-x:auto}
.nav a{padding:6px 12px;border-radius:999px;font-size:13px;color:#475569;text-decoration:none;white-space:nowrap}
.nav a:hover{background:#eceef2;color:#0f172a}
.sec{background:#fff;border:1px solid #e7eaee;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.sec-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.sec h2{font-size:15px;font-weight:700}
.hint{color:#94a3b8;font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.kpi{background:#fafbfc;border:1px solid #eef0f3;border-radius:10px;padding:10px 14px}
.kpi .l{font-size:12px;color:#64748b;margin-bottom:2px}
.kpi .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:12px;font-weight:500;color:#64748b;text-align:left;padding:8px 10px;border-bottom:1px solid #eef0f3;white-space:nowrap}
td{padding:10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fafbfc}
tr.hot td{background:#fffbeb}
.ta-r{text-align:right}
.money{font-variant-numeric:tabular-nums;font-weight:600}
.money.pos{color:#067647}
.num2{font-variant-numeric:tabular-nums;color:#334155;white-space:nowrap}
.dim{color:#94a3b8}
.uname{font-weight:600}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;white-space:nowrap}
.tag.pending{background:#fef3c7;color:#92400e}
.tag.approved{background:#e7f6ef;color:#067647}
.tag.rejected{background:#fdecec;color:#b42318}
.tag.disabled{background:#eceef2;color:#475569}
.badge{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;white-space:nowrap}
.b-green{background:#e7f6ef;color:#067647}.b-blue{background:#eaf1fe;color:#175cd3}.b-amber{background:#fef4e6;color:#b54708}.b-gray{background:#eceef2;color:#475569}
.ops{display:inline-flex;flex-wrap:wrap;gap:6px;align-items:center}
form.inline{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
.ratios{display:flex;flex-direction:column;gap:2px;margin-bottom:10px}
.ratios form{padding:9px 0;border-bottom:1px dashed #e2e8f0}
select{height:30px;padding:0 8px;border:1px solid #d9dde3;border-radius:7px;font-size:13px;color:#0f172a;background:#fff}
input[type=file]{font-size:13px;color:#475569;max-width:260px}
.unit{color:#94a3b8;font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:6px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;background:#f8fafc;border:1px solid #e2e6ec;border-radius:8px;padding:5px 9px;cursor:pointer;transition:border-color .15s,color .15s}
.chip:hover{border-color:#4f46e5;color:#4f46e5}
.chip i{font-style:normal;color:#64748b}
.flash{border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:10px;word-break:break-all}
.flash.ok{background:#e7f6ef;color:#067647}
.flash.err{background:#fdecec;color:#b42318}
.strip{background:#fafbfc;border:1px solid #eef0f3;border-radius:10px;padding:8px 14px;font-size:12px;color:#64748b;margin-bottom:12px}
textarea{width:100%;min-height:96px;padding:10px 12px;border:1px solid #d9dde3;border-radius:10px;font-size:14px;font-family:inherit;resize:vertical;line-height:1.6}
textarea:focus{outline:none;border-color:#4f46e5;box-shadow:0 0 0 3px #eef2ff}
.frow{display:flex;justify-content:flex-end;margin-top:10px}
.ok-msg{display:none}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#0f172a;color:#fff;font-size:13px;border-radius:8px;padding:8px 16px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:50}
.toast.on{opacity:.92}
@media (max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}.kpi .v{font-size:17px}.wrap{padding:14px 10px 40px}.sec{padding:14px 12px}}
</style></head><body><div class="wrap">
<div class="bar"><h1>管理面板</h1>
<form action="/admin/logout" method="get"><button class="btn ghost sm">退出</button></form></div>
<div class="sub">待审核与续期申请自动置顶。通过=开通会员并设有效期；续期=在当前有效期基础上追加天数；余额调整实时生效。</div>
<nav class="nav">
<a href="#sec-announce">公告</a><a href="#sec-users">用户</a><a href="#sec-wallet">计费</a><a href="#sec-codes">充值码</a><a href="#sec-ratios">定价</a><a href="#sec-pay">充值审批{{paybadge}}</a><a href="#sec-tx">流水</a>
</nav>

<section class="sec" id="sec-announce">
  <div class="sec-h"><h2>登录页公告</h2><span class="hint">展示在登录页顶部，留空不显示；换行会保留</span></div>
  <form method="post" action="/admin/announce">
    <textarea name="text" placeholder="写给所有访客的公告，例如维护通知、新功能说明…">{{announcement}}</textarea>
    <div class="frow"><button class="btn">保存公告</button></div>
  </form>
  <div class="ok-msg" id="anmsg">公告已保存</div>
</section>
"""

ADMIN_PAGE_FOOT = """</div>
<div class="toast" id="toast"></div>
<script>
document.querySelectorAll('[data-confirm]').forEach(function(b){
  b.addEventListener('click', function(e){
    if(!confirm(b.getAttribute('data-confirm'))) e.preventDefault();
  });
});
function cpCode(el){
  var t = el.getAttribute('data-code') || '';
  if(navigator.clipboard) navigator.clipboard.writeText(t);
  var o = document.getElementById('toast');
  o.textContent = '已复制 ' + t;
  o.classList.add('on');
  setTimeout(function(){ o.classList.remove('on'); }, 1600);
}
if(location.hash === '#announce-saved'){
  var m = document.getElementById('anmsg');
  if(m){ m.className = 'flash ok'; m.style.display = 'block'; }
}
document.querySelectorAll('.nav a').forEach(function(a){
  a.addEventListener('click', function(e){
    e.preventDefault();
    var t = document.querySelector(a.getAttribute('href'));
    if(t) window.scrollTo({top: t.getBoundingClientRect().top + window.pageYOffset - 52, behavior: 'smooth'});
  });
});
</script></body></html>"""

STATUS_TEXT = {
    "pending": "待审核",
    "approved": "已开通",
    "rejected": "已拒绝",
    "disabled": "已禁用",
}

app = FastAPI(title="Paid Chat Gate")


# ── 审查 R8/R9：请求体上限「读之前拦」而非「读完再判」 ──
# 原先 5 处都是 body = await request.body() 之后才 len(body) > LIMIT，上限只约束
# 合法用户，约束不了故意发超大 body（单请求即可顶满内存）。这里在中间件层按
# Content-Length 预检；chunked（无 CL）由反代 client_max_body_size 兜底 + 路由层判断。
BODY_LIMIT_DEFAULT = 2 * 1024 * 1024
_BODY_LIMIT_RULES = (
    ("/api/upload", "UPLOAD_MAX_BYTES"),
    ("/api/backup", "BACKUP_MAX_BYTES"),
    ("/anthropic/", "GW_BODY_MAX_BYTES"),
)


@app.middleware("http")
async def _body_limit_middleware(request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit():
            limit = BODY_LIMIT_DEFAULT
            path = request.url.path
            for _pref, _name in _BODY_LIMIT_RULES:
                if path.startswith(_pref):
                    limit = globals().get(_name) or BODY_LIMIT_DEFAULT   # 常量在下方定义，延迟取值
                    break
            if int(cl) > limit:
                return JSONResponse(
                    {"ok": False, "msg": "请求体过大（上限 %d MB）" % (limit >> 20)},
                    status_code=413,
                )
    return await call_next(request)


# ── 审查 R8/R9：导出/恢复是「全量会话+图片在内存拼 zip」，单账号上限 200MB，
# 线程池下并发导出峰值可达数 GB。用小信号量把并发压到 2，超限快速失败。
_EXPORT_SEM = threading.BoundedSemaphore(2)


def _export_busy() -> JSONResponse:
    return JSONResponse({"ok": False, "msg": "导出任务较多，请稍后重试"}, status_code=503)


init_db()

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # 已登录且有效的用户直接进入对话页，不再看登录框
    user = get_user(request)
    if valid_paid_user(user):
        return RedirectResponse("/", status_code=303)
    html = LOGIN_PAGE_HEAD + LOGIN_FORMS + LOGIN_PAGE_TAIL
    return HTMLResponse(html)

@app.get("/api/announcement")
def api_announcement():
    return JSONResponse({"ok": True, "text": get_setting("announcement", "")})

# 登录/注册限流：{ip: [窗口起始, 次数]}（进程内；多 worker 部署需换共享存储）
_AUTH_ATTEMPTS = {}


def _prune_attempts(table: dict, ttl: int = 60, cap: int = 4096, ts_idx: int = 0) -> None:
    """审查 R11（P2）：限流表只增不减——IPv6 一个 /64 段就能造出天文数字的键，
    长期运行内存无上限增长。超过 cap 时按时间戳清掉过窗条目（摊还成本 O(n)/cap 次）。
    只影响限流精度（过窗记录本就该失效），不影响安全判定。"""
    if len(table) <= cap:
        return
    cutoff = time.time() - ttl
    for k in [k for k, v in table.items() if not v or (v[ts_idx] or 0) < cutoff]:
        table.pop(k, None)
    # 过窗清理压不回上限（60 秒内被灌满，所有条目都还"新鲜"）→ 按插入序丢最老的。
    # 安全代价可忽略：限流本就按 IP/uid 独立计数，丢掉最老条目最多让其多试一个窗口；
    # 要利用它先得制造 4096 个不同源地址，那时换地址本身就能绕过限流。
    if len(table) > cap:
        for k in list(table)[:len(table) - cap]:
            table.pop(k, None)


def _auth_rate_limited(ip: str, limit: int) -> bool:
    now = time.time()
    _prune_attempts(_AUTH_ATTEMPTS, ttl=60, cap=4096, ts_idx=0)
    win = _AUTH_ATTEMPTS.get(ip)
    if win is None or now - win[0] >= 60:
        _AUTH_ATTEMPTS[ip] = [now, 1]
        return False
    win[1] += 1
    return win[1] > limit


@app.post("/api/register")
async def api_register(request: Request):
    if _auth_rate_limited(request.client.host if request.client else "?", 10):
        return JSONResponse({"ok": False, "msg": "注册尝试过于频繁，请一分钟后再试"}, status_code=429)
    # 审查 R11（P2）：非法 JSON / 非对象 body 会让 d.get 抛异常 → 500（实测 6/6 全 500）。
    # 客户端错误统一 400，且不写入任何状态。
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "msg": "请求格式错误"}, status_code=400)
    if not isinstance(d, dict):
        return JSONResponse({"ok": False, "msg": "请求格式错误"}, status_code=400)
    u = str(d.get("u", "")).strip()
    p = str(d.get("p", ""))
    if not re.fullmatch(r"[\w\u4e00-\u9fa5]{2,24}", u):
        return JSONResponse({"ok": False, "msg": "用户名需2-24位，仅限字母/数字/下划线/中文"})
    if len(p) < 6:
        return JSONResponse({"ok": False, "msg": "密码至少6位"})
    with db() as c:
        try:
            c.execute(
                "INSERT INTO users(username,pw_hash,created_at) VALUES(?,?,?)",
                (u, hash_pw(p), site_now().isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            return JSONResponse({"ok": False, "msg": "用户名已存在"})
    return JSONResponse({"ok": True, "msg": "注册成功！已提交管理员审核，请等待通过后登录。"})

@app.post("/api/login")
async def api_login(request: Request):
    if _auth_rate_limited(request.client.host if request.client else "?", 20):
        return JSONResponse({"ok": False, "msg": "登录尝试过于频繁，请一分钟后再试"}, status_code=429)
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "msg": "请求格式错误"}, status_code=400)
    if not isinstance(d, dict):
        return JSONResponse({"ok": False, "msg": "请求格式错误"}, status_code=400)
    u = str(d.get("u", "")).strip()
    p = str(d.get("p", ""))
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    if row is None or not check_pw(p, row["pw_hash"]):
        return JSONResponse({"ok": False, "msg": "用户名或密码错误"})
    if row["status"] == "pending":
        return JSONResponse({"ok": False, "msg": "账号正在等待管理员审核，请稍后再试"})
    if row["status"] == "rejected":
        return JSONResponse({"ok": False, "msg": "注册申请未通过" + (f"（备注：{row['note']}）" if row["note"] else "")})
    if row["status"] == "disabled":
        return JSONResponse({"ok": False, "msg": "账号已被禁用，请联系管理员"})
    if row["status"] == "approved" and is_expired(row):
        # 过期账号拒绝登录并清除残留登录态，防止"踢出→登录→又踢出"死循环
        resp = JSONResponse({
            "ok": False,
            "msg": f"会员已于 {row['expiry']} 过期，请联系管理员续费后重新登录",
        })
        resp.delete_cookie("session", path="/")
        return resp
    resp = JSONResponse({"ok": True, "msg": "", "redirect": "/"})
    set_cookie(resp, "session", sign({"uid": row["id"], "exp": time.time() + SESSION_DAYS * 86400}), SESSION_DAYS, request=request)
    return resp

@app.get("/api/logout")
def api_logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session", path="/")
    return resp

@app.get("/api/me")
def api_me(request: Request):
    user = get_user(request)
    if user is None:
        return JSONResponse({"ok": False})
    return JSONResponse({
        "ok": True, "username": user["username"], "status": user["status"],
        "expiry": user["expiry"], "expired": is_expired(user), "days_left": days_left(user),
        # 指定账号隐藏"会员剩余天数"角标（如宣传截图专用账号 admin）
        "hide_membership": user["username"] in HIDE_MEMBERSHIP_USERS,
        # 网关计费开启时，前端据此免 API Key 并显示钱包入口
        "gateway_enabled": gw_enabled(),
    })

@app.post("/api/request-renewal")
async def api_request_renewal(request: Request):
    user = get_user(request)
    if user is None:
        return JSONResponse({"ok": False, "msg": "请先登录"})
    if user["renewal_requested"]:
        return JSONResponse({"ok": False, "msg": "已提交过续期申请，请等待管理员处理"})
    with db() as c:
        c.execute("UPDATE users SET renewal_requested=1 WHERE id=?", (user["id"],))
    return JSONResponse({"ok": True, "msg": "续期申请已提交，请等待管理员处理"})

# ---------- 聊天记录云端同步（按会话合并 + 墓碑删除，防多设备互相覆盖） ----------
SESS_DIR = "chat_sessions/"
_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")   # P1-12：会话 id 白名单（防 Zip Slip/路径逃逸）
# P1-12（审查 R2）：归档内图片文件名白名单——不含分隔符/控制字符，不以点开头（防 ".."）
_SAFE_FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")


def _safe_sid(sid) -> str | None:
    """会话 id 只允许字母数字下划线连字符，拒绝 ../、分隔符等（P1-12）"""
    s = str(sid or "")
    return s if _SID_RE.match(s) else None


def _valid_img_entry(name: str) -> tuple[str, str] | None:
    """归档图片条目校验（审查 R2/P1-12）：必须形如 chat_sessions/<合法sid>_files/<白名单文件名>，
    通过则返回 (sid, 全名)，否则 None。文件名与 sid 分别白名单，杜绝嵌套路径/穿越。"""
    if not name.startswith(SESS_DIR) or "_files/" not in name:
        return None
    sid_part, fname = name[len(SESS_DIR):].split("_files/", 1)
    sid = _safe_sid(sid_part)
    if sid is None or not _SAFE_FNAME_RE.match(fname):
        return None
    return sid, name
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "chat_session_index.json"
ARCHIVE_VERSION = 3


class _QuotaExceeded(Exception):
    pass


def _ensure_cloud_sessions(c):
    c.execute(
        """CREATE TABLE IF NOT EXISTS cloud_sessions(
            uid INTEGER NOT NULL,
            sid TEXT NOT NULL,
            session_json BLOB,
            images BLOB,
            updated_at TEXT DEFAULT '',
            deleted INTEGER DEFAULT 0,
            rev INTEGER DEFAULT 0,
            PRIMARY KEY(uid, sid)
        )"""
    )
    # P1-15（审查 R2）：服务端单调递增 rev 作为版本权威，客户端 updated_at 仅作展示
    _cs_cols = [r[1] for r in c.execute("PRAGMA table_info(cloud_sessions)").fetchall()]
    if "rev" not in _cs_cols:
        _add_column(c, "cloud_sessions", "rev", "INTEGER DEFAULT 0")


def _split_archive(data: bytes):
    """旧版整包归档拆成 {sid: [session_json, updated_at]}（一次性迁移用）。"""
    out = {}
    zf = zipfile.ZipFile(io.BytesIO(data))
    for name in zf.namelist():
        if name.startswith(SESS_DIR) and name.endswith(".json"):
            rest = name[len(SESS_DIR):]
            if "/" in rest or "\\" in rest:
                continue
            # P1-12（审查 R2）：文件名 sid 与 JSON info["id"] 必须一致且均过白名单，
            # 杜绝「文件名合法但内嵌 id 逃逸」的不一致构造
            f_sid = _safe_sid(rest[:-5])
            try:
                info = json.loads(zf.read(name))
            except Exception:
                continue
            j_sid = _safe_sid(info.get("id")) if isinstance(info, dict) else None
            if not f_sid or j_sid != f_sid:
                continue  # 非法/不一致会话 id，拒绝入归档
            out[f_sid] = [zf.read(name), str(info.get("updated_at") or "")]
    return out


def _migrate_legacy(c, uid):
    """旧版单包数据 → 按会话表，迁移成功后才清空旧 blob。

    数据保护：拆包失败或拆不出任何会话时，绝不清空旧 blob（保留现场以待排查/恢复），
    只在确实迁入 ≥1 条会话后才置空。
    """
    row = c.execute("SELECT data FROM cloud_backups WHERE uid=?", (uid,)).fetchone()
    if row is None or not row["data"]:
        return
    cnt = c.execute("SELECT COUNT(*) FROM cloud_sessions WHERE uid=?", (uid,)).fetchone()[0]
    if cnt:
        return
    try:
        parts = _split_archive(row["data"])
    except Exception:
        parts = {}
    if not parts:
        return  # 解析不出会话：保留旧 blob，不清空
    for sid, (blob, upd) in parts.items():
        c.execute(
            "INSERT OR IGNORE INTO cloud_sessions(uid,sid,session_json,images,updated_at,deleted) "
            "VALUES(?,?,?,?,?,0)",
            (uid, sid, blob, None, upd),
        )
    c.execute("UPDATE cloud_backups SET data=NULL WHERE uid=?", (uid,))


def _imgs_zip(entries):
    if not entries:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as iz:
        for n, b in entries:
            iz.writestr(n, b)
    return buf.getvalue()


def _build_archive(c, uid) -> bytes:
    rows = c.execute(
        "SELECT sid, session_json, images FROM cloud_sessions WHERE uid=? AND deleted=0 ORDER BY sid",
        (uid,),
    ).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST_NAME, json.dumps({"export_format_version": ARCHIVE_VERSION}))
        index = []
        for r in rows:
            _sid = _safe_sid(r["sid"])
            if not _sid:
                continue  # P1-12：非法会话 id 不得进入导出归档
            z.writestr(SESS_DIR + _sid + ".json", r["session_json"])
            try:
                info = json.loads(r["session_json"])
            except Exception:
                info = {}
            index.append({
                "id": _sid,
                "title": info.get("title"),
                "pinned": False,
                "message_count": len(info.get("messages") or []),
                "inserted_at": info.get("inserted_at") or "",
                "updated_at": info.get("updated_at") or "",
            })
            if r["images"]:
                try:
                    iz = zipfile.ZipFile(io.BytesIO(r["images"]))
                    for n in iz.namelist():
                        # P1-12（审查 R2）：导出同样只放行双重白名单的图片条目（防历史脏数据逃逸）
                        if _valid_img_entry(n):
                            z.writestr(n, iz.read(n))
                except Exception:
                    pass
        z.writestr(INDEX_NAME, json.dumps(index))
    return buf.getvalue()


@app.get("/api/backup/meta")
def api_backup_meta(request: Request):
    user = get_user(request)
    # 审查 R11（P2）：与 /api/backup、/api/restore 的权限保持一致——过期/未审核账号
    # 不该看到云端备份的更新时间（原实现只判 user is not None）。
    if user is None or not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    with db() as c:
        _ensure_cloud_sessions(c)
        _migrate_legacy(c, user["id"])
        row = c.execute(
            "SELECT updated_at FROM cloud_backups WHERE uid=?", (user["id"],)
        ).fetchone()
    return JSONResponse({
        "ok": True,
        "exists": row is not None,
        "updated_at": row["updated_at"] if row else 0,
    })


@app.get("/api/backup")
def api_backup_get(request: Request):
    user = get_user(request)
    if user is None or not valid_paid_user(user):
        return Response(status_code=403)
    if not _EXPORT_SEM.acquire(blocking=False):
        return _export_busy()
    try:
        return _backup_get_locked(user)
    finally:
        _EXPORT_SEM.release()


def _backup_get_locked(user) -> Response:
    with db() as c:
        _ensure_cloud_sessions(c)
        _migrate_legacy(c, user["id"])
        row = c.execute(
            "SELECT updated_at FROM cloud_backups WHERE uid=?", (user["id"],)
        ).fetchone()
        if row is None:
            return Response(status_code=404)
        data = _build_archive(c, user["id"])
        # R5（审查 R3）：回传各会话最新 rev，供新版前端拉取后重建 base_revs 缓存
        revs = {
            r["sid"]: (r["rev"] or 0)
            for r in c.execute(
                "SELECT sid, rev FROM cloud_sessions WHERE uid=? AND deleted=0", (user["id"],)
            ).fetchall()
        }
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=cloud-backup.zip",
            "X-Cloud-Revs": json.dumps(revs, separators=(",", ":")),
        },
    )


def _parse_ts(value: str):
    """解析 ISO8601 时间戳；解析失败返回 None（退回字符串比较）。"""
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _cloud_newer(cloud_ts: str, local_ts: str) -> bool:
    """云端会话是否比本地新。用解析后的时间比较，避免字符串比较的坑：
    秒精度 "...16Z" 与毫秒精度 "...16.000Z" 逐字符比较时 'Z' > '.'，
    会让云端永远判"更新"，客户端每次推送都被要求整包拉取（推送→拉取死循环）。"""
    a, b = _parse_ts(cloud_ts), _parse_ts(local_ts)
    if a is not None and b is not None:
        return a > b
    return cloud_ts > local_ts


@app.post("/api/backup")
async def api_backup_post(request: Request):
    """接收聊天备份上传。

    兼容两种请求形态：
    - 原始二进制流（新版前端，application/octet-stream，抗代理改写，删除名单在 ?deleted= 参数）；
    - multipart 表单（旧版前端 file 字段 + deleted 字段）。
    """
    user = get_user(request)
    if user is None or not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        deleted = str(form.get("deleted") or "")
        if upload is None or isinstance(upload, str):
            return JSONResponse({"ok": False, "msg": "缺少文件"}, status_code=400)
        data = await upload.read()
    else:
        data = await request.body()
        deleted = request.query_params.get("deleted") or ""
    if len(data) > BACKUP_MAX_BYTES:
        return JSONResponse({"ok": False, "msg": "备份过大"}, status_code=413)
    if data[:2] != b"PK":  # ZIP 魔数校验
        return JSONResponse({"ok": False, "msg": "格式错误"}, status_code=400)
    try:
        del_ids = {str(x) for x in json.loads(deleted)} if deleted else set()
    except Exception:
        del_ids = set()
    now_ms = int(time.time() * 1000)
    # P1-15（审查 R2）：客户端可携带 base_revs（每会话上次同步到的服务端 rev），
    # 服务端以 rev 为版本权威检测并发冲突；旧客户端未携带时退回 updated_at 比较。
    try:
        _br_raw = request.query_params.get("base_revs") or request.headers.get("x-base-revs") or ""
        base_revs = {str(k): int(v) for k, v in json.loads(_br_raw).items()} if _br_raw else {}
    except Exception:
        base_revs = {}
    # 审查 R11（P1）：解压 + 合并 + 写库 + 落盘全是同步阻塞 IO，原实现在事件循环里执行；
    # 实测 60MB 备份上传期间并发请求延迟 1766ms（正常 3ms），单 worker 下等于全站卡死，
    # 200MB 上传可达 6s+。整段搬进线程池（FastAPI 默认 40 线程），不再占用事件循环。
    return await run_in_threadpool(_backup_merge, user, data, del_ids, base_revs, now_ms)


def _backup_merge(user, data, del_ids, base_revs, now_ms) -> JSONResponse:
    """同步执行备份合并 + 快照落盘（只在线程池中调用，禁止在事件循环直接调用）。"""
    try:
        with db() as c:
            _ensure_cloud_sessions(c)
            _migrate_legacy(c, user["id"])
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except Exception:
                return JSONResponse({"ok": False, "msg": "无法解析备份"}, status_code=400)
            # P1-11：解压资源限制——条目数/单文件/总解压大小/压缩比（ZIP bomb 防护）
            infos = zf.infolist()
            if len(infos) > ZIP_MAX_ENTRIES:
                return JSONResponse({"ok": False, "msg": "归档条目过多"}, status_code=413)
            _total_unc = 0
            for zi in infos:
                if zi.file_size > ZIP_MAX_ENTRY_BYTES:
                    return JSONResponse({"ok": False, "msg": "归档内单文件过大"}, status_code=413)
                _total_unc += zi.file_size
                if _total_unc > ZIP_MAX_TOTAL_UNC:
                    return JSONResponse({"ok": False, "msg": "归档解压总量超限"}, status_code=413)
                if zi.file_size > 0 and zi.compress_size > 0:
                    if zi.compress_size / zi.file_size < 1 / ZIP_MAX_RATIO and zi.file_size > 10 * 1024 * 1024:
                        return JSONResponse({"ok": False, "msg": "归档压缩比异常（疑似 ZIP bomb）"}, status_code=413)
            names = zf.namelist()
            if MANIFEST_NAME not in names:
                return JSONResponse({"ok": False, "msg": "缺少归档清单"}, status_code=400)
            # 解析传入的会话与所属图片
            incoming = {}
            for name in names:
                if name.startswith(SESS_DIR) and name.endswith(".json"):
                    rest = name[len(SESS_DIR):]
                    if "/" in rest or "\\" in rest:
                        continue
                    # P1-12（审查 R2）：文件名 sid 与 JSON info["id"] 三处一致
                    # （归档文件名 == JSON id == 云端主键），任一非法/不一致直接拒绝
                    f_sid = _safe_sid(rest[:-5])
                    try:
                        info = json.loads(zf.read(name))
                    except Exception:
                        continue
                    j_sid = _safe_sid(info.get("id")) if isinstance(info, dict) else None
                    if not f_sid or j_sid != f_sid:
                        continue
                    incoming[f_sid] = {
                        "json": zf.read(name),
                        "upd": str(info.get("updated_at") or ""),
                        "imgs": [],
                    }
            for name in names:
                if not name.startswith(SESS_DIR) or "_files/" not in name:
                    continue
                # P1-12（审查 R2）：图片条目双重白名单（sid + 文件名），只落入本会话 _files/
                v = _valid_img_entry(name)
                if v and v[0] in incoming:
                    incoming[v[0]]["imgs"].append((name, zf.read(name)))
            # 墓碑：客户端明确删除的会话，云端标记删除（保留 blob 以防万一）。
            # 只对云端真实存在且未删除的会话生效——从未上传过的 sid 一律忽略，
            # 防止客户端误判（如导入替换期间的误报）在云端制造垃圾墓碑并回传扩散。
            for sid in del_ids:
                if not _safe_sid(sid):
                    continue  # P1-12：非法 sid 不参与墓碑
                live = c.execute(
                    "SELECT 1 FROM cloud_sessions WHERE uid=? AND sid=? AND deleted=0",
                    (user["id"], sid),
                ).fetchone()
                if live is not None:
                    c.execute(
                        "UPDATE cloud_sessions SET deleted=1 WHERE uid=? AND sid=?",
                        (user["id"], sid),
                    )
            # 按会话合并（P1-15）：服务端 rev 为版本权威；冲突时保留云端并回传 conflicts
            changed = False
            conflicts = []
            for sid, item in incoming.items():
                row = c.execute(
                    "SELECT updated_at, deleted, rev, session_json, images FROM cloud_sessions WHERE uid=? AND sid=?",
                    (user["id"], sid),
                ).fetchone()
                if row is not None and row["deleted"]:
                    # 云端已删除而本地还留着：响应里已有 tombstones，客户端会自行删除，
                    # 不再把 changed 置 True——否则每次推送都触发整包拉取，打断正在看对话的用户
                    continue
                cur_rev = (row["rev"] or 0) if row is not None else 0
                same = row is not None and row["session_json"] == item["json"]
                if same:
                    continue  # 内容未变：不动 rev，避免每次上传都虚增版本
                if row is None:
                    # 审查 R11（P1-b）：新建分支防并发——两个设备同时首次推送同一 sid，
                    # 普通 INSERT 会互相覆盖（后到者把先到者的内容抹掉）。
                    # ON CONFLICT DO NOTHING + rowcount 判定，撞上即按冲突处理。
                    ins = c.execute(
                        "INSERT INTO cloud_sessions(uid,sid,session_json,images,updated_at,deleted,rev) "
                        "VALUES(?,?,?,?,?,0,1) ON CONFLICT(uid,sid) DO NOTHING",
                        (user["id"], sid, item["json"], _imgs_zip(item["imgs"]), item["upd"]),
                    )
                    if ins.rowcount != 1:
                        conflicts.append(sid)
                        changed = True
                elif sid in base_revs:
                    # 新版客户端：rev 权威判定。base_rev 落后于云端 = 双设备并发编辑 → 冲突，
                    # 保留云端版本，回传 conflicts 让客户端拉取合并（绝不以客户端时间戳新者胜）
                    if base_revs[sid] != cur_rev:
                        conflicts.append(sid)
                        changed = True
                        continue
                    # 审查 R11（P1-a）：本次没带图片条目 ≠ 用户要删图片（新设备首推、图片
                    # 缓存被清、blob 读取失败都会 imgs=[]），无条件写 None 会把云端已存
                    # 图片永久抹掉。此处保留云端现值；真正删图需要显式协议，不用"没上传"当删除语义。
                    _imgs_blob = _imgs_zip(item["imgs"])
                    if _imgs_blob is None:
                        _imgs_blob = row["images"] if row is not None else None
                    # 审查 R11（P1-b）：读 rev 与写 rev 之间无原子性，两设备并发都能通过
                    # base_revs 校验 → 后写静默覆盖且双方 conflicts=[]（对话丢失）。
                    # 改原子 CAS：WHERE 带上 rev=cur_rev，rowcount≠1 即判定冲突。
                    upd = c.execute(
                        "UPDATE cloud_sessions SET session_json=?, images=?, updated_at=?, rev=? "
                        "WHERE uid=? AND sid=? AND deleted=0 AND rev=?",
                        (item["json"], _imgs_blob, item["upd"], cur_rev + 1, user["id"], sid, cur_rev),
                    )
                    if upd.rowcount != 1:
                        conflicts.append(sid)
                        changed = True
                elif _cloud_newer(row["updated_at"] or "", item["upd"]):
                    changed = True  # 云端这条更新，客户端待会儿拉取
                else:
                    # 旧客户端（无 base_rev）：保留 updated_at 兼容路径，服务端 rev 单调递增
                    _imgs_blob = _imgs_zip(item["imgs"])
                    if _imgs_blob is None:
                        _imgs_blob = row["images"] if row is not None else None
                    c.execute(
                        "UPDATE cloud_sessions SET session_json=?, images=?, updated_at=?, rev=? "
                        "WHERE uid=? AND sid=?",
                        (item["json"], _imgs_blob, item["upd"], cur_rev + 1, user["id"], sid),
                    )
            # 云端存在但本次上传没有的会话 → 客户端缺数据，需要拉取
            extra = c.execute(
                "SELECT COUNT(*) FROM cloud_sessions WHERE uid=? AND deleted=0",
                (user["id"],),
            ).fetchone()[0] - len(incoming)
            if extra > 0:
                changed = True
            # 用量限额（超出则整体回滚）
            total = c.execute(
                "SELECT COALESCE(SUM(COALESCE(LENGTH(session_json),0)+COALESCE(LENGTH(images),0)),0) "
                "FROM cloud_sessions WHERE uid=? AND deleted=0",
                (user["id"],),
            ).fetchone()[0]
            if total > BACKUP_MAX_BYTES:
                raise _QuotaExceeded
            c.execute(
                "INSERT INTO cloud_backups(uid,data,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET updated_at=excluded.updated_at",
                (user["id"], None, now_ms),
            )
            tombs = [
                r["sid"]
                for r in c.execute(
                    "SELECT sid FROM cloud_sessions WHERE uid=? AND deleted=1", (user["id"],)
                ).fetchall()
            ]
            revs = {
                r["sid"]: (r["rev"] or 0)
                for r in c.execute(
                    "SELECT sid, rev FROM cloud_sessions WHERE uid=? AND deleted=0", (user["id"],)
                ).fetchall()
            }
    except _QuotaExceeded:
        return JSONResponse({"ok": False, "msg": "云端空间已满"}, status_code=413)
    # 完整导出包原样存档（审计用；恢复走服务端合并状态，见 /api/restore）
    _save_snapshot(user["id"], data, now_ms)
    return JSONResponse({
        "ok": True, "updated_at": now_ms, "changed": changed, "tombstones": tombs,
        "conflicts": conflicts, "revs": revs,
    })


@app.get("/api/restore")
def api_restore_get(request: Request):
    """恢复云端备份：返回**服务端当前合并状态**的一致快照（审查 R2/P1-15），
    而非最近一次原始上传包——原始上传可能落后于云端其他设备的更新。"""
    user = get_user(request)
    if user is None or not valid_paid_user(user):
        return Response(status_code=403)
    if not _EXPORT_SEM.acquire(blocking=False):
        return _export_busy()
    try:
        with db() as c:
            _ensure_cloud_sessions(c)
            _migrate_legacy(c, user["id"])
            data = _build_archive(c, user["id"])
    finally:
        _EXPORT_SEM.release()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=cloud-backup.zip"},
    )


def _snap_key(n: str):
    """快照文件名排序键：'1700000000001-ab12cd34.zip' → (ms, 名字)。
    兼容审查 R2 前的旧格式 '{now_ms}.zip'（无 '-' 后缀段）；
    异常文件名（非数字前缀）排最前（视为最旧，R6/审查 P2：容错不抛 ValueError）——
    SNAP_KEEP 裁剪时最先删它，_latest_snapshot 也不会选中它。"""
    try:
        return (int(n[:-4].split("-")[0]), n)
    except ValueError:
        # 异常文件名排最前（视为最旧）：SNAP_KEEP 裁剪最先删它，_latest_snapshot 不会选中它
        return (float("-inf"), n)


def _save_snapshot(uid: int, data: bytes, now_ms: int) -> None:
    """把用户上传的完整导出包原样存到磁盘（审计用；恢复走 /api/restore 服务端合并状态）。"""
    try:
        udir = os.path.join(SNAP_DIR, str(uid))
        os.makedirs(udir, exist_ok=True)
        # P1-15（审查 R2）：临时文件名唯一化，并发上传不互相覆盖；写完原子 replace
        tmp = os.path.join(udir, ".tmp-%s" % uuid.uuid4().hex)
        with open(tmp, "wb") as f:
            f.write(data)
        # R5（审查 R3）：最终文件名也带随机后缀——同毫秒并发上传各留一份，
        # 不再互相覆盖（原 '{now_ms}.zip' 同 ms 直接覆盖，8 份只剩 1 份）
        os.replace(tmp, os.path.join(udir, "%d-%s.zip" % (now_ms, uuid.uuid4().hex[:8])))
        snaps = sorted((f for f in os.listdir(udir) if f.endswith(".zip")), key=_snap_key)
        for old in snaps[:-SNAP_KEEP]:
            try:
                os.remove(os.path.join(udir, old))
            except OSError:
                pass
    except OSError as e:
        # P1-15（审查 R2）：快照失败必须留日志告警，不得静默
        print("[backup] ⚠️ 快照存档失败 uid=%s err=%s（云同步主流程不受影响）" % (uid, e))


def _latest_snapshot(uid: int) -> bytes | None:
    """取该用户最新的一份完整导出包。"""
    udir = os.path.join(SNAP_DIR, str(uid))
    try:
        snaps = sorted((f for f in os.listdir(udir) if f.endswith(".zip")), key=_snap_key)
        if not snaps:
            return None
        with open(os.path.join(udir, snaps[-1]), "rb") as f:
            return f.read()
    except OSError:
        return None


def status_page_html(user) -> str:
    # 审查 P1-09：任何数据库文本进 HTML 前必须转义（user['note'] 曾被原样插入，存储型 XSS）
    _e = lambda s: html.escape(str(s or ""))
    st = _e(user["status"]) if user["status"] in ("pending", "rejected", "disabled") else user["status"]
    if st == "pending":
        st_text, detail = "待审核", "你的注册申请已提交，管理员审核通过后即可登录使用。"
        renew = "none"
    elif st == "rejected":
        st_text, detail = "已拒绝", "注册申请未通过。" + (f"管理员备注：{_e(user['note'])}" if user["note"] else "")
        renew = "none"
    elif st == "disabled":
        st_text, detail = "已禁用", "账号已被禁用，请联系管理员。"
        renew = "none"
    else:
        if is_expired(user):
            st_text = "会员已到期"
            detail = f"会员已于 {_e(user['expiry'])} 到期，可申请续期，管理员确认后即可继续使用。"
            renew = "block"
        else:
            st_text = "正常"
            detail = f"会员有效期至 {user['expiry']}（剩 {days_left(user)} 天）。"
            renew = "none"
    html = STATUS_PAGE.replace("{{status_text}}", st_text).replace("{{detail}}", detail)
    return html.replace("{{renew_display}}", renew)

def serve_static(path: str) -> Response:
    full = os.path.normpath(os.path.join(STATIC_DIR, path))
    # 必须带路径分隔符做前缀校验，防止 ../staticxxx/ 形式的前缀混淆逃逸
    if not full.startswith(STATIC_DIR + os.sep) and full != STATIC_DIR:
        full = os.path.join(STATIC_DIR, "index.html")
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    if not os.path.isfile(full):
        full = os.path.join(STATIC_DIR, "index.html")
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    rel = os.path.relpath(full, STATIC_DIR)
    if rel == "index.html" or not rel.startswith("static" + os.sep):
        # no-store：彻底禁止浏览器缓存 HTML，前端升级后用户刷新页面必定拿到新版本
        headers = {"Cache-Control": "no-store, must-revalidate"}
    else:
        headers = {"Cache-Control": "public, max-age=2592000, immutable"}
    return FileResponse(full, media_type=mt, headers=headers)

# ---------- 管理面板 ----------
def is_admin(request: Request) -> bool:
    return (unsign(request.cookies.get("admin")) or {}).get("admin") is True

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not is_admin(request):
        return HTMLResponse(
            ADMIN_PAGE_HEAD.split("<div class=\"bar\">")[0].replace("{{announcement}}", "")
            + '<div class="sec"><h2>管理员登录</h2><form method="post" action="/admin/login">'
            + '<input name="pw" type="password" placeholder="管理密码" style="width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px">'
            + '<button class="btn" style="margin-top:12px;width:100%">进 入</button></form></div></div></body></html>'
        )
    with db() as c:
        rows = c.execute(
            """SELECT * FROM users ORDER BY
               CASE WHEN status='pending' THEN 0
                    WHEN status='approved' AND renewal_requested=1 THEN 1
                    ELSE 2 END,
               created_at DESC"""
        ).fetchall()
        _ensure_cloud_sessions(c)
        sizes = {
            r["uid"]: r["size"]
            for r in c.execute(
                "SELECT uid, COALESCE(SUM(COALESCE(LENGTH(session_json),0)+COALESCE(LENGTH(images),0)),0) AS size "
                "FROM cloud_sessions WHERE deleted=0 GROUP BY uid"
            ).fetchall()
        }
    announcement = get_setting("announcement", "")
    # 待审充值数徽标（导航"充值审批(N)"，网关关闭时不显示）
    paybadge = ""
    if gw_enabled():
        with db() as c:
            npay = c.execute("SELECT COUNT(*) AS n FROM pay_orders WHERE status='pending'").fetchone()["n"]
        if npay:
            paybadge = f' <span style="background:#dc2626;color:#fff;border-radius:999px;padding:1px 7px;font-size:11px">{npay}</span>'
    parts = [ADMIN_PAGE_HEAD
             .replace("{{announcement}}", html.escape(announcement))
             .replace("{{paybadge}}", paybadge)]

    # 云端备份用量汇总（接近上限的账号置顶提醒）
    BACKUP_LIMIT = BACKUP_MAX_BYTES
    warn_line = 0.8 * BACKUP_LIMIT
    used_users = {uid: s for uid, s in sizes.items() if s}
    total_mb = sum(used_users.values()) / 1048576
    near = {
        uid: s for uid, s in used_users.items() if s >= warn_line
    }
    name_of = {r["id"]: r["username"] for r in rows}
    if used_users:
        near_text = (
            "；⚠️ 接近上限(≥80%)：" + "、".join(
                f"{name_of.get(uid, uid)}({s / 1048576:.1f}MB)" for uid, s in
                sorted(near.items(), key=lambda kv: -kv[1])
            ) if near else "，暂无接近上限的账号"
        )
        summary = (
            f"共 {len(used_users)} 个账号有云端备份，合计 {total_mb:.1f} MB"
            f"（单账号上限 {BACKUP_LIMIT / 1048576:.0f}MB）{near_text}"
        )
        summary_style = "color:#b45309;font-weight:600" if near else ""
    else:
        summary, summary_style = "暂无账号产生云端备份", ""
    backup_strip = ""
    if used_users:
        style_attr = f' style="{summary_style}"' if summary_style else ""
        backup_strip = f'<div class="strip"{style_attr}>☁️ {summary}</div>'

    # ── 网关计费（new-api 集成） ──
    wallet_html = None
    if gw_enabled():
        try:
            with napi_connect() as nc:
                # 审查 R11（P2）：site_now() 返回不带 tzinfo 的北京时间，.timestamp() 会按
                # 宿主时区解释——Docker（UTC）主机上「今日消费」起点偏到北京 08:00，当天
                # 0-8 点的消费漏算。显式挂 SITE_TZ 再取时间戳。
                today_start = int(datetime.combine(site_now().date(), datetime.min.time(), tzinfo=SITE_TZ).timestamp())
                # 每个站点用户的余额与用量
                wallet_rows = {}
                with db() as c:
                    maps = c.execute(
                        "SELECT uid, napi_uid FROM gateway_map"
                    ).fetchall()
                balance_of = {}
                used_of = {}
                today_of = {}
                for m in maps:
                    u = nc.execute(
                        "SELECT quota, used_quota FROM users WHERE id=?", (m["napi_uid"],)
                    ).fetchone()
                    if u:
                        balance_of[m["uid"]] = quota_to_cny(u["quota"])
                        used_of[m["uid"]] = quota_to_cny(u["used_quota"])
                    t = nc.execute(
                        "SELECT COALESCE(SUM(quota),0) AS s FROM logs "
                        "WHERE user_id=? AND type=2 AND created_at>=?",
                        (m["napi_uid"], today_start),
                    ).fetchone()["s"]
                    today_of[m["uid"]] = quota_to_cny(t)
                wallet_rows = {m["uid"]: m["napi_uid"] for m in maps}
        except Exception as e:
            wallet_rows = None
            print("[wallet] 管理面板读取计费库失败: %s" % e)
            wallet_err = "读取计费库失败，请检查 new-api 服务与数据库路径（详情见服务端日志）"

        if wallet_rows is None:
            parts.append(
                '<section class="sec" id="sec-wallet"><div class="sec-h"><h2>网关计费</h2></div>'
                f'<div class="flash err">⚠️ {html.escape(wallet_err)}</div></section>'
            )
        else:
            # 调账幂等操作号：正常渲染生成新的；上次失败重试时复用同一 op_id（P0-3）
            _retry_op = request.query_params.get("op_id", "")
            adjust_op_id = _retry_op if re.fullmatch(r"[0-9a-f\-]{8,64}", _retry_op or "") else uuid.uuid4().hex
            lines = []
            for r in rows:
                uid = r["id"]
                uname = html.escape(str(r["username"]))
                if uid not in wallet_rows:
                    bal_cell = '<span class="dim">未开通</span>'
                    used_cell = today_cell = '<span class="dim">—</span>'
                    ops = (
                        f'<form method="post" action="/admin/wallet/adjust" class="inline">'
                        f'<input type="hidden" name="uid" value="{uid}">'
                        f'<input type="hidden" name="op_id" value="{adjust_op_id}">'
                        f'<input class="num" name="amount" value="10" inputmode="decimal" aria-label="金额">'
                        f'<span class="unit">元</span>'
                        f'<button class="btn sm">开通并充值</button>'
                        f'<span class="hint">首次对话/充值时自动开通，赠 ¥{GW_DEFAULT_GIFT_CNY:.2f}</span></form>'
                    )
                else:
                    bal_cell = f'<span class="money">¥{balance_of.get(uid, 0):.2f}</span>'
                    used_cell = f'<span class="num2">¥{used_of.get(uid, 0):.2f}</span>'
                    today_cell = f'<span class="num2">¥{today_of.get(uid, 0):.2f}</span>'
                    ops = (
                        f'<form method="post" action="/admin/wallet/adjust" class="inline">'
                        f'<input type="hidden" name="uid" value="{uid}">'
                        f'<input type="hidden" name="op_id" value="{adjust_op_id}">'
                        f'<input class="num" name="amount" value="10" inputmode="decimal" aria-label="金额">'
                        f'<span class="unit">元</span>'
                        f'<input class="num note" name="note" placeholder="备注" aria-label="备注">'
                        f'<button class="btn sm">充值</button>'
                        f'<button class="btn sm danger" data-confirm="确认真金白银扣减该用户余额？">扣减</button></form>'
                    )
                lines.append(
                    f'<tr><td class="uname">{uname}</td>'
                    f'<td class="ta-r">{bal_cell}</td>'
                    f'<td class="ta-r">{used_cell}</td>'
                    f'<td class="ta-r">{today_cell}</td>'
                    f'<td><div class="ops">{ops}</div></td></tr>'
                )
            with db() as c:
                agg = c.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(cny),0) AS s FROM redeem_codes WHERE used_by IS NULL"
                ).fetchone()
                unused = c.execute(
                    "SELECT code, cny, created_at FROM redeem_codes WHERE used_by IS NULL "
                    "ORDER BY rowid DESC LIMIT 30"
                ).fetchall()
                txs = c.execute(
                    "SELECT t.type, t.cny, t.balance_cny, t.note, t.created_at, u.username "
                    "FROM wallet_tx t LEFT JOIN users u ON u.id=t.uid ORDER BY t.id DESC LIMIT 15"
                ).fetchall()
            code_items = " ".join(
                f'<span class="chip" data-code="{html.escape(c["code"])}" onclick="cpCode(this)" title="点击复制">'
                f'{html.escape(c["code"])}<i>¥{c["cny"]:g}</i></span>'
                for c in unused
            ) or '<span class="dim">暂无未用充值码</span>'

            def _tx_row(t):
                m = {
                    "gift": ("赠送", "b-green"), "redeem": ("兑换", "b-blue"),
                    "adjust": ("调整", "b-amber"), "gen_codes": ("生成充值码", "b-gray"),
                    "pay": ("扫码充值", "b-blue"),
                }
                kind, kcls = m.get(t["type"], (t["type"], "b-gray"))
                bal = (
                    f'<td class="ta-r num2">¥{t["balance_cny"]:.2f}</td>'
                    if t["balance_cny"] is not None else '<td class="ta-r dim">—</td>'
                )
                note = html.escape(str(t["note"])) if t["note"] else '<span class="dim">—</span>'
                uname = html.escape(str(t["username"])) if t["username"] else "系统"
                sign = "+" if t["type"] in ("gift", "redeem", "adjust", "pay") else ""
                return (
                    f'<tr><td class="num2 dim">{html.escape(str(t["created_at"])[:16])}</td>'
                    f'<td>{uname}</td>'
                    f'<td><span class="badge {kcls}">{kind}</span></td>'
                    f'<td class="ta-r money{" pos" if sign else ""}">{sign}¥{t["cny"]:g}</td>'
                    f'{bal}<td class="dim">{note}</td></tr>'
                )

            tx_items = "".join(_tx_row(t) for t in txs) or '<tr><td colspan="6" class="dim" style="text-align:center;padding:18px">暂无流水</td></tr>'
            new_codes = html.escape(request.query_params.get("codes", ""))
            wallet_err_qp = request.query_params.get("wallet_err", "")
            err_flash = ""
            if wallet_err_qp == "1":
                err_flash = '<div class="flash err">余额调整失败，请检查 new-api 服务是否正常（详情见服务端日志）</div>'
            elif wallet_err_qp == "code":
                err_flash = '<div class="flash err">充值码参数非法：面值需为 0 &lt; 面值 ≤ 10000</div>'
            lines_html = "".join(lines) or '<tr><td colspan="5" class="dim" style="text-align:center;padding:18px">暂无用户</td></tr>'
            flash = (
                f'<div class="flash ok">新充值码（点击可复制，发给用户即可）：{new_codes}</div>'
                if new_codes else ""
            )
            if err_flash:
                flash = err_flash + flash
            total_bal = sum(balance_of.values())
            today_sum = sum(today_of.values())
            kpis = (
                '<div class="kpis">'
                f'<div class="kpi"><div class="l">用户总数</div><div class="v">{len(rows)}</div></div>'
                f'<div class="kpi"><div class="l">平台总余额</div><div class="v">¥{total_bal:.2f}</div></div>'
                f'<div class="kpi"><div class="l">今日消耗</div><div class="v">¥{today_sum:.2f}</div></div>'
                f'<div class="kpi"><div class="l">未用充值码</div><div class="v">{agg["n"]} <span class="dim" style="font-size:13px">张 · ¥{agg["s"]:g}</span></div></div>'
                '</div>'
            )
            head = (
                '<section class="sec" id="sec-wallet"><div class="sec-h"><h2>网关计费 · 用户余额</h2>'
                '<span class="hint">余额实时来自 new-api，充值/扣减同步记账</span></div>'
                + kpis
                + '<div class="tscroll"><table><thead><tr>'
                '<th>用户</th><th class="ta-r">余额</th><th class="ta-r">累计消耗</th>'
                '<th class="ta-r">今日</th><th>调整余额</th></tr></thead><tbody>'
                + lines_html + '</tbody></table></div></section>'
            )
            codes_sec = (
                '<section class="sec" id="sec-codes"><div class="sec-h"><h2>充值码</h2>'
                '<span class="hint">点击已生成的码即可复制</span></div>'
                + flash
                + '<form method="post" action="/admin/wallet/gen-codes" class="inline">'
                '<input class="num" name="cny" value="10" inputmode="decimal" title="单张面值(元)" aria-label="单张面值">'
                '<span class="unit">元/张</span><span class="unit">×</span>'
                '<input class="num" name="count" value="5" inputmode="numeric" aria-label="张数">'
                '<span class="unit">张</span>'
                '<button class="btn sm">生成充值码</button></form>'
                f'<div class="chips">{code_items}</div></section>'
            )
            tx_sec = (
                '<section class="sec" id="sec-tx"><div class="sec-h"><h2>钱包流水</h2>'
                '<span class="hint">最近 15 条</span></div>'
                '<div class="tscroll"><table><thead><tr>'
                '<th>时间</th><th>用户</th><th>类型</th><th class="ta-r">金额</th>'
                '<th class="ta-r">变动后余额</th><th>备注</th></tr></thead><tbody>'
                + tx_items + '</tbody></table></div></section>'
            )
            ratio_err_qp = request.query_params.get("ratio_err", "")
            ratio_flash = ""
            if request.query_params.get("ratio_ok") == "1":
                ratio_flash = '<div class="flash ok">已保存：倍率=官方实时价×倍数，本站预检即时生效，new-api 结算约 1 分钟内同步。</div>'
            elif request.query_params.get("official_ok") == "1":
                ratio_flash = '<div class="flash ok">已抓取官方最新价格并重算倍率。</div>'
            elif request.query_params.get("official_ok") == "0":
                ratio_flash = '<div class="flash err">官方价格页抓取失败，暂沿用上次成功数据（后台每 10 分钟自动重试）。</div>'
            elif request.query_params.get("model_ok") == "1":
                ratio_flash = '<div class="flash ok">站点对话模型已切换，用户端即时生效。</div>'
            elif request.query_params.get("model_err") == "1":
                ratio_flash = '<div class="flash err">模型名不在可选列表内。</div>'
            elif ratio_err_qp == "1":
                ratio_flash = '<div class="flash err">参数非法：模型名限字母/数字/./-/-，倍数需为 0.1 ~ 100 的数字</div>'
            elif ratio_err_qp == "write":
                ratio_flash = '<div class="flash err">⚠️ 定价写库失败：new-api 未生效，请检查服务后重试（倍数已保存，重试只重算不重复）</div>'
            elif request.query_params.get("tm_ok") == "1":
                ratio_flash = '<div class="flash ok">时段倍率已保存并即时重算生效。</div>'
            elif request.query_params.get("tm_err") == "full":
                ratio_flash = '<div class="flash err">时段规则已达上限（%d 条），请先删除旧规则。</div>' % _TIME_MAX_RULES
            elif request.query_params.get("tm_err") == "1":
                ratio_flash = '<div class="flash err">时段倍率参数非法：时间格式 HH:MM，倍数 0.1 ~ 100</div>'
            elif request.query_params.get("tm_err") == "write":
                ratio_flash = '<div class="flash err">⚠️ 倍率写库失败：new-api 未生效，请检查服务后重试（规则已保存）</div>'
            # ── 官方价 × 倍数 ──
            official = _load_official_prices()
            tier_peak = _is_peak_now()
            # P1-06 告警展示：价格变动 / 数据陈旧
            price_alert_html = ""
            try:
                _ch = get_setting(PRICE_CHANGE_KEY, "")
                if _ch:
                    _cd = json.loads(_ch)
                    _items = _cd.get("items") or []
                    if _items:
                        price_alert_html += (
                            '<div class="flash err">⚠️ 官方价变动超过 %d%%：%s（%s）——已自动按新价重算倍率</div>'
                            % (PRICE_CHANGE_ALERT_PCT * 100, html.escape("; ".join(_items[:6])),
                               time.strftime("%m-%d %H:%M", time.localtime(_cd.get("ts") or 0))))
            except Exception:
                pass
            try:
                _st = get_setting(PRICE_STALE_KEY, "")
                if _st:
                    _sd = json.loads(_st)
                    price_alert_html += (
                        '<div class="flash err">⚠️ 官方价格已超过 24 小时抓取失败，计费沿用 %s 快照价（后台每 10 分钟重试）</div>'
                        % time.strftime("%m-%d %H:%M", time.localtime(_sd.get("since") or 0)))
            except Exception:
                pass
            mults = get_multipliers()
            tm_cfg = get_time_mults()
            tmult, trule = _active_time_mult()
            mr_map_r, cr_map_r = _gw_load_ratios()
            official_ts = time.strftime("%m-%d %H:%M", time.localtime(official.get("ts") or 0)) if official.get("ts") else "默认快照"
            ratio_rows = []
            for mname in SITE_MODELS:
                src = OFFICIAL_PRICE_MAP.get(mname, "deepseek-flash")
                base = (official.get("models") or {}).get(src) or OFFICIAL_PRICES_DEFAULT[src]
                n = float(mults.get(mname, 1.0))
                n_eff = n * tmult
                mrv = mr_map_r.get(mname, 0)
                crv = cr_map_r.get(mname, 0)
                pin = base["in_miss"][1 if tier_peak else 0]
                pout = base["out"][1 if tier_peak else 0]
                fin_in = pin * n_eff
                fin_out = pout * n_eff
                ratio_rows.append(
                    f'<form method="post" action="/admin/wallet/ratios" class="inline" '
                    f'onsubmit="return confirm(\'确认将 {html.escape(mname)} 的定价改为官方价 × {n:g}？\')">'
                    f'<input type="hidden" name="model" value="{html.escape(mname)}">'
                    f'<b style="min-width:150px;display:inline-block">{html.escape(mname)}</b>'
                    f'<span class="hint">官方（{html.escape(src)}）：输入 ${pin:g} / 输出 ${pout:g}（每百万 token）</span>'
                    f'<span class="unit">× 倍数</span>'
                    f'<input class="num" name="mult" value="{n:g}" inputmode="decimal" aria-label="加价倍数" title="加价倍数">'
                    f'<button class="btn sm">保存</button>'
                    f'<span class="hint">= 输入 ${fin_in:g} / 输出 ${fin_out:g}'
                    f'（≈¥{fin_in * USD_TO_CNY:.2f} / ¥{fin_out * USD_TO_CNY:.2f}，已含时段倍数 ×{tmult:g}）'
                    f'· 当前倍率 {mrv:g}/{crv:g}</span></form>'
                )
            ratio_items = "".join(ratio_rows)
            # ── 时段倍率 ──
            tm_rule_rows = "".join(
                f'<tr><td>{html.escape(r["name"] or "未命名")}</td>'
                f'<td class="num2">{html.escape(r["start"])} - {html.escape(r["end"])}</td>'
                f'<td class="ta-r num2">×{r["mult"]:g}</td>'
                f'<td class="ta-r"><form method="post" action="/admin/wallet/time-mult/del" class="inline">'
                f'<input type="hidden" name="idx" value="{i}">'
                f'<button class="btn sm danger" data-confirm="删除时段规则「{html.escape(r["name"] or "未命名")}」？删除后该时段回落到默认倍数。">删除</button></form></td></tr>'
                for i, r in enumerate(tm_cfg["rules"])
            ) or '<tr><td colspan="4" class="dim" style="text-align:center;padding:12px">暂无时段规则，全天按默认倍数计价</td></tr>'
            tmult_sec = (
                '<div class="sec-h" style="margin-top:16px"><h3 style="font-size:14px">时段倍率（不同时间不同价）</h3>'
                f'<span class="hint">最终售价 = 官方实时价 × 模型倍数 × 时段倍数；规则按顺序匹配（支持跨零点，如 22:00-06:00）；'
                f'时段切换每分钟自动检测，最多 1 分钟内生效。'
                f'<b>以下时间均为北京时间（Asia/Shanghai，SITE_TZ 可改）</b>；官方峰谷判定固定按 UTC</span></div>'
                + f'<div class="flash ok" style="margin-bottom:8px">当前生效：<b>×{tmult:g}</b>'
                f'（{"规则「" + html.escape(trule) + "」" if trule != "默认" else "默认倍数"}）'
                f' · 峰谷官方价档：{"官方峰时" if tier_peak else "官方谷时"}</div>'
                + '<div class="tscroll"><table><thead><tr><th>名称</th><th>时段（站点时区）</th>'
                '<th class="ta-r">时段倍数</th><th></th></tr></thead><tbody>' + tm_rule_rows + '</tbody></table></div>'
                + '<div class="ops" style="margin-top:8px">'
                + '<form method="post" action="/admin/wallet/time-mult" class="inline">'
                '<input name="name" placeholder="规则名（如 高峰）" maxlength="20" aria-label="规则名">'
                '<input name="start" placeholder="18:00" aria-label="开始时间" style="max-width:80px">'
                '<input name="end" placeholder="23:00" aria-label="结束时间" style="max-width:80px">'
                '<input class="num" name="mult" value="1" inputmode="decimal" aria-label="时段倍数" title="时段倍数">'
                '<button class="btn sm">添加时段规则</button></form>'
                + '<form method="post" action="/admin/wallet/time-mult/default" class="inline">'
                '<span class="unit">默认倍数：</span>'
                f'<input class="num" name="mult" value="{tm_cfg["default"]:g}" inputmode="decimal" aria-label="默认倍数">'
                '<button class="btn sm ghost">保存默认</button></form>'
                + '</div>'
            )
            default_model = get_setting(GW_DEFAULT_MODEL_KEY, "deepseek-flash")
            model_opts = "".join(
                f'<option value="{m}"{" selected" if m == default_model else ""}>{m}</option>'
                for m in SITE_MODELS
            )
            ratios_sec = (
                '<section class="sec" id="sec-ratios"><div class="sec-h"><h2>定价（官方价 × 倍数）</h2>'
                f'<span class="hint">官方价每 10 分钟自动抓取；当前时段：{"峰时" if tier_peak else "谷时"} · 数据时间 {official_ts}'
                f'（峰时=UTC 周一至周五 01:00-04:00 与 06:00-10:00；中国法定节假日全天按谷时计，周末/其余时段按谷时）'
                ' · 倍数=在你的售价上相对官方价的倍率</span></div>'
                + ratio_flash
                + price_alert_html
                + '<div class="ratios">' + ratio_items + '</div>'
                + tmult_sec
                + '<div class="ops" style="margin-top:8px">'
                + '<form method="post" action="/admin/wallet/refresh-official" class="inline">'
                '<button class="btn sm ghost">刷新官方价</button></form>'
                + '<form method="post" action="/admin/wallet/model" class="inline" '
                'onsubmit="return confirm(\'确认切换站点对话模型？所有用户的对话将立即改用新模型（按新模型价格计费）。\')">'
                '<span class="unit">站点对话模型（用户无感知）：</span>'
                f'<select name="model" aria-label="站点对话模型">{model_opts}</select>'
                '<button class="btn sm">切换</button></form>'
                + '</div></section>'
            )
            # ── 充值审批 ──
            pay_err_qp = request.query_params.get("pay_err", "")
            pay_flash = ""
            if request.query_params.get("pay_ok") == "1":
                pay_flash = '<div class="flash ok">已处理该充值申请。</div>'
            elif pay_err_qp == "1":
                pay_flash = '<div class="flash err">处理失败：订单不存在或已被处理（刷新后查看最新状态）。</div>'
            with db() as c:
                pending_pays = c.execute(
                    "SELECT id, uid, username, cny, created_at, status FROM pay_orders "
                    "WHERE status IN ('pending','processing') ORDER BY id").fetchall()
                handled_pays = c.execute(
                    "SELECT id, username, cny, status, created_at, handled_at FROM pay_orders "
                    "WHERE status!='pending' ORDER BY id DESC LIMIT 10").fetchall()
            pay_rows = "".join(
                f'<tr><td>{p["id"]}</td><td class="num2 dim">{html.escape(str(p["created_at"])[:16])}</td>'
                f'<td>{html.escape(str(p["username"]))}</td>'
                f'<td class="ta-r money">¥{p["cny"]:.2f}</td>'
                f'<td><form method="post" action="/admin/wallet/pay" class="inline">'
                f'<input type="hidden" name="oid" value="{p["id"]}">'
                f'<button class="btn sm" data-confirm="确认已收到 {html.escape(str(p["username"]))} 的 ¥{p["cny"]:g} 付款并为其到账？">{"续跑到账" if p["status"] == "processing" else "通过"}</button></form>'
                f'<form method="post" action="/admin/wallet/pay" class="inline">'
                f'<input type="hidden" name="oid" value="{p["id"]}">'
                f'<button class="btn sm danger" data-confirm="拒绝该笔充值申请？">拒绝</button></form></td></tr>'
                for p in pending_pays
            ) or '<tr><td colspan="5" class="dim" style="text-align:center;padding:18px">暂无待审充值申请</td></tr>'
            pay_hist = "".join(
                f'<tr><td class="num2 dim">{html.escape(str(p["handled_at"] or "")[:16])}</td>'
                f'<td>{html.escape(str(p["username"]))}</td>'
                f'<td class="ta-r num2">¥{p["cny"]:.2f}</td>'
                f'<td><span class="badge {"b-green" if p["status"] == "approved" else "b-gray"}">'
                f'{ {"approved": "已到账", "rejected": "已拒绝", "processing": "处理中(可续跑)"}.get(p["status"], p["status"]) }</span></td></tr>'
                for p in handled_pays
            ) or '<tr><td colspan="4" class="dim" style="text-align:center;padding:14px">暂无历史</td></tr>'
            has_qr = os.path.exists(QR_PATH)
            qr_img = (f'<img src="/pay-qr?ts={int(time.time())}" alt="收款码" '
                      f'style="width:150px;height:150px;object-fit:contain;border:1px solid #e2e8f0;border-radius:8px;background:#fff">'
                      if has_qr else '<span class="dim">尚未上传收款码</span>')
            qr_err_map = {"size": "图片超过 2MB", "type": "只支持 png/jpg 图片", "1": "请选择文件"}
            qr_flash = (
                '<div class="flash err">%s</div>' % qr_err_map.get(request.query_params.get("qr_err", ""), "上传失败")
                if request.query_params.get("qr_err") else
                ('<div class="flash ok">收款码已更新，用户端钱包页即时生效。</div>' if request.query_params.get("qr_ok") == "1" else "")
            )
            pay_sec = (
                '<section class="sec" id="sec-pay"><div class="sec-h"><h2>充值审批 · 收款码</h2>'
                f'<span class="hint">用户扫码付款后点「我已完成支付」，申请出现在这里；通过后余额即时到账</span></div>'
                + pay_flash + qr_flash
                + '<div class="ops" style="margin-bottom:12px;align-items:flex-start">'
                + qr_img
                + '<form method="post" action="/admin/wallet/qr" enctype="multipart/form-data" class="inline">'
                '<input type="file" name="file" accept="image/png,image/jpeg" aria-label="收款码图片">'
                '<button class="btn sm">上传/更换收款码</button>'
                '<span class="hint">png/jpg，≤2MB；用户在钱包页扫码付款</span></form></div>'
                + f'<div class="kpi" style="margin-bottom:8px"><div class="l">待审充值申请</div><div class="v">{len(pending_pays)}</div></div>'
                + '<div class="tscroll"><table><thead><tr><th>单号</th><th>申请时间</th><th>用户</th>'
                '<th class="ta-r">金额</th><th>操作</th></tr></thead><tbody>' + pay_rows + '</tbody></table></div>'
                + '<div class="sec-h" style="margin-top:14px"><h3 style="font-size:14px">最近处理记录</h3></div>'
                + '<div class="tscroll"><table><thead><tr><th>处理时间</th><th>用户</th>'
                '<th class="ta-r">金额</th><th>结果</th></tr></thead><tbody>' + pay_hist + '</tbody></table></div>'
                + '</section>'
            )
            wallet_html = head + codes_sec + ratios_sec + pay_sec + tx_sec
    npending = sum(1 for r in rows if r["status"] == "pending")
    user_rows = []
    for r in rows:
        left = f"{days_left(r)}天" if r["status"] == "approved" and r["expiry"] and not is_expired(r) else "—"
        ops = []
        if r["status"] != "approved":
            ops.append(
                f'<form method="post" action="/admin/action" class="inline"><input type="hidden" name="uid" value="{r["id"]}">'
                f'<input type="hidden" name="action" value="approve"><input class="num" name="days" value="30" inputmode="numeric" aria-label="天数">'
                f'<span class="unit">天</span><button class="btn sm">通过</button></form>'
            )
        if r["status"] == "approved":
            ops.append(
                f'<form method="post" action="/admin/action" class="inline"><input type="hidden" name="uid" value="{r["id"]}">'
                f'<input type="hidden" name="action" value="renew"><input class="num" name="days" value="30" inputmode="numeric" aria-label="天数">'
                f'<span class="unit">天</span><button class="btn sm ghost">续期</button></form>'
            )
        ops.append(
            f'<form method="post" action="/admin/action" class="inline"><input type="hidden" name="uid" value="{r["id"]}">'
            f'<input type="hidden" name="action" value="reject"><button class="btn sm danger" data-confirm="拒绝该用户的注册申请？">拒绝</button></form>'
        )
        if r["status"] == "disabled":
            ops.append(
                f'<form method="post" action="/admin/action" class="inline"><input type="hidden" name="uid" value="{r["id"]}">'
                f'<input type="hidden" name="action" value="enable"><button class="btn sm ghost">启用</button></form>'
            )
        else:
            ops.append(
                f'<form method="post" action="/admin/action" class="inline"><input type="hidden" name="uid" value="{r["id"]}">'
                f'<input type="hidden" name="action" value="disable"><button class="btn sm danger" data-confirm="禁用该用户？其将无法登录和使用 AI">禁用</button></form>'
            )
        hot = r["status"] == "pending" or (r["renewal_requested"] and r["status"] == "approved")
        status_tag = f'<span class="tag {r["status"]}">{STATUS_TEXT.get(r["status"], r["status"])}</span>'
        if r["renewal_requested"] and r["status"] == "approved":
            status_tag += ' <span class="badge b-amber">申请续期</span>'
        elif r["note"]:
            status_tag += f' <span class="dim">{html.escape(str(r["note"]))}</span>'
        expiry_line = html.escape(r["expiry"]) if r["expiry"] else "—"
        backup_mb = sizes.get(r["id"], 0) / 1048576
        backup_cell = (
            f'<span class="num2">{backup_mb:.1f} MB / 200 MB（{backup_mb / 204.8:.0f}%）</span>'
            + (' <span class="badge b-amber">接近上限</span>' if sizes.get(r["id"], 0) >= warn_line else "")
            if sizes.get(r["id"]) else '<span class="dim">—</span>'
        )
        row_cls = ' class="hot"' if hot else ""
        user_rows.append(
            f'<tr{row_cls}><td class="uname">{html.escape(str(r["username"]))}</td>'
            f'<td>{status_tag}</td>'
            f'<td class="num2">{expiry_line}<span class="dim">（剩 {left}）</span></td>'
            f'<td>{backup_cell}</td>'
            f'<td><div class="ops">{"".join(ops)}</div></td></tr>'
        )
    parts.append(
        '<section class="sec" id="sec-users"><div class="sec-h"><h2>用户管理</h2>'
        f'<span class="hint">{len(rows)} 个账号 · 待审核 {npending} · 待审核与续期申请自动置顶</span></div>'
        + backup_strip
        + '<div class="tscroll"><table><thead><tr>'
        '<th>用户</th><th>状态</th><th>到期</th><th>云端备份</th><th>操作</th></tr></thead><tbody>'
        + ("".join(user_rows) or '<tr><td colspan="5" class="dim" style="text-align:center;padding:18px">暂无用户</td></tr>')
        + '</tbody></table></div></section>'
    )
    if wallet_html:
        parts.append(wallet_html)
    parts.append(ADMIN_PAGE_FOOT)
    return HTMLResponse("".join(parts))

_ADMIN_LOGIN_FAILS = {}   # ip -> [fail_count, locked_until]
_ADMIN_LOCK_SECONDS = 300  # 连续 5 次失败锁 5 分钟
_ADMIN_LOCK_AFTER = 5


def _admin_login_check(ip: str) -> str:
    """返回 'ok' / 'locked'；失败计数与退避（审查 P1-10）"""
    ent = _ADMIN_LOGIN_FAILS.get(ip)
    if ent and ent[1] > time.time():
        return "locked"
    if ent and ent[1] and ent[1] <= time.time():
        # 锁定过期，重置
        _ADMIN_LOGIN_FAILS.pop(ip, None)
    return "ok"


def _admin_login_fail(ip: str):
    # 审查 R11（P2）：锁定表同样无上限——失败 1 次就建键，IPv6 一段地址即可撑爆内存。
    # 超限时按插入序丢最老的，保留最近 4096 条（dict 自 3.7 保序）。
    # 代价：被丢弃的 IP 失败计数归零（最多多试 _ADMIN_LOCK_AFTER 次），
    # 但拿到这个收益需要先失败、再制造 4096 个不同源地址，成本远高于收益。
    if len(_ADMIN_LOGIN_FAILS) > 8192:
        for _k in list(_ADMIN_LOGIN_FAILS)[:len(_ADMIN_LOGIN_FAILS) - 4096]:
            _ADMIN_LOGIN_FAILS.pop(_k, None)
    ent = _ADMIN_LOGIN_FAILS.setdefault(ip, [0, 0.0])
    # 审查 R11（P1）：锁定期内继续失败不再累加、不续期——否则攻击者每 5 分钟打 5 次
    # 错误密码就能把锁定时间无限延长，真正的管理员永远进不了后台（纯 DoS，无需密码）。
    # 一次锁定最多 _ADMIN_LOCK_SECONDS，到期后重新计数。
    if ent[1] > time.time():
        return
    ent[0] += 1
    if ent[0] >= _ADMIN_LOCK_AFTER:
        ent[1] = time.time() + _ADMIN_LOCK_SECONDS
        print("[安全] 管理登录已锁定 ip=%s（连续失败 %d 次）" % (ip, ent[0]))


@app.post("/admin/login")
async def admin_login(request: Request, pw: str = Form("")):
    ip = request.client.host if request.client else "?"
    if _admin_login_check(ip) == "locked":
        print("[安全] 管理登录拒绝(锁定中) ip=%s" % ip)
        return RedirectResponse("/admin?login_err=locked", status_code=303)
    # 审查 R11（P1）：hmac.compare_digest 对 str 要求 ASCII-only，管理员设置中文密码
    # 或攻击者提交中文密码都会抛 TypeError → 500（实测确认）。统一按 UTF-8 字节比较。
    if not hmac.compare_digest(pw.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        _admin_login_fail(ip)
        print("[安全] 管理登录失败 ip=%s" % ip)
        return RedirectResponse("/admin", status_code=303)
    _ADMIN_LOGIN_FAILS.pop(ip, None)
    resp = RedirectResponse("/admin", status_code=303)
    set_cookie(resp, "admin", sign({"admin": True, "exp": time.time() + 12 * 3600}), 1, request=request)
    return resp

@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("admin", path="/")
    return resp

@app.post("/admin/announce")
async def admin_announce(request: Request, text: str = Form("")):
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    set_setting("announcement", text.strip())
    return RedirectResponse("/admin#announce-saved", status_code=303)

@app.post("/admin/action")
async def admin_action(request: Request, uid: int = Form(...), action: str = Form(...), days: int = Form(30)):
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    days = max(1, min(days, 3650))
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            return RedirectResponse("/admin", status_code=303)
        now = site_now().isoformat(timespec="seconds")
        if action == "approve":
            if row["status"] == "approved":
                # 已开通用户不响应「通过」，防止重复点击导致无限加时间
                return RedirectResponse("/admin", status_code=303)
            base = max(today(), row["expiry"]) if row["expiry"] else today()
            new_exp = (date.fromisoformat(base) + timedelta(days=days)).isoformat()
            c.execute(
                "UPDATE users SET status='approved', expiry=?, renewal_requested=0, approved_at=? WHERE id=?",
                (new_exp, now, uid),
            )
        elif action == "renew" and row["status"] == "approved":
            base = max(today(), row["expiry"]) if row["expiry"] else today()
            new_exp = (date.fromisoformat(base) + timedelta(days=days)).isoformat()
            c.execute("UPDATE users SET expiry=?, renewal_requested=0 WHERE id=?", (new_exp, uid))
        elif action == "reject":
            c.execute("UPDATE users SET status='rejected', renewal_requested=0 WHERE id=?", (uid,))
        elif action == "disable":
            c.execute("UPDATE users SET status='disabled' WHERE id=?", (uid,))
        elif action == "enable":
            # 审查 R11（P2）：原实现只改 status，过期账号启用后仍是 approved+过去日期 →
            # 依旧登录不了（表现为"点了启用没反应"）。未过期则原样恢复，已过期则补足有效期。
            if row["expiry"] and not is_expired(row):
                c.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
            else:
                _base = max(today(), row["expiry"]) if row["expiry"] else today()
                _new = (date.fromisoformat(_base) + timedelta(days=days)).isoformat()
                c.execute(
                    "UPDATE users SET status='approved', expiry=?, renewal_requested=0 WHERE id=?",
                    (_new, uid),
                )
    return RedirectResponse("/admin", status_code=303)

# ---------- 文件上传中转 ----------
# 手机浏览器直连 DeepSeek 上传文件时，multipart 表单会被运营商/浏览器代理链路改坏导致上传失败。
# 因此上传改走自家服务器：浏览器→服务器用原始二进制流（已在云同步链路验证可靠），
# 服务器在机房网络里重建标准 multipart 转发给 DeepSeek Files API。
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
FILES_BETA = "files-api-2025-04-14"
UPLOAD_MAX_BYTES = 64 * 1024 * 1024  # 与前端限制一致

@app.post("/api/upload")
async def api_upload(request: Request):
    user = get_user(request)
    if user is None:
        return JSONResponse({"error": {"message": "未登录"}}, status_code=401)
    if not valid_paid_user(user):
        return JSONResponse({"error": {"message": "账号不可用"}}, status_code=403)
    # 网关计费模式：统一走 new-api 计费链路（服务端注入计费 Key），不再要求用户自带 Key
    if gw_enabled():
        api_key, _ = gw_provision(user)
        upload_base = NEWAPI_BASE
        upload_path = "/v1/files"
    else:
        api_key = request.headers.get("x-api-key") or ""
        if not api_key:
            return JSONResponse({"error": {"message": "缺少 API Key"}}, status_code=401)
        upload_base = DEEPSEEK_BASE
        upload_path = "/anthropic/v1/files"
    name = (request.query_params.get("name") or "file").replace('"', "").replace("\r", "").replace("\n", "")
    purpose = request.query_params.get("purpose") or None
    body = await request.body()
    if not body:
        return JSONResponse({"error": {"message": "空文件"}}, status_code=400)
    if len(body) > UPLOAD_MAX_BYTES:
        return JSONResponse({"error": {"message": "文件超过 64MB 上限"}}, status_code=413)

    boundary = "----miandning" + secrets.token_hex(16)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # 中文文件名放进 header 会炸 latin-1：用 RFC 5987 filename* 携带原名，filename 用 ASCII 兜底
    ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
    ascii_name = f"upload.{ext}"
    encoded_name = urllib.parse.quote(name, safe="")
    parts = []
    if purpose:
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n').encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}\r\n"
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + body
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)

    _up_headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "anthropic-beta": FILES_BETA,
    }
    if gw_enabled():
        _up_headers["Authorization"] = "Bearer " + api_key  # new-api 只认 Bearer
    else:
        _up_headers["x-api-key"] = api_key
    req = urllib.request.Request(
        upload_base + upload_path,
        data=payload,
        method="POST",
        headers=_up_headers,
    )
    def _do_upload():
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return Response(r.read(), status_code=r.status, media_type="application/json")
        except urllib.error.HTTPError as e:
            # DeepSeek 的错误原样透传，前端按既有逻辑展示
            detail = e.read().decode("utf-8", "replace")
            return Response(detail, status_code=e.code, media_type="application/json")
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": {"message": f"上传中转失败: {e}"}}, status_code=502)

    # 审查 P1-14：同步 urlopen 移入线程池，上游挂起不再阻塞事件循环拖垮全站
    return await run_in_threadpool(_do_upload)

# ---------- 网关计费（new-api 集成，GATEWAY_ENABLED=1 时开启） ----------
import requests as http_requests

GATEWAY_ENABLED = (
    os.environ.get("GATEWAY_ENABLED", "0") == "1"
    or os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.on"))
)
NEWAPI_BASE = os.environ.get("NEWAPI_BASE", "http://127.0.0.1:3001")
NEWAPI_DB = os.environ.get("NEWAPI_DB", "/opt/newapi/one-api.db")
GW_UPSTREAM_BASE = os.environ.get("GW_UPSTREAM_BASE", "https://api.deepseek.com")
QUOTA_PER_USD = 500000          # new-api 计量单位：500000 quota = $1
USD_TO_CNY = 7.2                # 余额展示汇率
GW_DEFAULT_GIFT_CNY = float(os.environ.get("GW_DEFAULT_GIFT_CNY", "2"))  # 开通赠送
GW_REDEEM_RATE_LIMIT = 5        # 兑换接口限流：每账号每分钟最多尝试次数
GW_BODY_MAX_BYTES = 10 * 1024 * 1024   # 网关代理请求体上限 10MB

# ── 扫码充值（管理员审核到账） ──
GW_PAY_MIN_CNY = 5.0            # 起充金额
GW_PAY_MAX_CNY = 10000.0        # 单笔上限
GW_PAY_RATE_LIMIT = 5           # 提交支付确认限流：每账号每分钟次数
GW_PAY_MAX_PENDING = 5          # 单用户同时在审订单上限
# max_tokens 网关钳制（审查 P1-01）：预检估值与实际转发共用同一上限
GW_MAX_TOKENS_CAP = 8192        # 单请求输出 token 硬上限
GW_MAX_TOKENS_DEFAULT = 4096    # 请求缺省 max_tokens 时的服务端默认值
QR_PATH = os.path.join(BASE, "pay_qr.png")   # 站长收款码落盘位置

# ── 官方价 × 倍数 定价模式 ──
# 官方价来源：https://api-docs.deepseek.com/quick_start/pricing（美元 / 每百万 tokens）
# 结构：{模型: {in_hit:[谷,峰], in_miss:[谷,峰], out:[谷,峰]}}，页面校验失败时回退到这份快照
OFFICIAL_PRICE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
OFFICIAL_PRICES_DEFAULT = {
    "deepseek-flash": {"in_hit": [0.003, 0.006], "in_miss": [0.15, 0.3], "out": [0.6, 1.2]},
    "deepseek-v4-pro": {"in_hit": [0.022, 0.044], "in_miss": [0.66, 1.32], "out": [1.98, 3.96]},
}
# 站内模型 → 官方价目源映射（官方只发布这两档价，站内其余模型名按档位对应）
OFFICIAL_PRICE_MAP = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",        # legacy 名按官方规则由 Flash 档承接
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",   # legacy 名按 Pro 档承接
}
SITE_MODELS = ["deepseek-flash", "deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro"]
GW_DEFAULT_MODEL_KEY = "gw_default_model"
GW_MULTIPLIER_KEY = "model_multipliers"
GW_TIME_MULT_KEY = "time_multipliers"   # 时段倍率：{"default": x, "rules": [{name,start,end,mult}]}
GW_OFFICIAL_KEY = "official_prices"
GW_RATIO_REFRESH_INTERVAL = 600  # 官方价抓取周期（秒）；时段/峰谷切换每分钟自检并即时重算
# 峰时定义（官方）：UTC 周一至周五 01:00-04:00 与 06:00-10:00，中国法定节假日除外；
# 中国法定节假日全天按谷时（见下方 CN_HOLIDAYS），周末/其余时段按谷时
_PEAK_WINDOWS_UTC = [(1, 4), (6, 10)]

# 兑换尝试限流表：{uid: [窗口起始时间, 次数]}（进程内即可，多worker部署时换共享存储）
_REDEEM_ATTEMPTS = {}

# 网关代理每用户并发上限（缓解 new-api pre-consume 并发透支窗口：把单用户在途请求数封顶）
_GW_INFLIGHT = {}
_GW_LOCK = threading.Lock()
_GW_PROVISION_LOCK = threading.Lock()
_GW_INFLIGHT_CAP = 3
_GW_INFLIGHT_ZOMBIE_SECONDS = 330   # 上游 read timeout(10,300)s + 30s 余量
_GW_RATIO_CACHE = {"data": None, "ts": 0.0}


def _gw_load_ratios():
    """从 new-api options 读取模型倍率（60s 缓存），用于本地成本估算预检"""
    now = time.time()
    if _GW_RATIO_CACHE["data"] is not None and now - _GW_RATIO_CACHE["ts"] < 60:
        return _GW_RATIO_CACHE["data"]
    try:
        with napi_connect() as nc:
            rows = {r["key"]: r["value"] for r in nc.execute(
                "SELECT key, value FROM options WHERE key IN ('ModelRatio','CompletionRatio')")}
        mr = json.loads(rows.get("ModelRatio") or "{}")
        cr = json.loads(rows.get("CompletionRatio") or "{}")
        data = (mr, cr)
    except Exception:  # noqa: BLE001
        data = _GW_RATIO_CACHE["data"] or ({}, {})
    _GW_RATIO_CACHE["data"] = data
    _GW_RATIO_CACHE["ts"] = now
    return data


def _gw_estimate_quota(body: bytes, model: str) -> int:
    """粗估单请求成本（quota）：输入按字符数/2 估算 + 输出按 max_tokens 全额预留，并放大 20% 余量"""
    mr_map, cr_map = _gw_load_ratios()
    max_ratio = max([v for v in mr_map.values()] or [0.55])
    max_cr = max([v for v in cr_map.values()] or [4.07])
    mr = mr_map.get(model, max_ratio)
    cr = cr_map.get(model, max_cr)
    est_in = 20
    max_tokens = 1000
    try:
        b = json.loads(body)
        for m in b.get("messages", []):
            c_ = m.get("content")
            if isinstance(c_, str):
                est_in += len(c_) // 2
            elif isinstance(c_, list):
                est_in += sum(len(p.get("text", "")) // 2 for p in c_ if isinstance(p, dict))
        sys_ = b.get("system")
        if isinstance(sys_, str):
            est_in += len(sys_) // 2
        max_tokens = min(int(b.get("max_tokens") or 0) or GW_MAX_TOKENS_DEFAULT, GW_MAX_TOKENS_CAP)
    except Exception:  # noqa: BLE001
        pass
    return int((est_in + max_tokens * cr) * mr * 1.2)


# ── 官方价 × 倍数 定价引擎 ──
# 中国法定节假日（官方口径：节假日全天谷时，审查 R3 P1）。落在周末的日期无影响（周末本就全谷）。
# ⚠️ 每年由站长按国务院办公厅通知更新一次（部署指南 §五 有提醒）。
CN_HOLIDAYS = {
    # 2025
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026（按国务院 2026 部分节假日安排，春节 2/15-2/21、劳动节 5/1-5/5、国庆 10/1-10/7 等）
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
}


def _is_peak_now(now: datetime | None = None) -> bool:
    """官方峰时：UTC 周一至周五 01:00-04:00 与 06:00-10:00，法定节假日全天谷时
    （官方原文：Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday through Friday,
    excluding Chinese public holidays. All other hours are off-peak, including weekends
    and Chinese public holidays in full.）"""
    now = now or datetime.utcnow()
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in CN_HOLIDAYS:
        return False
    h = now.hour + now.minute / 60.0
    return any(lo <= h < hi for lo, hi in _PEAK_WINDOWS_UTC)


def _check_holiday_freshness() -> None:
    """R6（审查 P2）：CN_HOLIDAYS 覆盖到 2026 年末——过期后会**静默**把节假日按峰时
    （多收）。每年国务院公告后由站长在 app.py 更新一次（部署指南 §五）；过期前 30 天起
    打印红色告警并落 cn_holidays_stale_alert 供管理面板展示。"""
    try:
        last_year = max(int(x[:4]) for x in CN_HOLIDAYS)
        today = site_now().date()
        days_left = (datetime(last_year, 12, 31).date() - today).days
        if days_left < 0:
            set_setting("cn_holidays_stale_alert", json.dumps({"ts": time.time(), "last_year": last_year}))
            print("[official-price] \033[91m⚠️ CN_HOLIDAYS 仅覆盖到 %d 年，节假日已开始按峰时计费（多收）！"
                  "请按国务院最新通知更新后重启\033[0m" % last_year)
        elif days_left < 30:
            set_setting("cn_holidays_stale_alert", json.dumps({"ts": time.time(), "last_year": last_year}))
            print("[official-price] ⚠️ CN_HOLIDAYS 将于 %d-12-31 过期（剩 %d 天），请准备按国务院通知更新" % (last_year, days_left))
        else:
            set_setting("cn_holidays_stale_alert", "")
    except Exception as e:  # noqa: BLE001
        print("[official-price] 节假日时效检查异常:", e)


def _load_official_prices() -> dict:
    """官方价缓存（设置表持久化，抓取失败沿用上次/默认快照）"""
    raw = get_setting(GW_OFFICIAL_KEY, "")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("models"):
                return d
        except Exception:  # noqa: BLE001
            pass
    return {"ts": 0, "models": json.loads(json.dumps(OFFICIAL_PRICES_DEFAULT))}


def _parse_official_page(text: str) -> dict | None:
    """从官方定价页 HTML 解析 12 个价格（两模型×{命中,未命中,输出}×{谷,峰}），峰=谷×2 校验。
    加固（审查 P1-06）：主锚点失败时尝试备用锚点；全部数值限合理上界。"""
    seg = ""
    for anchor in ("1M INPUT TOKENS", "Input (cache miss)", "INPUT TOKENS", "标准（缓存未命中）"):
        i = text.find(anchor)
        if i >= 0:
            j = text.find("Concurrency Limit", i)
            seg = text[max(0, i - 200): j if j > i else i + 4000]
            break
    if not seg:
        return None
    try:
        vals = [float(v) for v in re.findall(r"\$\s*([0-9]+(?:\.[0-9]+)?)", seg)]
        if len(vals) < 12:
            return None
        vals = vals[:12]
        # 数值上界（P1-06）：页面上任何价格超过 $1000 视为解析异常（页面结构变化/垃圾值）
        if any(v <= 0 or v > 1000 for v in vals):
            return None
        f_hit_off, p_hit_off, f_hit_pk, p_hit_pk = vals[0:4]
        f_miss_off, p_miss_off, f_miss_pk, p_miss_pk = vals[4:8]
        f_out_off, p_out_off, f_out_pk, p_out_pk = vals[8:12]
        # 校验：峰时=谷时×2（官方规则），价格合理范围
        for off, pk in ((f_hit_off, f_hit_pk), (p_hit_off, p_hit_pk),
                        (f_miss_off, f_miss_pk), (p_miss_off, p_miss_pk),
                        (f_out_off, f_out_pk), (p_out_off, p_out_pk)):
            if not (pk > 0 and off > 0 and abs(pk - 2 * off) < off * 0.01):
                return None
        return {
            "deepseek-flash": {"in_hit": [f_hit_off, f_hit_pk], "in_miss": [f_miss_off, f_miss_pk], "out": [f_out_off, f_out_pk]},
            "deepseek-v4-pro": {"in_hit": [p_hit_off, p_hit_pk], "in_miss": [p_miss_off, p_miss_pk], "out": [p_out_off, p_out_pk]},
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_official_prices() -> tuple[dict, bool]:
    """实时抓取官方定价页；成功返回 (数据, True)，失败沿用缓存/默认并返回 (数据, False)。
    告警（审查 P1-06）：成功后对比旧价，变动>5% 落 price_change_alert；失败且缓存>24h 落 price_stale_alert。
    R5（审查 R3）：每次成功抓取把**原始响应**原样落盘 BASE/official_price_snapshot.html
    （+ .meta.json 记录抓取时间），作为 CacheRatio / 模型倍数的可查证据，不经过任何改写。"""
    try:
        req = urllib.request.Request(OFFICIAL_PRICE_URL, headers={"User-Agent": "Mozilla/5.0 (billing-sync)"})
        text = urllib.request.urlopen(req, timeout=15).read().decode(errors="replace")
        parsed = _parse_official_page(text)
        if parsed:
            try:
                with open(os.path.join(BASE, "official_price_snapshot.html"), "w", encoding="utf-8") as _f:
                    _f.write(text)
                with open(os.path.join(BASE, "official_price_snapshot.html.meta.json"), "w", encoding="utf-8") as _f:
                    json.dump({"ts": time.time(), "fetched_at": site_now().isoformat(timespec="seconds"),
                               "url": OFFICIAL_PRICE_URL, "parsed": parsed}, _f, ensure_ascii=False, indent=2)
            except OSError as _se:
                print("[official-price] ⚠️ 官方价快照落盘失败（不影响计费）: %s" % _se)
            _check_price_stale(clear=True)
            _check_price_change(parsed)
            data = {"ts": time.time(), "models": parsed}
            set_setting(GW_OFFICIAL_KEY, json.dumps(data))
            return data, True
    except Exception as e:  # noqa: BLE001
        print("[official-price] 抓取失败，沿用上次数据:", e)
    _check_price_stale()
    return _load_official_prices(), False


PRICE_CHANGE_KEY = "price_change_alert"   # 官方价变动>阈值告警 {"ts","items"}
PRICE_STALE_KEY = "price_stale_alert"     # 官方价陈旧告警 {"ts","since"}
PRICE_CHANGE_ALERT_PCT = 0.05             # 单价变动告警阈值 5%
PRICE_STALE_SECONDS = 86400               # 快照超过 24h 未更新视为陈旧


def _check_price_change(parsed: dict) -> None:
    """对比上次缓存价，任一档位变动超过阈值即落告警（管理面板展示 + 日志）。"""
    try:
        old = _load_official_prices()
        oldm = old.get("models") or {}
        if not old.get("ts"):
            return  # 无历史快照（首次抓取）不比较
        alerts = []
        for src, d in (parsed or {}).items():
            o = oldm.get(src)
            if not o:
                continue
            for k in ("in_hit", "in_miss", "out"):
                for ti in (0, 1):
                    a, b = o.get(k, [0, 0])[ti], d.get(k, [0, 0])[ti]
                    if a > 0 and abs(b - a) / a > PRICE_CHANGE_ALERT_PCT:
                        alerts.append("%s.%s[%s] $%g→$%g" % (src, k, "峰" if ti else "谷", a, b))
        if alerts:
            set_setting(PRICE_CHANGE_KEY, json.dumps({"ts": time.time(), "items": alerts}))
            print("[official-price] ⚠️ 官方价变动告警(>%d%%): %s" % (PRICE_CHANGE_ALERT_PCT * 100, "; ".join(alerts)))
    except Exception as e:  # noqa: BLE001
        print("[official-price] 变动检查异常:", e)


def _check_price_stale(clear: bool = False) -> None:
    """抓取失败且缓存快照超过 24h → 落 stale 告警；成功抓取（clear=True）时清除。"""
    try:
        if clear:
            set_setting(PRICE_STALE_KEY, "")
            return
        d = _load_official_prices()
        ts = d.get("ts") or 0
        if ts and time.time() - ts > PRICE_STALE_SECONDS:
            set_setting(PRICE_STALE_KEY, json.dumps({"ts": time.time(), "since": ts}))
            print("[official-price] ⚠️ 官方价数据陈旧(>%dh 未成功抓取)，沿用 %s 快照计费" % (
                PRICE_STALE_SECONDS // 3600, time.strftime("%m-%d %H:%M", time.localtime(ts))))
    except Exception as e:  # noqa: BLE001
        print("[official-price] stale 检查异常:", e)


def get_multipliers() -> dict:
    try:
        return json.loads(get_setting(GW_MULTIPLIER_KEY, "{}")) or {}
    except Exception:  # noqa: BLE001
        return {}


_MULT_MIN, _MULT_MAX = 0.1, 100.0    # 加价倍数允许范围（时段倍率同样适用）
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_TIME_MAX_RULES = 10


def get_time_mults() -> dict:
    """读取时段倍率配置（持久化于设置表）：{"default": float, "rules": [{name,start,end,mult}]}"""
    raw = get_setting(GW_TIME_MULT_KEY, "")
    try:
        d = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        d = {}
    rules = []
    for r in (d.get("rules") or [])[:_TIME_MAX_RULES]:
        try:
            rules.append({
                "name": str(r.get("name") or "").strip()[:20],
                "start": str(r["start"]), "end": str(r["end"]),
                "mult": min(max(float(r["mult"]), _MULT_MIN), _MULT_MAX),
            })
        except Exception:  # noqa: BLE001
            continue
    try:
        default = min(max(float(d.get("default", 1.0)), _MULT_MIN), _MULT_MAX)
    except Exception:  # noqa: BLE001
        default = 1.0
    return {"default": default, "rules": rules}


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _active_time_mult(now: datetime | None = None) -> tuple[float, str]:
    """当前时刻生效的时段倍数。规则按保存顺序匹配，第一条命中即生效（支持跨零点窗口如 22:00-06:00，
    start==end 视为全天）；无命中规则时使用默认倍数。返回 (倍数, 规则名)。"""
    now = now or site_now()
    cur = now.hour * 60 + now.minute
    tm = get_time_mults()
    for r in tm["rules"]:
        try:
            lo, hi = _hhmm_to_min(r["start"]), _hhmm_to_min(r["end"])
        except Exception:  # noqa: BLE001
            continue
        if lo < hi:
            hit = lo <= cur < hi
        elif lo > hi:
            hit = cur >= lo or cur < hi
        else:
            hit = True
        if hit:
            return r["mult"], r["name"] or "未命名"
    return tm["default"], "默认"


def _ratio_from_price(usd_per_m: float, mult: float) -> float:
    """美元价(每百万tokens)×倍数 → new-api ModelRatio(quota/token)；500000 quota=$1"""
    return round(usd_per_m / 1e6 * QUOTA_PER_USD * mult, 6)


RATIOS_UNCHANGED = "unchanged"
RATIOS_SUCCESS = "success"
RATIOS_FAILURE = "failure"


def recompute_ratios(official: dict | None = None) -> str:
    """按 官方当前时段价 × 每模型倍数 × 当前时段倍数 重算 ModelRatio/CompletionRatio 写回 new-api options。

    三态返回（审查 P1-04）：success=已写库并回读确认 / unchanged=值未变无需写 / failure=写库异常。
    合并写回（审查 P1-05）：只覆盖本站 SITE_MODELS 的键，共享 new-api 的其他模型配置原样保留。
    写库后回读生效值确认一致才算 success。"""
    official = official or _load_official_prices()
    models = official.get("models") or {}
    _check_holiday_freshness()   # R6（审查 P2）：节假日表过期告警（面板 + 日志）
    tier = 1 if _is_peak_now() else 0   # 0=谷 1=峰
    tmult, trule = _active_time_mult()
    mults = get_multipliers()
    mr_map, cr_map, cache_map = {}, {}, {}
    for m in SITE_MODELS:
        base = models.get(OFFICIAL_PRICE_MAP.get(m, "deepseek-flash")) or OFFICIAL_PRICES_DEFAULT[OFFICIAL_PRICE_MAP.get(m, "deepseek-flash")]
        n = float(mults.get(m, 1.0)) * tmult
        mr_map[m] = _ratio_from_price(base["in_miss"][tier], n)
        # 审查 R11（P0）：CompletionRatio 是「输出相对输入」的倍数（new-api:
        # completionQuota = out_tokens × CR × MR），与加价倍数 n 无关（n 已含在
        # ModelRatio 里）。原实现把 out 绝对价当倍数写入 → 输出部分只收 7.5%~33%，
        # 纯输出请求安静少收 3~13 倍（flash 实测 26.0%、pro 49.7%）。
        cr_map[m] = round(base["out"][tier] / base["in_miss"][tier], 6)
        # P0-C：缓存命中计价比 = 官方缓存命中价 / 未命中价（与 ModelRatio 同源同时段）。
        # 两个模型比值不同（flash 0.02 / pro ≈0.0333），按 map 逐模型写入；
        # 比值取两个时段中的较小值——宁可少收（按更低的缓存价），绝不多收。
        cache_map[m] = round(min(
            base["in_hit"][0] / base["in_miss"][0],
            base["in_hit"][1] / base["in_miss"][1],
        ), 6)
    try:
        with napi_connect() as nc:
            rows = {r["key"]: r["value"] for r in nc.execute(
                "SELECT key, value FROM options WHERE key IN ('ModelRatio','CompletionRatio','CacheRatio')")}
            try:
                old_mr = json.loads(rows.get("ModelRatio") or "{}")
                old_cr = json.loads(rows.get("CompletionRatio") or "{}")
                old_cache = json.loads(rows.get("CacheRatio") or "{}")
            except json.JSONDecodeError:
                old_mr, old_cr, old_cache = {}, {}, {}
            # 合并：其他模型（可能被共享 new-api 的其他项目配置）原样保留
            merged_mr = dict(old_mr); merged_mr.update(mr_map)
            merged_cr = dict(old_cr); merged_cr.update(cr_map)
            merged_cache = dict(old_cache); merged_cache.update(cache_map)
            if old_mr == merged_mr and old_cr == merged_cr and old_cache == merged_cache:
                _GW_RATIO_CACHE["data"] = (mr_map, cr_map)
                _GW_RATIO_CACHE["ts"] = time.time()
                return RATIOS_UNCHANGED
            for key, val in (("ModelRatio", merged_mr), ("CompletionRatio", merged_cr),
                             ("CacheRatio", merged_cache)):
                if key in rows:
                    nc.execute("UPDATE options SET value=? WHERE key=?", (json.dumps(val), key))
                else:
                    nc.execute("INSERT INTO options(key, value) VALUES(?,?)", (key, json.dumps(val)))
            nc.commit()
    except Exception as e:  # noqa: BLE001
        print("[official-price] 倍率写回失败:", e)
        return RATIOS_FAILURE
    # 提交后 durable 确认（审查 R1/P1-04）：用新连接读已提交生效值，
    # 并校验全部 SITE_MODELS × ModelRatio/CompletionRatio/CacheRatio（P0-C），而非单模型单事务快照。
    try:
        with napi_connect() as nc2:
            rows2 = {r["key"]: r["value"] for r in nc2.execute(
                "SELECT key, value FROM options WHERE key IN ('ModelRatio','CompletionRatio','CacheRatio')")}
        mr_eff = json.loads(rows2.get("ModelRatio") or "{}")
        cr_eff = json.loads(rows2.get("CompletionRatio") or "{}")
        cache_eff = json.loads(rows2.get("CacheRatio") or "{}")
        ok = all(mr_eff.get(m) == mr_map[m] and cr_eff.get(m) == cr_map[m]
                 and cache_eff.get(m) == cache_map[m] for m in SITE_MODELS)
        if not ok:
            print("[official-price] ⚠️ 提交后回读不一致: eff_mr=%s expect=%s cache_eff=%s" % (
                {m: mr_eff.get(m) for m in SITE_MODELS}, mr_map, cache_eff))
    except Exception as e:  # noqa: BLE001
        print("[official-price] 回读确认失败:", e)
        ok = False
    _GW_RATIO_CACHE["data"] = (mr_map, cr_map)
    _GW_RATIO_CACHE["ts"] = time.time()
    print("[official-price] 倍率已按 官方%s价×倍数(时段%s=%g) 重算: %s" % ("峰" if tier else "谷", trule, tmult, mr_map))
    return RATIOS_SUCCESS if ok else RATIOS_FAILURE


def _ratio_refresher():
    """后台线程：官方价每 GW_RATIO_REFRESH_INTERVAL 秒抓取一次；时段倍率/峰谷切换每分钟自检，
    生效组合变化时立即重算写回（时段边界最多延迟 60 秒生效）。"""
    last_fetch = 0.0
    last_key = None
    while True:
        try:
            now = time.time()
            tier = 1 if _is_peak_now() else 0
            tmult, _ = _active_time_mult()
            key = (round(tmult, 6), tier)
            if key != last_key or now - last_fetch >= GW_RATIO_REFRESH_INTERVAL:
                data, ok = fetch_official_prices()
                recompute_ratios(data)
                if ok or last_fetch == 0.0:
                    last_fetch = now
                    # 审查 R8/R9：只有成功（或首次）才推进 key。失败时若也更新，
                    # 下一轮 key==last_key 且未到 INTERVAL → 不再重试，峰谷切换失败
                    # 会以旧时段倍率计费最多 GW_RATIO_REFRESH_INTERVAL(600s)。
                    last_key = key
        except Exception as e:  # noqa: BLE001
            print("[official-price] 后台刷新异常:", e)
        time.sleep(60)


# 网关开启时启动定价同步线程（守护线程，随进程退出）；放在 gw_enabled 定义之后



def _gw_inflight_acquire(uid: int) -> bool:
    """在途请求额度。uid -> [count, last_ts]。

    审查 R5/P2：StreamingResponse 的 gen() 是惰性生成器，若从未被消费，
    其 finally 不会执行（Python 语义：未启动的生成器 close()/GC 都不跑 finally），
    _gw_inflight_release 就永远不会被调用。CAP=3 且原本无 TTL → 漏 3 次即永久拒绝，
    只能重启进程恢复（表现为"重启就好、过几天又不行"）。
    这里按上游 read timeout(300s) + 余量回收僵尸额度，使泄漏可自愈。
    """
    with _GW_LOCK:
        now = time.time()
        ent = _GW_INFLIGHT.get(uid)
        n, ts = (ent[0], ent[1]) if ent else (0, now)
        if n and now - ts > _GW_INFLIGHT_ZOMBIE_SECONDS:
            n = 0                     # 僵尸额度回收，不误杀正常长回答（SSE 持续有 chunk）
        if n >= _GW_INFLIGHT_CAP:
            return False
        _GW_INFLIGHT[uid] = [n + 1, now]
        return True


def _gw_inflight_release(uid: int):
    with _GW_LOCK:
        ent = _GW_INFLIGHT.get(uid)
        n, ts = (ent[0], ent[1]) if ent else (1, time.time())
        _GW_INFLIGHT[uid] = [max(0, n - 1), ts]


def _redeem_rate_limited(uid: int) -> bool:
    now = time.time()
    _prune_attempts(_REDEEM_ATTEMPTS, ttl=60, cap=4096, ts_idx=0)
    win = _REDEEM_ATTEMPTS.get(uid)
    if win is None or now - win[0] >= 60:
        _REDEEM_ATTEMPTS[uid] = [now, 1]
        return False
    win[1] += 1
    return win[1] > GW_REDEEM_RATE_LIMIT


_PAY_ATTEMPTS = {}


def _pay_rate_limited(uid: int) -> bool:
    now = time.time()
    _prune_attempts(_PAY_ATTEMPTS, ttl=60, cap=4096, ts_idx=0)
    win = _PAY_ATTEMPTS.get(uid)
    if win is None or now - win[0] >= 60:
        _PAY_ATTEMPTS[uid] = [now, 1]
        return False
    win[1] += 1
    return win[1] > GW_PAY_RATE_LIMIT


def cny_to_quota(cny: float) -> int:
    return int(round(cny * QUOTA_PER_USD / USD_TO_CNY))


def quota_to_cny(q) -> float:
    return (q or 0) / QUOTA_PER_USD * USD_TO_CNY


def gw_enabled() -> bool:
    return GATEWAY_ENABLED and os.path.exists(NEWAPI_DB)


# 定价同步线程（官方价每 10 分钟刷新 + 峰谷时段自动重算倍率）
if gw_enabled():
    print("[gateway] 计费网关已启用 (new-api %s)" % NEWAPI_BASE)
    threading.Thread(target=_ratio_refresher, daemon=True).start()
    try:
        _check_holiday_freshness()   # R6（审查 P2）：启动即检查节假日表时效（过期红色告警 + 面板标记）
    except Exception:
        pass
else:
    # 审查 R11（P1）：GATEWAY_ENABLED 是模块级常量（import 时求值），启动后再 touch
    # gateway.on 不会生效——照老文档部署会得到「能登录能看界面但聊天全 503」的站，
    # 且启动日志里看不出网关是关的，排查方向会被引向 Key/渠道。第一屏打明状态。
    print("[gateway] 计费网关未启用（gateway.on 不存在且未设 GATEWAY_ENABLED=1）"
          "→ /anthropic 将返回 503；改开关后必须重启进程")


def napi_connect():
    conn = sqlite3.connect(NEWAPI_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _wallet_tx(c, uid, tx_type, cny, balance_cny, note=""):
    c.execute(
        "INSERT INTO wallet_tx(uid,type,cny,balance_cny,note,created_at) VALUES(?,?,?,?,?,?)",
        (uid, tx_type, cny, balance_cny, note, site_now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def gw_provision(user):
    """确保站点用户在 new-api 里有对应的计费用户和令牌，返回 (token_key, napi_uid)

    并发安全：token 以 name=site-<uid> 幂等复用（先查后插），gift 流水按
    wallet_tx 里是否已有 gift 记录去重，gateway_map 用 upsert 收敛。
    """
    uid = user["id"]
    with db() as c:
        row = c.execute("SELECT napi_uid, token_key FROM gateway_map WHERE uid=?", (uid,)).fetchone()
        if row and row["token_key"]:
            return row["token_key"], row["napi_uid"]
    uname = ("site%d_%s" % (uid, re.sub(r"[^0-9A-Za-z_]", "", user["username"])[:16]))[:32]
    gift_quota = cny_to_quota(GW_DEFAULT_GIFT_CNY)
    token_name = "site-%d" % uid
    # new-api 的 users.username 无 UNIQUE 约束，并发开通会产生重复用户行（账务错绑）。
    # 进程内全局锁串行化"查-建"段（单进程部署下等效互斥；多 worker 需换分布式锁）。
    with _GW_PROVISION_LOCK:
        with napi_connect() as nc:
            exist = nc.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
            if not exist:
                nc.execute(
                    "INSERT INTO users(username, password, display_name, role, status, quota) "
                    "VALUES(?,?,?,1,1,?)",
                    (uname, secrets.token_hex(16), user["username"][:20], gift_quota),
                )
            nid = nc.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()[0]
            if exist is None:
                # 防御式复核：同用户名不得多于一行（历史脏数据防御）
                dup = nc.execute("SELECT COUNT(*) FROM users WHERE username=?", (uname,)).fetchone()[0]
                if dup > 1:
                    print("[gateway] 警告: new-api 存在重复用户行 username=%s，请人工合并" % uname)
        # 令牌幂等：同名令牌已存在则复用，避免重复发放孤儿令牌
        trow = nc.execute(
            "SELECT key FROM tokens WHERE user_id=? AND name=? LIMIT 1", (nid, token_name)
        ).fetchone()
        if trow:
            key = trow["key"]
        else:
            key = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(48))
            nc.execute(
                "INSERT INTO tokens(user_id, key, status, name, created_time, accessed_time, "
                "expired_time, remain_quota, unlimited_quota) VALUES(?,?,1,?,?,?,-1,0,1)",
                (nid, key, token_name, int(time.time()), int(time.time())),
            )
        nc.commit()
    with db() as c:
        c.execute(
            "INSERT INTO gateway_map(uid, napi_uid, token_key) VALUES(?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET napi_uid=excluded.napi_uid, token_key=excluded.token_key",
            (uid, nid, key),
        )
        # gift 流水去重：只有首次开通才记一条
        already = c.execute("SELECT 1 FROM wallet_tx WHERE uid=? AND type='gift'", (uid,)).fetchone()
        if not already:
            _wallet_tx(c, uid, "gift", GW_DEFAULT_GIFT_CNY, quota_to_cny(gift_quota), "开通赠送")
    return key, nid


def gw_napi_uid(user) -> int:
    with db() as c:
        row = c.execute("SELECT napi_uid FROM gateway_map WHERE uid=?", (user["id"],)).fetchone()
    return row["napi_uid"] if row else gw_provision(user)[1]


def gw_add_quota(napi_uid: int, delta_quota: int, op_id: str | None = None) -> int:
    """给 new-api 用户加减余额，返回调整后余额（quota）

    单语句原子更新 + IMMEDIATE 事务，防止并发读-改-写丢失更新。
    幂等（审查 P0-1/P0-3）：传 op_id 时，在 new-api 库内以 quota_ops 唯一表
    实现「操作号记账 + 余额变更」同一事务原子提交——进程崩溃要么都没发生、
    要么都已生效，重试绝不会重复加款。
    """
    if not isinstance(delta_quota, int) or delta_quota == 0:
        # delta==0 直接读当前值，避免无意义写库
        with napi_connect() as nc:
            row = nc.execute("SELECT quota FROM users WHERE id=?", (napi_uid,)).fetchone()
        if row is None:
            raise RuntimeError("new-api 用户不存在: %s" % napi_uid)
        return max(0, row["quota"] or 0)
    with napi_connect() as nc:
        nc.execute("BEGIN IMMEDIATE")
        try:
            if op_id:
                nc.execute(
                    "CREATE TABLE IF NOT EXISTS quota_ops("
                    "op_id TEXT PRIMARY KEY, napi_uid INTEGER, delta INTEGER, done_at TEXT)"
                )
                # 历史表迁移：补 payload_hash 列（审查 R1：幂等命中必须校验操作语义）
                cols = [r[1] for r in nc.execute("PRAGMA table_info(quota_ops)").fetchall()]
                if "payload_hash" not in cols:
                    try:
                        nc.execute("ALTER TABLE quota_ops ADD COLUMN payload_hash TEXT")
                    except Exception:  # noqa: BLE001
                        pass
                payload_hash = hashlib.sha256(
                    ("%s|%d" % (napi_uid, delta_quota)).encode()
                ).hexdigest()
                prev = nc.execute(
                    "SELECT napi_uid, delta, payload_hash FROM quota_ops WHERE op_id=?",
                    (op_id,),
                ).fetchone()
                if prev is not None:
                    # 语义校验（审查 R1）：同一 op_id 绑定的 (uid, delta) 必须与本次完全一致；
                    # 不一致 = op_id 被误用/重放，立即报错告警，绝不静默返回"成功"。
                    if prev["napi_uid"] != napi_uid or prev["delta"] != delta_quota:
                        nc.rollback()
                        print(
                            "[quota] ⚠️ op_id=%s 语义冲突: 已记录(uid=%s,delta=%s) "
                            "本次(uid=%s,delta=%s)，拒绝执行并告警"
                            % (op_id, prev["napi_uid"], prev["delta"], napi_uid, delta_quota)
                        )
                        raise RuntimeError(
                            "quota_ops 语义冲突: op_id=%s 已绑定不同的账户或金额，拒绝重复使用" % op_id
                        )
                    # 语义一致：该笔此前已原子生效，绝不再执行（幂等返回当前余额）
                    nc.rollback()
                    row = nc.execute(
                        "SELECT quota FROM users WHERE id=?", (napi_uid,)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("new-api 用户不存在: %s" % napi_uid)
                    print("[quota] op_id=%s 已生效过，跳过重复加款(幂等)" % op_id)
                    return max(0, row["quota"] or 0)
                nc.execute(
                    "INSERT INTO quota_ops(op_id, napi_uid, delta, done_at, payload_hash) "
                    "VALUES(?,?,?,?,?)",
                    (op_id, napi_uid, delta_quota,
                     site_now().strftime("%Y-%m-%d %H:%M:%S"), payload_hash),
                )
            # 负向扣减改为单语句原子条件更新（审查 R1/P1-02）：余额不足整体失败回滚，
            # 禁止 MAX(0,...) 截断——否则实际扣 0 但 quota_ops 记完整负数，账目永久失配。
            if delta_quota < 0:
                upd = nc.execute(
                    "UPDATE users SET quota = COALESCE(quota,0) + ? "
                    "WHERE id=? AND COALESCE(quota,0) + ? >= 0",
                    (delta_quota, napi_uid, delta_quota),
                )
                if upd.rowcount == 0:
                    nc.rollback()
                    raise RuntimeError(
                        "余额不足: 扣减 %d 超过用户 %s 当前余额" % (-delta_quota, napi_uid)
                    )
            else:
                nc.execute(
                    "UPDATE users SET quota = COALESCE(quota,0) + ? WHERE id=?",
                    (delta_quota, napi_uid),
                )
            row = nc.execute("SELECT quota FROM users WHERE id=?", (napi_uid,)).fetchone()
            if row is None:
                raise RuntimeError("new-api 用户不存在: %s" % napi_uid)
            nc.commit()
        except Exception:
            nc.rollback()
            raise
    return max(0, row["quota"] or 0)


@app.get("/api/wallet")
def api_wallet(request: Request):
    user = get_user(request)
    if not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    if not gw_enabled():
        return JSONResponse({"ok": False, "msg": "网关未开启"}, status_code=404)
    _, nid = gw_provision(user)
    with napi_connect() as nc:
        u = nc.execute("SELECT quota, used_quota, request_count FROM users WHERE id=?", (nid,)).fetchone()
        # 审查 R11（P2）：同上——UTC 宿主上今日起点偏 8 小时，显式挂 SITE_TZ。
        today_start = int(datetime.combine(site_now().date(), datetime.min.time(), tzinfo=SITE_TZ).timestamp())
        today = nc.execute(
            "SELECT COALESCE(SUM(quota),0) AS s FROM logs WHERE user_id=? AND type=2 AND created_at>=?",
            (nid, today_start),
        ).fetchone()["s"]
    if u is None:
        return JSONResponse({"ok": False, "msg": "计费账户异常，请联系管理员"}, status_code=502)
    return JSONResponse({
        "ok": True,
        "balance_quota": u["quota"],
        "balance_cny": round(quota_to_cny(u["quota"]), 4),
        "total_used_quota": u["used_quota"],
        "total_used_cny": round(quota_to_cny(u["used_quota"]), 4),
        "request_count": u["request_count"],
        "today_used_quota": today,
        "today_used_cny": round(quota_to_cny(today), 4),
    })


@app.get("/api/wallet/logs")
def api_wallet_logs(request: Request, limit: int = 50):
    user = get_user(request)
    if not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    if not gw_enabled():
        return JSONResponse({"ok": False}, status_code=404)
    nid = gw_napi_uid(user)
    with napi_connect() as nc:
        rows = nc.execute(
            "SELECT created_at, model_name, prompt_tokens, completion_tokens, quota "
            "FROM logs WHERE user_id=? AND type=2 ORDER BY id DESC LIMIT ?",
            (nid, max(1, min(limit, 200))),
        ).fetchall()
    items = [
        {
            "time": r["created_at"],
            "model": r["model_name"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "quota": r["quota"],
            "cny": round(quota_to_cny(r["quota"]), 6),
            "kind": "usage",
        }
        for r in rows
    ]
    # 合并钱包资金流水（充值/赠送/管理员调整）：kind=wallet 时前端显示为资金事件
    _TX_LABEL = {
        "gift": "🎁 注册赠送",
        "redeem": "💰 充值码兑换",
        "adjust": "⚙️ 管理员调整",
        "pay": "💳 扫码充值",
    }
    with db() as c:
        txs = c.execute(
            "SELECT type, cny, note, created_at FROM wallet_tx WHERE uid=? ORDER BY id DESC LIMIT ?",
            (user["id"], max(1, min(limit, 200))),
        ).fetchall()
    for t in txs:
        label = _TX_LABEL.get(t["type"])
        if not label or t["cny"] is None:
            continue  # gen_codes 是系统事件，redeem_pending 未到账，均不展示
        try:
            ts = int(datetime.strptime(str(t["created_at"]), "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            continue
        items.append({
            "time": ts,
            "model": label,
            "prompt_tokens": "",
            "completion_tokens": "",
            "quota": None,
            "cny": round(float(t["cny"]), 4),
            "kind": "wallet",
        })
    items.sort(key=lambda x: x["time"], reverse=True)
    items = items[: max(1, min(limit, 200))]
    return JSONResponse({"ok": True, "logs": items})


@app.post("/api/wallet/redeem")
async def api_wallet_redeem(request: Request):
    user = get_user(request)
    if not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    if not gw_enabled():
        return JSONResponse({"ok": False, "msg": "网关未开启"}, status_code=404)
    if _redeem_rate_limited(user["id"]):
        return JSONResponse({"ok": False, "msg": "尝试过于频繁，请一分钟后再试"}, status_code=429)
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    code = str(body.get("code") or "").strip().upper()
    if not code or len(code) > 32:
        return JSONResponse({"ok": False, "msg": "请输入兑换码"}, status_code=400)
    # ── 兑换 saga（审查 R2/P1-03）：码有持久状态机 unused→processing→used，
    # 加款以稳定业务键 op_id="redeem:<code>" 走 quota_ops 原子幂等；
    # quota_ops 与余额变更同事务提交，故障后查询 quota_ops 可可靠区分
    # 「已提交」/「未提交」——不确定时绝不释放已占码。
    op_id = "redeem:" + code
    uid = user["id"]
    face = None
    with db() as c:
        row = c.execute(
            "SELECT cny, status, used_by FROM redeem_codes WHERE code=?", (code,)
        ).fetchone()
        if row is None or row["cny"] is None or not isinstance(row["cny"], (int, float)) or row["cny"] <= 0:
            return JSONResponse({"ok": False, "msg": "兑换码无效"})
        st = row["status"] or ("used" if row["used_by"] is not None else "unused")
        if st == "used":
            return JSONResponse({"ok": False, "msg": "兑换码无效"})
        if st == "processing":
            # 上次兑换中断：只有占码本人可续跑（恢复任务），他人一律拒绝
            if row["used_by"] != uid:
                return JSONResponse({"ok": False, "msg": "兑换码无效"})
        else:
            # 原子占码：并发下只有一方 rowcount=1（status IS NULL 兼容历史/外部插入行）
            cur = c.execute(
                "UPDATE redeem_codes SET status='processing', used_by=?, used_at=? "
                "WHERE code=? AND (status='unused' OR status IS NULL)",
                (uid, site_now().strftime("%Y-%m-%d %H:%M:%S"), code),
            )
            if cur.rowcount != 1:
                return JSONResponse({"ok": False, "msg": "兑换码无效"})
        face = float(row["cny"])
        # 意图流水（凭据，不删除；ref 唯一幂等，重试不重复记录）
        c.execute(
            "INSERT OR IGNORE INTO wallet_tx(uid,type,cny,balance_cny,note,created_at,ref) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, "redeem_pending", face, None, "兑换码 " + code,
             site_now().strftime("%Y-%m-%d %H:%M:%S"), op_id),
        )
    # ── 阶段二：跨库加款（op_id 幂等；quota_ops 与余额变更同一事务） ──
    newq = None
    try:
        _, nid = gw_provision(user)
        newq = gw_add_quota(nid, cny_to_quota(face), op_id=op_id)
    except Exception as e:  # noqa: BLE001
        print("[wallet] redeem 加款异常 code=%s uid=%s err=%s，检查 quota_ops 判定是否已提交" % (code, uid, e))
        try:
            with napi_connect() as nc:
                hit = nc.execute("SELECT 1 FROM quota_ops WHERE op_id=?", (op_id,)).fetchone()
        except Exception as e2:  # noqa: BLE001
            print("[wallet] redeem 恢复判定查询失败 code=%s err=%s（保持 processing，绝不释放）" % (code, e2))
            hit = None
            # 判定不了 = 不确定：保持占码不释放，等恢复任务/人工核查
            return JSONResponse({"ok": False, "msg": "兑换处理中断，请联系管理员核实（码已锁定防重复到账）"}, status_code=502)
        if hit is not None:
            # 加款其实已提交（异常发生在提交后）：按恢复续跑，补流水+置 used，绝不回滚销码
            print("[wallet] redeem 恢复: 加款已提交(op_id=%s)，续跑收尾" % op_id)
            try:
                with napi_connect() as nc:
                    r = nc.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()
                newq = max(0, r["quota"] or 0) if r else None
            except Exception:
                newq = None
            if newq is None:
                return JSONResponse({"ok": False, "msg": "兑换处理中断，请联系管理员核实（码已锁定防重复到账）"}, status_code=502)
        else:
            # 确认未提交：补偿——码回 unused 可重试；意图流水转 redeem_fail（追加状态，不删除账务凭据）
            try:
                with db() as c:
                    c.execute(
                        "UPDATE redeem_codes SET status='unused', used_by=NULL, used_at=NULL "
                        "WHERE code=? AND status='processing' AND used_by=?",
                        (code, uid),
                    )
                    c.execute(
                        "UPDATE wallet_tx SET type='redeem_fail' WHERE uid=? AND ref=? AND type='redeem_pending'",
                        (uid, op_id),
                    )
            except Exception as e2:  # noqa: BLE001
                print("[wallet] redeem 补偿失败 code=%s err=%s（processing 保留，需人工核查）" % (code, e2))
            return JSONResponse({"ok": False, "msg": "兑换失败，请稍后重试"}, status_code=502)
    # ── 阶段三：到账流水（ref 幂等）+ 码置 used ──
    try:
        with db() as c:
            bal_snap = quota_to_cny(newq) if newq is not None else None
            c.execute(
                "INSERT OR IGNORE INTO wallet_tx(uid,type,cny,balance_cny,note,created_at,ref) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, "redeem", face, bal_snap, "兑换码 " + code,
                 site_now().strftime("%Y-%m-%d %H:%M:%S"), op_id),
            )
            # 意图流水转正（兜底老数据：无 ref 的历史 redeem_pending 按 note 匹配）
            c.execute(
                "UPDATE wallet_tx SET type='redeem', balance_cny=? "
                "WHERE uid=? AND type='redeem_pending' AND (ref=? OR (ref IS NULL AND note=?))",
                (bal_snap, uid, op_id, "兑换码 " + code),
            )
            c.execute("UPDATE redeem_codes SET status='used' WHERE code=?", (code,))
    except Exception as e:  # noqa: BLE001
        print("[wallet] redeem 收尾失败（流水/置used） code=%s err=%s（加款已生效，凭据可对账）" % (code, e))
    return JSONResponse({"ok": True, "balance_cny": round(quota_to_cny(newq), 4)})


async def _gw_proxy(request: Request, method: str):
    """浏览器 → new-api 网关：同源转发，服务端注入计费 Key（不暴露给前端）"""
    user = get_user(request)
    if user is None:
        return JSONResponse({"ok": False}, status_code=401)
    if not gw_enabled():
        return JSONResponse({"error": {"type": "gateway_disabled", "message": "网关未开启"}}, status_code=503)
    if not valid_paid_user(user):
        if user["status"] == "disabled":
            msg = "账号已被禁用，请联系管理员"
        elif user["status"] == "pending":
            msg = "账号待审核，暂不可使用"
        else:
            msg = "账号已过期，请联系管理员续费"
        return JSONResponse(
            {"error": {"type": "expired" if user["status"] == "approved" else "account_" + str(user["status"]),
                       "message": msg}},
            status_code=403,
        )
    path = request.scope["path"].split("/anthropic/v1/", 1)[1]
    # 防路径穿越：拒绝 .. 与空路径
    if not path or any(seg == ".." for seg in path.split("/")):
        return JSONResponse({"error": {"type": "invalid_path", "message": "非法路径"}}, status_code=400)
    key, nid = gw_provision(user)
    body = await request.body()
    if len(body) > GW_BODY_MAX_BYTES:
        return JSONResponse({"error": {"type": "too_large", "message": "请求体过大"}}, status_code=413)
    # 本地余额预检：余额不足直接拒绝（第二道闸，new-api pre-consume 之前挡掉绝大多数超扣）
    with napi_connect() as nc:
        qrow = nc.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()
    if qrow is None:
        return JSONResponse({"error": {"type": "account_error", "message": "计费账户异常，请联系管理员"}}, status_code=502)
    balance = qrow["quota"] or 0
    if balance < cny_to_quota(0.005):
        return JSONResponse(
            {"error": {"type": "insufficient_balance", "message": "钱包余额不足，请充值后再使用"}},
            status_code=402,
        )
    # 站点默认模型改写：用户端不感知也不可选（前端固定发 deepseek-flash，
    # 网关按后台「站点对话模型」配置统一改写，计费倍率随之自动匹配）
    default_model = get_setting(GW_DEFAULT_MODEL_KEY, "deepseek-flash")
    if method == "POST" and default_model:
        try:
            _j = json.loads(body)
            if isinstance(_j, dict) and _j.get("model") != default_model:
                _j["model"] = default_model
                body = json.dumps(_j, ensure_ascii=False).encode()
        except Exception:  # noqa: BLE001
            pass
    # max_tokens 服务端强制钳制（审查 P1-01）：预检估值与实际转发用同一上限，
    # 杜绝「预检按 8192 算、转发 100 万 tokens」绕过防透支边界。
    # 缺省/非法值一律钳为安全默认，负数直接拒绝。
    if method == "POST":
        try:
            _j = json.loads(body)
            if isinstance(_j, dict):
                _mt = _j.get("max_tokens")
                if _mt is None or _mt == "":
                    _j["max_tokens"] = GW_MAX_TOKENS_DEFAULT
                    body = json.dumps(_j, ensure_ascii=False).encode()
                elif isinstance(_mt, bool) or isinstance(_mt, float):
                    # 严格类型（审查 R1/P1-01）：bool/浮点直接拒绝，不得 int() 截断
                    return JSONResponse(
                        {"error": {"type": "invalid_request", "message": "max_tokens 必须为正整数"}},
                        status_code=400,
                    )
                else:
                    if isinstance(_mt, str):
                        if not re.fullmatch(r"\d+", _mt.strip()):
                            return JSONResponse(
                                {"error": {"type": "invalid_request", "message": "max_tokens 必须为正整数"}},
                                status_code=400,
                            )
                        _mt = int(_mt.strip())
                    if not isinstance(_mt, int) or _mt <= 0:
                        return JSONResponse(
                            {"error": {"type": "invalid_request", "message": "max_tokens 必须为正整数"}},
                            status_code=400,
                        )
                    if _mt != _j.get("max_tokens") or _mt > GW_MAX_TOKENS_CAP:
                        _j["max_tokens"] = min(_mt, GW_MAX_TOKENS_CAP)
                        body = json.dumps(_j, ensure_ascii=False).encode()
        except json.JSONDecodeError:
            pass
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "max_tokens 必须为正整数"}},
                status_code=400,
            )
    # 按 max_tokens 与模型倍率粗估请求成本，余额明显不够的请求提前拒绝，收窄并发透支窗口
    try:
        model = str(json.loads(body).get("model") or "")
    except Exception:  # noqa: BLE001
        model = ""
    est = _gw_estimate_quota(body, model)
    if est and balance < est:
        return JSONResponse(
            {"error": {"type": "insufficient_balance", "message": "余额不足以完成本次请求（预估成本超过当前余额），请充值后再试"}},
            status_code=402,
        )
    # 单用户并发上限（第三道闸：封顶在途请求数，限制最坏透支暴露）
    if not _gw_inflight_acquire(user["id"]):
        return JSONResponse(
            {"error": {"type": "rate_limited", "message": "当前请求过多，请等待正在进行的回复完成"}},
            status_code=429,
        )
    target = NEWAPI_BASE + "/v1/" + path
    qs = request.scope.get("query_string", b"")
    if qs:
        target += "?" + qs.decode(errors="replace")
    headers = {
        "Authorization": "Bearer " + key,
        "x-api-key": key,
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "text/event-stream"),
    }
    for h in ("anthropic-version", "anthropic-beta"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    try:
        # P1-14（审查 R1）：同步 requests 必须移出事件循环，避免慢上游阻塞全站
        upstream = await run_in_threadpool(
            http_requests.request,
            method, target, data=body, headers=headers, stream=True, timeout=(10, 300),
        )
    except Exception as e:  # noqa: BLE001
        _gw_inflight_release(user["id"])
        print("[gateway] 上游连接失败 path=%s err=%s" % (path, e))
        return JSONResponse(
            {"error": {"type": "upstream_error", "message": "计费网关暂时不可用，请稍后重试"}},
            status_code=502,
        )

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=2048):
                if chunk:
                    yield chunk
        except Exception as e:  # noqa: BLE001
            # 流中断时发一个 SSE 错误帧，避免前端只收到被掐断的连接
            try:
                yield (
                    "event: error\ndata: " + json.dumps(
                        {"type": "error", "error": {"type": "upstream_error", "message": "网关读取上游失败，本次请求已中断"}}
                    ) + "\n\n"
                ).encode("utf-8")
                print("[gateway] 流转发中断 path=%s err=%s" % (path, e))
            except Exception:  # noqa: BLE001
                pass
        finally:
            upstream.close()
            _gw_inflight_release(user["id"])
    return StreamingResponse(
        gen(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@app.post("/anthropic/v1/{path:path}")
async def gw_anthropic_post(path: str, request: Request):
    return await _gw_proxy(request, "POST")


@app.get("/anthropic/v1/{path:path}")
async def gw_anthropic_get(path: str, request: Request):
    return await _gw_proxy(request, "GET")


@app.delete("/anthropic/v1/{path:path}")
async def gw_anthropic_delete(path: str, request: Request):
    return await _gw_proxy(request, "DELETE")


@app.put("/anthropic/v1/{path:path}")
async def gw_anthropic_put(path: str, request: Request):
    return await _gw_proxy(request, "PUT")


# ── 扫码充值（用户提交 → 管理员审核到账；卡密兑换并行保留） ──
def _pay_dup_response(dup):
    """幂等命中原订单的统一响应。
    审查 R7/P2：status=rejected 时明确告知「已驳回」，避免用户误以为新申请已提交；
    仍返回 ok=True 让前端按约定清掉 crid 键——下次提交自动生成新幂等键 → 新订单，
    不会被旧键锁死。"""
    msg = "该充值申请已存在（原订单 #%s），请勿重复提交" % dup["id"]
    if dup["status"] == "rejected":
        msg = "原充值订单 #%s 已被驳回，如需充值请重新提交" % dup["id"]
    return JSONResponse({
        "ok": True, "order_id": dup["id"], "cny": dup["cny"],
        "status": dup["status"], "created_at": dup["created_at"], "msg": msg,
    })


@app.post("/api/wallet/pay-request")
async def api_wallet_pay_request(request: Request):
    user = get_user(request)
    if not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    if not gw_enabled():
        return JSONResponse({"ok": False, "msg": "网关未开启"}, status_code=404)
    if _pay_rate_limited(user["id"]):
        return JSONResponse({"ok": False, "msg": "提交过于频繁，请一分钟后再试"}, status_code=429)
    try:
        body = json.loads(await request.body())
        amount = round(float(body.get("cny")), 2)
        # 客户端幂等键（P0-4，审查 R1 复审）：强制要求合法 client_request_id，
        # 缺失/非法一律 400 拒绝——不得降级为无幂等键创建（否则旧客户端/隐私模式/直接调 API 仍可重复下单）
        crid = str(body.get("client_request_id") or "").strip()
        if not re.fullmatch(r"[0-9a-zA-Z\-]{8,64}", crid):
            return JSONResponse(
                {"ok": False, "msg": "缺少有效的客户端幂等键（client_request_id），请刷新页面后重试"},
                status_code=400,
            )
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    if not math.isfinite(amount) or not (GW_PAY_MIN_CNY <= amount <= GW_PAY_MAX_CNY):
        return JSONResponse(
            {"ok": False, "msg": "充值金额需为 ¥%g ~ ¥%g 之间的数字" % (GW_PAY_MIN_CNY, GW_PAY_MAX_CNY)},
            status_code=400,
        )
    with db() as c:
        # 幂等 1：同用户同 client_request_id 已有订单 → 直接返回原订单（重试/双击不会建新单）
        # 金额指纹校验（审查 R1）：同 key 不同金额 = 参数冲突，返回 409 而非静默返回原订单
        dup_rows = c.execute(
            "SELECT id, cny, status, created_at FROM pay_orders "
            "WHERE uid=? AND client_request_id=?", (user["id"], crid),
        ).fetchall()
        if dup_rows:
            dup = dup_rows[0]
            if abs(round(float(dup["cny"]), 2) - amount) > 1e-9:
                print("[pay] ⚠️ 幂等键金额冲突 crid=%s 原单¥%s 本次¥%s" % (crid[:8], dup["cny"], amount))
                return JSONResponse(
                    {"ok": False, "msg": "该幂等键已绑定其他金额（¥%s），请刷新页面重新发起" % dup["cny"]},
                    status_code=409,
                )
            print("[pay] 幂等命中 crid=%s 返回原订单 #%s status=%s" % (crid[:8], dup["id"], dup["status"]))
            return _pay_dup_response(dup)
        pending = c.execute(
            "SELECT COUNT(*) AS n FROM pay_orders WHERE uid=? AND status='pending'", (user["id"],)
        ).fetchone()["n"]
        if pending >= GW_PAY_MAX_PENDING:
            return JSONResponse(
                {"ok": False, "msg": "您有多笔充值待确认，请等待管理员处理后再提交"}, status_code=429)
        # 幂等 2：唯一索引 (uid, client_request_id) 兜底并发提交，撞唯一键返回既有订单（含金额校验）
        try:
            c.execute(
                "INSERT INTO pay_orders(uid, username, cny, status, created_at, client_request_id) "
                "VALUES(?,?,?,'pending',?,?)",
                (user["id"], user["username"], amount,
                 site_now().strftime("%Y-%m-%d %H:%M:%S"), crid),
            )
        except sqlite3.IntegrityError:
            dup = c.execute(
                "SELECT id, cny, status, created_at FROM pay_orders "
                "WHERE uid=? AND client_request_id=?", (user["id"], crid),
            ).fetchone()
            if dup is None:
                raise
            if abs(round(float(dup["cny"]), 2) - amount) > 1e-9:
                return JSONResponse(
                    {"ok": False, "msg": "该幂等键已绑定其他金额（¥%s），请刷新页面重新发起" % dup["cny"]},
                    status_code=409,
                )
            return _pay_dup_response(dup)
        oid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    print("[pay] 新充值申请 #%s user=%s ¥%s" % (oid, user["username"], amount))
    return JSONResponse({"ok": True, "order_id": oid, "cny": amount,
                         "msg": "已提交，请扫码付款后等待管理员确认到账"})


@app.get("/api/wallet/pay-orders")
async def api_wallet_pay_orders(request: Request):
    user = get_user(request)
    if not valid_paid_user(user):
        return JSONResponse({"ok": False}, status_code=403)
    with db() as c:
        rows = c.execute(
            "SELECT id, cny, status, created_at, handled_at FROM pay_orders "
            "WHERE uid=? ORDER BY id DESC LIMIT 5", (user["id"],)
        ).fetchall()
    return JSONResponse({
        "ok": True,
        "min_cny": GW_PAY_MIN_CNY,
        "has_qr": os.path.exists(QR_PATH),
        "orders": [dict(r) for r in rows],
    })


@app.get("/pay-qr")
def pay_qr_image(request: Request):
    """收款二维码（需登录；管理员在面板上传后生效）"""
    user = get_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if os.path.exists(QR_PATH):
        mime = "image/png"
        try:
            with open(QR_PATH + ".type") as f:
                mime = f.read().strip() or mime
        except Exception:  # noqa: BLE001
            pass
        return FileResponse(QR_PATH, media_type=mime)
    return JSONResponse({"ok": False, "msg": "暂未配置收款码"}, status_code=404)


GW_ADJUST_MAX_CNY = 100000  # 单次后台调整上限，防误操作/注入异常值


@app.post("/admin/wallet/adjust")
async def admin_wallet_adjust(request: Request, uid: int = Form(...), amount: str = Form(""),
                              note: str = Form(""), op_id: str = Form("")):
    """调账 saga（审查 P0-3）：加款与流水都有持久化操作号凭据。
    - 加款：quota_ops 唯一表（adjust:<op_id>），同事务原子生效，重试绝不重复加款
    - 流水：wallet_tx.ref='adjust:<op_id>' 唯一索引，加款成功后流水可安全重写
    - 页面失败时回传同一 op_id，管理员重试自动续跑未完成步骤，不会重复加款"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    if not op_id or len(op_id) > 64 or not re.fullmatch(r"[0-9a-f\-]{8,64}", op_id):
        op_id = uuid.uuid4().hex
    try:
        amount_cny = round(float(amount), 2)
        if not math.isfinite(amount_cny) or abs(amount_cny) > GW_ADJUST_MAX_CNY or amount_cny == 0:
            raise ValueError("金额非法")
        delta = cny_to_quota(amount_cny)
        with db() as c:
            u = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise ValueError("用户不存在")
        fake_user = {"id": uid, "username": u["username"]}
        _, nid = gw_provision(fake_user)
        if delta < 0:
            # 扣减不得超过当前余额，否则 MAX(0,...) 截断会让流水与真实余额永久失配
            with napi_connect() as nc:
                cur = nc.execute("SELECT quota FROM users WHERE id=?", (nid,)).fetchone()
            if cur is None or (cur["quota"] or 0) + delta < 0:
                raise ValueError("扣减超过当前余额")
        # 步骤1：幂等加款（quota_ops 事务保证本 op 只生效一次）
        newq = gw_add_quota(nid, delta, op_id="adjust:%s" % op_id)
        # 步骤2：流水（ref 唯一；已存在说明上次已写完，幂等跳过）
        with db() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO wallet_tx(uid,type,cny,balance_cny,note,created_at,ref) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, "adjust", amount_cny, quota_to_cny(newq),
                 (note or ("充值" if amount_cny > 0 else "扣减"))[:100],
                 site_now().strftime("%Y-%m-%d %H:%M:%S"), "adjust:%s" % op_id),
            )
            if cur.rowcount == 0:
                # 流水已存在但余额快照未回填（上次在此处崩溃）→ 补齐
                c.execute(
                    "UPDATE wallet_tx SET balance_cny=? WHERE ref=? AND balance_cny IS NULL",
                    (quota_to_cny(newq), "adjust:%s" % op_id),
                )
    except Exception as e:  # noqa: BLE001
        # 失败必须让管理员看见，绝不静默吞掉；回传同一 op_id 保证重试幂等
        print("[wallet] adjust 失败 uid=%s amount=%s op=%s err=%s" % (uid, amount, op_id, e))
        return RedirectResponse("/admin?wallet_err=1&op_id=" + op_id, status_code=303)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/wallet/gen-codes")
async def admin_wallet_gen_codes(request: Request, cny: str = Form(""), count: str = Form("")):
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    try:
        face = round(float(cny), 2)
        n = int(count)
        if not math.isfinite(face) or face < 0.01 or face > 10000:
            raise ValueError("面值非法")
        n = max(1, min(n, 50))
    except ValueError:
        return RedirectResponse("/admin?wallet_err=code", status_code=303)
    made = []
    with db() as c:
        for _ in range(n):
            # 撞码重试（码空间 42.9 亿，实际几乎不会发生）
            for _attempt in range(5):
                # 26 位大写字母+数字组合（剔除易混淆的 I/O/0/1）；
                # redeem 端有 .upper() 归一化，用户小写输入也能匹配
                _alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
                code = "".join(secrets.choice(_alphabet) for _ in range(26))
                cur = c.execute(
                    "INSERT OR IGNORE INTO redeem_codes(code, cny, created_at, status) VALUES(?,?,?,'unused')",
                    (code, face, site_now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                if cur.rowcount == 1:
                    made.append(code)
                    break
            else:
                print("[wallet] gen-codes 撞码重试耗尽")
        _wallet_tx(c, 0, "gen_codes", face * len(made), None, "生成 %d 张 ¥%g 充值码" % (len(made), face))
    # 充值码不进 URL（审查 P1-08）：URL 会进浏览器历史/访问日志/Referer，改为一次性 POST 响应页展示
    rows = "".join(
        '<tr><td class="num2" style="font-family:monospace;user-select:all">%s</td>'
        '<td class="ta-r money">¥%g</td></tr>' % (html.escape(code), face)
        for code in made
    )
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>充值码生成成功</title>'
        '<body style="font-family:system-ui;max-width:640px;margin:48px auto;padding:0 16px">'
        '<h2>✅ 已生成 %d 张充值码（¥%g/张）</h2>'
        '<p class="dim">本页只显示一次，请立即复制保存；不要通过聊天/截图以外的方式泄露。</p>'
        '<table border="0" cellpadding="8" style="border-collapse:collapse;width:100%%">'
        '<tr style="color:#64748b"><td>充值码</td><td style="text-align:right">面值</td></tr>%s</table>'
        '<p><a href="/admin">← 返回管理面板</a></p></body>' % (len(made), face, rows)
    )


_RATIO_MODEL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_MULT_MIN, _MULT_MAX = 0.1, 100.0    # 加价倍数允许范围


@app.post("/admin/wallet/ratios")
async def admin_wallet_ratios(request: Request, model: str = Form(""), mult: str = Form("1")):
    """官方价 × 倍数 定价：保存某模型的加价倍数并立即重算倍率写回 new-api"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    model = model.strip()
    try:
        if not _RATIO_MODEL_RE.match(model):
            raise ValueError("模型名非法")
        n = round(float(mult), 4)
        if not math.isfinite(n) or not (_MULT_MIN <= n <= _MULT_MAX):
            raise ValueError("倍数超出允许范围 (0.1, 100]")
    except ValueError:
        return RedirectResponse("/admin?ratio_err=1", status_code=303)
    mults = get_multipliers()
    mults[model] = n
    set_setting(GW_MULTIPLIER_KEY, json.dumps(mults))
    st = recompute_ratios()
    print("[wallet] 定价倍数更新 model=%s mult=%s 重算=%s" % (model, n, st))
    if st == RATIOS_FAILURE:
        # 写库失败：明确报错并保留可重试状态（倍数已存站点库，重试只需重算）
        return RedirectResponse("/admin?ratio_err=write", status_code=303)
    return RedirectResponse("/admin?ratio_ok=1", status_code=303)


@app.post("/admin/wallet/time-mult")
async def admin_wallet_time_mult(request: Request, name: str = Form(""), start: str = Form(""),
                                 end: str = Form(""), mult: str = Form("1")):
    """新增时段倍率规则：该时间段内 全体模型 最终售价 = 官方价 × 模型倍数 × 该时段倍数"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    name, start, end = name.strip()[:20], start.strip(), end.strip()
    try:
        if not _TIME_RE.match(start) or not _TIME_RE.match(end):
            raise ValueError("时间格式非法")
        n = round(float(mult), 4)
        if not math.isfinite(n) or not (_MULT_MIN <= n <= _MULT_MAX):
            raise ValueError("倍数超出允许范围")
        tm = get_time_mults()
        if len(tm["rules"]) >= _TIME_MAX_RULES:
            raise ValueError("full")
        tm["rules"].append({"name": name, "start": start, "end": end, "mult": n})
        set_setting(GW_TIME_MULT_KEY, json.dumps(tm, ensure_ascii=False))
    except ValueError as e:
        return RedirectResponse("/admin?tm_err=%s" % ("full" if str(e) == "full" else "1"), status_code=303)
    if recompute_ratios() == RATIOS_FAILURE:
        return RedirectResponse("/admin?tm_err=write", status_code=303)
    print("[wallet] 时段倍率新增 %s %s-%s ×%g" % (name or "未命名", start, end, n))
    return RedirectResponse("/admin?tm_ok=1", status_code=303)


@app.post("/admin/wallet/time-mult/del")
async def admin_wallet_time_mult_del(request: Request, idx: int = Form(...)):
    """删除时段倍率规则（按序号）"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    tm = get_time_mults()
    if not (0 <= idx < len(tm["rules"])):
        return RedirectResponse("/admin?tm_err=1", status_code=303)
    removed = tm["rules"].pop(idx)
    set_setting(GW_TIME_MULT_KEY, json.dumps(tm, ensure_ascii=False))
    if recompute_ratios() == RATIOS_FAILURE:
        return RedirectResponse("/admin?tm_err=write", status_code=303)
    print("[wallet] 时段倍率删除 %s %s-%s" % (removed.get("name"), removed.get("start"), removed.get("end")))
    return RedirectResponse("/admin?tm_ok=1", status_code=303)


@app.post("/admin/wallet/time-mult/default")
async def admin_wallet_time_mult_default(request: Request, mult: str = Form("1")):
    """设置默认时段倍数（无规则命中时段的价格倍数）"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    try:
        n = round(float(mult), 4)
        if not math.isfinite(n) or not (_MULT_MIN <= n <= _MULT_MAX):
            raise ValueError("倍数超出允许范围")
    except ValueError:
        return RedirectResponse("/admin?tm_err=1", status_code=303)
    tm = get_time_mults()
    tm["default"] = n
    set_setting(GW_TIME_MULT_KEY, json.dumps(tm, ensure_ascii=False))
    if recompute_ratios() == RATIOS_FAILURE:
        return RedirectResponse("/admin?tm_err=write", status_code=303)
    print("[wallet] 默认时段倍数设为 %g" % n)
    return RedirectResponse("/admin?tm_ok=1", status_code=303)


@app.post("/admin/wallet/refresh-official")
async def admin_wallet_refresh_official(request: Request):
    """手动刷新官方实时价格并重算倍率"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    data, ok = fetch_official_prices()
    st = recompute_ratios(data)
    return RedirectResponse("/admin?official_ok=%d&ratio_st=%s" % (1 if ok else 0, st), status_code=303)


@app.post("/admin/wallet/model")
async def admin_wallet_model(request: Request, model: str = Form("")):
    """站点对话模型（用户无感知，网关统一改写）"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    model = model.strip()
    if model not in SITE_MODELS:
        return RedirectResponse("/admin?model_err=1", status_code=303)
    set_setting(GW_DEFAULT_MODEL_KEY, model)
    print("[wallet] 站点模型切换为", model)
    return RedirectResponse("/admin?model_ok=1", status_code=303)


@app.post("/admin/wallet/pay")
async def admin_wallet_pay(request: Request, oid: int = Form(...), action: str = Form("")):
    """充值审批 saga（审查 P0-1）：pending →(原子占单)→ processing →(幂等加款+流水)→ approved
    - 占单：pending→processing 单语句原子，并发点两次只有一个进入 saga
    - 加款：quota_ops 唯一表（pay:<oid>）与余额变更同一事务原子提交，崩溃/重试绝不重复加款
    - 流水：wallet_tx.ref='pay:<oid>' 唯一索引，流水与到账可逐笔对账
    - 任何一步失败订单保持 processing，管理员再次点「通过」自动从未完成步骤续跑
    - 拒绝：仅 pending 可拒，processing 必须先完成或走加款恢复"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    if action not in ("approve", "reject"):
        return RedirectResponse("/admin?pay_err=1", status_code=303)
    try:
        with db() as c:
            row = c.execute("SELECT * FROM pay_orders WHERE id=?", (oid,)).fetchone()
            if row is None:
                raise ValueError("订单不存在")
            if action == "reject":
                # 原子占单：并发点两次通过时只有一个成功
                cur = c.execute(
                    "UPDATE pay_orders SET status='rejected', handled_at=? WHERE id=? AND status='pending'",
                    (site_now().strftime("%Y-%m-%d %H:%M:%S"), oid),
                )
                if cur.rowcount != 1:
                    raise ValueError("订单已被处理")
            else:
                # 原子占单：pending→processing；已是 processing 说明上次 saga 未完成 → 续跑
                cur = c.execute(
                    "UPDATE pay_orders SET status='processing', handled_at=? WHERE id=? AND status IN ('pending','processing')",
                    (site_now().strftime("%Y-%m-%d %H:%M:%S"), oid),
                )
                if cur.rowcount != 1:
                    raise ValueError("订单已被处理")
            row = c.execute("SELECT * FROM pay_orders WHERE id=?", (oid,)).fetchone()
        if action == "approve":
            fake_user = {"id": row["uid"], "username": row["username"]}
            _, nid = gw_provision(fake_user)
            # 步骤1：幂等加款（pay:<oid> 在 quota_ops 唯一，事务内原子生效）
            newq = gw_add_quota(nid, cny_to_quota(row["cny"]), op_id="pay:%s" % oid)
            # 步骤2：流水（ref 唯一；已存在说明上次已写完，幂等跳过）
            with db() as c:
                cur = c.execute(
                    "INSERT OR IGNORE INTO wallet_tx(uid,type,cny,balance_cny,note,created_at,ref) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (row["uid"], "pay", row["cny"], quota_to_cny(newq),
                     "扫码充值 #%s" % oid, site_now().strftime("%Y-%m-%d %H:%M:%S"), "pay:%s" % oid),
                )
                if cur.rowcount == 0:
                    c.execute(
                        "UPDATE wallet_tx SET balance_cny=? WHERE ref=? AND balance_cny IS NULL",
                        (quota_to_cny(newq), "pay:%s" % oid),
                    )
                # saga 收尾
                c.execute(
                    "UPDATE pay_orders SET status='approved', handled_at=? WHERE id=? AND status='processing'",
                    (site_now().strftime("%Y-%m-%d %H:%M:%S"), oid),
                )
        print("[pay] 订单 #%s %s ¥%s user=%s" % (oid, action, row["cny"], row["username"]))
    except Exception as e:  # noqa: BLE001
        print("[pay] 审批失败 oid=%s err=%s" % (oid, e))
        return RedirectResponse("/admin?pay_err=1", status_code=303)
    return RedirectResponse("/admin?pay_ok=1", status_code=303)


@app.post("/admin/wallet/qr")
async def admin_wallet_qr(request: Request, file: UploadFile = File(None)):
    """上传站长收款二维码（png/jpg，≤2MB）"""
    if not is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    if file is None or not file.filename:
        return RedirectResponse("/admin?qr_err=1", status_code=303)
    data = await file.read()
    if len(data) > 2 * 1024 * 1024 or not data:
        return RedirectResponse("/admin?qr_err=size", status_code=303)
    kind = (file.content_type or "").lower()
    if not kind.startswith("image/"):
        return RedirectResponse("/admin?qr_err=type", status_code=303)
    # 魔数校验：只收 png/jpeg，防止伪装成图片的任意文件
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpg = data[:3] == b"\xff\xd8\xff"
    if not (is_png or is_jpg):
        return RedirectResponse("/admin?qr_err=type", status_code=303)
    with open(QR_PATH, "wb") as f:
        f.write(data)
    os.chmod(QR_PATH, 0o644)
    with open(QR_PATH + ".type", "w") as f:
        f.write("image/png" if is_png else "image/jpeg")
    print("[pay] 收款码已更新 %d bytes (%s)" % (len(data), kind))
    return RedirectResponse("/admin?qr_ok=1", status_code=303)


# ---------- 门禁 + 静态托管（必须放在最后） ----------
@app.get("/{full_path:path}")
def gate(full_path: str, request: Request):
    if full_path in ("favicon.ico", "robots.txt"):
        return Response(status_code=204)
    # 审查 R11（P2）：未注册的 /api/* 原本落到这里返回 index.html（200 text/html），
    # 前端 res.json() 直接抛异常、日志里也看不到 404。接口路径统一返回 JSON 404。
    if full_path.startswith("api/") or full_path.startswith("admin/"):
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)
    user = get_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["status"] == "approved" and is_expired(user):
        # 账号过期：立马踢出到登录页并清除登录态，同时登录接口会拒绝过期账号，不会死循环
        resp = RedirectResponse("/login?expired=1", status_code=303)
        resp.delete_cookie("session", path="/")
        return resp
    if not valid_paid_user(user):
        return HTMLResponse(status_page_html(user))
    resp = serve_static(full_path or "index.html")
    # 滑动续期：每次打开网站都把登录态再续 30 天。
    # 活跃用户只要 30 天内来过一次就永远不会被要求重新登录。
    # 审查 R11（P2）：只对 HTML 导航续期。给 /static/js|css（Cache-Control: public,
    # max-age=2592000, immutable）也塞 Set-Cookie，会让 CDN/浏览器放弃长缓存（实测每个
    # 静态资源都带 172 字节 Set-Cookie）；若前置缓存配置了 proxy_ignore_headers Set-Cookie，
    # 还会把 A 的会话发给 B。静态资源不需要续期，导航请求续期足够。
    if (resp.headers.get("content-type") or "").startswith("text/html"):
        resp.set_cookie(
            "session", sign({"uid": user["id"], "exp": time.time() + SESSION_DAYS * 86400}),
            max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/",
            secure=_req_secure(request),
        )
    return resp
