// 上下文时间线：读取插件后端 /list /detail，渲染逐请求的组成与变化。
// 只依赖 AstrBot 注入的 window.AstrBotPluginView bridge，无第三方库。

// 两种运行方式共用同一份页面：
// 1) AstrBot 插件视图：走注入的 window.AstrBotPluginView bridge；
// 2) 插件自带的本地页面（http://127.0.0.1:6199）：没有 bridge，直接 fetch 同源 /api/*。
function localBridge() {
  return {
    ready: async () => ({}),
    apiGet: async (path, params = {}) => {
      const qs = new URLSearchParams(params).toString();
      const res = await fetch(`/api/${path}${qs ? `?${qs}` : ""}`);
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body && body.message) msg = body.message;
        } catch {
          /* 响应体不是 JSON 就用状态码 */
        }
        throw new Error(msg);
      }
      return await res.json();
    },
  };
}

const bridge = window.AstrBotPluginView || localBridge();

// AstrBot 的插件视图 iframe 带 sandbox="allow-scripts allow-forms allow-downloads"，
// 没有 allow-same-origin，所以 origin 是 opaque 的：访问 localStorage 会抛 SecurityError。
// 之前把 localStorage 直接写在模块顶层，异常导致整个模块不执行，页面就永远停在「加载中」。
const storage = (() => {
  try {
    const probe = "__ctxdump_probe__";
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    const mem = new Map();
    return {
      getItem: (key) => (mem.has(key) ? mem.get(key) : null),
      setItem: (key, value) => mem.set(key, String(value)),
    };
  }
})();

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} 超时（${ms}ms）`)), ms)),
  ]);
}

const CATS = [
  ["system", "系统提示词", "#6b8cff"],
  ["tools", "工具定义", "#8b5cf6"],
  ["user", "用户消息", "#22c55e"],
  ["assistant", "助手回复", "#f59e0b"],
  ["tool_calls", "工具调用", "#ef4444"],
  ["tool_result", "工具结果", "#14b8a6"],
  ["reasoning", "推理", "#a855f7"],
  ["summary", "压缩摘要", "#f43f5e"],
  ["prompt", "本次输入", "#f472b6"],
];
const CAT_LABEL = Object.fromEntries(CATS.map(([k, label]) => [k, label]));
const CAT_COLOR = Object.fromEntries(CATS.map(([k, , c]) => [k, c]));

const state = {
  date: "",
  umo: "",
  steps: [],
  selected: null,
  detail: null,
  notice: null,
  // 消息列表 / 时间线的视图偏好；沙箱 iframe 里 localStorage 不可用，自动退化成内存
  tlOrder: storage.getItem("ctxdump.tlOrder") || "desc",
  msgsOrder: storage.getItem("ctxdump.msgsOrder") || "asc",
  msgsMode: storage.getItem("ctxdump.msgsMode2") || "diff",
};

const $ = (sel) => document.querySelector(sel);

function today() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function fmtTok(n) {
  n = Number(n) || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, params = {}) {
  const res = await bridge.apiGet(path, params);
  if (res && res.status === "error") throw new Error(res.message || "接口返回错误");
  if (!res) throw new Error("接口无返回");
  return res;
}

// ---------------------------------------------------------------- 渲染：条形
function buildBar(breakdown, total, options = {}) {
  const bar = el("div", "bar");
  const denom = Math.max(1, options.scale || total || 1);
  bar.style.width = options.scale ? `${Math.min(100, (total / denom) * 100)}%` : "100%";
  bar.classList.add("bar-inner");
  const wrap = el("div", "bar-wrap");
  wrap.appendChild(bar);
  for (const [key, , color] of CATS) {
    const v = breakdown[key] || 0;
    if (!v) continue;
    const seg = el("div", "seg");
    seg.style.width = `${(v / Math.max(1, total)) * 100}%`;
    seg.style.background = color;
    seg.title = `${CAT_LABEL[key]} ≈ ${fmtTok(v)} tokens`;
    bar.appendChild(seg);
  }
  if (total === 0) {
    bar.appendChild(el("div", "seg seg-empty"));
  }
  return wrap;
}

// ---------------------------------------------------------------- 渲染：时间线
function renderTimeline() {
  const box = $("#timeline");
  box.textContent = "";
  $("#count").textContent = state.steps.length ? `共 ${state.steps.length} 次请求` : "";

  if (!state.steps.length) {
    box.appendChild(el("div", "empty", "这一天没有记录。发一条消息给机器人再刷新。"));
    return;
  }

  const maxTotal = Math.max(...state.steps.map((s) => s.tokens || 0), 1);
  const kpi = $("#kpis");
  kpi.textContent = "";
  const sum = state.steps.reduce((a, s) => a + (s.tokens || 0), 0);
  const maxCtx = Math.max(...state.steps.map((s) => s.n_contexts || 0), 0);
  for (const [label, value] of [
    ["请求", state.steps.length],
    ["估算 token 合计", fmtTok(sum)],
    ["单次最大", fmtTok(maxTotal)],
    ["contexts 峰值", maxCtx],
  ]) {
    const chip = el("div", "kpi");
    chip.appendChild(el("span", "kpi-k", label));
    chip.appendChild(el("span", "kpi-v", typeof value === "number" && label === "请求" ? value : value));
    kpi.appendChild(chip);
  }

  // 时间线排序：默认最新在上（逆序）
  const ordered = state.tlOrder === "desc" ? [...state.steps].reverse() : state.steps;

  for (const step of ordered) {
    const row = el("button", "step");
    row.dataset.id = step.id;
    if (state.selected === step.id) row.classList.add("active");

    const head = el("div", "step-head");
    head.appendChild(el("span", "seq", `#${step.n ?? step.seq}`));
    head.appendChild(el("span", "time", fmtTime(step.iso)));
    head.appendChild(el("span", "spacer"));
    const diff = step.diff || {};
    if (diff.new) head.appendChild(el("span", "tag tag-new", `+${diff.new}`));
    if (diff.changed) head.appendChild(el("span", "tag tag-chg", `~${diff.changed}`));
    if (step.compress) {
      const c = step.compress;
      const tag = el(
        "span",
        "tag tag-cut",
        `✂ ${c.kind === "summary" ? "摘要压缩" : "截断"} -${fmtTok(Math.max(0, c.saved))}`,
      );
      tag.title = `上下文 ${c.before} 条 → ${c.after} 条，估算 ${fmtTok(c.before_tokens)} → ${fmtTok(c.after_tokens)} tokens`;
      head.appendChild(tag);
    }
    head.appendChild(el("span", "ctx", `${step.n_contexts} 条`));
    head.appendChild(el("span", "tok", fmtTok(step.tokens)));
    row.appendChild(head);

    // 用全体最大值做刻度，才能看出各轮相对大小
    row.appendChild(buildBar(step.breakdown || {}, step.tokens || 0, { scale: maxTotal }));
    row.addEventListener("click", () => selectStep(step.id));
    box.appendChild(row);
  }
}

// ---------------------------------------------------------------- 渲染：详情
function renderDelta(step) {
  const prev = step.prev;
  if (!prev) return el("div", "muted", "这是该会话的第一条请求。");
  const box = el("div", "delta");
  const cur = step.breakdown || {};
  const old = prev.breakdown || {};
  const total = (cur.total || 0) - (old.total || 0);
  const head = el("div", "delta-head");
  head.appendChild(el("span", "muted", `对比 #${prev.n ?? prev.seq}：`));
  const badge = el("span", "badge " + (total >= 0 ? "up" : "down"), `${total >= 0 ? "+" : ""}${fmtTok(total)} tokens`);
  head.appendChild(badge);
  head.appendChild(el("span", "muted", `（上下文 ${prev.n_contexts ?? "-"} → ${step.n_contexts} 条）`));
  box.appendChild(head);

  const chips = el("div", "chips");
  for (const [key, , color] of CATS) {
    const d = (cur[key] || 0) - (old[key] || 0);
    if (!d) continue;
    const chip = el("span", "chip " + (d > 0 ? "up" : "down"));
    chip.style.borderColor = color;
    chip.appendChild(el("span", "dot", "●")).style.color = color;
    if (key === "prompt") {
      // 本次输入是「这一轮的用户消息」，不是累积量：它的正负只说明这轮输入比上轮长/短，
      // 不代表上下文变化。所以同时给出绝对值，避免误读。
      chip.appendChild(el("span", null, `本次输入 ${fmtTok(cur.prompt)}（较上轮 ${d > 0 ? "+" : ""}${fmtTok(d)}）`));
      chip.title = "本轮用户输入的 token；括号里是它比上一轮输入长/短多少，与上下文增长无关";
    } else {
      chip.appendChild(el("span", null, `${CAT_LABEL[key]} ${d > 0 ? "+" : ""}${fmtTok(d)}`));
      chip.title = `相对上一轮的 ${CAT_LABEL[key]} 变化`;
    }
    chips.appendChild(chip);
  }
  if (chips.childElementCount) box.appendChild(chips);
  return box;
}

function collapse(title, node, open) {
  const d = document.createElement("details");
  d.className = "sec";
  if (open) d.open = true;
  const s = document.createElement("summary");
  s.textContent = title;
  d.appendChild(s);
  d.appendChild(node);
  return d;
}

// ------------------------------------------------------------ 消息列表（排序 / 增量）
function msgTokens(step) {
  const b = step.breakdown || {};
  return (b.user || 0) + (b.assistant || 0) + (b.tool_result || 0) + (b.tool_calls || 0) + (b.reasoning || 0);
}

function seg(title, options, current, onPick) {
  const wrap = el("div", "seg-ctl");
  wrap.appendChild(el("span", "seg-title", title));
  for (const [value, label] of options) {
    const btn = el("button", "seg-btn" + (value === current ? " on" : ""), label);
    btn.addEventListener("click", () => onPick(value));
    wrap.appendChild(btn);
  }
  return wrap;
}

function rerenderDetail() {
  if (state.detail) renderDetail(state.detail);
}

function buildMessageSection(step) {
  const contexts = step.contexts || [];
  const items = step.msg_items || [];
  const diff = step.diff || {};

  const sec = document.createElement("details");
  sec.className = "sec";
  sec.open = true;

  const summary = document.createElement("summary");
  summary.appendChild(document.createTextNode(`消息 · ${contexts.length} 条 · ≈${fmtTok(msgTokens(step))} tokens `));
  if (diff.new) summary.appendChild(el("span", "tag tag-new", `+${diff.new}`));
  if (diff.changed) summary.appendChild(el("span", "tag tag-chg", `~${diff.changed}`));
  if (diff.removed) summary.appendChild(el("span", "tag tag-del", `-${diff.removed}`));
  sec.appendChild(summary);

  const bar = el("div", "msgs-bar");
  bar.appendChild(
    seg(
      "排序",
      [
        ["asc", "旧 → 新"],
        ["desc", "新 → 旧"],
      ],
      state.msgsOrder,
      (v) => {
        state.msgsOrder = v;
        storage.setItem("ctxdump.msgsOrder", v);
        rerenderDetail();
      },
    ),
  );
  bar.appendChild(
    seg(
      "显示",
      [
        ["all", "全部"],
        ["diff", "仅变更"],
      ],
      state.msgsMode,
      (v) => {
        state.msgsMode = v;
        storage.setItem("ctxdump.msgsMode", v);
        rerenderDetail();
      },
    ),
  );
  sec.appendChild(bar);
  const hint = el(
    "div",
    "msgs-hint",
    "左侧色条＝角色：蓝 系统 · 绿 用户 · 橙 助手 · 青 工具；「#n 注入」＝这条消息在第 n 次请求时进入上下文",
  );
  sec.appendChild(hint);

  const list = el("div", "msgs");
  let order = contexts.map((_, i) => i);
  if (state.msgsOrder === "desc") order.reverse();
  if (state.msgsMode === "diff") {
    order = order.filter((i) => {
      const st = (items[i] || {}).state;
      return st === "new" || st === "changed";
    });
  }

  if (!order.length) {
    list.appendChild(
      el("div", "empty", diff.prev_count ? "这一轮没有新增或改动的消息。" : "没有可显示的消息。"),
    );
  }

  for (const idx of order) {
    const msg = contexts[idx] || {};
    const role = msg.role || "user";
    const item = items[idx] || {};
    const tokens = item.tokens || 0;
    const msgState = item.state || "same";
    // 后端已把 content 里的多模态分段抽成 text，优先用它，避免展示 JSON 噪音
    const raw =
      typeof msg.text === "string"
        ? msg.text
        : typeof msg.content === "string"
          ? msg.content
          : JSON.stringify(msg.content, null, 2);

    const d = document.createElement("details");
    d.className =
      "msg msg-role-" +
      role +
      (msgState === "new" ? " msg-new" : msgState === "changed" ? " msg-chg" : "");
    const s = document.createElement("summary");
    s.appendChild(el("span", "idx", `#${idx + 1}`));
    s.appendChild(el("span", "role role-" + role, role));
    if (item.summary) s.appendChild(el("span", "tag tag-cut", "摘要"));
    if (msgState === "new") s.appendChild(el("span", "tag tag-new", "新增"));
    else if (msgState === "changed") s.appendChild(el("span", "tag tag-chg", "改动"));
    if (item.inj_at) {
      const inj = el("span", "tag tag-inj", `#${item.inj_at} 注入`);
      inj.title = item.inj_iso
        ? `这条消息在第 ${item.inj_at} 次请求（${item.inj_iso}）进入上下文`
        : `这条消息在第 ${item.inj_at} 次请求进入上下文`;
      s.appendChild(inj);
    }
    s.appendChild(el("span", "preview", (raw || "").replace(/\s+/g, " ").slice(0, 80)));
    s.appendChild(el("span", "spacer"));
    s.appendChild(el("span", "muted", `≈${fmtTok(tokens)}`));
    d.appendChild(s);
    d.appendChild(el("pre", "pre", raw));
    list.appendChild(d);
  }

  sec.appendChild(list);
  return sec;
}

function renderDetail(step) {
  const box = $("#detail");
  box.textContent = "";

  const head = el("div", "detail-head");
  const title = el("div", "detail-title");
  title.appendChild(el("span", "seq", `#${step.n ?? step.seq}`));
  title.appendChild(el("span", "muted", `${step.iso || ""}`));
  head.appendChild(title);

  const meta = el("div", "meta");
  for (const [k, v] of [
    ["会话", step.umo || "-"],
    ["发送者", step.sender || "-"],
    ["平台", step.platform || "-"],
    ["模型", step.model || "-"],
    ["上下文条数", step.n_contexts],
    ["估算 tokens", fmtTok((step.breakdown || {}).total || 0)],
  ]) {
    const item = el("div", "meta-item");
    item.appendChild(el("span", "meta-k", k));
    item.appendChild(el("span", "meta-v", v));
    meta.appendChild(item);
  }
  head.appendChild(meta);
  box.appendChild(head);

  // 组成条
  const compo = el("div", "card");
  compo.appendChild(el("div", "card-title", "组成"));
  compo.appendChild(buildBar(step.breakdown || {}, (step.breakdown || {}).total || 0));
  const legend = el("div", "legend");
  for (const [key, , color] of CATS) {
    const v = (step.breakdown || {})[key] || 0;
    if (!v) continue;
    const item = el("span", "legend-item");
    const dot = el("span", "dot", "●");
    dot.style.color = color;
    item.appendChild(dot);
    item.appendChild(el("span", null, `${CAT_LABEL[key]} ${fmtTok(v)}`));
    legend.appendChild(item);
  }
  compo.appendChild(legend);
  box.appendChild(compo);

  // 与上一轮的差异
  const deltaCard = el("div", "card");
  deltaCard.appendChild(el("div", "card-title", "与上一轮相比"));
  deltaCard.appendChild(renderDelta(step));
  box.appendChild(deltaCard);

  // 上下文压缩
  if (step.compress) {
    const c = step.compress;
    const card = el("div", "card card-cut");
    card.appendChild(el("div", "card-title", `上下文压缩 · ${c.kind === "summary" ? "LLM 摘要" : "按轮次截断"}`));
    const line = el("div", "cut-line");
    line.appendChild(el("span", "cut-before", `${c.before} 条 / ≈${fmtTok(c.before_tokens)}`));
    line.appendChild(el("span", "cut-arrow", "→"));
    line.appendChild(el("span", "cut-after", `${c.after} 条 / ≈${fmtTok(c.after_tokens)}`));
    line.appendChild(el("span", "badge down", `回收 ≈${fmtTok(Math.max(0, c.saved))} tokens`));
    card.appendChild(line);
    const note = el("div", "muted");
    note.textContent =
      `移除 ${c.removed} 条消息` +
      (c.summary_tokens ? `，其中压缩摘要占 ≈${fmtTok(c.summary_tokens)} tokens（计入「压缩摘要」分类）` : "");
    card.appendChild(note);
    box.appendChild(card);
  }

  // 系统提示词
  if (step.system_prompt) {
    const pre = el("pre", "pre", step.system_prompt);
    box.appendChild(collapse(`系统提示词 · ≈${fmtTok(step.breakdown.system)} tokens`, pre, false));
  }

  // 工具定义
  const tools = step.tool_items || [];
  if (tools.length) {
    const list = el("div", "tool-list");
    for (const t of tools) {
      const row = el("div", "tool-row");
      row.appendChild(el("span", "tool-name", t.name));
      row.appendChild(el("span", "spacer"));
      row.appendChild(el("span", "muted", `≈${fmtTok(t.tokens)}`));
      list.appendChild(row);
    }
    box.appendChild(collapse(`工具定义 · ${tools.length} 个 · ≈${fmtTok(step.breakdown.tools)} tokens`, list, false));
  }

  // 消息（可切换排序 / 增量视图）
  const contexts = step.contexts || [];
  if (contexts.length) {
    box.appendChild(buildMessageSection(step));
  }

  // 本次输入
  if (step.prompt) {
    box.appendChild(collapse(`本次输入 · ≈${fmtTok(step.breakdown.prompt)} tokens`, el("pre", "pre", step.prompt), false));
  }
}

// ---------------------------------------------------------------- 数据流
async function selectStep(id) {
  state.selected = id;
  for (const row of document.querySelectorAll(".step")) {
    row.classList.toggle("active", row.dataset.id === id);
  }
  const box = $("#detail");
  box.textContent = "";
  box.appendChild(el("div", "empty", "加载中…"));
  try {
    let res;
    try {
      res = await api("detail", { date: state.date, id, full: 1 });
    } catch (firstErr) {
      // 后端还没重载成新版本时只认 seq，退回去再试一次
      const step = state.steps.find((s) => s.id === id);
      if (!step || step.seq === undefined) throw firstErr;
      res = await api("detail", { date: state.date, seq: step.seq, full: 1 });
    }
    state.detail = res.step;
    renderDetail(res.step);
  } catch (err) {
    state.detail = null;
    box.textContent = "";
    box.appendChild(el("div", "empty", `加载失败：${err.message}`));
  }
}

function renderNotice() {
  const box = $("#notice");
  if (!box) return;
  const msg = state.notice;
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = "";
  box.appendChild(el("span", "notice-icon", "⚠"));
  box.appendChild(el("span", "notice-text", msg));
}

async function loadSessions() {  try {
    const res = await api("sessions", { date: state.date });
    const sel = $("#sessions");
    sel.textContent = "";
    sel.appendChild(new Option("全部会话", ""));
    for (const s of res.sessions || []) {
      sel.appendChild(new Option(`${s.umo}（${s.steps}）`, s.umo));
    }
    if (state.umo) sel.value = state.umo;
  } catch {
    /* 会话列表失败不影响主流程 */
  }
}

async function loadList() {
  const box = $("#timeline");
  box.textContent = "";
  box.appendChild(el("div", "empty", "加载中…"));
  try {
    const res = await api("list", { date: state.date, umo: state.umo, limit: 400 });
    state.steps = res.steps || [];
    state.notice = res.notice || null;
    renderNotice();
    renderTimeline();
    if (state.steps.length) {
      const keep = state.steps.find((s) => s.id === state.selected);
      await selectStep((keep || state.steps[state.steps.length - 1]).id);
    } else {
      $("#detail").textContent = "";
      $("#detail").appendChild(el("div", "empty", "没有可显示的请求。"));
    }
  } catch (err) {
    box.textContent = "";
    box.appendChild(el("div", "empty", `加载失败：${err.message}`));
  }
}

async function boot() {
  if (typeof bridge.ready === "function") {
    // bridge 握手卡住时不要无限等待，8 秒后给出可读的错误
    await withTimeout(bridge.ready(), 8000, "bridge.ready()");
  }
  state.date = today();
  $("#date").value = state.date;

  // 时间线排序开关（默认新→旧）
  const tlOrder = $("#tl-order");
  if (tlOrder) {
    tlOrder.appendChild(
      seg(
        "排序",
        [
          ["asc", "旧 → 新"],
          ["desc", "新 → 旧"],
        ],
        state.tlOrder,
        (v) => {
          state.tlOrder = v;
          storage.setItem("ctxdump.tlOrder", v);
          renderTimeline();
        },
      ),
    );
  }
  $("#date").addEventListener("change", (e) => {
    state.date = e.target.value || today();
    state.selected = null;
    loadSessions();
    loadList();
  });
  $("#sessions").addEventListener("change", (e) => {
    state.umo = e.target.value;
    state.selected = null;
    loadList();
  });
  $("#reload").addEventListener("click", () => {
    loadSessions();
    loadList();
  });
  await loadSessions();
  await loadList();
}

boot()
  .then(() => {
    window.__ctxDumpBooted = true;
  })
  .catch((err) => {
    const box = $("#timeline");
    if (box) {
      box.textContent = "";
      box.appendChild(el("div", "empty", `初始化失败：${err.message}`));
    }
  });
