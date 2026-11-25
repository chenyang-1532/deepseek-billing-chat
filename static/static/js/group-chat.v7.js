/* Miandning AI 角色群聊（v29）——独立 overlay 模块
 * 不修改主 bundle 计费逻辑；群聊入口由主 bundle（main.fe540c10.js）侧栏按钮
 * 调用本模块暴露的 window.__gcOpenGroupChat() 打开（系统同款样式）。
 * 本文件作为新静态资源随 index.html 引用，更新时需改名（group-chat.v4.js）绕 immutable 缓存。
 * 数据面：群定义/群消息/群设置/角色记忆全部存服务端（/api/groups*），换设备一致；
 * 人设来源：云端会话（服务端 /api/personas，权威数据源）。
 * v29 变更（站长反馈 2026-11-25）：
 *  - 移除面板内部左侧群列表栏：聊天区全宽（与私聊一致），入口统一走主会话列表
 *    （群聊项混入）与侧栏「新建群聊」；右上角「关闭」退出
 * v28 变更（站长反馈 2026-11-24）：
 *  - 面板全屏化（inset:0），观感对齐单聊页面
 *  - 去掉底部计费小字（服务端延迟提示与前端费用文案一并移除；余额不足警告保留）
 * v27 变更（站长反馈 2026-11-24）：
 *  - 群聊混入主会话列表（与单人会话同栏，带「群聊」紫徽标），侧栏按钮改为「新建群聊」直达创建表单
 *  - @ 弹层：输入框键入 @ 自动浮出成员列表（头像+名字，点击插入）；长按成员头像亦可 @
 *  - 侧栏入口三件套统一：新建群聊 / 工单（跳 /tickets）/ 批量管理对话，全部系统同款按钮样式
 *  - 插科打诨默认开启且不再在前端展示（服务端强制 quip=true）
 * v26 变更：
 *  - 入口整合进左侧菜单（主 bundle 注入系统同款按钮），移除右下角 FAB 与 DOM 注入
 *  - 微信式布局：左侧群列表 + 右侧气泡（面板加宽）
 *  - 群设置面板：互聊轮数（1/2/3，默认 1）；插科打诨默认开启、不设前端开关
 *  - @点名必答（服务端解析），输入框提示
 *  - 打开群聊按概率（30%）补发闲聊泡泡（POST /api/groups/{gid}/idle，
 *    服务端有冷却窗 + 回合锁频控；不做后台定时器，服务端只在被请求时调用一次）
 *  - 计费保险丝（站长修订版）：无预检/无日限额，实时扣费；余额不足 →
 *    SSE insufficient_balance 终止 + 前端原地开钱包（已有链路保留）
 */
(function () {
  "use strict";
  if (window.__gcLoaded) return;
  window.__gcLoaded = true;

  // ---------- 常量 ----------
  var MIN_MEMBERS = 2, MAX_MEMBERS = 8;
  var ROUNDS_CHOICES = [1, 2, 3];      // 互聊轮数可选项（与服务端 GC_ROUNDS_CHOICES 对齐）
  var IDLE_PROBABILITY = 0.3;          // 打开群聊补发闲聊泡泡的概率（服务端另有 120s 冷却窗兜底）
  var AVATAR_MAX = 96;                 // 头像缩放边长（px）

  // ---------- 样式 ----------
  var css = [
    "#gc-mask{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:9100;display:none}",
    "#gc-panel{position:fixed;inset:0;width:100%;height:100%;z-index:9200;background:#f5f6f7;",
    "color:#0f172a;display:none;flex-direction:column;overflow:hidden;",
    "font-size:15px}",
    "#gc-body{flex:1;display:flex;min-height:0}",
    "#gc-chatwrap{flex:1;display:flex;flex-direction:column;min-width:0;background:#f5f6f7}",
    "#gc-chathead{flex:none;display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid #e2e6ea;background:#fff}",
    "#gc-chathead b{font-size:15px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    "#gc-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:14px}",
"#gc-input{position:relative}",
".gc-mention{position:absolute;bottom:calc(100% + 6px);left:0;right:74px;background:#fff;",
"border:1px solid #e2e6ea;border-radius:10px;box-shadow:0 10px 34px rgba(15,23,42,.18);",
"z-index:60;max-height:236px;overflow-y:auto}",
".gc-mitem{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;font-size:13px}",
".gc-mitem:hover,.gc-mitem.on{background:#eef2ff}",
".gc-mitem .gc-avatar{width:26px;height:26px;font-size:11px}",
".gc-gbadge{flex:none;font-size:10px;line-height:1;padding:3px 5px;border-radius:4px;",
"background:#eef2ff;color:#4f46e5;font-weight:600}",
    ".gc-empty{color:#94a3b8;text-align:center;margin:auto;font-size:13px;line-height:1.8}",
    ".gc-row{display:flex;gap:8px;align-items:flex-start}",
    ".gc-row.me{flex-direction:row-reverse}",
    ".gc-avatar{width:32px;height:32px;border-radius:6px;flex:none;background:#e2e8f0;color:#475569;",
    "display:flex;align-items:center;justify-content:center;font-size:13px;overflow:hidden;font-weight:600}",
    ".gc-avatar img{width:100%;height:100%;object-fit:cover}",
    ".gc-bubble{max-width:72%;padding:9px 12px;border-radius:10px;line-height:1.55;white-space:pre-wrap;word-break:break-word}",
    ".gc-row.ai .gc-bubble{background:#fff;box-shadow:0 1px 2px rgba(15,23,42,.06)}",
    ".gc-row.me .gc-bubble{background:#2563eb;color:#fff}",
    ".gc-row.sys{justify-content:center}",
    ".gc-row.sys .gc-bubble{background:#fffbeb;color:#92400e;font-size:12px;max-width:92%}",
    ".gc-who{font-size:11px;color:#64748b;margin-bottom:3px}",
    ".gc-row.me .gc-who{text-align:right}",
    ".gc-cost{display:none}",   // 计费小字不再展示
    ".gc-typing{display:inline-flex;gap:3px;align-items:center}",
    ".gc-typing i{width:5px;height:5px;border-radius:50%;background:#94a3b8;animation:gc-b 1.2s infinite}",
    ".gc-typing i:nth-child(2){animation-delay:.2s}.gc-typing i:nth-child(3){animation-delay:.4s}",
    "@keyframes gc-b{0%,60%,100%{opacity:.3}30%{opacity:1}}",
    "#gc-input{flex:none;border-top:1px solid #e2e6ea;padding:10px 12px;display:flex;gap:8px;align-items:flex-end;background:#fff}",
    "#gc-input textarea{flex:1;resize:none;border:1px solid #cbd5e1;border-radius:10px;padding:9px 11px;",
    "font-size:14px;font-family:inherit;max-height:120px;min-height:40px;outline:none}",
    "#gc-input textarea:focus{border-color:#4f46e5}",
    "#gc-costbar{flex:none;font-size:11px;color:#64748b;padding:4px 14px;border-top:1px dashed #e2e6ea;display:none;background:#fff}",
    ".gc-btn{border:0;border-radius:8px;padding:7px 12px;background:#4f46e5;color:#fff;font-size:13px;cursor:pointer;font-family:inherit}",
    ".gc-btn:hover{background:#4338ca}",
    ".gc-btn.ghost{background:#fff;border:1px solid #d9dde3;color:#374151}",
    ".gc-btn.sm{padding:4px 9px;font-size:12px}",
    ".gc-btn:disabled{opacity:.5;cursor:not-allowed}",
    "#gc-create,#gc-settings{position:absolute;inset:0;background:#fff;z-index:5;display:none;flex-direction:column}",
    "#gc-create .bd,#gc-settings .bd{flex:1;overflow-y:auto;padding:16px}",
    ".gc-field{margin-bottom:12px}",
    ".gc-field label{display:block;font-size:12px;color:#475569;margin-bottom:5px}",
    ".gc-field input[type=text]{width:100%;border:1px solid #cbd5e1;border-radius:8px;padding:9px 11px;font-size:14px;outline:none}",
    ".gc-field select{border:1px solid #cbd5e1;border-radius:8px;padding:8px 10px;font-size:14px;font-family:inherit;background:#fff}",
    ".gc-hint{font-size:11px;color:#94a3b8;line-height:1.6}",
    ".gc-warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.6;margin-bottom:10px}",
    ".gc-pcard{display:flex;gap:9px;align-items:center;border:1px solid #e2e6ec;border-radius:10px;padding:8px 10px;margin-bottom:8px;cursor:pointer}",
    ".gc-pcard.on{border-color:#4f46e5;background:#eef2ff}",
    ".gc-pcard .info{flex:1;min-width:0}",
    ".gc-pcard .nm{font-weight:600;font-size:13px}",
    ".gc-pcard .ds{color:#94a3b8;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".gc-err{background:#fdecec;color:#b42318;border-radius:8px;padding:8px 10px;font-size:12px;margin:8px 0;display:none}",
    ".gc-chip{display:inline-flex;align-items:center;gap:4px;background:#eef2ff;color:#4f46e5;border-radius:999px;padding:3px 10px;font-size:12px;margin:0 6px 6px 0}",
    ".gc-switch{display:flex;align-items:center;gap:8px;font-size:13px;color:#334155;cursor:pointer;user-select:none}",
    ".gc-switch input{width:16px;height:16px;accent-color:#4f46e5}",
  ].join("");
  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ---------- 工具 ----------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  // 真 HTML 转义（角色名/头像 data URL 含 " 或 < 时防属性/标签上下文逃逸）
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  async function api(path, opts) {
    var o = opts || {};
    var r = await fetch(path, {
      method: o.method || "GET",
      headers: o.body ? { "Content-Type": "application/json" } : undefined,
      body: o.body ? JSON.stringify(o.body) : undefined,
    });
    var d = null;
    try { d = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) {
      var m = (d && (d.msg || (d.error && d.error.message))) || ("请求失败(" + r.status + ")");
      var err = new Error(m);
      err.status = r.status;
      err.type = d && d.error && d.error.type;
      throw err;
    }
    return d;
  }

  // 角色清单：来自云端会话（服务端 /api/personas，权威数据源）；
  // 接口失败时回退到本机 IndexedDB(chat-demo/sessions) 里已加载的会话。
  function loadPersonas() {
    return api("/api/personas")
      .then(function (d) { return (d.personas || []).map(function (p) { delete p.sid; delete p.title; return p; }); })
      .catch(function () { return loadPersonasIDB(); });
  }
  function loadPersonasIDB() {
    return new Promise(function (resolve) {
      try {
        var req = indexedDB.open("chat-demo");
        req.onerror = function () { resolve([]); };
        req.onsuccess = function () {
          var db = req.result;
          try {
            var tx = db.transaction("sessions", "readonly");
            var greq = tx.objectStore("sessions").getAll();
            greq.onerror = function () { resolve([]); };
            greq.onsuccess = function () {
              var best = {};
              (greq.result || []).forEach(function (s) {
                var p = s && s.persona;
                if (!p || typeof p !== "object") return;
                var name = (p.name || "").trim();
                if (!name) return;
                var cnt = (s.messages || []).length;
                if (!best[name] || cnt > best[name]._cnt) {
                  best[name] = {
                    name: name,
                    avatar: typeof p.avatar === "string" ? p.avatar : "",
                    description: p.description || "",
                    personality: p.personality || "",
                    style: p.style || "",
                    example: p.example || "",
                    firstMes: p.firstMes || "",
                    _cnt: cnt,
                  };
                }
              });
              resolve(Object.keys(best).map(function (k) { return best[k]; }));
            };
          } catch (e) { resolve([]); }
        };
      } catch (e) { resolve([]); }
    });
  }
  // 头像缩小到 96px 再上传（控制 members_json 体积）
  function shrinkAvatar(dataUrl) {
    return new Promise(function (resolve) {
      if (!dataUrl || dataUrl.indexOf("data:image/") !== 0) return resolve("");
      var img = new Image();
      img.onload = function () {
        try {
          var s = Math.min(1, AVATAR_MAX / Math.max(img.width, img.height));
          var w = Math.max(1, Math.round(img.width * s)), h = Math.max(1, Math.round(img.height * s));
          var cv = document.createElement("canvas");
          cv.width = w; cv.height = h;
          cv.getContext("2d").drawImage(img, 0, 0, w, h);
          resolve(cv.toDataURL("image/png"));
        } catch (e) { resolve(""); }
      };
      img.onerror = function () { resolve(""); };
      img.src = dataUrl;
    });
  }

  // ---------- 状态 ----------
  var groups = [], personas = [], currentGid = null, sending = false;
  var curSettings = { rounds: 1, quip: true };
  var roundCostShown = 0;
  var membersCache = {};   // gid -> members [{name,avatar}]

  // ---------- DOM ----------
  var mask = el("div");
  mask.id = "gc-mask";
  document.body.appendChild(mask);

  var panel = el("div");
  panel.id = "gc-panel";
  panel.innerHTML =
    '<div id="gc-body">' +
    '  <div id="gc-chatwrap">' +
    '    <div id="gc-chathead"><b id="gc-title">角色群聊</b>' +
    '      <button class="gc-btn ghost sm" id="gc-gear">群设置</button>' +
    '      <button class="gc-btn ghost sm" id="gc-close">关闭</button></div>' +
    '    <div id="gc-msgs"></div>' +
    '    <div id="gc-costbar"></div>' +
    '    <div id="gc-input">' +
    '      <textarea id="gc-text" placeholder="发消息（输入 @ 选择成员，可点名必答）"></textarea>' +
    '      <button class="gc-btn" id="gc-send">发送</button>' +
    '    </div>' +
    '  </div>' +
    '</div>' +
    '<div id="gc-create">' +
    '  <div class="gc-head" style="display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #e7eaee"><b>创建群聊</b><span style="flex:1"></span><button class="gc-btn ghost sm" id="gc-cback">返回</button></div>' +
    '  <div class="bd">' +
    '    <div class="gc-warn">费用提示：群聊按实时计费，每条消息会触发一次或多次模型调用（判定该谁说话 + 选中角色依次发言，' +
    '       各自独立计费到你的钱包）。可先在「群设置」里调整互聊轮数。</div>' +
    '    <div class="gc-field"><label>群聊名称（1-30 字）</label><input type="text" id="gc-gname" maxlength="30" placeholder="例如：产品讨论群"></div>' +
    '    <div class="gc-field"><label>选择角色（<span id="gc-selcnt">0</span>/' + MAX_MEMBERS + '，至少 ' + MIN_MEMBERS + ' 个，来自已有单聊角色）</label>' +
    '      <div id="gc-selchips"></div>' +
    '      <div id="gc-plist"></div>' +
    '      <div class="gc-hint" id="gc-nope" style="display:none">本机还没有带名字的对话角色——先在单聊里给某个对话设置「人设·角色名」，再来创建群聊。</div>' +
    '    </div>' +
    '    <div class="gc-err" id="gc-cerr"></div>' +
    '    <button class="gc-btn" id="gc-cok" style="width:100%">创建群聊</button>' +
    '  </div>' +
    '</div>' +
    '<div id="gc-settings">' +
    '  <div class="gc-head" style="display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #e7eaee"><b>群设置</b><span style="flex:1"></span><button class="gc-btn ghost sm" id="gc-sback">返回</button></div>' +
    '  <div class="bd">' +
    '    <div class="gc-field"><label>互聊轮数（每条用户消息触发的发言轮数；已回复角色可被再次选中接话）</label>' +
    '      <select id="gc-rounds">' +
           ROUNDS_CHOICES.map(function (n) { return '<option value="' + n + '">' + n + ' 轮</option>'; }).join("") +
    '      </select>' +
    '      <div class="gc-hint" style="margin-top:6px">结构上限：单回合总调用数 ≤ 角色数 × 轮数 × 2（防互聊失控，不是计费限制）。</div>' +
    '    </div>' +
        '    <div class="gc-err" id="gc-serr"></div>' +
    '    <button class="gc-btn" id="gc-sok" style="width:100%">保存设置</button>' +
    '  </div>' +
    '</div>';
  document.body.appendChild(panel);

  var $ = function (id) { return document.getElementById(id); };
  var msgsEl = $("gc-msgs"), textEl = $("gc-text"),
      sendBtn = $("gc-send"), costbar = $("gc-costbar");

  // ---------- 群列表（数据层；入口为主会话列表混入项，不再有面板内列表） ----------
  async function refreshGroups() {
    try {
      var d = await api("/api/groups");
      groups = d.groups || [];
      if (currentGid && !groups.some(function (g) { return g.id === currentGid; })) {
        currentGid = null;
        renderEmpty();
      }
    } catch (e) {
      if (e.status === 403) showLoginHint();
    }
  }

  function showLoginHint() {
    msgsEl.innerHTML = "";
    msgsEl.appendChild(el("div", "gc-empty", "登录后才能使用群聊（余额计费）。"));
  }
  function renderEmpty() {
    msgsEl.innerHTML = "";
    var box = el("div", "gc-empty", "还没有打开的群聊\n从左侧会话列表点「群聊」进入，或创建新群");
    var b = el("button", "gc-btn", "＋ 创建群聊");
    b.style.marginTop = "10px";
    b.onclick = openCreate;
    box.appendChild(document.createElement("br"));
    box.appendChild(b);
    msgsEl.appendChild(box);
    costbar.style.display = "none";
    sendBtn.disabled = true;
    textEl.disabled = true;
    $("gc-gear").disabled = true;
    $("gc-title").textContent = "角色群聊";
  }

  // ---------- 群消息渲染 ----------
  function avatarHTML(m) {
    if (m && m.avatar && m.avatar.indexOf("data:image/") === 0) {
      // 属性值必须转义（后端另有白名单+魔数强校验，此处纵深防御）
      return '<img src="' + esc(m.avatar) + '" alt="">';
    }
    return esc((m && m.name ? m.name : "?").slice(0, 1));
  }
  function msgRow(m) {
    if (m.sender_type === "system") {
      var row = el("div", "gc-row sys");
      row.appendChild(Object.assign(el("div", "gc-bubble"), { textContent: m.content }));
      return row;
    }
    if (m.sender_type === "user") {
      var r2 = el("div", "gc-row me");
      var wrap2 = el("div");
      wrap2.appendChild(el("div", "gc-who", "我"));
      wrap2.appendChild(Object.assign(el("div", "gc-bubble"), { textContent: m.content }));
      r2.appendChild(wrap2);
      return r2;
    }
    var mem = (membersCache[m.gid] || []).find(function (x) { return x.name === m.sender_name; });
    var r = el("div", "gc-row ai");
    var av = el("div", "gc-avatar");
    av.innerHTML = avatarHTML(mem || { name: m.sender_name });
    var wrap = el("div");
    wrap.appendChild(el("div", "gc-who", m.sender_name));
    wrap.appendChild(Object.assign(el("div", "gc-bubble"), { textContent: m.content }));
    if (m.cost_cny != null) {
      wrap.appendChild(el("div", "gc-cost", "¥" + m.cost_cny));
    }
    r.appendChild(av); r.appendChild(wrap);
    bindMentionAvatar(av, m.sender_name);
    return r;
  }
  function appendMsg(m) {
    msgsEl.appendChild(msgRow(m));
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
  function scrollBottom() { msgsEl.scrollTop = msgsEl.scrollHeight; }

  // ---------- 闲聊泡泡（打开群聊按概率补发，不做后台定时器） ----------
  function maybeIdle(gid, hasMessages) {
    if (sending || !hasMessages) return;
    if (Math.random() >= IDLE_PROBABILITY) return;
    // 服务端有冷却窗/回合锁兜底；失败静默（泡泡是锦上添花）
    api("/api/groups/" + gid + "/idle", { method: "POST", body: {} })
      .then(function (d) {
        if (d && d.ok && d.message && currentGid === gid) {
          var m = d.message;
          m.gid = gid;
          appendMsg(m);
        }
      })
      .catch(function () { /* 静默 */ });
  }

  async function openGroup(gid) {
    currentGid = gid;
    sendBtn.disabled = false;
    textEl.disabled = false;
    $("gc-gear").disabled = false;
    msgsEl.innerHTML = "";
    msgsEl.appendChild(el("div", "gc-empty", "加载中…"));
    try {
      var d = await api("/api/groups/" + gid);
      var g = d.group;
      $("gc-title").textContent = g.name + "（" + g.member_count + " 个角色）";
      membersCache[gid] = g.members || [];
      curSettings = g.settings || { rounds: 1, quip: true };
      roundCostShown = 0;
      costbar.style.display = "none";
      var m2 = await api("/api/groups/" + gid + "/messages?limit=200");
      msgsEl.innerHTML = "";
      var list = m2.messages || [];
      if (!list.length) {
        msgsEl.appendChild(el("div", "gc-empty",
          "群聊已就绪。发第一条消息，角色们会开始讨论；@角色名 可以点名必答。"));
      } else {
        list.forEach(function (m) { m.gid = gid; appendMsg(m); });
      }
      maybeIdle(gid, list.length > 0);
    } catch (e) {
      msgsEl.innerHTML = "";
      msgsEl.appendChild(el("div", "gc-empty", "加载失败：" + e.message));
    }
  }

  // ---------- 发送（SSE 编排） ----------
  function setSending(v) {
    sending = v;
    sendBtn.disabled = v;
    textEl.disabled = v;
    sendBtn.textContent = v ? "回复中…" : "发送";
  }

  async function sendCurrent() {
    if (sending || !currentGid) return;
    var text = textEl.value.trim();
    if (!text) return;
    if (text.length > 4000) { alert("消息过长（上限 4000 字）"); return; }
    textEl.value = "";
    setSending(true);
    roundCostShown = 0;
    costbar.style.display = "block";
    costbar.textContent = "正在安排角色发言…（@点名必答，其余由轻量判定决定）";
    // 移除占位提示
    var ph = msgsEl.querySelector(".gc-empty");
    if (ph) ph.remove();
    try {
      var resp = await fetch("/api/groups/" + currentGid + "/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });
      if (!resp.ok) {
        var jd = null;
        try { jd = await resp.json(); } catch (e) { /* empty */ }
        var m = (jd && (jd.msg || (jd.error && jd.error.message))) || ("发送失败(" + resp.status + ")");
        throw Object.assign(new Error(m), { status: resp.status });
      }
      // SSE 解析（含无 ReadableStream 的整包回退）
      var ctype = resp.headers.get("Content-Type") || "";
      if (!resp.body || ctype.indexOf("text/event-stream") < 0) {
        var all = await resp.text();
        handleSseBuffer(all, true);
      } else {
        var reader = resp.body.getReader();
        var dec = new TextDecoder();
        var buf = "";
        for (; ;) {
          var r = await reader.read();
          if (r.done) { handleSseBuffer(buf, true); break; }
          buf += dec.decode(r.value, { stream: true });
          buf = handleSseBuffer(buf, false);
        }
      }
    } catch (e) {
      appendMsg({ sender_type: "system", content: "发送失败：" + e.message +
        (e.status === 402 ? "（余额不足，请充值后再试）" : "") });
      // 402 与 SSE 内 insufficient_balance 行为对齐——原地开钱包
      if (e.status === 402 && typeof window.__gwOpenWallet === "function") {
        try { window.__gwOpenWallet(); } catch (e2) { /* 忽略 */ }
      }
    } finally {
      setSending(false);
      refreshGroups();
    }
  }

  // 解析 SSE 缓冲，返回剩余未完整部分；flush=true 时处理全部
  function handleSseBuffer(buf, flush) {
    var parts = buf.split("\n\n");
    var rest = parts.pop() || "";
    if (flush && rest) { parts.push(rest); rest = ""; }
    parts.forEach(function (blk) {
      var ev = "message", data = "";
      blk.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += (data ? "\n" : "") + line.slice(5).trim();
      });
      if (!data) return;
      var d;
      try { d = JSON.parse(data); } catch (e) { return; }
      handleEvent(ev, d);
    });
    return rest;
  }

  function handleEvent(ev, d) {
    if (ev === "user") {
      appendMsg({ sender_type: "user", content: d.content });
    } else if (ev === "reply_start") {
      var row = el("div", "gc-row ai");
      row.dataset.seq = d.seq;
      var av = el("div", "gc-avatar");
      av.innerHTML = avatarHTML({ name: d.sender, avatar: findAvatar(d.sender) });
      var wrap = el("div");
      wrap.appendChild(el("div", "gc-who", d.sender));
      var bub = el("div", "gc-bubble");
      bub.innerHTML = '<span class="gc-typing"><i></i><i></i><i></i></span>';
      wrap.appendChild(bub);
      row.appendChild(av); row.appendChild(wrap);
      bindMentionAvatar(av, d.sender);
      msgsEl.appendChild(row);
      scrollBottom();
    } else if (ev === "delta") {
      var r = msgsEl.querySelector('.gc-row[data-seq="' + d.seq + '"] .gc-bubble');
      if (r) {
        if (d.text) {
          if (r.dataset.text == null) r.dataset.text = "";
          r.dataset.text += d.text;
          r.textContent = r.dataset.text;
        }
        scrollBottom();
      }
    } else if (ev === "reply_end") {
      if (d.sender) {
        roundCostShown += 1;
        costbar.textContent = "已完成 " + roundCostShown + " 条回复…";
      }
    } else if (ev === "reply_error") {
      // 移除该角色的 typing 占位
      var rr = msgsEl.querySelector('.gc-row[data-seq="' + d.seq + '"]');
      if (rr) rr.remove();
      appendMsg({ sender_type: "system", content: d.message || "该角色本次未回复" });
      if (d.kind === "insufficient_balance") {
        costbar.textContent = "⚠️ 余额不足，本轮已停止";
        if (typeof window.__gwOpenWallet === "function") {
          try { window.__gwOpenWallet(); } catch (e) { /* 忽略 */ }
        }
      }
    } else if (ev === "done") {
      costbar.style.display = "none";   // 计费小字不再展示（站长规则 2026-11-24）
      // 以服务端为准刷一遍（含 system 错误行）
      if (currentGid) {
        api("/api/groups/" + currentGid + "/messages?limit=200").then(function (m2) {
          msgsEl.innerHTML = "";
          (m2.messages || []).forEach(function (m) { m.gid = currentGid; appendMsg(m); });
        }).catch(function () { /* 保留当前视图 */ });
      }
    } else if (ev === "error") {
      appendMsg({ sender_type: "system", content: d.message || "出错" });
    }
  }
  function findAvatar(name) {
    var ms = membersCache[currentGid] || [];
    for (var i = 0; i < ms.length; i++) if (ms[i].name === name) return ms[i].avatar || "";
    return "";
  }

  // ---------- 创建群聊 ----------
  var selected = {};
  function openCreate() {
    selected = {};
    $("gc-gname").value = "";
    $("gc-cerr").style.display = "none";
    $("gc-create").style.display = "flex";
    renderPersonaList();
    renderChips();
  }
  function renderPersonaList() {
    var box = $("gc-plist");
    box.innerHTML = "";
    $("gc-nope").style.display = personas.length ? "none" : "block";
    personas.forEach(function (p) {
      var c = el("div", "gc-pcard");
      var av = el("div", "gc-avatar");
      av.innerHTML = avatarHTML(p);
      var info = el("div", "info");
      info.appendChild(el("div", "nm", p.name));
      info.appendChild(el("div", "ds", (p.description || p.personality || "").slice(0, 40)));
      c.appendChild(av); c.appendChild(info);
      c.onclick = function () {
        if (selected[p.name]) { delete selected[p.name]; c.classList.remove("on"); }
        else {
          if (Object.keys(selected).length >= MAX_MEMBERS) { return; }
          selected[p.name] = p;
          c.classList.add("on");
        }
        renderChips();
      };
      if (selected[p.name]) c.classList.add("on");
      box.appendChild(c);
    });
  }
  function renderChips() {
    var box = $("gc-selchips");
    box.innerHTML = "";
    Object.keys(selected).forEach(function (n) {
      box.appendChild(el("span", "gc-chip", n));
    });
    $("gc-selcnt").textContent = Object.keys(selected).length;
  }
  async function submitCreate() {
    var name = $("gc-gname").value.trim();
    var members = Object.keys(selected).map(function (n) { return selected[n]; });
    var err = $("gc-cerr");
    err.style.display = "none";
    function fail(m) { err.textContent = m; err.style.display = "block"; }
    if (!name) return fail("请填写群聊名称");
    if (members.length < MIN_MEMBERS) return fail("至少选择 " + MIN_MEMBERS + " 个角色");
    var btn = $("gc-cok");
    btn.disabled = true;
    try {
      var payloadMembers = [];
      for (var i = 0; i < members.length; i++) {
        var p = members[i];
        payloadMembers.push({
          name: p.name,
          avatar: await shrinkAvatar(p.avatar),
          persona: {
            description: p.description, personality: p.personality,
            style: p.style, example: p.example, firstMes: p.firstMes,
          },
        });
      }
      var d = await api("/api/groups", {
        method: "POST",
        body: { name: name, members: payloadMembers, settings: curSettings },
      });
      $("gc-create").style.display = "none";
      await refreshGroups();
      openGroup(d.id);
    } catch (e) {
      fail(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 群设置 ----------
  function openSettings() {
    if (!currentGid) return;
    $("gc-rounds").value = String(ROUNDS_CHOICES.indexOf(curSettings.rounds) >= 0 ? curSettings.rounds : 1);
    $("gc-serr").style.display = "none";
    $("gc-settings").style.display = "flex";
  }
  async function saveSettings() {
    if (!currentGid) return;
    var err = $("gc-serr");
    err.style.display = "none";
    var body = {
      rounds: parseInt($("gc-rounds").value, 10) || 1,
    };
    var btn = $("gc-sok");
    btn.disabled = true;
    try {
      var d = await api("/api/groups/" + currentGid + "/settings", { method: "PUT", body: body });
      curSettings = d.settings || body;
      $("gc-settings").style.display = "none";
      appendMsg({ sender_type: "system",
        content: "群设置已更新：互聊 " + curSettings.rounds + " 轮" });
    } catch (e) {
      err.textContent = e.message;
      err.style.display = "block";
    } finally {
      btn.disabled = false;
    }
  }


  // ---------- @ 成员选择弹层（v27） ----------
  function hideMention() {
    var mb = document.getElementById("gc-mention");
    if (mb) mb.remove();
  }
  function mentionQuery() {
    var pos = textEl.selectionStart;
    var val = textEl.value.slice(0, pos);
    var m = val.match(/@([^@\n]*)$/);
    return m ? { start: pos - m[0].length, q: m[1] } : null;
  }
  function showMention() {
    hideMention();
    if (!currentGid) return;
    var st = mentionQuery();
    if (!st) return;
    var ms = (membersCache[currentGid] || []).filter(function (x) {
      return x.name && x.name.toLowerCase().indexOf(st.q.toLowerCase()) >= 0;
    });
    if (!ms.length) return;
    var box = el("div", "gc-mention");
    box.id = "gc-mention";
    ms.slice(0, 8).forEach(function (mem) {
      var it = el("div", "gc-mitem");
      var av = el("div", "gc-avatar");
      av.innerHTML = avatarHTML(mem);
      it.appendChild(av);
      it.appendChild(el("span", null, mem.name));
      it.onmousedown = function (ev) {
        ev.preventDefault();
        var val = textEl.value, pos = textEl.selectionStart;
        textEl.value = val.slice(0, st.start) + "@" + mem.name + " " + val.slice(pos);
        var np = st.start + mem.name.length + 2;
        textEl.focus();
        textEl.setSelectionRange(np, np);
        hideMention();
      };
      box.appendChild(it);
    });
    $("gc-input").appendChild(box);
  }
  textEl.addEventListener("input", showMention);
  textEl.addEventListener("keyup", showMention);
  textEl.addEventListener("blur", function () { setTimeout(hideMention, 150); });
  textEl.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hideMention();
  });

  // ---------- 长按成员头像 @（v27） ----------
  function bindMentionAvatar(avEl, name) {
    if (!name || !avEl) return;
    var timer = null;
    var clear = function () { if (timer) { clearTimeout(timer); timer = null; } };
    avEl.addEventListener("pointerdown", function () {
      if (!currentGid || sending) return;
      timer = setTimeout(function () {
        timer = null;
        var add = "@" + name + " ";
        textEl.value = (textEl.value && textEl.value.slice(-1) !== " " && textEl.value !== ""
          ? textEl.value + " " : textEl.value) + add;
        textEl.focus();
        textEl.setSelectionRange(textEl.value.length, textEl.value.length);
        hideMention();
      }, 500);
    });
    ["pointerup", "pointerleave", "pointercancel", "pointermove"].forEach(function (evn) {
      avEl.addEventListener(evn, clear);
    });
    avEl.style.cursor = "pointer";
    avEl.title = "长按 @" + name;
  }

  // ---------- 会话列表混入（v27）：群聊与单人会话同栏，带「群聊」徽标 ----------
  var GC_LIST_CLASS = "list-ac14ac", GC_ITEM_CLASS = "section-ac14ac";
  var gcGroupsCache = [], gcLastFetch = 0, gcPending = false;
  function gcEnsureSidebar() {
    var list = document.querySelector("." + GC_LIST_CLASS);
    if (!list) return;
    var block = null;
    for (var i = 0; i < list.children.length; i++) {
      if (list.children[i].getAttribute && list.children[i].getAttribute("data-gc-block")) { block = list.children[i]; break; }
    }
    var groups = gcGroupsCache;
    if (!groups.length) { if (block) block.remove(); return; }
    if (block && block.childElementCount === groups.length) return;  // 已是最新
    if (block) block.remove();
    block = el("div");
    block.setAttribute("data-gc-block", "1");
    groups.forEach(function (g) {
      var it = el("div", GC_ITEM_CLASS);
      it.style.cssText = "display:flex;align-items:center;gap:9px;padding:10px 12px;cursor:pointer";
      var av = el("div", null, "👥");
      av.style.cssText = "width:34px;height:34px;border-radius:8px;background:#4f46e5;color:#fff;" +
        "display:flex;align-items:center;justify-content:center;font-size:15px;flex:none";
      var gi = el("div");
      gi.style.cssText = "flex:1;min-width:0";
      var nrow = el("div");
      nrow.style.cssText = "display:flex;align-items:center;gap:6px";
      var nm = el("div", null, g.name || "未命名群聊");
      nm.style.cssText = "font-weight:600;font-size:13px;flex:1;min-width:0;" +
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
      nrow.appendChild(nm);
      nrow.appendChild(el("span", "gc-gbadge", "群聊"));
      gi.appendChild(nrow);
      var sub = el("div", null, (g.member_count || 0) + " 人" +
        (g.last && g.last.content ? " · " + g.last.content.slice(0, 16) : ""));
      sub.style.cssText = "color:#94a3b8;font-size:11px;margin-top:2px;" +
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
      gi.appendChild(sub);
      it.appendChild(av); it.appendChild(gi);
      it.onclick = function () { window.__gcOpenGroupChat(g.id); };
      block.appendChild(it);
    });
    list.insertBefore(block, list.firstChild);
  }
  function gcRefreshSidebarGroups() {
    var now = Date.now();
    if (now - gcLastFetch < 3000) { gcEnsureSidebar(); return; }
    gcLastFetch = now;
    api("/api/groups").then(function (d) {
      gcGroupsCache = d.groups || [];
      gcEnsureSidebar();
    }).catch(function () { /* 未登录等，静默 */ });
  }
  function gcSchedule() {
    if (gcPending) return;
    gcPending = true;
    setTimeout(function () { gcPending = false; gcEnsureSidebar(); }, 60);
  }
  new MutationObserver(gcSchedule).observe(document.body, { childList: true, subtree: true });
  gcRefreshSidebarGroups();
  setInterval(gcRefreshSidebarGroups, 60000);

  // ---------- 面板开关（入口：主 bundle 侧栏按钮 → window.__gcOpenGroupChat） ----------
  function openPanel(mode) {
    mask.style.display = "block";
    panel.style.display = "flex";
    $("gc-create").style.display = "none";
    $("gc-settings").style.display = "none";
    if (!groups.length && !currentGid) renderEmpty();
    refreshGroups();
    loadPersonas().then(function (ps) {
      personas = ps;
      if (mode === "new") openCreate();                   // 侧栏「新建群聊」直达创建表单
      else if (typeof mode === "number") openGroup(mode); // 会话列表点群项直达该群
      else if (currentGid) openGroup(currentGid);         // 无参重开：恢复上次群
      else openCreate();                                  // 兜底：直接进创建表单
    });
  }
  function closePanel() {
    mask.style.display = "none";
    panel.style.display = "none";
    $("gc-create").style.display = "none";
    $("gc-settings").style.display = "none";
    gcRefreshSidebarGroups();   // 回到主界面时立刻刷新会话列表里的群聊预览
  }
  window.__gcOpenGroupChat = openPanel;   // 主 bundle（fe540c10）侧栏入口调用
  mask.onclick = closePanel;
  $("gc-close").onclick = closePanel;
  $("gc-gear").onclick = openSettings;
  $("gc-send").onclick = sendCurrent;
  textEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendCurrent();
    }
  });
  $("gc-cback").onclick = function () { $("gc-create").style.display = "none"; if (!currentGid) renderEmpty(); };
  $("gc-cok").onclick = submitCreate;
  $("gc-sback").onclick = function () { $("gc-settings").style.display = "none"; };
  $("gc-sok").onclick = saveSettings;

  renderEmpty();
})();
