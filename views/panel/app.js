// WeChat management panel (AstrBot plugin view). Runs in AstrBot's sandboxed
// iframe: no alert()/confirm()/localStorage, so dialogs are built in-page and
// all state lives in memory. Every node is built with textContent (no HTML
// injection from chat names or log lines).
(() => {
  "use strict";

  const view = window.AstrBotPluginView;
  const $ = (id) => document.getElementById(id);
  const TITLES = {
    dashboard: "仪表盘",
    login: "登录管理",
    chats: "会话与通讯录",
    logs: "日志与事件",
  };
  const TERMINAL = new Set(["succeeded", "failed", "cancelled", "expired"]);
  const LOGIN_HINTS = {
    "-2007": "手机没有在有效期内确认，或在手机上拒绝了登录",
    login_poll_failed: "可能是用错了账号扫码，或在手机上点了取消；重新生成一个二维码再试",
    needs_login: "这台设备的登录凭据已失效，请改用扫码登录",
    rejected: "微信拒绝了设备确认登录，请改用扫码登录",
  };

  const state = {
    page: "dashboard",
    platform: "",
    overview: null,
    avatarFor: null,
    attempt: null,
    attemptStrategy: null,
    qrGeneration: null,
    attemptTimer: null,
    logSeq: 0,
    logPaused: false,
    logTab: "plugin",
    eventsAll: false,
    threads: [],
    threadCursor: null,
    threadsLoaded: false,
    threadFilter: "all",
    contacts: [],
    contactCursor: null,
    contactSearch: "",
    contactsLoaded: false,
    chatTab: "threads",
  };

  // ---------- small helpers ----------

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function toMillis(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function fmtTime(value) {
    const ms = toMillis(value);
    if (ms === null) return "—";
    const d = new Date(ms);
    const now = new Date();
    const time = d.toLocaleTimeString("zh-CN", { hour12: false });
    if (d.toDateString() === now.toDateString()) return time;
    return `${d.getMonth() + 1}-${d.getDate()} ${time.slice(0, 5)}`;
  }

  function fmtClock(value) {
    const ms = toMillis(value);
    if (ms === null) return "—";
    return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false }).slice(0, 5);
  }

  function ago(value) {
    const ms = toMillis(value);
    if (ms === null) return "从未";
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 10) return "刚刚";
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }

  function duration(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  }

  function toast(message, bad) {
    const el = h("div", { class: bad ? "toast bad" : "toast", text: message });
    $("toasts").append(el);
    setTimeout(() => el.remove(), bad ? 6000 : 3200);
  }

  function setKv(dl, rows) {
    dl.replaceChildren(
      ...rows.flatMap(([label, value]) => [
        h("dt", { text: label }),
        h("dd", {}, value instanceof Node ? value : String(value ?? "—")),
      ]),
    );
  }

  async function api(method, endpoint, payload) {
    if (!view) throw new Error("请在 AstrBot WebUI 的插件页面里打开这个面板");
    await view.ready();
    const data = Object.assign({}, payload || {});
    if (state.platform && !data.platform) data.platform = state.platform;
    return method === "GET"
      ? view.apiGet(`panel/${endpoint}`, data)
      : view.apiPost(`panel/${endpoint}`, data);
  }

  async function run(button, task) {
    if (button) button.disabled = true;
    try {
      return await task();
    } catch (error) {
      toast(error.message || String(error), true);
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ---------- dialogs ----------

  function dialog({ title, message, confirmText, danger, input, textarea, maxLength }) {
    return new Promise((resolve) => {
      let field = null;
      let counter = null;
      if (input || textarea) {
        field = textarea
          ? h("textarea", { rows: 4, maxlength: maxLength || null, placeholder: input || "" })
          : h("input", { placeholder: input || "", autocomplete: "off" });
        if (maxLength) {
          counter = h("div", { class: "counter", text: `0 / ${maxLength}` });
          field.addEventListener("input", () => {
            counter.textContent = `${field.value.length} / ${maxLength}`;
          });
        }
      }
      const close = (value) => {
        backdrop.remove();
        resolve(value);
      };
      const ok = h("button", {
        class: danger ? "btn danger solid" : "btn primary",
        text: confirmText || "确定",
        onclick: () => close(field ? field.value : true),
      });
      const backdrop = h(
        "div",
        {
          class: "modal-backdrop",
          onclick: (event) => {
            if (event.target === backdrop) close(null);
          },
        },
        h(
          "div",
          { class: "modal", role: "dialog" },
          h("h3", { text: title }),
          message ? h("p", { text: message }) : null,
          field,
          counter,
          h(
            "div",
            { class: "actions" },
            h("button", { class: "btn", text: "取消", onclick: () => close(null) }),
            ok,
          ),
        ),
      );
      backdrop.addEventListener("keydown", (event) => {
        if (event.key === "Escape") close(null);
      });
      document.body.append(backdrop);
      (field || ok).focus();
    });
  }

  // ---------- navigation ----------

  function showPage(page) {
    state.page = page;
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.page === page);
    });
    document.querySelectorAll(".page").forEach((section) => {
      section.classList.toggle("active", section.id === `page-${page}`);
    });
    $("page-title").textContent = TITLES[page];
    if (page === "chats" && !state.threadsLoaded) loadThreads(true);
    if (page === "logs") refreshLogs();
  }

  // ---------- overview ----------

  function accountStatus(o) {
    if (!o) return ["读取中", ""];
    if (!o.bridge.reachable) return ["桥接不可达", "bad"];
    const a = o.account;
    if (!a.authenticated) return ["已退出登录", "bad"];
    if (a.connection === "online") return ["在线", "ok live"];
    if (["connecting", "reconnecting"].includes(a.connection)) return ["连接中", "warn"];
    return [`连接：${a.connection || "未知"}`, "warn"];
  }

  function setPill(el, [label, kind]) {
    el.textContent = label;
    el.className = `pill ${kind}`.trim();
  }

  async function refreshOverview() {
    try {
      const o = await api("GET", "overview");
      state.overview = o;
      if (!state.platform) state.platform = o.platformId || "";
      renderOverview(o);
    } catch (error) {
      setPill($("top-status"), ["面板读取失败", "bad"]);
      $("dash-banner").replaceChildren(
        h(
          "div",
          { class: "banner bad" },
          h("div", { class: "grow" }, h("strong", { text: "读取状态失败" }), error.message),
        ),
      );
    }
  }

  function renderOverview(o) {
    const status = accountStatus(o);
    const a = o.account;
    const rt = o.runtime;
    setPill($("top-status"), status);
    setPill($("bot-status"), status);

    const select = $("platform-select");
    select.hidden = o.platforms.length <= 1;
    if (!select.hidden && select.options.length !== o.platforms.length) {
      select.replaceChildren(
        ...o.platforms.map((p) => h("option", { value: p.id, text: p.id })),
      );
    }
    select.value = o.platformId;

    $("foot-version").textContent =
      `插件 v${o.pluginVersion || "?"}` + (o.bridge.version ? ` · 桥接 ${o.bridge.version}` : "");

    // Banner: the one thing that needs attention, if any.
    let banner = null;
    const goLogin = h("button", { class: "btn primary", text: "去登录", "data-goto": "login" });
    if (!o.bridge.reachable) {
      banner = h(
        "div",
        { class: "banner bad" },
        h("div", { class: "grow" }, h("strong", { text: "连不上微信桥接" }), o.bridge.error || ""),
      );
    } else if (!a.authenticated) {
      banner = h(
        "div",
        { class: "banner bad" },
        h(
          "div",
          { class: "grow" },
          h("strong", { text: "微信已退出登录，机器人收不到消息" }),
          "通常是手机上切换或退出了小号。请到「登录管理」重新登录。",
        ),
        goLogin,
      );
    } else if (a.connection !== "online") {
      banner = h(
        "div",
        { class: "banner warn" },
        h(
          "div",
          { class: "grow" },
          h("strong", { text: `连接状态：${a.connection || "未知"}` }),
          "桥接会自动重连；长时间不恢复可以在「登录管理」里点“重新连接”。",
        ),
      );
    } else if (rt.lastError && (toMillis(rt.lastErrorAt) || 0) > (toMillis(rt.lastPollAt) || 0)) {
      banner = h(
        "div",
        { class: "banner warn" },
        h("div", { class: "grow" }, h("strong", { text: "收消息出错" }), rt.lastError),
      );
    }
    $("dash-banner").replaceChildren(...(banner ? [banner] : []));

    // Account hero.
    const name = o.botName || a.alias || "微信机器人";
    $("bot-name").textContent = name;
    $("bot-sub").textContent = [
      a.ownUsername || "未登录",
      a.alias ? `桥接账号 ${a.alias}` : null,
      a.incomingTransport ? `接收方式 ${a.incomingTransport}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    const avatar = $("bot-avatar");
    if (!avatar.querySelector("img")) avatar.textContent = name.slice(0, 1);
    if (a.ownUsername && state.avatarFor !== a.ownUsername) {
      state.avatarFor = a.ownUsername;
      api("GET", "avatar")
        .then((res) => {
          if (res && res.dataUrl) avatar.replaceChildren(h("img", { src: res.dataUrl, alt: "" }));
          else state.avatarFor = null; // retry on a later refresh
        })
        .catch(() => {
          state.avatarFor = null;
        });
    }
    $("login-own-hint").textContent = a.ownUsername
      ? `也就是 ${o.botName ? `${o.botName}（${a.ownUsername}）` : a.ownUsername}，`
      : "";

    // Stats.
    $("stat-recv").textContent = rt.receivedToday;
    $("stat-recv-foot").textContent = `启动以来 ${rt.receivedTotal} 条`;
    $("stat-sent").textContent = rt.sentToday;
    $("stat-sent-foot").textContent = `启动以来 ${rt.sentTotal} 条`;
    $("stat-last-recv").textContent = rt.lastReceivedAt ? fmtClock(rt.lastReceivedAt) : "—";
    $("stat-last-recv-foot").textContent = rt.lastReceivedAt ? ago(rt.lastReceivedAt) : "启动以来还没有";
    $("stat-last-sent").textContent = rt.lastSentAt ? fmtClock(rt.lastSentAt) : "—";
    $("stat-last-sent-foot").textContent = rt.lastSentAt ? ago(rt.lastSentAt) : "启动以来还没有";

    // Alerts.
    const al = o.alerts;
    $("alert-hint").textContent = al.watching ? "每 30 秒检查一次" : "";
    let current;
    if (!al.watching) current = h("span", { class: "muted", text: "未运行（没有设置提醒会话）" });
    else if (al.offline)
      current = h("span", {
        class: "badge danger",
        text: `已离线 ${duration(al.offlineSeconds)}，${al.alerted ? "已发提醒" : "未到提醒时间"}`,
      });
    else current = h("span", { class: "badge direct", text: "正常" });
    setKv($("alert-kv"), [
      [
        "提醒发往",
        al.sessions.length
          ? h("span", { class: "mono", text: al.sessions.join("\n") })
          : h("span", { class: "muted", text: "未设置" }),
      ],
      ["离线多久提醒", `${al.afterSeconds} 秒`],
      ["当前", current],
      ["最近检查", al.checkedAt ? `${ago(al.checkedAt)}：${al.detail || ""}` : "—"],
    ]);

    // Environment.
    setKv($("env-kv"), [
      ["插件版本", o.pluginVersion ? `v${o.pluginVersion}` : "—"],
      [
        "桥接",
        o.bridge.reachable
          ? `agent-wechat ${o.bridge.version || "?"}（${o.bridge.state || "?"}）`
          : `不可达：${o.bridge.error || ""}`,
      ],
      ["适配器", rt.running ? `运行中，${ago(rt.startedAt)}启动` : "未运行"],
      ["最后轮询", rt.lastPollAt ? ago(rt.lastPollAt) : "—"],
      [
        "最近错误",
        rt.lastError
          ? h("span", {}, h("span", { class: "muted", text: `${fmtTime(rt.lastErrorAt)} ` }), rt.lastError)
          : "无",
      ],
    ]);

    // Policy.
    const p = o.policy;
    setKv($("policy-kv"), [
      [
        "允许的群",
        p.allowedGroups.length
          ? h("span", { class: "mono", text: p.allowedGroups.join("  ") })
          : "没有",
      ],
      ["好友私聊", !p.privateChats ? "关闭" : p.friendWhitelist ? `白名单 ${p.allowedFriends} 人` : "所有好友"],
      ["主动消息", p.proactiveMessages ? "开启（定时提醒等可随时发到允许的会话）" : "仅限被 @ 后的短时间内"],
    ]);

    renderLoginCurrent(o, status);
  }

  // ---------- login ----------

  function renderLoginCurrent(o, status) {
    const a = o.account;
    const pill = h("span", { class: "pill" });
    setPill(pill, status);
    $("login-current").replaceChildren(
      pill,
      h("span", {
        class: "muted",
        text: a.authenticated
          ? `${o.botName || a.ownUsername || ""} · 自动重连${a.autoConnect ? "已开启" : "已关闭"}`
          : "需要重新登录",
      }),
    );
    const busy = state.attempt && !TERMINAL.has(state.attempt.state);
    $("login-saved").disabled = !!busy;
    $("login-qr").disabled = !!busy;
  }

  function stepsFor(strategy, op) {
    const qr = strategy === "qr" || (op && op.result && op.result.qrAvailable);
    return qr
      ? [
          ["starting", "发起登录"],
          ["awaiting_scan", "用机器人小号扫码"],
          ["awaiting_approval", "在手机上确认登录"],
          ["authenticating", "正在登录"],
          ["succeeded", "登录成功"],
        ]
      : [
          ["starting", "发起登录"],
          ["awaiting_approval", "在手机上确认登录"],
          ["authenticating", "正在登录"],
          ["succeeded", "登录成功"],
        ];
  }

  function renderAttempt() {
    const op = state.attempt;
    if (!op) {
      $("attempt-area").hidden = true;
      return;
    }
    $("attempt-area").hidden = false;
    const steps = stepsFor(state.attemptStrategy, op);
    let index = steps.findIndex(([key]) => key === op.state);
    const failed = op.state === "failed" || op.state === "cancelled" || op.state === "expired";
    if (index < 0) index = failed ? Math.max(0, state.lastStepIndex || 0) : 0;
    if (!failed) state.lastStepIndex = index;
    $("attempt-steps").replaceChildren(
      ...steps.map(([, label], i) => {
        let cls = "";
        if (op.state === "succeeded" || i < index) cls = "done";
        else if (i === index) cls = failed ? "failed" : "current";
        return h(
          "li",
          { class: cls },
          h("span", { class: "dot", text: cls === "done" ? "✓" : cls === "failed" ? "✕" : String(i + 1) }),
          label,
        );
      }),
    );

    const result = op.result || {};
    let detail = "";
    if (op.state === "succeeded") detail = "登录成功，机器人会自动恢复收发消息。";
    else if (failed) {
      const code = result.code || op.state;
      const service = result.serviceCode;
      const hint = LOGIN_HINTS[String(service)] || LOGIN_HINTS[code] || "";
      detail = `登录失败：${code}${service !== null && service !== undefined ? `（serviceCode ${service}）` : ""}${hint ? `。${hint}` : ""}`;
    } else if (result.expiresAt) {
      const left = Math.round(result.expiresAt - Date.now() / 1000);
      detail = left > 0 ? `剩余 ${duration(left)}` : "已过期，桥接正在刷新…";
    }
    $("attempt-detail").textContent = detail;
    $("attempt-detail").className = failed ? "callout bad" : "muted";
    $("attempt-cancel").hidden = TERMINAL.has(op.state);
    $("attempt-close").hidden = !TERMINAL.has(op.state);

    const box = $("qr-box");
    const showQr = result.qrAvailable || state.attemptStrategy === "qr";
    if (!showQr) {
      box.className = "qr-box";
      box.replaceChildren(
        h(
          "span",
          {},
          op.state === "succeeded" ? "✓ 已登录" : failed ? "本次登录已结束" : "请在手机微信上点「确认登录」",
        ),
      );
    } else {
      box.classList.toggle("expired", TERMINAL.has(op.state) && op.state !== "succeeded");
    }
  }

  async function loadQr() {
    const op = state.attempt;
    const generation = op && op.result ? op.result.qrGeneration : null;
    if (!op || !op.result || !op.result.qrAvailable || generation === state.qrGeneration) return;
    const res = await api("GET", "login/qr", { id: op.id, generation: generation ?? "" });
    if (res.available) {
      state.qrGeneration = generation;
      $("qr-box").className = "qr-box";
      $("qr-box").replaceChildren(h("img", { src: res.dataUrl, alt: "微信登录二维码" }));
    }
  }

  async function pollAttempt() {
    clearTimeout(state.attemptTimer);
    const op = state.attempt;
    if (!op || TERMINAL.has(op.state)) return;
    try {
      const next = await api("GET", "login/attempt", { id: op.id });
      if (state.attempt && state.attempt.id === next.id) {
        state.attempt = next;
        await loadQr();
        renderAttempt();
        if (TERMINAL.has(next.state) && state.overview) {
          renderLoginCurrent(state.overview, accountStatus(state.overview));
        }
        if (next.state === "succeeded") {
          toast("微信登录成功");
          refreshOverview();
        } else if (next.state === "failed") {
          toast("这次登录没有成功，详情见登录管理", true);
        }
      }
    } catch (error) {
      $("attempt-detail").textContent = `查询进度失败：${error.message}，稍后重试…`;
    }
    if (state.attempt && !TERMINAL.has(state.attempt.state)) {
      state.attemptTimer = setTimeout(pollAttempt, 2000);
    }
  }

  async function startLogin(strategy, button) {
    const op = await run(button, () => api("POST", "login", { strategy }));
    if (!op) return;
    state.attempt = op;
    state.attemptStrategy = strategy;
    state.qrGeneration = null;
    state.lastStepIndex = 0;
    $("qr-box").className = "qr-box";
    $("qr-box").replaceChildren(h("span", { class: "muted", text: "等待二维码…" }));
    renderAttempt();
    if (state.overview) renderLoginCurrent(state.overview, accountStatus(state.overview));
    pollAttempt();
  }

  // ---------- chats ----------

  async function loadThreads(reset) {
    if (reset) {
      state.threads = [];
      state.threadCursor = null;
    }
    $("thread-body").replaceChildren(
      ...(state.threads.length ? [] : [h("tr", {}, h("td", { colspan: 6, class: "empty", text: "加载中…" }))]),
    );
    try {
      const page = await api("GET", "threads", state.threadCursor ? { cursor: state.threadCursor } : {});
      state.threads = state.threads.concat(page.items || []);
      state.threadCursor = page.nextCursor || null;
      state.threadsLoaded = true;
    } catch (error) {
      toast(error.message, true);
    }
    renderThreads();
  }

  function renderThreads() {
    const term = $("thread-search").value.trim().toLowerCase();
    const rows = state.threads.filter((t) => {
      if (state.threadFilter === "permitted" && !t.permitted) return false;
      if (["group", "direct"].includes(state.threadFilter) && t.kind !== state.threadFilter) return false;
      return !term || `${t.name} ${t.id}`.toLowerCase().includes(term);
    });
    const kindLabel = { group: "群聊", direct: "私聊", system: "系统" };
    $("thread-body").replaceChildren(
      ...(rows.length
        ? rows.map((t) =>
            h(
              "tr",
              {},
              h("td", {}, h("div", { class: "name", text: t.name }), h("div", { class: "sub mono", text: t.id })),
              h("td", {}, h("span", { class: `badge ${t.kind}`, text: kindLabel[t.kind] || t.kind })),
              h(
                "td",
                { class: "last" },
                t.last
                  ? [
                      h("div", {
                        class: "text",
                        text: `${t.last.direction === "outgoing" ? "我" : t.last.sender || ""}：${t.last.text || "[非文字消息]"}`,
                        title: t.last.text || "",
                      }),
                      h("div", { class: "sub", text: fmtTime(t.last.createdAt) }),
                    ]
                  : h("span", { class: "muted", text: "—" }),
              ),
              h("td", {}, t.unread ? h("span", { class: "badge warn", text: t.unread }) : h("span", { class: "muted", text: "0" })),
              h("td", {}, t.permitted ? h("span", { class: "yes", text: "✓ 会回复" }) : h("span", { class: "no", text: "不回复" })),
              h(
                "td",
                {},
                t.permitted
                  ? h("button", { class: "btn small", text: "发测试消息", onclick: () => sendTest(t) })
                  : null,
              ),
            ),
          )
        : [h("tr", {}, h("td", { colspan: 6, class: "empty", text: state.threadsLoaded ? "没有符合条件的会话" : "加载中…" }))]),
    );
    $("thread-more-row").hidden = !state.threadCursor;
  }

  async function sendTest(thread) {
    const text = await dialog({
      title: `发测试消息到「${thread.name}」`,
      message: "消息会以机器人小号的身份真实发出，群里所有人都能看到。",
      textarea: true,
      input: "例如：面板测试，请忽略",
      maxLength: 500,
      confirmText: "发送",
    });
    if (!text || !text.trim()) return;
    const res = await run(null, () => api("POST", "send-test", { thread_id: thread.id, text: text.trim() }));
    if (res) {
      toast("测试消息已发出");
      refreshOverview();
    }
  }

  let contactTimer = null;
  async function loadContacts(reset) {
    if (reset) {
      state.contacts = [];
      state.contactCursor = null;
    }
    const params = {};
    if (state.contactSearch) params.search = state.contactSearch;
    if (state.contactCursor) params.cursor = state.contactCursor;
    try {
      const page = await api("GET", "contacts", params);
      state.contacts = state.contacts.concat(page.items || []);
      state.contactCursor = page.nextCursor || null;
      state.contactsLoaded = true;
    } catch (error) {
      toast(error.message, true);
    }
    const categories = { group: "群聊", system: "系统", unknown: "联系人", official: "公众号" };
    $("contact-body").replaceChildren(
      ...(state.contacts.length
        ? state.contacts.map((c) => {
            const main = c.remark || c.displayName || c.nickname || c.id;
            const extra = [c.remark && c.nickname && c.remark !== c.nickname ? `昵称 ${c.nickname}` : null, c.alias ? `微信号 ${c.alias}` : null]
              .filter(Boolean)
              .join(" · ");
            return h(
              "tr",
              {},
              h("td", {}, h("div", { class: "name", text: main }), extra ? h("div", { class: "sub", text: extra }) : null),
              h("td", { class: "mono", text: c.id }),
              h("td", {}, h("span", { class: "badge", text: categories[c.category] || c.category || "—" })),
            );
          })
        : [h("tr", {}, h("td", { colspan: 3, class: "empty", text: "没有找到联系人" }))]),
    );
    $("contact-more-row").hidden = !state.contactCursor;
  }

  function showChatTab(tab) {
    state.chatTab = tab;
    document.querySelectorAll("#chat-tabs .chip").forEach((chip) => {
      chip.classList.toggle("active", chip.dataset.tab === tab);
    });
    $("chat-threads").hidden = tab !== "threads";
    $("chat-contacts").hidden = tab !== "contacts";
    if (tab === "contacts" && !state.contactsLoaded) loadContacts(true);
  }

  // ---------- logs and events ----------

  function nearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  async function refreshLogs() {
    if (state.logPaused || state.page !== "logs") return;
    if (state.logTab === "plugin") {
      const box = $("log-view");
      try {
        const res = await api("GET", "logs", { after: state.logSeq });
        if (res.seq < state.logSeq) {
          // Plugin reloaded: its buffer restarted.
          state.logSeq = 0;
          box.replaceChildren();
          return refreshLogs();
        }
        const stick = nearBottom(box);
        for (const item of res.items || []) {
          box.append(
            h(
              "div",
              { class: "log-line", "data-level": item.level },
              h("span", { class: "t", text: fmtTime(item.time) }),
              h("span", { class: `lv lv-${item.level}`, text: item.level }),
              h("span", { class: "msg", text: item.message }),
            ),
          );
          state.logSeq = Math.max(state.logSeq, item.seq);
        }
        while (box.childElementCount > 1000) box.firstElementChild.remove();
        if (!box.childElementCount) box.append(h("div", { class: "empty", text: "插件重启以来还没有日志" }));
        else box.querySelector(".empty")?.remove();
        if (stick) box.scrollTop = box.scrollHeight;
      } catch (error) {
        toast(error.message, true);
      }
    } else {
      const box = $("event-view");
      try {
        const res = await api("GET", "events", { all: state.eventsAll ? 1 : 0 });
        // The bridge repeats e.g. "awaiting_approval" every few seconds while a
        // login waits; fold identical neighbours into one line with a count.
        const rows = [];
        for (const item of res.items || []) {
          const key = `${item.kind}|${item.summary}|${(item.data && item.data.id) || ""}`;
          const last = rows[rows.length - 1];
          if (last && last.key === key) {
            last.count += 1;
            last.first = item.createdAt;
          } else rows.push({ key, item, count: 1, first: item.createdAt });
        }
        box.replaceChildren(
          ...(rows.length
            ? rows.map(({ item, count, first }) =>
                h(
                  "div",
                  { class: "log-line event-line" },
                  h("span", { class: "t", text: fmtTime(item.createdAt) }),
                  h("span", {}, h("span", { class: `badge ${item.kind.startsWith("account.") ? "warn" : ""}`, text: item.kind })),
                  h(
                    "span",
                    { class: "msg" },
                    item.summary || "",
                    count > 1 ? h("span", { class: "muted", text: `  ×${count}（自 ${fmtTime(first)}）` }) : null,
                  ),
                ),
              )
            : [h("div", { class: "empty", text: "最近没有重要事件（可勾选“显示全部事件”）" })]),
        );
      } catch (error) {
        toast(error.message, true);
      }
    }
  }

  function showLogTab(tab) {
    state.logTab = tab;
    document.querySelectorAll("#log-tabs .chip").forEach((chip) => {
      chip.classList.toggle("active", chip.dataset.tab === tab);
    });
    $("log-view").hidden = tab !== "plugin";
    $("event-view").hidden = tab !== "events";
    $("log-level-wrap").hidden = tab !== "plugin";
    $("event-all-wrap").hidden = tab !== "events";
    refreshLogs();
  }

  // ---------- wiring ----------

  function bind() {
    document.addEventListener("click", (event) => {
      const nav = event.target.closest("[data-page]");
      if (nav && nav.classList.contains("nav-item")) showPage(nav.dataset.page);
      const go = event.target.closest("[data-goto]");
      if (go) showPage(go.dataset.goto);
    });
    $("refresh-btn").addEventListener("click", () => {
      refreshOverview();
      if (state.page === "chats") {
        if (state.chatTab === "threads") loadThreads(true);
        else loadContacts(true);
      }
      if (state.page === "logs") refreshLogs();
    });
    $("platform-select").addEventListener("change", (event) => {
      state.platform = event.target.value;
      state.avatarFor = null;
      state.threadsLoaded = false;
      state.contactsLoaded = false;
      refreshOverview();
      if (state.page === "chats") loadThreads(true);
    });

    $("login-saved").addEventListener("click", (event) => startLogin("saved-device", event.currentTarget));
    $("login-qr").addEventListener("click", (event) => startLogin("qr", event.currentTarget));
    $("attempt-cancel").addEventListener("click", (event) =>
      run(event.currentTarget, async () => {
        if (!state.attempt) return;
        await api("POST", "login/cancel", { id: state.attempt.id });
        state.attempt = Object.assign({}, state.attempt, { state: "cancelled", result: { code: "已取消" } });
        renderAttempt();
        if (state.overview) renderLoginCurrent(state.overview, accountStatus(state.overview));
      }),
    );
    $("attempt-close").addEventListener("click", () => {
      state.attempt = null;
      renderAttempt();
      if (state.overview) renderLoginCurrent(state.overview, accountStatus(state.overview));
    });
    $("conn-connect").addEventListener("click", (event) =>
      run(event.currentTarget, async () => {
        await api("POST", "connection", { action: "connect" });
        toast("已请求重新连接");
        setTimeout(refreshOverview, 1500);
      }),
    );
    $("conn-disconnect").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const yes = await dialog({
        title: "断开微信连接？",
        message: "断开后机器人暂时收不到、发不出消息；登录状态保留，可以随时点“重新连接”。",
        confirmText: "断开",
        danger: true,
      });
      if (!yes) return;
      await run(button, async () => {
        await api("POST", "connection", { action: "disconnect" });
        toast("已断开连接");
        setTimeout(refreshOverview, 1000);
      });
    });
    $("conn-logout").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const typed = await dialog({
        title: "退出微信登录？",
        message: "会注销桥接上的机器人小号并关闭自动重连，之后只能重新扫码登录。确认请输入“退出登录”。",
        input: "退出登录",
        confirmText: "退出登录",
        danger: true,
      });
      if (typed === null) return;
      if (typed.trim() !== "退出登录") {
        toast("输入不一致，已取消", true);
        return;
      }
      await run(button, async () => {
        await api("POST", "logout", { confirm: "退出登录" });
        toast("已退出登录");
        setTimeout(refreshOverview, 1000);
      });
    });

    document.querySelectorAll("#chat-tabs .chip").forEach((chip) => {
      chip.addEventListener("click", () => showChatTab(chip.dataset.tab));
    });
    document.querySelectorAll("#thread-filter .chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        state.threadFilter = chip.dataset.filter;
        document.querySelectorAll("#thread-filter .chip").forEach((c) => c.classList.toggle("active", c === chip));
        renderThreads();
      });
    });
    $("thread-search").addEventListener("input", renderThreads);
    $("thread-reload").addEventListener("click", () => loadThreads(true));
    $("thread-more").addEventListener("click", () => loadThreads(false));
    $("contact-search").addEventListener("input", (event) => {
      clearTimeout(contactTimer);
      contactTimer = setTimeout(() => {
        state.contactSearch = event.target.value.trim();
        loadContacts(true);
      }, 400);
    });
    $("contact-more").addEventListener("click", () => loadContacts(false));

    document.querySelectorAll("#log-tabs .chip").forEach((chip) => {
      chip.addEventListener("click", () => showLogTab(chip.dataset.tab));
    });
    $("log-warn-only").addEventListener("change", (event) => {
      $("log-view").classList.toggle("warn-only", event.target.checked);
    });
    $("event-all").addEventListener("change", (event) => {
      state.eventsAll = event.target.checked;
      refreshLogs();
    });
    $("log-pause").addEventListener("click", (event) => {
      state.logPaused = !state.logPaused;
      event.currentTarget.textContent = state.logPaused ? "继续" : "暂停";
      if (!state.logPaused) refreshLogs();
    });
  }

  function visible() {
    return document.visibilityState !== "hidden";
  }

  function start() {
    bind();
    if (view && typeof view.onContext === "function") {
      view.onContext((ctx) => {
        if (ctx && typeof ctx.isDark === "boolean") {
          document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
        }
      });
    }
    refreshOverview();
    setInterval(() => visible() && refreshOverview(), 5000);
    setInterval(() => visible() && state.logTab === "plugin" && refreshLogs(), 3000);
    setInterval(() => visible() && state.logTab === "events" && refreshLogs(), 6000);
    setInterval(() => state.attempt && !TERMINAL.has(state.attempt.state) && renderAttempt(), 1000);
  }

  start();
})();
