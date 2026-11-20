import sys
# 网关计费端到端：对话→计量→钱包→兑换→后台→余额不足
# v2: 全程使用 rttest_ 前缀测试用户（数据铁律），测完自动清理
import asyncio, http.cookiejar, json, sqlite3, sys, urllib.request

import os
from _guard import guard, assert_restart_allowed
TAG = os.environ.get("RT_TAG", "")

ADMIN_PW = __import__("os").environ.get("ADMIN_PW", "")
BASE = "http://127.0.0.1:3000"
SITE_DB = "/tmp/chatsite-test/data.db"
NAPI = "/opt/newapi/one-api.db"
guard(SITE_DB, NAPI)
U = "rttest%s_gw_e2e" % TAG
PWD = "gwe2e123"

results = []
ok = lambda n, c, extra=None: results.append((n, c))


def q(sql, params=(), db=SITE_DB):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OP = urllib.request.build_opener(NR)


def api(method, path, data=None, form=False):
    if form and isinstance(data, dict):
        body = "&".join("%s=%s" % kv for kv in data.items()).encode()
    elif isinstance(data, dict):
        body = json.dumps(data).encode()
    else:
        body = data
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if isinstance(data, dict) and not form:
        req.add_header("Content-Type", "application/json")
    if form:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        return OP.open(req, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code


def prepare():
    _st = api("POST", "/api/register", {"u": U, "p": PWD})
    c = sqlite3.connect(SITE_DB)
    _row = c.execute("SELECT id FROM users WHERE username=?", (U,)).fetchone()
    if not _row:
        c.close()
        # R13：注册被拒（如用户名超 24 位校验返回 200+ok:false，或限流 429）时给出
        # 可诊断的失败信息，不再裸 IndexError；U=len("rttest")+len(TAG)+len("_gw_e2e")
        raise SystemExit("[PREPARE-FAIL] 注册未落库：status=%s U=%r(len=%d)——检查 RT_TAG 长度"
                         % (_st, U, len(U)))
    c.execute("UPDATE users SET status='approved', expiry=date('now','localtime','+30 days') WHERE username=?", (U,))
    c.commit()
    c.close()
    uid = _row[0]
    jar = http.cookiejar.CookieJar()
    adm = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NR())
    try:
        adm.open(urllib.request.Request(BASE + "/admin/login", data=("pw=%s" % ADMIN_PW).encode(),
                                        headers={"Content-Type": "application/x-www-form-urlencoded"}))
    except urllib.error.HTTPError:
        pass
    try:
        adm.open(urllib.request.Request(BASE + "/admin/wallet/adjust",
                                        data=("uid=%d&amount=50&note=e2e准备" % uid).encode(),
                                        headers={"Content-Type": "application/x-www-form-urlencoded"}))
    except urllib.error.HTTPError:
        pass
    nid = q("SELECT napi_uid FROM gateway_map WHERE uid=?", (uid,))[0]["napi_uid"]
    print("准备 rttest 用户 uid=%d nid=%d（充值 ¥50）" % (uid, nid))
    return uid, nid


def cleanup(uid, nid):
    c = sqlite3.connect(SITE_DB)
    c.execute("DELETE FROM wallet_tx WHERE uid=?", (uid,))
    c.execute("DELETE FROM redeem_codes WHERE used_by=?", (uid,))
    c.execute("DELETE FROM gateway_map WHERE uid=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()
    c.close()
    c = sqlite3.connect(NAPI)
    c.execute("DELETE FROM logs WHERE user_id=?", (nid,))
    c.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
    c.execute("DELETE FROM users WHERE id=?", (nid,))
    c.commit()
    c.close()
    print(U, "已清理")


async def login(page, u, pw):
    await page.goto(BASE + "/login", wait_until="networkidle")
    await page.fill("#li-user", u)
    await page.fill("#li-pw", pw)
    await page.click("#pane-login button")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)
    for _ in range(3):
        if await page.locator("[role=dialog]").count():
            await page.keyboard.press("Escape"); await asyncio.sleep(0.3)
        else:
            break


async def main():
    uid, nid = prepare()
    okf = []
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            await ctx.add_init_script("localStorage.setItem('chat-demo:locale','zh-CN');localStorage.removeItem('chat-demo:api-key')")
            page = await ctx.new_page()
            await login(page, U, PWD)

            # 1. 网关模式：无 Key 不弹设置窗，钱包按钮出现
            await asyncio.sleep(1)
            settings_open = await page.locator("[role=dialog]").count() > 0
            wallet_btn = await page.locator('button:has-text("钱包")').count()
            ok("1a 网关模式无 Key 不弹设置窗", not settings_open)
            ok("1b 侧栏出现钱包入口", wallet_btn > 0)

            # 2. 打开钱包 → 余额显示
            await page.locator('button:has-text("钱包")').first.click()
            await asyncio.sleep(2)
            body = await page.evaluate("() => (document.body.textContent||'')")
            ok("2a 钱包显示余额", "账户余额" in body and "¥" in body)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 3. 发一条对话 → 流式回复 + 计量
            await page.locator("[class*=personaChip]", has_text="铁平教练").first.click()
            await asyncio.sleep(1.5)
            ta = page.locator("textarea").first
            await ta.click(); await ta.fill("网关计费链路验证，请回复")
            await page.locator("[class*=sendButton]").first.click()
            streamed = False
            for _ in range(60):
                await asyncio.sleep(0.5)
                body = await page.evaluate("() => (document.body.textContent||'')")
                if "网关计费链路已打通" in body:
                    streamed = True
                    break
            ok("3a 对话收到流式回复（经 new-api→mock 渠道）", streamed)

            # 4. 钱包消费明细出现记录
            await page.locator('button:has-text("钱包")').first.click()
            await asyncio.sleep(2)
            body = await page.evaluate("() => (document.body.textContent||'')")
            ok("4a 消费明细出现记录(实际计费模型 deepseek-flash)", "deepseek-flash" in body)
            ok("4b 明细含 Tokens", "↑" in body)
            await page.keyboard.press("Escape")

            # 5. 后台：调整余额 + 生成充值码
            page2 = await ctx.new_page()
            await page2.goto(BASE + "/admin")
            await page2.fill('input[name=pw]', ADMIN_PW)
            await page2.click('button')
            await asyncio.sleep(1.5)
            body = await page2.evaluate("() => (document.body.textContent||'')")
            ok("5a 后台出现网关计费区块", "网关计费 · 用户余额" in body)
            ok("5b 后台出现充值码区块", "充值码" in body)
            # 给 rttest 用户充 100 元（定位其所在行，不碰真实用户）
            before_quota = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
            adj = page2.locator('tr', has_text=U).locator('form[action="/admin/wallet/adjust"]').first
            await adj.locator('input[name=amount]').fill("100")
            await adj.locator('button:has-text("充值")').click()
            await page2.wait_for_load_state("networkidle")
            after_quota = q("SELECT quota FROM users WHERE id=?", (nid,), NAPI)[0]["quota"]
            ok("5c 后台充值到账(测试用户 +¥100)", after_quota - before_quota == 6944444,
               (before_quota, after_quota))
            # 生成 2 张 5 元充值码
            await page2.locator('input[name=cny]').fill("5")
            await page2.locator('input[name=count]').fill("2")
            await page2.locator('button:has-text("生成充值码")').click()
            await page2.wait_for_load_state("networkidle")
            body = await page2.evaluate("() => (document.body.textContent||'')")
            import re
            # P1-08 修复后：充值码在一次性 POST 响应页展示（不进 URL）
            ok("5d 充值码生成并展示(一次性响应页)", "已生成" in body and any(len(x) == 26 for x in re.findall(r"[A-Z0-9]{26}", body)))
            m = re.search(r"[A-HJ-NP-Z2-9]{26}", body)  # 26位大写字母+数字（无 I/O/0/1）
            code = m.group(0) if m else None
            ok("5e 能提取充值码", code is not None)
            await page2.close()

            # 6. 用户兑换充值码
            await page.locator('button:has-text("钱包")').first.click()
            await asyncio.sleep(1.5)
            await page.locator("[class*=codeInput]").fill(code)
            await page.locator("button:has-text('兑换')").last.click()
            await asyncio.sleep(2)
            body = await page.evaluate("() => (document.body.textContent||'')")
            ok("6a 兑换成功提示", "兑换成功" in body)
            await page.keyboard.press("Escape")

            # 7. 余额不足路径：精确清零 rttest 用户余额再发消息 → 友好报错
            conn = sqlite3.connect(NAPI)
            conn.execute("UPDATE users SET quota=0 WHERE id=?", (nid,))
            conn.commit(); conn.close()
            await page.locator("textarea").first.click()
            await page.locator("textarea").first.fill("余额不足测试")
            await page.locator("[class*=sendButton]").first.click()
            err_shown = False
            for _ in range(30):
                await asyncio.sleep(0.5)
                body = await page.evaluate("() => (document.body.textContent||'')")
                if "额度" in body or "余额不足" in body:
                    err_shown = True
                    break
            ok("7a 余额耗尽给出明确报错", err_shown)
            await browser.close()
    finally:
        cleanup(uid, nid)

    all_ok = all(c for _, c in results)
    for n, c in results:
        print(("✅" if c else "❌"), n)
    print("\n" + ("✅ 网关计费端到端全部通过" if all_ok else "❌ 存在失败项"))
    return 0 if all_ok else 1


sys.exit(asyncio.run(main()))
