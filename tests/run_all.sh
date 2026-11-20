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
c = sqlite3.connect(site, timeout=15)
c.execute("PRAGMA busy_timeout=15000")
uids = [r[0] for r in c.execute("SELECT id FROM users WHERE username LIKE ?", (pat,)).fetchall()]
n1 = c.execute("DELETE FROM cloud_sessions WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,)).rowcount
c.execute("DELETE FROM cloud_backups WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,))
c.execute("DELETE FROM gateway_map WHERE uid IN (SELECT id FROM users WHERE username LIKE ?)", (pat,))
n2 = c.execute("DELETE FROM users WHERE username LIKE ?", (pat,)).rowcount
c.commit()
napi_deleted = 0
if napi:
    nc = sqlite3.connect(napi, timeout=15)
    nc.execute("PRAGMA busy_timeout=15000")
    for nid in [r[0] for r in nc.execute("SELECT id FROM users WHERE username LIKE ?", (pat,)).fetchall()]:
        nc.execute("DELETE FROM logs WHERE user_id=?", (nid,))
        nc.execute("DELETE FROM tokens WHERE user_id=?", (nid,))
    napi_deleted = nc.execute("DELETE FROM users WHERE username LIKE ?", (pat,)).rowcount
    nc.execute("DELETE FROM tokens WHERE user_id NOT IN (SELECT id FROM users)")
    nc.commit(); nc.close()
c.close()
print("[run_all] 测试账号收口清理: 站点 %d 用户/%d 会话, new-api %d 用户" % (n2, n1, napi_deleted))
PYEOF
fi

exit $_final_rc
