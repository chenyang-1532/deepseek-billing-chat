#!/bin/bash
# 全量回归测试 - 测试环境（localhost:8000，v16.3 版）
# 基于 /workspace/regression.sh 适配：HTTP、本机直查库
BASE="http://127.0.0.1:3000"
RT_TAG="${RT_TAG:-}"
# 用户名上限 24 位（app.py 注册校验 [\w]{2,24}）：自动标签 runnerHHMMSS(12) 时
# rttest(6)+tag(12)+_regress(8)=26 会超限被拒 → 级联 31 失败。收短后缀，最长 6+12+4=22。
RREG="rttest${RT_TAG}_reg"
RLIKE="rttest${RT_TAG}_%"

SITE_DIR="/tmp/chatsite-test"
ADMIN_PW="${ADMIN_PW:?请先 export ADMIN_PW}"

# ── R9（审查 P1）：实例校验，必须在发出第一个请求之前 ──
# 生产部署命令就是 --port 3000（见部署指南 §四），而本脚本 BASE 也打 3000：
# 在跑着生产站点的机器上执行一次，注册/审批/续期/禁用会全部落到生产库。
# _guard.py 只覆盖 Python 套件，bash 脚本此前只靠 ADMIN_PW 兜底 → 知道密码就能误伤。
_site_ok() {
  [ -f "$SITE_DIR/data.db" ] || return 1
  local pid cwd real_site
  real_site=$(readlink -f "$SITE_DIR") || return 1
  for pid in $(pgrep -f "uvicorn app:app" 2>/dev/null); do
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null) || continue
    [ "$cwd" = "$real_site" ] && return 0
  done
  return 1
}
if ! _site_ok; then
  echo "🛑 [TEST-GUARD] 3000 端口上找不到以 $SITE_DIR 为工作目录的实例，拒绝执行（防止误打生产站点）" >&2
  exit 2
fi

PASS=0; FAIL=0
A_JAR=/tmp/rt_admin.jar; U_JAR=/tmp/rt_user.jar
rm -f $A_JAR $U_JAR

ck() { if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ✅ $1"; else FAIL=$((FAIL+1)); echo "  ❌ $1 (期望:$2 实际:$3)"; fi; }
dbq() { (cd "$SITE_DIR" && python3 -c "
import sqlite3
c = sqlite3.connect('file:data.db?mode=ro', uri=True)
print($1)
"); }

echo "═══ 一、基础设施 ═══"
CODE=$(curl -s -o /dev/null -m 10 -w "%{http_code}" $BASE/login)
ck "登录页可访问" 200 "$CODE"
PROC=$(pgrep -f "uvicorn app:app" >/dev/null && echo ok || echo bad)
ck "服务进程运行中" ok "$PROC"

echo "═══ 二、门禁 ═══"
CODE=$(curl -s -o /dev/null -m 10 -w "%{http_code}" $BASE/)
ck "未登录访问首页→跳登录页(303)" 303 "$CODE"
CODE=$(curl -s -o /dev/null -m 10 -w "%{http_code}" $BASE/static/js/x.js)
ck "未登录访问JS资源→被拦截(303)" 303 "$CODE"
BODY=$(curl -s -m 10 $BASE/login)
echo "$BODY" | grep -q 'notice'; ck "登录页含公告栏组件" 0 $?
echo "$BODY" | grep -q 'pane-reg'; ck "登录页含注册入口" 0 $?

echo "═══ 三、注册校验 ═══"
R=$(curl -s -m 10 -X POST $BASE/api/register -H "Content-Type: application/json" -d "{\"u\":\"a\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '"ok":false'; ck "非法用户名(过短)被拒" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/register -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"12\"}")
echo "$R" | grep -q '"ok":false'; ck "密码过短被拒" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/register -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '"ok":true'; ck "正常注册成功" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/register -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '"ok":false'; ck "重复用户名被拒" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '等待管理员审核'; ck "未审核用户登录被拦" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"wrongpass\"}")
echo "$R" | grep -q '"ok":false'; ck "错误密码被拒" 0 $?

echo "═══ 四、管理面板与审核 ═══"
CODE=$(curl -s -c $A_JAR -m 10 -X POST $BASE/admin/login -d "pw=wrongpw" -o /dev/null -w "%{http_code}")
ck "错误管理密码→303(回登录)" 303 "$CODE"
CK=$(grep -c admin $A_JAR 2>/dev/null || true)
ck "错误密码不产生管理Cookie" 0 "$CK"
CODE=$(curl -s -c $A_JAR -m 10 -X POST $BASE/admin/login -d "pw=$ADMIN_PW" -o /dev/null -w "%{http_code}")
ck "正确管理密码登录" 303 "$CODE"
CODE=$(curl -s -b $A_JAR -m 10 $BASE/admin -o /dev/null -w "%{http_code}")
ck "管理面板可访问" 200 "$CODE"
BODY=$(curl -s -b $A_JAR -m 10 $BASE/admin)
echo "$BODY" | grep -q 'class="tscroll"'; ck "面板表格化布局(v17.3改版)" 0 $?
echo "$BODY" | grep -q 'textarea'; ck "公告编辑框存在" 0 $?
echo "$BODY" | grep -q "$RREG"; ck "待审核用户出现在面板" 0 $?
TUID=$(dbq "c.execute(\"SELECT id FROM users WHERE username='$RREG'\").fetchone()[0]")
ck "能查到测试用户ID(本机直查)" 1 "$(echo "$TUID" | grep -cE '^[0-9]+$')"
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/action -d "uid=$TUID&action=approve&days=30" -o /dev/null
EXP=$(dbq "c.execute(\"SELECT expiry FROM users WHERE username='$RREG'\").fetchone()[0]")
TODAY=$(date +%F); EXPECT=$(date -d "$TODAY +30 days" +%F 2>/dev/null || date -v+30d +%F)
ck "审核通过→到期日=今天+30($EXPECT)" "$EXPECT" "$EXP"

echo "═══ 五、登录态与聊天页 ═══"
MAXAGE=$(curl -s -D - -o /dev/null -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}" | grep -i set-cookie | grep -o "Max-Age=[0-9]*")
ck "登录Cookie有效期30天" "Max-Age=2592000" "$MAXAGE"
CODE=$(curl -s -c $U_JAR -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}" -o /dev/null -w "%{http_code}")
ck "审核后登录成功" 200 "$CODE"
TITLE=$(curl -s -b $U_JAR -m 10 $BASE/ | grep -o "<title>[^<]*</title>")
ck "聊天页标题=Miandning AI" "<title>Miandning AI</title>" "$TITLE"
MAIN_JS=$(ls -t "$SITE_DIR/static/static/js" 2>/dev/null | grep '^main' | head -1)
CODE=$(curl -s -b $U_JAR -m 10 -o /dev/null -w "%{http_code}" $BASE/static/js/$MAIN_JS)
ck "JS资源正常加载($MAIN_JS)" 200 "$CODE"
CC=$(curl -s -b $U_JAR -m 10 -D - -o /dev/null $BASE/ | grep -i "^cache-control" | sed 's/^[Cc]ache-[Cc]ontrol: //' | tr -d "\r")
ck "HTML入口 no-store" "no-store, must-revalidate" "$CC"
CC=$(curl -s -b $U_JAR -m 10 -D - -o /dev/null $BASE/static/js/$MAIN_JS | grep -i "^cache-control" | sed 's/^[Cc]ache-[Cc]ontrol: //' | tr -d "\r")
ck "哈希JS immutable 长缓存" "public, max-age=2592000, immutable" "$CC"
R=$(curl -s -b $U_JAR -m 10 $BASE/api/me)
echo "$R" | grep -q '"days_left":30'; ck "/api/me 剩余30天" 0 $?
LOC=$(curl -s -b $U_JAR -m 10 -D - -o /dev/null $BASE/login | grep -i '^location' | tr -d "\r")
ck "已登录访问/login→自动跳回对话页" "location: /" "$LOC"

echo "═══ 六、云端备份（原始二进制流 + 会话级合并） ═══"
python3 -c "
import zipfile, json
with zipfile.ZipFile('/tmp/rt.zip','w') as z:
    z.writestr('manifest.json', json.dumps({'export_format_version':3}))
    z.writestr('chat_sessions/rt-sess-1.json', json.dumps({'id':'rt-sess-1','title':'RT测试','inserted_at':'','updated_at':'2026-01-01T00:00:00Z','messages':[]}))
    z.writestr('chat_session_index.json', json.dumps([{'id':'rt-sess-1','title':'RT测试','pinned':False,'message_count':0,'inserted_at':'','updated_at':'2026-01-01T00:00:00Z'}]))
"
CODE=$(curl -s -m 30 -X POST $BASE/api/backup -o /dev/null -w "%{http_code}" -H "Content-Type: application/octet-stream" --data-binary "junk")
ck "未登录上传备份→403" 403 "$CODE"
R=$(curl -s -b $U_JAR -m 30 -X POST $BASE/api/backup -H "Content-Type: application/octet-stream" --data-binary @/tmp/rt.zip)
echo "$R" | grep -q '"ok":true'; ck "登录后上传备份成功(会话级合并)" 0 $?
R=$(curl -s -b $U_JAR -m 10 $BASE/api/backup/meta)
echo "$R" | grep -q '"exists":true'; ck "备份元信息=已存在" 0 $?
BODY=$(curl -s -b $U_JAR -m 10 $BASE/api/backup)
echo "$BODY" | grep -q "rt-sess-1"; ck "下载备份包含已上传会话" 0 $?
R=$(curl -s -b $U_JAR -m 10 -X POST "$BASE/api/backup?deleted=%5B%22never-existed-sid%22%5D" -H "Content-Type: application/octet-stream" --data-binary @/tmp/rt.zip)
echo "$R" | grep -q '"ok":true'; ck "未知sid的墓碑被忽略(不误删)" 0 $?
B=$(curl -s -b $U_JAR -m 10 -o /dev/null -w "%{http_code}" $BASE/api/restore)
ck "恢复接口返回完整原包(200)" 200 "$B"

echo "═══ 七、上传中转接口（v15 新增） ═══"
CODE=$(curl -s -m 15 -X POST "$BASE/api/upload?name=t.txt&purpose=assistants" -o /dev/null -w "%{http_code}" --data-binary "hello")
[ "$CODE" = "403" ] || [ "$CODE" = "401" ]; ck "未登录调用上传中转→拒绝($CODE)" 0 $?
# 网关计费模式(v17.2)：服务端注入计费Key，客户端Key被忽略——无效Key不再401，
# 而是带着有效网关Key打到上游；mock上游无 /v1/files 端点，预期 400/404/502 均属正常透传
CODE=$(curl -s -m 15 -b $U_JAR -X POST "$BASE/api/upload?name=t.txt&purpose=assistants" -H "x-api-key: sk-invalid-test" -o /dev/null -w "%{http_code}" --data-binary "hello")
case "$CODE" in 400|404|502|503) CKOK=0;; *) CKOK=1;; esac
ck "网关模式upload忽略客户端Key并透传上游响应($CODE)" "$CKOK" "0"
CODE=$(curl -s -m 15 -b $U_JAR -X POST "$BASE/api/upload?name=t.txt" -o /dev/null -w "%{http_code}" --data-binary "hello")
case "$CODE" in 400|404|502|503) CKOK=0;; *) CKOK=1;; esac
ck "网关模式upload无需客户端Key(不再要求x-api-key)($CODE)" "$CKOK" "0"

echo "═══ 八、续期流程 ═══"
R=$(curl -s -b $U_JAR -m 10 -X POST $BASE/api/request-renewal)
echo "$R" | grep -q '"ok":true'; ck "用户申请续期" 0 $?
R=$(curl -s -b $U_JAR -m 10 -X POST $BASE/api/request-renewal)
echo "$R" | grep -q '已提交过'; ck "重复申请被拦" 0 $?
BODY=$(curl -s -b $A_JAR -m 10 $BASE/admin)
echo "$BODY" | grep -q '申请续期'; ck "面板显示续期申请标记" 0 $?
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/action -d "uid=$TUID&action=renew&days=15" -o /dev/null
DL=$(curl -s -b $U_JAR -m 10 $BASE/api/me | grep -o '"days_left":[0-9]*' | cut -d: -f2)
ck "续期15天→剩余45天" 45 "$DL"

echo "═══ 九、公告系统 ═══"
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/announce --data-urlencode "text=回归测试公告ABC123" -o /dev/null
R=$(curl -s -m 10 $BASE/api/announcement)
echo "$R" | grep -q '回归测试公告ABC123'; ck "公告保存并对外可见" 0 $?
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/announce --data-urlencode "text=" -o /dev/null
R=$(curl -s -m 10 $BASE/api/announcement)
echo "$R" | grep -q '"text":""'; ck "公告可清空" 0 $?

echo "═══ 十、禁用/启用/到期踢出 ═══"
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/action -d "uid=$TUID&action=disable" -o /dev/null
R=$(curl -s -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '已被禁用'; ck "禁用后登录被拦" 0 $?
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/action -d "uid=$TUID&action=enable" -o /dev/null
R=$(curl -s -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '"ok":true'; ck "启用后可登录" 0 $?
(cd "$SITE_DIR" && python3 -c "
import sqlite3
c = sqlite3.connect('data.db')
c.execute('UPDATE users SET expiry=? WHERE username=?', ('$(date -d '-1 day' +%F 2>/dev/null || date -v-1d +%F)', '$RREG'))
c.commit()")
ck "模拟到期(改库为昨天)" 0 $?
LOC=$(curl -s -b $U_JAR -m 10 -D - -o /dev/null $BASE/ | grep -i '^location' | tr -d "\r")
ck "到期用户访问→立马踢出到登录页" "location: /login?expired=1" "$LOC"
BODY=$(curl -s -m 10 "$BASE/login?expired=1")
echo "$BODY" | grep -q '会员已过期'; ck "登录页显示过期提示" 0 $?
R=$(curl -s -m 10 -X POST $BASE/api/login -H "Content-Type: application/json" -d "{\"u\":\"$RREG\",\"p\":\"rtpass123\"}")
echo "$R" | grep -q '过期，请联系管理员续费'; ck "过期账号登录被拒(防死循环)" 0 $?
curl -s -b $A_JAR -m 10 -X POST $BASE/admin/action -d "uid=$TUID&action=renew&days=10" -o /dev/null
TITLE=$(curl -s -b $U_JAR -m 10 $BASE/ | grep -o "<title>[^<]*</title>")
ck "续期后恢复进聊天页" "<title>Miandning AI</title>" "$TITLE"

echo "═══ 十一、静态兜底与安全 ═══"
TITLE=$(curl -s -b $U_JAR -m 10 $BASE/不存在的路径xyz | grep -o "<title>[^<]*</title>")
ck "未知路径SPA兜底返回首页" "<title>Miandning AI</title>" "$TITLE"
R=$(curl -s -m 10 --path-as-is "$BASE/api/../etc/passwd" -b $U_JAR -o /dev/null -w "%{http_code}")
# 审查 R11（P2-2）：未注册的 /api/* 路径从 SPA HTML 兜底(200)改为 JSON 404——
# 两者都不泄漏 /etc/passwd 内容，语义同为「兜住且非泄漏」
if [ "$R" = "200" ] || [ "$R" = "404" ]; then
  ck "路径穿越攻击被兜底(非泄漏,200或404)" "$R" "$R"
else
  ck "路径穿越攻击被兜底(非泄漏,200或404)" "200或404" "$R"
fi
CODE=$(curl -s -m 10 -X POST $BASE/api/backup -F "file=@/etc/hostname" -b $U_JAR -o /dev/null -w "%{http_code}")
ck "非ZIP文件上传被拒(400)" 400 "$CODE"

echo ""
echo "════════════════════════════"
echo "回归测试完成: ✅通过 $PASS  ❌失败 $FAIL"
# 门禁完整性：有失败必须退出非零，否则 run_all 只看退出码会放行带病回归
# R10（审查 P1）：退出码必须放到清理之后再执行——原写法 exit 0 把下面的清理块
# 变成死代码，测试账号永不清理（实测 FAIL=50 时 exit=1、grep DELETE=0、账号残留）。
_rt_rc=0
if [ "$FAIL" -gt 0 ]; then _rt_rc=1; fi

# 清理 rttest_ 测试账号（仅限测试环境，绝不触碰账号"1"等其他用户）
(cd "$SITE_DIR" && python3 -c "
import sqlite3
c = sqlite3.connect('data.db')
c.execute(\"DELETE FROM cloud_sessions WHERE uid IN (SELECT id FROM users WHERE username LIKE '$RLIKE')\")
c.execute(\"DELETE FROM cloud_backups WHERE uid IN (SELECT id FROM users WHERE username LIKE '$RLIKE')\")
c.execute(\"DELETE FROM users WHERE username LIKE '$RLIKE'\")
c.commit()
print('测试账号已清理; 现存用户:', c.execute('SELECT username FROM users').fetchall())
")

exit $_rt_rc
