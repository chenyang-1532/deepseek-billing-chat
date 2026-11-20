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
    _check_http_instance(db_paths)


def _site_dirs_from(db_paths):
    """从待检库路径推导「测试站点目录」：只认 /tmp 下的 .../data.db 或其所在目录。"""
    allowed = [os.path.abspath(x.strip()) for x in
               os.environ.get("RT_ALLOWED_DB_DIRS", "").split(",") if x.strip()]
    dirs = set()
    for p in db_paths:
        if not p:
            continue
        ap = os.path.abspath(p)
        if ap.endswith(os.sep + "data.db"):
            d = os.path.dirname(ap)
        elif os.path.isdir(ap) and os.path.exists(os.path.join(ap, "data.db")):
            d = ap
        else:
            continue
        # 只把「已被放行的站点库」当实例基准，new-api 的 one-api.db 不参与
        if d.startswith("/tmp/") or d in allowed or any(d.startswith(a + os.sep) for a in allowed):
            dirs.add(d)
    return dirs


def _uvicorn_cwds():
    """列出「真的是 uvicorn 在跑 app:app」的进程工作目录。

    必须按 argv 判断，不能用子串匹配：`zsh -c "... pkill -f uvicorn app:app ..."`、
    部署脚本、编辑器缓冲区都会命中子串，实测把执行测试的 shell 自己当成了站点实例
    （导致本应放行的场景被拒）。只认 argv[0] 的 basename == uvicorn，
    或 `python -m uvicorn`。
    """
    cwds = []
    try:
        pids = [x for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        return cwds
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        if os.path.basename(argv[0]) != "uvicorn" and not (
                len(argv) >= 3 and argv[1] == "-m" and argv[2] == "uvicorn"):
            continue
        if not any(a == "app:app" or a.startswith("app:app ") for a in argv[1:]):
            continue
        try:
            cwds.append(os.path.realpath("/proc/%s/cwd" % pid))
        except OSError:
            continue
    return cwds


def _check_http_instance(db_paths):
    """审查 R11（P1）：DB 路径检查只保证「连接的是测试库」，不保证「HTTP 打的是测试站点」。

    16 个 Python 套件全部硬编码 http://127.0.0.1:3000，而生产部署命令也是 --port 3000
    （部署指南 §四）——在跑着生产的机器上执行，注册/审批/调额会通过 HTTP 落到生产库，
    而 SQL 断言看的是 /tmp 测试库，表现为「测试全绿、生产被写脏」。

    判定：3000 上若有 uvicorn app:app 进程，其工作目录必须等于测试站点目录。
    找不到任何 uvicorn 进程时放行（容器/远程/CI 无法枚举，不误杀）；
    找到但都不匹配 → 退出码 2。RT_SKIP_HTTP_GUARD=1 可显式绕过。
    """
    if os.environ.get("RT_SKIP_HTTP_GUARD") == "1":
        return
    dirs = _site_dirs_from(db_paths)
    if not dirs:
        return
    cwds = _uvicorn_cwds()
    if not cwds:
        return
    if any(os.path.realpath(d) in cwds for d in dirs):
        return
    sys.stdout.write(
        "\033[91m[TEST-GUARD] 3000 端口上的 uvicorn 工作目录与测试站点目录不一致，"
        "拒绝执行（防止 HTTP 回归打到生产站点）: 期望 %s / 实际 %s\033[0m\n"
        "  （确认无误且确为非标准部署时，可设 RT_SKIP_HTTP_GUARD=1 显式绕过）"
        % (sorted(dirs), sorted(set(cwds))))
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
