# -*- coding: utf-8 -*-
"""测试环境防护（审查 P0-测试安全/P1-测试安全）：

所有测试启动时必须调用 guard()：断言数据库路径位于 /tmp/ 下，
或在 RT_ALLOWED_DB_DIRS 中显式声明（如沙箱测试实例 /opt/newapi）。
生产机（未声明）上执行会直接退出码 2 拒绝，绝不触碰生产数据库。
"""
import os
import sys


def guard(*db_paths) -> None:
    # R5（审查 R3）：无参调用视为编程错误——空列表 bad=[] 会永远放行，
    # 等于假防护。必须显式传入待检查的库路径。
    if not db_paths:
        sys.stdout.write(
            "\033[91m[TEST-GUARD] guard() 必须传入待检查的库路径（无参=永远放行=假防护）\033[0m\n")
        sys.stdout.flush()
        sys.exit(2)
    allowed = [p.strip() for p in os.environ.get("RT_ALLOWED_DB_DIRS", "").split(",") if p.strip()]

    def ok(p):
        if not p:
            return True
        ap = os.path.abspath(p)
        if ap.startswith("/tmp/"):
            return True
        return any(ap.startswith(os.path.abspath(d) + os.sep) or ap == os.path.abspath(d)
                   for d in allowed)

    bad = [(p,) for p in db_paths if not ok(p)]
    if bad:
        sys.stdout.write(
            "\033[91m[TEST-GUARD] 数据库路径不在 /tmp/ 下且未通过 RT_ALLOWED_DB_DIRS 显式声明，"
            "拒绝执行（防止误跑在生产机）: %s\033[0m\n" % bad)
        sys.stdout.flush()
        sys.exit(2)


def assert_restart_allowed() -> None:
    """涉及重启真实 new-api 进程的用例必须三重前置全过，否则 skip（审查 P0-测试安全）。"""
    import urllib.request  # noqa: F401
    reasons = []
    if os.environ.get("RT_ALLOW_RESTART") != "1":
        reasons.append("RT_ALLOW_RESTART 未显式设为 1")
    for p in os.environ.get("RT_RESTART_DB_CHECK", "").split(","):
        p = p.strip()
        if p and not (os.path.abspath(p).startswith("/tmp/") or
                      any(os.path.abspath(p).startswith(os.path.abspath(d) + os.sep)
                          for d in [x.strip() for x in os.environ.get("RT_ALLOWED_DB_DIRS", "").split(",") if x.strip()])):
            reasons.append("重启目标 DB %s 不在允许目录" % p)
    if reasons:
        print("[SKIP] new-api 重启恢复用例跳过：%s（重启类用例禁止在未显式声明的环境执行）" % "; ".join(reasons))
        return False
    return True
