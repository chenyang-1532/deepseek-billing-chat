# -*- coding: utf-8 -*-
"""审查 R1/P1-06 官方价解析加固与告警回归：
多锚点解析容错、数值上界校验、价格变动阈值告警、stale 陈旧告警"""
import json, os, sys, time, sqlite3

sys.path.insert(0, "/tmp/chatsite-test")
import app as A  # noqa: E402
from _guard import guard, assert_restart_allowed

results = []


def ck(name, cond, extra=None):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % (extra,)))


SITE = "/tmp/chatsite-test/data.db"
guard(SITE)


def set_setting_raw(key, val):
    c = sqlite3.connect(SITE, timeout=15)
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
    c.commit(); c.close()


def get_setting_raw(key):
    c = sqlite3.connect(SITE, timeout=15)
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return row[0] if row else None


# ── 1. 解析加固：峰=谷×2 校验与数值上界 ──
def price_html(prices):
    # 12 个价格：flash hit/miss/out × (谷,峰)，pro 同理；峰=谷×2
    order = ["f_hit", "p_hit", "f_hit_pk", "p_hit_pk",
             "f_miss", "p_miss", "f_miss_pk", "p_miss_pk",
             "f_out", "p_out", "f_out_pk", "p_out_pk"]
    vals = {
        "f_hit": prices["f_hit"], "f_hit_pk": prices["f_hit"] * 2,
        "p_hit": prices["p_hit"], "p_hit_pk": prices["p_hit"] * 2,
        "f_miss": prices["f_miss"], "f_miss_pk": prices["f_miss"] * 2,
        "p_miss": prices["p_miss"], "p_miss_pk": prices["p_miss"] * 2,
        "f_out": prices["f_out"], "f_out_pk": prices["f_out"] * 2,
        "p_out": prices["p_out"], "p_out_pk": prices["p_out"] * 2,
    }
    body = " ".join("$%g" % vals[k] for k in order)
    return "<html>1M INPUT TOKENS ... %s ... Concurrency Limit</html>" % body


good = {"f_hit": 0.003, "p_hit": 0.022, "f_miss": 0.15, "p_miss": 0.66, "f_out": 0.6, "p_out": 1.98}
d = A._parse_official_page(price_html(good))
ck("正常 12 值解析成功", d is not None and d["deepseek-flash"]["in_miss"][0] == 0.15, d)
# 峰≠谷×2 → 拒绝
# 直接构造错误 HTML（price_html 工具函数会强制 pk=off*2，故手动拼）
bad_html = "<html>1M INPUT TOKENS ... $0.003 $0.022 $0.006 $0.044 $0.15 $0.30 $0.66 $1.32 $0.6 $1.2 $1.5 $3.96 ... Concurrency Limit</html>"
ck("峰≠谷×2 被拒", A._parse_official_page(bad_html) is None)
# 数值上界：异常天价 → 拒绝
huge = dict(good); huge["f_miss"] = 99999; huge["f_miss_pk"] = 199998
ck("异常天价被拒(上界校验)", A._parse_official_page(price_html(huge)) is None)
# 值不足 12 个 → 拒绝
ck("值不足被拒", A._parse_official_page("<html>1M INPUT TOKENS $0.15 $0.30 Concurrency Limit</html>") is None)
# 备用锚点：主锚点缺失时仍可解析
alt = price_html(good).replace("1M INPUT TOKENS", "Input (cache miss)")
ck("备用锚点可解析", A._parse_official_page(alt) is not None)

# ── 2. 价格变动阈值告警（>5%） ──
old_snap = {
    "ts": time.time(),
    "models": {
        "deepseek-flash": {"in_hit": [0.003, 0.006], "in_miss": [0.15, 0.30], "out": [0.6, 1.2]},
        "deepseek-v4-pro": {"in_hit": [0.022, 0.044], "in_miss": [0.66, 1.32], "out": [1.98, 3.96]},
    },
}
set_setting_raw(A.GW_OFFICIAL_KEY, json.dumps(old_snap))
A._check_price_change({"deepseek-flash": {"in_hit": [0.003, 0.006], "in_miss": [0.20, 0.40], "out": [0.6, 1.2]},
                       "deepseek-v4-pro": old_snap["models"]["deepseek-v4-pro"]})
alert = get_setting_raw(A.PRICE_CHANGE_KEY)
ck("变动>5% 触发告警落库", alert is not None and "in_miss" in (alert or ""), alert)

# 小变动不告警（先清告警）
set_setting_raw(A.PRICE_CHANGE_KEY, "")
small = {"deepseek-flash": {"in_hit": [0.003, 0.006], "in_miss": [0.152, 0.304], "out": [0.6, 1.2]},
         "deepseek-v4-pro": old_snap["models"]["deepseek-v4-pro"]}
A._check_price_change(small)
ck("变动<5% 不告警", not get_setting_raw(A.PRICE_CHANGE_KEY), get_setting_raw(A.PRICE_CHANGE_KEY))

# ── 3. stale 陈旧告警（>24h 未成功抓取） ──
stale_snap = dict(old_snap)
stale_snap["ts"] = time.time() - 90000  # 25h 前
set_setting_raw(A.GW_OFFICIAL_KEY, json.dumps(stale_snap))
set_setting_raw(A.PRICE_STALE_KEY, "")
A._check_price_stale()
st = get_setting_raw(A.PRICE_STALE_KEY)
ck("抓取失败且数据>24h 触发 stale 告警", st is not None and "since" in json.loads(st), st)
# 新鲜数据不告警
set_setting_raw(A.GW_OFFICIAL_KEY, json.dumps(old_snap))
set_setting_raw(A.PRICE_STALE_KEY, "")
A._check_price_stale()
ck("数据新鲜不告警", not get_setting_raw(A.PRICE_STALE_KEY), get_setting_raw(A.PRICE_STALE_KEY))
# 恢复默认快照，避免污染后续测试
set_setting_raw(A.GW_OFFICIAL_KEY, json.dumps(old_snap))
set_setting_raw(A.PRICE_CHANGE_KEY, "")
set_setting_raw(A.PRICE_STALE_KEY, "")

fails = [n for n, ok in results if not ok]
print("\n════ P1-06 官方价加固: ✅%d ❌%d ════" % (len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
