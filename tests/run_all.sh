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

run() {
  local name="$1"; shift
  echo "════ $name ════"
  "$@" > "/tmp/rt_${name}.log" 2>&1
  local rc=$?
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
  exit 1
fi
echo "════ 总结果: ✅ 全部套件退出码为 0 ════"
exit 0
