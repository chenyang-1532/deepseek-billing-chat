#!/usr/bin/env bash
# 统一测试 runner（审查 P1-13 验收门禁）：
# - 串行跑全部套件，逐个捕获并输出退出码；
# - 任一套件失败 → 汇总表 + 总退出码 1（否则 0）；
# - 套件间强制间隔 65s：注册/登录限流为 10·20 次/分钟/IP，
#   连续注册会被 429 造成假失败，间隔规避。
# 用法：RT_TAG=<唯一标签> ADMIN_PW=<管理密码> bash tests/run_all.sh
# RT_ALLOWED_DB_DIRS：显式声明测试实例所在目录（审查 P1-测试安全），
#   只有声明后 guard() 才允许测试访问 /tmp 之外的库路径；生产机不声明 → 一律退出码 2 拒绝。
# RT_ALLOW_RESTART=1：显式允许重启类用例（如 reconciliation 的 new-api 重启恢复）。
set -u
cd "$(dirname "$0")/.."
export RT_TAG="${RT_TAG:-runner$(date +%H%M%S)}"
export RT_ALLOWED_DB_DIRS="${RT_ALLOWED_DB_DIRS:-/opt/newapi}"   # 沙箱测试实例声明；生产机请勿声明
export RT_ALLOW_RESTART="${RT_ALLOW_RESTART:-1}"                 # 沙箱测试实例允许重启；生产机必须显式关闭
FAIL=0
RESULTS=()

run() {
  local name="$1"; shift
  echo "════ $name ════"
  "$@" > "/tmp/rt_${name}.log" 2>&1
  local rc=$?
  RESULTS+=("$name exit=$rc")
  tail -3 "/tmp/rt_${name}.log"
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
