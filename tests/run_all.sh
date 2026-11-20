#!/usr/bin/env bash
# 统一测试 runner（审查 P1-13 验收门禁）：
# - 串行跑全部套件，逐个捕获并输出退出码；
# - 任一套件失败 → 汇总表 + 总退出码 1（否则 0）；
# - 套件返回 2（被 guard 拒绝：生产库/未声明路径）→ 立即 exit 2 中止整轮；
# - 套件间强制间隔 65s：注册/登录限流为 10·20 次/分钟/IP，
#   连续注册会被 429 造成假失败，间隔规避。
# 用法（沙箱/CI 显式声明全部测试实例路径）：
#   RT_TAG=<标签> ADMIN_PW=<密码> \
#   RT_ALLOWED_DB_DIRS=/opt/newapi RT_ALLOW_RESTART=1 \
#   RT_SITE_DB=/tmp/chatsite-test/data.db RT_NAPI_DB=/opt/newapi/one-api.db \
#   bash tests/run_all.sh
# R6（审查 R5/C「门禁自废」）：默认值全部反转——生产机不传任何 RT_* 变量时，
#   guard 无声明直接拒绝（exit 2）、restart 类用例跳过、performance 拿不到
#   测试库路径自动 SKIP。绝不再默认把生产库路径写进环境。
set -u
cd "$(dirname "$0")/.."
export RT_TAG="${RT_TAG:-runner$(date +%H%M%S)}"
# R13：TAG 规整——调用方误带 "rttest" 前缀时去掉（套件内用户名已自带 rttest 前缀，
# 双前缀 + 长 TAG 会撞上 app.py 用户名 2-24 位校验，如 rttestrttest120039_gw_e2e=25 字符
# 被拒 → gw_e2e 用户不存在 IndexError）；再截断到 10 字符兜底（最长后缀 _gw_e2e=7）。
RT_TAG="${RT_TAG#rttest}"
export RT_TAG="$(printf '%s' "$RT_TAG" | cut -c1-10)"
export RT_ALLOWED_DB_DIRS="${RT_ALLOWED_DB_DIRS:-}"   # 默认拒绝：只有显式声明测试实例目录才放行
export RT_ALLOW_RESTART="${RT_ALLOW_RESTART:-0}"      # 默认不重启线上 new-api
export RT_SITE_DIR="${RT_SITE_DIR:-/tmp/chatsite-test}"
export RT_SITE_DB="${RT_SITE_DB:-}"                   # 默认空 → test_performance SKIP（不碰任何库）
export RT_NAPI_DB="${RT_NAPI_DB:-}"
FAIL=0
RESULTS=()
SKIPN=0

run() {
  local name="$1"; shift
  echo "════ $name ════"
  "$@" > "/tmp/rt_${name}.log" 2>&1
  local rc=$?
  # 审查 R11/R12：SKIP 用专用退出码 3 单列——test_performance 在 RT_SITE_DB 为空时
  # 跳过是 SKIP 不是 PASS，汇总里必须写明（如 16 PASS / 1 SKIP），否则「17/17」名不副实。
  if [ $rc -eq 3 ] || { [ $rc -eq 0 ] && grep -q "^\[SKIP\]" "/tmp/rt_${name}.log" 2>/dev/null; }; then
    SKIPN=$((SKIPN+1))
    RESULTS+=("$name SKIP")
    echo "  ⏭️ $name SKIP（依赖未设置，未实际执行）"
    return
  fi
  RESULTS+=("$name exit=$rc")
  tail -3 "/tmp/rt_${name}.log"
  if [ $rc -eq 2 ]; then
    # guard 拒绝（生产库/未声明路径）：立即中止整轮，绝不继续触碰后续套件
    echo "  🛑 $name 被 TEST-GUARD 拒绝（退出码 2）→ 中止整轮"
    exit 2
  fi
  if [ $rc -ne 0 ]; then
    echo "  ❌ $name 退出码 $rc"
    FAIL=1
  else
    echo "  ✅ $name 退出码 0"
  fi
}

cooldown() { echo "（限流冷却 65s）"; sleep 65; }

run regression    bash tests/regression_3000.sh
cooldown
run money_special python3.11 tests/test_money_special_3000.py
cooldown
run money_security python3.11 tests/test_money_security.py
cooldown
run p0_saga       python3.11 tests/test_p0_saga.py
run p0_r3         python3.11 tests/test_p0_r3.py
run p0_acceptance python3.11 tests/test_p0_acceptance.py
run p1_sid        python3.11 tests/test_p1_sid.py
run p1_cloud      python3.11 tests/test_p1_cloud.py
run p1_price      python3.11 tests/test_p1_price.py
cooldown
run pay_model     python3.11 tests/test_pay_model.py
cooldown
run time_mult     python3.11 tests/test_time_mult.py
cooldown
run billing       python3.11 tests/test_billing_accuracy.py
cooldown
run concurrency   python3.11 tests/test_concurrency.py
cooldown
run isolation     python3.11 tests/test_isolation.py
cooldown
run reconciliation python3.11 tests/test_reconciliation.py
cooldown
run gw_e2e        python3.11 tests/test_gw_e2e.py
cooldown
run performance   python3.11 tests/test_performance.py

echo
echo "════════ 退出码汇总 ════════"
printf '%s\n' "${RESULTS[@]}"
if [ $FAIL -ne 0 ]; then
  echo "════ 总结果: ❌ 存在失败套件（exit 1） ════"
  _final_rc=1
elif [ $SKIPN -gt 0 ]; then
  echo "════ 总结果: ✅ $(( ${#RESULTS[@]} - SKIPN )) PASS / $SKIPN SKIP（SKIP 套件未实际执行） ════"
  # R15（审查 P3）：SKIP>0 门禁视角仍要拦——全绿但不完整的轮次不允许静默通过 CI
  echo "[run_all] SKIP>0 → 退出码 4（PASS 但不完整，供 CI 区分）"
  _final_rc=4
  _final_rc=0
else
  echo "════ 总结果: ✅ 全部套件退出码为 0 ════"
  _final_rc=0
fi

# 审查 R11/E：测试账号清理收口——只靠 regression 末尾清理会漏掉 performance 等
# 后跑套件注册的账号（实测每轮 gate 残留 100+），手动清一定会漏，漏了下次 gate
# 的账号基数就变、test_performance 结论不可复现。统一在两库按 RT_TAG 前缀批量删。
if [ -n "$RT_SITE_DB" ] && [ -f "$RT_SITE_DB" ]; then
  RT_TAG="$RT_TAG" python3 - "$RT_SITE_DB" "${RT_NAPI_DB:-}" <<'PYEOF'
import os, sqlite3, sys
tag = os.environ["RT_TAG"]
site, napi = sys.argv[1], sys.argv[2] or None
# 匹配 rttest…TAG…任意位置（覆盖 regression 的 rttest<TAG>_、p0_r3 的 rttest_r3<TAG>、
# performance 在 new-api 的 site<uid>_rttest<TAG>_ 前缀命名）；TAG 不参与 SQL 拼接
pat = "%%rttest%%%s%%" % tag
# R13 兜底：测试实例是专用库，任何含 rttest 的账号都是测试残留——TAG 模式覆盖不了的
# 截断名（超 24/25 位被 DB 截断，TAG 尾部丢失，LIKE 匹配不到，实测 gate18 遗留 101 个
# site<uid>_rttestrttest1200）也一并清掉
pat_all = "%%rttest%%"
c = sqlite3.connect(site, timeout=15)
c.execute("PRAGMA busy_timeout=15000")
uids = [r[0] for r in c.execute("SELECT id FROM users WHERE username LIKE ?", (pat,)).fetchall()]
n1 = c.execute("DELETE FROM cloud_sessions WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,)).rowcount
c.execute("DELETE FROM cloud_backups WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,))
c.execute("DELETE FROM gateway_map WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,))
n2 = c.execute("DELETE FROM users WHERE username LIKE ?", (pat,)).rowcount
# R13 兜底第二遍：清 TAG 模式漏掉的截断名/异名残留
n1 += c.execute("DELETE FROM cloud_sessions WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat_all,)).rowcount
c.execute("DELETE FROM cloud_backups WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat_all,))
c.execute("DELETE FROM gateway_map WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat_all,))
n2 += c.execute("DELETE FROM users WHERE username LIKE ?", (pat_all,)).rowcount
# R13 兜底第三遍：先前的轮次可能已删用户但漏了关联行——孤儿 gateway_map 一并清
n1 += c.execute("DELETE FROM gateway_map WHERE uid NOT IN (SELECT id FROM users)").rowcount
# R15（审查回归）：pay_orders/quota_ops 必须与 users 同步清——上一轮的测试订单残留
# 会让本轮 pay_orders.id 自增复用后撞 quota_ops 旧 op_id（pay:<oid> 绑定不同账户
# → P0-3 语义拒绝 → 审批假失败，gate 结论不可复现，实测 r15c 环境 p0_saga 撞上）。
_test_orders = [r[0] for r in c.execute(
    "SELECT id FROM pay_orders WHERE username LIKE ? OR uid NOT IN (SELECT id FROM users)",
    (pat_all,)).fetchall()]
n_orders = 0
for _oid in _test_orders:
    n_orders += c.execute("DELETE FROM pay_orders WHERE id=?", (_oid,)).rowcount
c.commit()
napi_deleted = 0
if napi:
    nc = sqlite3.connect(napi, timeout=15)
    nc.execute("PRAGMA busy_timeout=15000")
    for patx in (pat, pat_all):
        for nid in [r[0] for r in nc.execute("SELECT id FROM users WHERE username LIKE ?", (patx,)).fetchall()]:
            nc.execute("DELETE FROM logs WHERE user_id=?", (nid,))
            nc.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
        napi_deleted += nc.execute("DELETE FROM users WHERE username LIKE ?", (patx,)).rowcount
    nc.execute("DELETE FROM tokens WHERE user_id NOT IN (SELECT id FROM users)")
    # R15：quota_ops 同步清——①孤儿（napi_uid 已不在 users）；②pay:<oid> 指向
    # 站点库已删除的订单（id 复用会撞语义冲突）；adjust:* 只按孤儿清，绝不动真实 op
    nc.execute("DELETE FROM quota_ops WHERE napi_uid NOT IN (SELECT id FROM users)")
    _live_oids = {r[0] for r in c.execute("SELECT id FROM pay_orders").fetchall()}
    _q_rows = [r[0] for r in nc.execute(
        "SELECT op_id FROM quota_ops WHERE op_id LIKE 'pay:%'").fetchall()]
    for _op in _q_rows:
        try:
            _oid = int(_op[4:])
        except ValueError:
            continue
        if _oid not in _live_oids:
            nc.execute("DELETE FROM quota_ops WHERE op_id=?", (_op,))
    nc.commit(); nc.close()
# R13 兜底第四遍：孤儿快照目录（uid 已不存在的 snapshots/<uid>，历史轮次遗留）
import glob as _glob
import shutil as _shutil
_live_uids = {str(r[0]) for r in c.execute("SELECT id FROM users").fetchall()}
_snap_root = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "snapshots")
_orphan_n = 0
# R15（审查 P3）：snapshots 真正的位置在 app.py 所在目录（SNAP_DIR=BASE/snapshots），
# 不是 data.db 所在目录；RT_SITE_DB 与 app 不同目录时不能乱猜，误删别人的快照。
if os.path.exists(os.path.join(os.path.dirname(_snap_root), "app.py")):
    if os.path.isdir(_snap_root):
        for d in _glob.glob(os.path.join(_snap_root, "*")):
            if os.path.basename(d) not in _live_uids:
                _shutil.rmtree(d, ignore_errors=True)
                _orphan_n += 1
c.close()
print("[run_all] 测试账号收口清理: 站点 %d 用户/%d 会话, new-api %d 用户, 孤儿快照目录 %d"
      % (n2, n1, napi_deleted, _orphan_n))
PYEOF
fi

exit $_final_rc
