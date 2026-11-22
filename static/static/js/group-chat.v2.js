/* Miandning AI 角色群聊（v25）——独立 overlay 模块
 * 不修改主 bundle（main.fe540c09.js 保持不变，无需改名）；
 * 本文件作为新静态资源随 index.html 引用，更新时需改名（group-chat.v2.js）绕 immutable 缓存。
 * 数据面：群定义/群消息/角色记忆全部存服务端（/api/groups*），换设备一致；
 * 人设来源：本机 IndexedDB(chat-demo/sessions) 中已有单聊角色的 persona 字段。
 */
(function () {
  "use strict";
  if (window.__gcLoaded) return;
  window.__gcLoaded = true;

  // ---------- 常量 ----------
  var MIN_MEMBERS = 2, MAX_MEMBERS = 8;
  var LS_MEM = "gc.mem.v1";            // 群消息本地缓存（仅加速渲染，服务端为准）
  var AVATAR_MAX = 96;                 // 头像缩放边长（px）

  // ---------- 样式 ----------
  var css = [
    "#gc-fab{position:fixed;right:18px;bottom:18px;z-index:9000;width:48px;height:48px;border-radius:50%;",
    "background:#4f46e5;color:#fff;font-size:20px;border:0;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.22);display:flex;align-items:center;justify-content:center}",
    "#gc-fab:hover{background:#4338ca}",
    "#gc-mask{position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:9100;display:none}",
    "#gc-panel{position:fixed;top:0;right:0;bottom:0;width:430px;max-width:100vw;z-index:9200;background:#fff;",
    "color:#0f172a;display:none;flex-direction:column;box-shadow:-8px 0 28px rgba(0,0,0,.18);font-size:14px}",
    "#gc-panel .gc-head{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #e7eaee;flex:none}",
    "#gc-panel .gc-head b{font-size:15px}",
    "#gc-panel .gc-head .sp{flex:1}",
    "#gc-panel button{font-family:inherit}",
    ".gc-btn{border:0;border-radius:8px;padding:7px 12px;background:#4f46e5;color:#fff;font-size:13px;cursor:pointer}",
    ".gc-btn:hover{background:#4338ca}",
    ".gc-btn.ghost{background:#fff;border:1px solid #d9dde3;color:#374151}",
    ".gc-btn.sm{padding:4px 9px;font-size:12px}",
    ".gc-btn:disabled{opacity:.5;cursor:not-allowed}",
    "#gc-body{flex:1;display:flex;min-height:0}",
    "#gc-groups{width:132px;border-right:1px solid #e7eaee;overflow-y:auto;flex:none;background:#fafbfc}",
    ".gc-gitem{padding:10px 10px;border-bottom:1px solid #f0f2f5;cursor:pointer}",
    ".gc-gitem:hover{background:#f1f5f9}",
    ".gc-gitem.on{background:#eef2ff}",
    ".gc-gitem .n{font-weight:600;font-size:13px;word-break:break-all}",
    ".gc-gitem .m{color:#94a3b8;font-size:11px;margin-top:2px}",
    "#gc-chatwrap{flex:1;display:flex;flex-direction:column;min-width:0}",
    "#gc-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}",
    ".gc-empty{color:#94a3b8;text-align:center;margin:auto;font-size:13px;line-height:1.8}",
    ".gc-row{display:flex;gap:8px;align-items:flex-start}",
    ".gc-row.me{flex-direction:row-reverse}",
    ".gc-avatar{width:30px;height:30px;border-radius:50%;flex:none;background:#e2e8f0;color:#475569;",
    "display:flex;align-items:center;justify-content:center;font-size:13px;overflow:hidden;font-weight:600}",
    ".gc-avatar img{width:100%;height:100%;object-fit:cover}",
    ".gc-bubble{max-width:78%;padding:8px 11px;border-radius:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word}",
    ".gc-row.ai .gc-bubble{background:#f1f5f9}",
    ".gc-row.me .gc-bubble{background:#2563eb;color:#fff}",
    ".gc-row.sys{justify-content:center}",
    ".gc-row.sys .gc-bubble{background:#fffbeb;color:#92400e;font-size:12px;max-width:92%}",
    ".gc-who{font-size:11px;color:#64748b;margin-bottom:2px}",
    ".gc-row.me .gc-who{text-align:right}",
    ".gc-cost{color:#94a3b8;font-size:11px;margin-top:2px}",
    ".gc-typing{display:inline-flex;gap:3px;align-items:center}",
    ".gc-typing i{width:5px;height:5px;border-radius:50%;background:#94a3b8;animation:gc-b 1.2s infinite}",
    ".gc-typing i:nth-child(2){animation-delay:.2s}.gc-typing i:nth-child(3){animation-delay:.4s}",
    "@keyframes gc-b{0%,60%,100%{opacity:.3}30%{opacity:1}}",
    "#gc-input{flex:none;border-top:1px solid #e7eaee;padding:10px;display:flex;gap:8px;align-items:flex-end}",
    "#gc-input textarea{flex:1;resize:none;border:1px solid #cbd5e1;border-radius:10px;padding:9px 11px;",
    "font-size:14px;font-family:inherit;max-height:120px;min-height:40px;outline:none}",
    "#gc-input textarea:focus{border-color:#4f46e5}",
    "#gc-costbar{flex:none;font-size:11px;color:#64748b;padding:4px 12px;border-top:1px dashed #e7eaee;display:none}",
    "#gc-create{position:absolute;inset:0;background:#fff;z-index:5;display:none;flex-direction:column}",
    "#gc-create .bd{flex:1;overflow-y:auto;padding:14px}",
    ".gc-field{margin-bottom:12px}",
    ".gc-field label{display:block;font-size:12px;color:#475569;margin-bottom:5px}",
    ".gc-field input[type=text]{width:100%;border:1px solid #cbd5e1;border-radius:8px;padding:9px 11px;font-size:14px;outline:none}",
    ".gc-hint{font-size:11px;color:#94a3b8;line-height:1.6}",
    ".gc-warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.6;margin-bottom:10px}",
    ".gc-pcard{display:flex;gap:9px;align-items:center;border:1px solid #e2e6ec;border-radius:10px;padding:8px 10px;margin-bottom:8px;cursor:pointer}",
    ".gc-pcard.on{border-color:#4f46e5;background:#eef2ff}",
    ".gc-pcard .info{flex:1;min-width:0}",
    ".gc-pcard .nm{font-weight:600;font-size:13px}",
    ".gc-pcard .ds{color:#94a3b8;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".gc-err{background:#fdecec;color:#b42318;border-radius:8px;padding:8px 10px;font-size:12px;margin:8px 0;display:none}",
    ".gc-chip{display:inline-flex;align-items:center;gap:4px;background:#eef2ff;color:#4f46e5;border-radius:999px;padding:3px 10px;font-size:12px;margin:0 6px 6px 0}",
    "@media (max-width:640px){#gc-panel{width:100vw}#gc-groups{width:104px}}",
    /* 侧栏注入项 */
    ".gc-navitem{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:#334155}",
    ".gc-navitem:hover{background:#f1f5f9}",
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
  function esc(s) { return String(s == null ? "" : s); }
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
  function fmtCny(q) { return q == null ? null : (q / 500000 * 7.2).toFixed(4); }

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
  var roundCostShown = 0;
  var membersCache = {};   // gid -> members [{name,avatar}]

  // ---------- DOM ----------
  var fab = el("button", "", "👥");
  fab.id = "gc-fab";
  fab.title = "角色群聊";
  document.body.appendChild(fab);

  var mask = el("div");
  mask.id = "gc-mask";
  document.body.appendChild(mask);

  var panel = el("div");
  panel.id = "gc-panel";
  panel.innerHTML =
    '<div class="gc-head"><b id="gc-title">角色群聊</b><span class="sp"></span>' +
    '<button class="gc-btn ghost sm" id="gc-close">关闭</button></div>' +
    '<div id="gc-body">' +
    '  <div id="gc-groups"></div>' +
    '  <div id="gc-chatwrap">' +
    '    <div id="gc-msgs"></div>' +
    '    <div id="gc-costbar"></div>' +
    '    <div id="gc-input">' +
    '      <textarea id="gc-text" placeholder="发消息到群聊（所有角色会依次回复）"></textarea>' +
    '      <button class="gc-btn" id="gc-send">发送</button>' +
    '    </div>' +
    '  </div>' +
    '</div>' +
    '<div id="gc-create">' +
    '  <div class="gc-head"><b>创建群聊</b><span class="sp"></span><button class="gc-btn ghost sm" id="gc-cback">返回</button></div>' +
    '  <div class="bd">' +
    '    <div class="gc-warn">费用提示：群聊里每发一条消息，群内每个角色会依次调用一次模型（' +
    '       各自独立计费），消耗约为单聊的 <b>角色数</b> 倍。每条用户消息只触发一轮发言。</div>' +
    '    <div class="gc-field"><label>群聊名称（1-30 字）</label><input type="text" id="gc-gname" maxlength="30" placeholder="例如：产品讨论群"></div>' +
    '    <div class="gc-field"><label>选择角色（<span id="gc-selcnt">0</span>/' + MAX_MEMBERS + '，至少 ' + MIN_MEMBERS + ' 个，来自已有单聊角色）</label>' +
    '      <div id="gc-selchips"></div>' +
    '      <div id="gc-plist"></div>' +
    '      <div class="gc-hint" id="gc-nope" style="display:none">本机还没有带名字的对话角色——先在单聊里给某个对话设置「人设·角色名」，再来创建群聊。</div>' +
    '    </div>' +
    '    <div class="gc-err" id="gc-cerr"></div>' +
    '    <button class="gc-btn" id="gc-cok" style="width:100%">创建群聊</button>' +
    '  </div>' +
    '</div>';
  document.body.appendChild(panel);

  var $ = function (id) { return document.getElementById(id); };
  var msgsEl = $("gc-msgs"), groupsEl = $("gc-groups"), textEl = $("gc-text"),
      sendBtn = $("gc-send"), costbar = $("gc-costbar");

  // ---------- 群列表 ----------
  function renderGroups() {
    groupsEl.innerHTML = "";
    var add = el("div", "gc-gitem");
    add.innerHTML = '<div class="n" style="color:#4f46e5">＋ 新建群聊</div>';
    add.onclick = openCreate;
    groupsEl.appendChild(add);
    groups.forEach(function (g) {
      var it = el("div", "gc-gitem" + (g.id === currentGid ? " on" : ""));
      var nm = el("div", "n", g.name);
      var mt = el("div", "m", g.member_count + " 人 · " + (g.last && g.last.content ? g.last.content.slice(0, 12) : ""));
      it.appendChild(nm); it.appendChild(mt);
      it.onclick = function () { openGroup(g.id); };
      groupsEl.appendChild(it);
    });
  }

  async function refreshGroups() {
    try {
      var d = await api("/api/groups");
      groups = d.groups || [];
      renderGroups();
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
    msgsEl.appendChild(el("div", "gc-empty",
      "选择左侧群聊，或点「＋ 新建群聊」\n把几个单聊里的人物拉进一个群里畅聊"));
    costbar.style.display = "none";
    sendBtn.disabled = true;
    textEl.disabled = true;
  }

  // ---------- 群消息渲染 ----------
  function avatarHTML(m) {
    if (m && m.avatar && m.avatar.indexOf("data:image/") === 0) {
      return '<img src="' + m.avatar + '" alt="">';
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
    return r;
  }
  function appendMsg(m) {
    msgsEl.appendChild(msgRow(m));
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
  function scrollBottom() { msgsEl.scrollTop = msgsEl.scrollHeight; }

  async function openGroup(gid) {
    currentGid = gid;
    renderGroups();
    sendBtn.disabled = false;
    textEl.disabled = false;
    msgsEl.innerHTML = "";
    msgsEl.appendChild(el("div", "gc-empty", "加载中…"));
    try {
      var d = await api("/api/groups/" + gid);
      var g = d.group;
      $("gc-title").textContent = g.name + "（" + g.member_count + " 个角色）";
      membersCache[gid] = g.members || [];
      roundCostShown = 0;
      costbar.style.display = "none";
      var m2 = await api("/api/groups/" + gid + "/messages?limit=200");
      msgsEl.innerHTML = "";
      if (!(m2.messages || []).length) {
        msgsEl.appendChild(el("div", "gc-empty",
          "群聊已就绪。发第一条消息，" + g.member_names.join("、") + " 会依次回复。"));
      } else {
        m2.messages.forEach(function (m) { m.gid = gid; appendMsg(m); });
      }
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
    costbar.textContent = "本群 " + (membersCache[currentGid] || []).length +
      " 个角色，每条消息触发对应次数的模型调用…";
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
        costbar.textContent = "已完成 " + roundCostShown + " 个角色回复…（费用以钱包流水为准）";
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
      if (d.total_cny != null) {
        costbar.textContent = "本轮群聊消耗 ¥" + d.total_cny + "（" + d.replies + " 个角色回复）";
      } else {
        costbar.textContent = d.note || "本轮完成，计费落账中…";
      }
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
      var d = await api("/api/groups", { method: "POST", body: { name: name, members: payloadMembers } });
      $("gc-create").style.display = "none";
      await refreshGroups();
      openGroup(d.id);
    } catch (e) {
      fail(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 侧栏注入（尽力而为：主程序 SPA 动态渲染，MutationObserver 找「开启新对话」） ----------
  var sidebarTried = false;
  var SIDEBAR_LABELS = ["开启新对话", "新对话", "New chat", "new chat"];
  function tryInjectSidebar() {
    if (sidebarTried) return;
    var btns = document.querySelectorAll("button, [role=button], a");
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].textContent || "").trim();
      if (SIDEBAR_LABELS.indexOf(t) >= 0) {
        var host = btns[i].parentElement;
        if (!host) return;
        var item = el("div", "gc-navitem");
        item.innerHTML = '<span>👥</span><span>角色群聊</span>';
        item.onclick = openPanel;
        host.insertBefore(item, btns[i]);
        sidebarTried = true;
        return;
      }
    }
  }
  var mo = new MutationObserver(function () { tryInjectSidebar(); });
  mo.observe(document.body, { childList: true, subtree: true });

  // ---------- 面板开关 ----------
  function openPanel() {
    mask.style.display = "block";
    panel.style.display = "flex";
    $("gc-create").style.display = "none";
    if (!groups.length && !currentGid) renderEmpty();
    refreshGroups();
    loadPersonas().then(function (ps) { personas = ps; });
  }
  function closePanel() {
    mask.style.display = "none";
    panel.style.display = "none";
    $("gc-create").style.display = "none";
  }
  fab.onclick = openPanel;
  mask.onclick = closePanel;
  $("gc-close").onclick = closePanel;
  $("gc-send").onclick = sendCurrent;
  textEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendCurrent();
    }
  });
  $("gc-cback").onclick = function () { $("gc-create").style.display = "none"; };
  $("gc-cok").onclick = submitCreate;

  renderEmpty();
})();
