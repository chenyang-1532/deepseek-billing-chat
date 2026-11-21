# -*- coding: utf-8 -*-
"""前端真浏览器 UI 冒烟测试（审查建议任务，2026-11-21 R20 签字后）

范围（审查验收清单 A-D）：
  B 用户侧：注册（正常/重复/限流429文案）、登录（错误密码×5/正常）、
            聊天页流式渲染→正常收尾、多轮对话、会话列表新建/切换、
            云同步双设备端到端（R15 P1 的真浏览器回归）、钱包页可达
  C 管理侧：/admin 表单登录、状态页渲染、审批真实点按（pending→通过→用户可登录）、
            告警横幅写入→展示→清除→消失
  D 质量断言：每个场景 console 无 error（预期内 429 除外）、全程零 5xx、
            移动端视口 375×667 关键页可达且无横向滚动条

约定：
  - playwright headless chromium；浏览器/playwright 不可用 → [SKIP] 退出码 3（不 FAIL）
  - 上游为 tests/mock_upstream.py（:3002），不依赖真实 Key
  - 账号铁律：全部 rttest 前缀，RT_TAG 进用户名，run_all 收尾统一清理
  - 截图证据：/tmp/ui_smoke_shots/（不进仓库）
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/tmp/chatsite-test")
from _guard import guard  # noqa: E402

BASE = "http://127.0.0.1:3000"
SITE_DIR = "/tmp/chatsite-test"
SITE_DB = SITE_DIR + "/data.db"
NAPI = "/opt/newapi/one-api.db"
TAG = os.environ.get("RT_TAG", "uismoke")
ADMIN_PW = os.environ.get("ADMIN_PW", "")
SHOTS = "/tmp/ui_smoke_shots"

guard(SITE_DB, NAPI)

# ── SKIP 探测：playwright / chromium / 三服务任一不可用即 SKIP（exit 3），绝不 FAIL ──
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[SKIP] playwright 未安装（pip3 install playwright && playwright install chromium），未实际执行")
    sys.exit(3)

def _services_up():
    import urllib.error
    try:
        urllib.request.urlopen(BASE + "/login", timeout=3)
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:3002/v1/messages", data=b'{"messages":[]}',
            headers={"content-type": "application/json"}, method="POST"), timeout=3)
        return True
    except Exception:
        return False

if not os.path.exists(SITE_DIR + "/gateway.on") or not _services_up():
    print("[SKIP] 站点/网关/mock 上游未就绪（需 :3000 网关开启 + :3002 mock），未实际执行")
    sys.exit(3)

results = []
def ck(name, cond, extra=None):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % (extra,)))

U_S = "rttest%s_uis" % TAG      # 聊天/云同步用户（API 造号）
U_C = "rttest%s_uic" % TAG      # 注册UI→审批链路用户（UI 造号）
PWD = "uismoke123"
MARK_A = "UISMK-A-%d" % int(time.time())
MARK_B = "UISMK-B-%d" % int(time.time())

os.makedirs(SHOTS, exist_ok=True)

# ── HTTP 造号工具（仅用于准备 S 用户；C 用户走 UI 真实注册） ──
class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def prepare_user_s():
    """API 注册 + 直改库过审 + 后台调账 ¥50（同 gw_e2e prepare 模式）"""
    import http.cookiejar
    req = urllib.request.Request(BASE + "/api/register",
                                 data=json.dumps({"u": U_S, "p": PWD}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
    c = sqlite3.connect(SITE_DB, timeout=15)
    uid = c.execute("SELECT id FROM users WHERE username=?", (U_S,)).fetchone()[0]
    c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U_S,))
    c.commit(); c.close()
    jar = http.cookiejar.CookieJar()
    adm = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NR())

    def adm_post(path, data):
        # _NR 把 303 也抛成 HTTPError——管理端登录/调账成功都返回 303，视为正常
        try:
            adm.open(urllib.request.Request(BASE + path, data=data.encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=10)
            return True
        except urllib.error.HTTPError as e:
            return e.code in (301, 302, 303)
        except Exception as e:  # noqa: BLE001
            print("[prep] %s 异常: %s" % (path, e))
            return False

    ok_login = adm_post("/admin/login", "pw=%s" % ADMIN_PW)
    ok_adj = adm_post("/admin/wallet/adjust", "uid=%d&amount=50&note=ui_smoke准备" % uid) if ok_login else False
    if not ok_adj:
        print("[prep] ⚠️ 调账未确认成功（聊天用例可能依赖账号初始额度）")
    print("准备 rttest 用户 uid=%d（¥50：登录=%s 调账=%s）" % (uid, ok_login, ok_adj))
    return uid

def cleanup_all():
    """按 RT_TAG 模式清理本套件全部 rttest 残留（同 run_all 收尾语义，独立跑也自洽）"""
    pat = "%%rttest%%%s%%" % TAG
    c = sqlite3.connect(SITE_DB, timeout=15)
    uids = [r[0] for r in c.execute("SELECT id FROM users WHERE username LIKE ?", (pat,)).fetchall()]
    for tbl in ("cloud_sessions", "gateway_map", "wallet_tx"):
        c.execute("DELETE FROM %s WHERE uid IN (%s)" % (tbl, ",".join(map(str, uids)) or "0"))
    c.execute("DELETE FROM users WHERE username LIKE ?", (pat,))
    c.commit(); c.close()
    print("cleanup: %d 个 rttest 用户及关联数据已清（napi 侧由 run_all 收尾兜底）" % len(uids))

def save_shot(page, name):
    try:
        page.screenshot(path="%s/%s.png" % (SHOTS, name), full_page=False)
    except Exception:  # noqa: BLE001
        pass

# ── 质量断言采集器：console error / JS 未捕获异常 / 5xx 响应 ──
class Quality:
    def __init__(self):
        self.console_errors = []   # (scenario, text)
        self.page_errors = []      # (scenario, text)
        self.resp_5xx = []         # (scenario, url, status)

    def attach(self, page, scenario):
        page.on("console", lambda m, s=scenario: (
            self.console_errors.append((s, m.text)) if m.type == "error" else None))
        page.on("pageerror", lambda e, s=scenario: self.page_errors.append((s, str(e))))
        page.on("response", lambda r, s=scenario: (
            self.resp_5xx.append((s, r.url, r.status)) if r.status >= 500 else None))

    def report(self):
        # 预期内 4xx：429 烧桶触发（故意）。其余 console error / 任意 pageerror / 任意 5xx 都是问题
        unexpected = [(s, t) for s, t in self.console_errors
                      if "429" not in t and "过于频繁" not in t]
        return unexpected, self.page_errors, self.resp_5xx

Q = Quality()

INIT_SCRIPT = ("localStorage.setItem('chat-demo:locale','zh-CN');"
               "localStorage.removeItem('chat-demo:api-key')")

def ui_login(page, u, pw):
    """登录页表单真实提交，等待进入聊天页（已登录被 /login 重定向时直接返回）"""
    page.goto(BASE + "/login", wait_until="networkidle")
    if page.locator("#li-user").count() == 0:
        return  # 已有登录态，/login 303 回聊天页
    page.fill("#li-user", u)
    page.fill("#li-pw", pw)
    page.click("#pane-login button")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    for _ in range(3):
        if page.locator("[role=dialog]").count():
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        else:
            break

def show_reg_pane(page):
    """注册成功后 doReg 会 sw(0) 切回登录页——每次提交前确保注册面板可见"""
    if not page.locator("#pane-reg").is_visible():
        page.locator("#tab-reg").click()
        page.wait_for_timeout(300)

def open_sync(page):
    """设置 → 数据管理 → 点『同步聊天记录』按钮（注意：不是同名文字 label），等待结果"""
    page.locator('button:has-text("设置")').first.click()
    page.wait_for_timeout(1200)
    page.locator('text=数据管理').first.click()
    page.wait_for_timeout(600)
    page.locator('button:has-text("同步聊天记录")').first.click()
    ok_flag = False
    for _ in range(40):
        page.wait_for_timeout(500)
        body = page.evaluate("() => (document.querySelector('[class*=dialog]')||document.body).textContent||''")
        if "同步完成" in body:
            ok_flag = True
            break
        if "同步失败" in body:
            return False
    save_shot(page, "sync")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    return ok_flag

def send_and_wait_reply(page, text, expect_frag, timeout_s=45):
    """personaChip 会话内发送消息，等待流式回复出现（mock 回复特征串）"""
    ta = page.locator("textarea").first
    ta.click(); ta.fill(text)
    page.locator("[class*=sendButton]").first.click()
    for _ in range(timeout_s * 2):
        page.wait_for_timeout(500)
        body = page.evaluate("() => (document.body.textContent||'')")
        if expect_frag in body:
            return True
        if "余额不足" in body or "额度" in body:
            return False
    return False

def sidebar_open_conv(page, kw):
    """确保边栏展开，在会话列表（[class*=list]）中点击标题含 kw 的会话"""
    ham = page.locator('[aria-label=打开边栏]')
    if ham.count():
        ham.first.click()
        page.wait_for_timeout(800)
    items = page.locator('[class*=list] >> text=%s' % kw)
    if not items.count():
        # 调试：列表项标题可能不含消息原文——dump 出来便于诊断
        titles = page.evaluate(
            "() => JSON.stringify(Array.from(document.querySelectorAll('[class*=list] *'))"
            ".filter(e=>e.children.length===0&&(e.textContent||'').trim()).map(e=>e.textContent.trim().slice(0,24)).slice(0,10))")
        print("  [diag] 会话列表项:", titles)
        items = page.locator('[class*=list] [class*=item], [class*=list] [class*=conv]')
        if not items.count():
            return False
    items.first.click()
    page.wait_for_timeout(1500)
    return kw in page.evaluate("() => (document.body.textContent||'')")

def no_horiz_scroll(page):
    return page.evaluate(
        "() => Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0)"
        " <= document.documentElement.clientWidth + 1")

def main():
    uid_s = prepare_user_s()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

            # ════ B1 注册页（UI 真实表单） ════
            ctxA = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            ctxA.add_init_script(INIT_SCRIPT)
            pageA = ctxA.new_page()
            Q.attach(pageA, "register/login")
            pageA.goto(BASE + "/login", wait_until="networkidle")
            ck("登录页含注册入口", pageA.locator("#pane-reg").count() == 1)
            pageA.locator("#tab-reg").click()
            pageA.fill("#rg-user", U_C)
            pageA.fill("#rg-pw", PWD)
            pageA.click("#pane-reg button")
            pageA.wait_for_timeout(1200)
            msg1 = pageA.evaluate("() => (document.getElementById('msg')||{}).textContent||''")
            ck("注册UI：正常注册提交成功提示", "注册成功" in msg1, msg1)
            # 重复用户名
            show_reg_pane(pageA)
            pageA.fill("#rg-user", U_C)
            pageA.fill("#rg-pw", PWD)
            pageA.click("#pane-reg button")
            pageA.wait_for_timeout(1200)
            msg2 = pageA.evaluate("() => (document.getElementById('msg')||{}).textContent||''")
            ck("注册UI：重复用户名被拒提示", "已存在" in msg2, msg2)
            # 限流：连续提交直到 429 文案（烧掉本分钟桶，作为本套件最后一次注册活动）
            got_429 = False
            for i in range(14):
                show_reg_pane(pageA)
                pageA.fill("#rg-user", "rttest%s_uir%d" % (TAG, int(time.time()) % 100000 + i))
                pageA.fill("#rg-pw", PWD)
                pageA.click("#pane-reg button")
                pageA.wait_for_timeout(700)
                msgx = pageA.evaluate("() => (document.getElementById('msg')||{}).textContent||''")
                if "频繁" in msgx:
                    got_429 = True
                    break
            ck("注册UI：限流429文案可见", got_429)
            save_shot(pageA, "register_ratelimit")

            # ════ B2 登录页：错误密码×5（每次有明确提示），随后审批链路在 C1 完成后正常登录 ════
            pageA.locator("#tab-login").click()
            all_err = True
            for i in range(5):
                pageA.fill("#li-user", U_C)
                pageA.fill("#li-pw", "wrongpw%d" % i)
                pageA.click("#pane-login button")
                pageA.wait_for_timeout(900)
                msgl = pageA.evaluate("() => (document.getElementById('msg')||{}).textContent||''")
                if "错误" not in msgl:
                    all_err = False
                    break
            ck("登录UI：连续5次错误密码均有明确提示", all_err)
            save_shot(pageA, "login_fail")

            # ════ C 管理侧：/admin 表单登录 + 审批真实点按 + 状态页 ════
            pageAdm = ctxA.new_page()
            Q.attach(pageAdm, "admin")
            pageAdm.goto(BASE + "/admin")
            pageAdm.wait_for_load_state("networkidle")
            # 错误密码 → 回登录页
            pageAdm.fill('input[name=pw]', "wrongadminpw")
            pageAdm.locator('button').first.click()
            pageAdm.wait_for_load_state("networkidle")
            pageAdm.wait_for_timeout(800)
            ck("管理UI：错误密码不放行", pageAdm.url.rstrip("/").endswith("/admin") and
               pageAdm.locator('input[name=pw]').count() == 1, pageAdm.url)
            pageAdm.fill('input[name=pw]', ADMIN_PW)
            pageAdm.locator('button').first.click()
            pageAdm.wait_for_load_state("networkidle")
            pageAdm.wait_for_timeout(1000)
            body_adm = pageAdm.evaluate("() => (document.body.textContent||'')")
            ck("管理UI：面板渲染（表格/公告框）", "class=\"tscroll" in pageAdm.content()
               and "textarea" in pageAdm.content())
            ck("管理UI：待审核用户出现在面板", U_C in body_adm)
            # 审批真实点按：定位 U_C 所在行 → 通过
            row = pageAdm.locator('tr', has_text=U_C).first
            approved = False
            if row.count():
                dsel = row.locator('input[name=days], select[name=days]')
                if dsel.count() and dsel.first.evaluate("e=>e.tagName") == "INPUT":
                    dsel.first.fill("30")
                row.locator('button:has-text("通过")').first.click()
                pageAdm.wait_for_load_state("networkidle")
                pageAdm.wait_for_timeout(1000)
                st = sqlite3.connect(SITE_DB, timeout=15).execute(
                    "SELECT status FROM users WHERE username=?", (U_C,)).fetchone()
                approved = bool(st) and st[0] == "approved"
            ck("管理UI：审批点按→用户状态 approved", approved)
            save_shot(pageAdm, "admin_approve")
            # 审批后 C 正常登录进入聊天页（B2 收尾 + 验证审批生效）
            ui_login(pageA, U_C, PWD)
            ck("登录UI：审批后正常登录进入聊天页", pageA.url.rstrip("/") == BASE,
               pageA.url)
            ck("聊天UI：欢迎页元素齐全（边栏/输入框）",
               pageA.locator("textarea").count() >= 1)

            # ════ B3 聊天页：S 用户流式对话 + 多轮 + 会话新建/切换 + 钱包可达 ════
            # S 用独立 context（ctxA 里已有 C 的会话 cookie，混用会被重定向串号）
            ctxS = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            ctxS.add_init_script(INIT_SCRIPT)
            pageA2 = ctxS.new_page()
            Q.attach(pageA2, "chat")
            ui_login(pageA2, U_S, PWD)
            ck("聊天UI：钱包入口可见", pageA2.locator('button:has-text("钱包")').count() > 0)
            pageA2.locator('button:has-text("钱包")').first.click()
            pageA2.wait_for_timeout(1800)
            body_w = pageA2.evaluate("() => (document.body.textContent||'')")
            ck("钱包UI：余额与充值说明渲染", "账户余额" in body_w and "¥" in body_w)
            ck("钱包UI：兑换码输入框可达",
               pageA2.locator("[class*=codeInput]").count() >= 1)
            ck("钱包UI：扫码充值区块注入", "扫码充值" in body_w)
            save_shot(pageA2, "wallet")
            pageA2.keyboard.press("Escape")
            pageA2.wait_for_timeout(500)
            # 流式对话（mock 上游特征回复）
            pageA2.locator("[class*=personaChip]", has_text="铁平教练").first.click()
            pageA2.wait_for_timeout(1500)
            ck("聊天UI：流式回复正常收尾",
               send_and_wait_reply(pageA2, "UI冒烟A侧消息 %s" % MARK_A,
                                   "网关计费链路已打通"))
            # 多轮
            ck("聊天UI：多轮对话第二条回复",
               send_and_wait_reply(pageA2, "UI冒烟A侧追问",
                                   "网关计费链路已打通"))
            save_shot(pageA2, "chat_stream")
            # 会话新建/切换
            pageA2.locator('button:has-text("开启新对话")').first.click()
            pageA2.wait_for_timeout(1500)
            body_new = pageA2.evaluate("() => (document.body.textContent||'')")
            ck("聊天UI：新建对话回到空白欢迎页", "今天有什么可以帮到你" in body_new)
            back = sidebar_open_conv(pageA2, MARK_A)
            body_back = pageA2.evaluate("() => (document.body.textContent||'')")
            ck("聊天UI：会话列表切回旧对话，历史消息保留", back and MARK_A in body_back,
               "切换=%s" % back)

            # ════ B4 云同步双设备端到端（R15 P1 真浏览器回归） ════
            ck("云同步：A端同步完成", open_sync(pageA2))
            ctxB = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            ctxB.add_init_script(INIT_SCRIPT)
            pageB = ctxB.new_page()
            Q.attach(pageB, "cloud_sync")
            ui_login(pageB, U_S, PWD)
            ck("云同步：B端登录后同步拉取完成", open_sync(pageB))
            pageB.goto(BASE + "/", wait_until="networkidle")
            pageB.wait_for_timeout(1500)
            got_a = sidebar_open_conv(pageB, MARK_A)
            body_b = pageB.evaluate("() => (document.body.textContent||'')")
            ck("云同步：B端可见A端会话与消息", got_a and MARK_A in body_b,
               "切换=%s" % got_a)
            # B 修改：同一会话追加消息 → 同步推送
            ck("云同步：B端追加消息收到流式回复",
               send_and_wait_reply(pageB, "云同步冒烟B侧修改 %s" % MARK_B,
                                   "网关计费链路已打通"))
            ck("云同步：B端推送完成", open_sync(pageB))
            # A 再推 → 拉到 B 的修改，无永久冲突
            body_fail = pageA2.evaluate("() => (document.body.textContent||'')")
            pageA2.goto(BASE + "/", wait_until="networkidle")
            pageA2.wait_for_timeout(1500)
            ck("云同步：A端再同步完成", open_sync(pageA2))
            pageA2.goto(BASE + "/", wait_until="networkidle")
            pageA2.wait_for_timeout(1500)
            got_b = sidebar_open_conv(pageA2, MARK_B)
            body_a = pageA2.evaluate("() => (document.body.textContent||'')")
            ck("云同步：A端最终可见B端修改（双向合并收敛）", got_b and MARK_B in body_a,
               "切换=%s" % got_b)
            ck("云同步：两端无『同步失败』/冲突文案",
               "同步失败" not in body_a and "同步失败" not in body_b
               and "冲突" not in body_a)
            save_shot(pageA2, "cloud_sync_a")

            # ════ C 告警横幅：写入→展示→清除→消失 ════
            c = sqlite3.connect(SITE_DB, timeout=15)
            c.execute("INSERT INTO settings(key,value) VALUES('price_change_alert',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps({"ts": time.time(), "items": ["deepseek-flash in_miss 0.15→0.20(UI冒烟)"]}),))
            c.commit(); c.close()
            pageAdm.reload(wait_until="networkidle")
            pageAdm.wait_for_timeout(1000)
            body_alert = pageAdm.evaluate("() => (document.body.textContent||'')")
            ck("管理UI：告警横幅写入后可见", "官方价变动超 5%" in body_alert)
            save_shot(pageAdm, "admin_alert")
            c = sqlite3.connect(SITE_DB, timeout=15)
            c.execute("UPDATE settings SET value='' WHERE key='price_change_alert'")
            c.commit(); c.close()
            pageAdm.reload(wait_until="networkidle")
            pageAdm.wait_for_timeout(1000)
            body_clean = pageAdm.evaluate("() => (document.body.textContent||'')")
            ck("管理UI：告警清除后消失", "官方价变动超 5%" not in body_clean)

            # ════ D 移动端视口 375×667 ════
            ctxM = browser.new_context(locale="zh-CN", viewport={"width": 375, "height": 667})
            ctxM.add_init_script(INIT_SCRIPT)
            pageM = ctxM.new_page()
            Q.attach(pageM, "mobile")
            pageM.goto(BASE + "/login", wait_until="networkidle")
            ck("移动端：登录页可达无横向滚动", no_horiz_scroll(pageM),
               pageM.evaluate("() => document.documentElement.scrollWidth"))
            ui_login(pageM, U_S, PWD)
            ck("移动端：聊天页可达无横向滚动", no_horiz_scroll(pageM))
            # 移动端侧栏收起：汉堡展开后再进钱包
            ham_m = pageM.locator('[aria-label=打开边栏]')
            if ham_m.count():
                ham_m.first.click()
                pageM.wait_for_timeout(800)
            pageM.locator('button:has-text("钱包")').first.click()
            pageM.wait_for_timeout(1500)
            ck("移动端：钱包页可达无横向滚动", no_horiz_scroll(pageM))
            save_shot(pageM, "mobile_chat")

            # ════ D 质量断言汇总 ════
            unexpected_console, page_errs, resp5 = Q.report()
            ck("质量：console 无非预期 error（故意触发的 429 已豁免）",
               not unexpected_console, unexpected_console[:5])
            ck("质量：无未捕获 JS 异常", not page_errs, page_errs[:5])
            ck("质量：全程零 5xx 响应", not resp5, resp5[:5])

            browser.close()
    finally:
        cleanup_all()

    fails = [n for n, ok in results if not ok]
    print("\n════ UI 冒烟: ✅%d ❌%d ════" % (len(results) - len(fails), len(fails)))
    print("截图证据: %s（%d 张）" % (SHOTS, len([f for f in os.listdir(SHOTS) if f.endswith(".png")])))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
