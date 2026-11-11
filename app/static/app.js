/* Nexus Chat — frontend (vanilla JS, nessun build step) */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICON = {
  logo: '<svg viewBox="0 0 32 32"><path d="M9 22V10l14 12V10"/></svg>',
  tool: '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  server: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>',
  upload: '<svg viewBox="0 0 24 24"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>',
  brain: '<svg viewBox="0 0 24 24"><path d="M9 3a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V6a3 3 0 0 0-3-3zM15 3a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  lock: '<svg viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>',
  plug: '<svg viewBox="0 0 24 24"><path d="M12 22v-5M9 8V2M15 8V2M18 8v4a6 6 0 0 1-12 0V8z"/></svg>',
};

const state = {
  settings: {},
  servers: [],
  docs: [],
  convs: [],
  convId: null,
  busy: false,
  abort: null,
  panel: null,
};

/* ===================== API ===================== */
async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData)) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(text, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = (kind === "ok" ? ICON.check : kind === "err" ? ICON.x : "") + `<span>${esc(text)}</span>`;
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = 0; t.style.transition = ".3s"; }, 3200);
  setTimeout(() => t.remove(), 3600);
}

/* ===================== Markdown ===================== */
const hasMd = typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
if (hasMd) marked.setOptions({ gfm: true, breaks: true });

function renderMd(text) {
  if (!hasMd) return `<p>${esc(text).replace(/\n/g, "<br>")}</p>`;
  return DOMPurify.sanitize(marked.parse(text || ""));
}

function enhanceCode(root) {
  $$("pre > code", root).forEach((code) => {
    const pre = code.parentElement;
    if (pre.querySelector(".code-head")) return;
    const lang = (code.className.match(/language-(\S+)/) || [])[1] || "code";
    if (typeof hljs !== "undefined") { try { hljs.highlightElement(code); } catch {} }
    const head = document.createElement("div");
    head.className = "code-head";
    head.innerHTML = `<span>${esc(lang)}</span><button>${ICON.copy}Copia</button>`;
    head.querySelector("button").onclick = () => {
      navigator.clipboard.writeText(code.innerText);
      head.querySelector("button").innerHTML = ICON.check + "Copiato";
      setTimeout(() => (head.querySelector("button").innerHTML = ICON.copy + "Copia"), 1500);
    };
    pre.prepend(head);
  });
}

/* ===================== Init ===================== */
async function init() {
  bindUI();
  initCtxWidget();
  await Promise.all([loadSettings(), loadServers(), loadDocs(), loadConvs()]);
  loadModels();
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  renderThinkChip();
  renderWebChip();
  const serverUI = state.settings.ui || {};
  if (Object.keys(serverUI).length) NexusTheme.set(serverUI);
  else if (JSON.stringify(NexusTheme.get()) !== JSON.stringify(NexusTheme.DEFAULTS)) saveUI(); // prime preferenze locali -> server
}

/* ===================== Aspetto ===================== */
let _uiTimer = null;
function saveUI() {
  clearTimeout(_uiTimer);
  _uiTimer = setTimeout(async () => {
    try { state.settings = await api("/api/settings", { method: "PUT", body: { ui: NexusTheme.get() } }); }
    catch (e) { toast("Preferenze di aspetto non salvate: " + e.message, "err"); }
  }, 400);
}
function setUI(patch, rerender = true) {
  NexusTheme.set({ ...NexusTheme.get(), ...patch });
  saveUI();
  if (rerender && state.panel === "appearance") {
    const body = $("#drawerBody"), top = body.scrollTop;
    renderAppearance();
    body.scrollTop = top;
  }
}

function renderAppearance() {
  const ui = NexusTheme.get();
  const P = NexusTheme.PRESETS;
  const seg = (key, opts) => `<div class="seg">${opts.map(([v, l]) => `<button data-set="${key}" data-val="${v}" class="${ui[key] === v ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const isCustom = !P[ui.preset];
  const bgTiles = [
    ["aurora", "Aurora", `radial-gradient(circle at 25% 20%, ${ui.accent}, transparent 55%), radial-gradient(circle at 80% 90%, ${ui.accent2}, transparent 55%), #0a0a12`],
    ["aurora-static", "Aurora ferma", `radial-gradient(circle at 30% 30%, ${ui.accent}aa, transparent 60%), radial-gradient(circle at 75% 80%, ${ui.accent2}aa, transparent 60%), #0a0a12`],
    ["gradient", "Gradiente", `linear-gradient(135deg, ${ui.accent}66, #0a0a12 55%, ${ui.accent2}55)`],
    ["solid", "Tinta unita", ui.bgColor],
    ["image", "Immagine", ui.bgImage ? `url("${ui.bgImage}") center/cover` : "repeating-linear-gradient(45deg, #2a2a3a 0 8px, #3a3a4e 8px 16px)"],
  ];
  $("#drawerTitle").textContent = "Aspetto";
  $("#drawerBody").innerHTML = `
    <div class="ap-preview">
      <div class="msg user pv-user"><div class="body" style="max-width:100%"><div class="bubble">Com'è il nuovo tema?</div></div></div>
      <div class="msg assistant"><div class="avatar">${ICON.logo}</div><div class="body"><div class="bubble" style="padding-top:4px">Ecco un'anteprima: testo, <a href="#" onclick="return false">link</a> e <code>codice</code>.</div></div></div>
      <div class="pv-row"><button class="btn primary sm">Primario</button><button class="btn sm">Secondario</button>
        <span class="badge online">online</span><label class="switch"><input type="checkbox" checked><span></span></label></div>
    </div>

    <div class="section"><h3>Tema</h3>${seg("theme", [["dark", "Scuro"], ["light", "Chiaro"], ["system", "Automatico"]])}</div>

    <div class="section"><h3>Colori</h3>
      <div class="ap-swatches">
        ${Object.entries(P).map(([k, p]) => `<div class="ap-swatch ${ui.preset === k ? "on" : ""}" data-preset="${k}"><i style="background:linear-gradient(135deg, ${p.accent}, ${p.accent2})"></i>${p.label}</div>`).join("")}
        <div class="ap-swatch ${isCustom ? "on" : ""}" data-preset="custom"><i style="background:conic-gradient(${ui.accent}, ${ui.accent2}, ${ui.accent3}, ${ui.accent})"></i>Personalizzato</div>
      </div>
      <div class="ap-colors" style="margin-top:12px">
        <label class="ap-color"><input type="color" data-color="accent" value="${ui.accent}">Primario</label>
        <label class="ap-color"><input type="color" data-color="accent2" value="${ui.accent2}">Secondario</label>
        <label class="ap-color"><input type="color" data-color="accent3" value="${ui.accent3}">Terzo (aurora)</label>
      </div>
    </div>

    <div class="section"><h3>Sfondo</h3>
      <div class="ap-bgs">${bgTiles.map(([v, l, bg]) => `<div class="ap-bg ${ui.bg === v ? "on" : ""}" data-set="bg" data-val="${v}" style="background:${bg}">${l}</div>`).join("")}</div>
      <div style="margin-top:14px">
        ${ui.bg.startsWith("aurora") ? `
          <div class="field"><label>Intensità aurora: <b id="apAI">${Math.round(ui.auroraIntensity * 100)}%</b></label>
            <input type="range" min="0.05" max="0.8" step="0.01" data-range="auroraIntensity" value="${ui.auroraIntensity}"></div>
          ${ui.bg === "aurora" ? `<div class="field"><label>Velocità: <b id="apAS">${ui.auroraSpeed}s</b> per ciclo</label>
            <input type="range" min="6" max="60" step="1" data-range="auroraSpeed" value="${ui.auroraSpeed}" style="direction:rtl"></div>` : ""}` : ""}
        ${ui.bg === "solid" ? `<label class="ap-color" style="width:max-content"><input type="color" data-color="bgColor" value="${ui.bgColor}">Colore di sfondo</label>` : ""}
        ${ui.bg === "image" ? `
          <div class="ap-img"><div class="thumb" style="${ui.bgImage ? `background-image:url('${ui.bgImage}')` : ""}"></div>
            <button class="btn sm" id="apUpload">${ICON.upload}Carica immagine</button>
            ${ui.bgImage ? `<button class="btn sm danger" id="apRemoveImg">${ICON.trash}Rimuovi</button>` : ""}
            <input type="file" id="apFile" accept="image/jpeg,image/png,image/webp,image/gif,image/avif" hidden></div>
          <div class="field" style="margin-top:12px"><label>Oscuramento per la leggibilità: <b id="apDim">${Math.round(ui.bgDim * 100)}%</b></label>
            <input type="range" min="0" max="0.9" step="0.01" data-range="bgDim" value="${ui.bgDim}"></div>` : ""}
      </div>
    </div>

    <div class="section"><h3>Testo</h3>
      <div class="row">
        <div class="field"><label>Carattere</label>
          <select class="inp" id="apFont">${Object.entries(NexusTheme.FONTS).map(([k, f]) => `<option value="${k}" ${ui.font === k ? "selected" : ""} style="font-family:${f.stack.replace(/"/g, "'")}">${f.label}</option>`).join("")}</select></div>
        <div class="field"><label>Dimensione: <b id="apSc">${Math.round(ui.scale * 100)}%</b></label>
          <input type="range" min="0.85" max="1.2" step="0.01" data-range="scale" value="${ui.scale}"></div>
      </div>
    </div>

    <div class="section"><h3>Forme e spazi</h3>
      <div class="field"><label>Angoli</label>${seg("corners", [["round", "Arrotondati"], ["medium", "Medi"], ["square", "Squadrati"]])}</div>
      <div class="field"><label>Densità dei messaggi</label>${seg("density", [["compact", "Compatta"], ["normal", "Normale"], ["relaxed", "Ariosa"]])}</div>
      <div class="field"><label>Larghezza della chat</label>${seg("width", [["normal", "Normale"], ["wide", "Larga"], ["full", "Tutto lo schermo"]])}</div>
      <div class="field"><label>Stile dei tuoi messaggi</label>${seg("bubble", [["gradient", "Sfumato"], ["solid", "Tinta"], ["outline", "Contorno"], ["minimal", "Minimale"]])}</div>
    </div>

    <div class="section"><h3>Effetti</h3>
      <div class="field"><label>Opacità dei pannelli: <b id="apGl">${Math.round(ui.glass * 100)}%</b> <small style="font-weight:400">(più bassa = più "vetro")</small></label>
        <input type="range" min="0.25" max="0.98" step="0.01" data-range="glass" value="${ui.glass}"></div>
      <label class="check"><input type="checkbox" data-bool="grain" ${ui.grain ? "checked" : ""}><span><b>Grana</b><small>Leggera texture sullo sfondo.</small></span></label>
      <label class="check"><input type="checkbox" data-bool="motion" ${ui.motion ? "checked" : ""}><span><b>Animazioni</b><small>Disattivale per un'interfaccia più statica o su computer lenti.</small></span></label>
    </div>

    <div class="actions" style="justify-content:space-between">
      <small style="color:var(--faint);align-self:center">Le modifiche si salvano automaticamente.</small>
      <button class="btn" id="apReset">${ICON.refresh}Ripristina predefiniti</button>
    </div>`;

  const body = $("#drawerBody");
  body.onclick = async (ev) => {
    const t = ev.target.closest("[data-set], [data-preset], #apUpload, #apRemoveImg, #apReset");
    if (!t) return;
    if (t.dataset.set) return setUI({ [t.dataset.set]: t.dataset.val });
    if (t.dataset.preset) {
      if (t.dataset.preset === "custom") return setUI({ preset: "custom" });
      const p = P[t.dataset.preset];
      return setUI({ preset: t.dataset.preset, accent: p.accent, accent2: p.accent2, accent3: p.accent3 });
    }
    if (t.id === "apUpload") return $("#apFile").click();
    if (t.id === "apRemoveImg") {
      await api("/api/ui/background", { method: "DELETE" }).catch(() => {});
      return setUI({ bgImage: "" });
    }
    if (t.id === "apReset") {
      if (!confirm("Ripristinare l'aspetto predefinito?")) return;
      return setUI({ ...NexusTheme.DEFAULTS });
    }
  };
  const labels = { auroraIntensity: ["#apAI", (v) => Math.round(v * 100) + "%"], auroraSpeed: ["#apAS", (v) => v + "s"],
    bgDim: ["#apDim", (v) => Math.round(v * 100) + "%"], scale: ["#apSc", (v) => Math.round(v * 100) + "%"], glass: ["#apGl", (v) => Math.round(v * 100) + "%"] };
  body.oninput = (ev) => {
    const t = ev.target;
    if (t.dataset.range) {
      const v = Number(t.value);
      setUI({ [t.dataset.range]: v }, false);
      const l = labels[t.dataset.range];
      if (l && $(l[0])) $(l[0]).textContent = l[1](v);
    } else if (t.dataset.color) {
      const patch = { [t.dataset.color]: t.value };
      if (t.dataset.color !== "bgColor") patch.preset = "custom";
      setUI(patch, false);
    }
  };
  body.onchange = async (ev) => {
    const t = ev.target;
    if (t.dataset.bool) return setUI({ [t.dataset.bool]: t.checked });
    if (t.dataset.color) return setUI({}, true); // aggiorna le miniature
    if (t.id === "apFont") return setUI({ font: t.value }, false);
    if (t.id === "apFile" && t.files[0]) {
      const fd = new FormData();
      fd.append("file", t.files[0]);
      try {
        const r = await api("/api/ui/background", { method: "POST", body: fd });
        setUI({ bg: "image", bgImage: r.url });
        toast("Sfondo aggiornato", "ok");
      } catch (e) { toast(e.message, "err"); }
    }
  };
}

function renderWebChip() {
  const on = !!state.settings.web_search_enabled;
  $("#useWeb").checked = on;
  $("#webLabel").textContent = on ? "On" : "Off";
}

const THINK_LABEL = { auto: "Auto", on: "On", off: "Off" };
const THINK_TIP = {
  auto: "Thinking: Auto — decide il modello (nessun parametro inviato)",
  on: "Thinking: On — ragionamento forzato",
  off: "Thinking: Off — ragionamento disattivato (risposte più rapide)",
};
function renderThinkChip() {
  const m = state.settings.thinking || "auto";
  const c = $("#thinkChip");
  c.dataset.mode = m;
  $("#thinkLabel").textContent = THINK_LABEL[m];
  c.title = THINK_TIP[m] + "  ·  clic per cambiare";
}
async function cycleThinking() {
  const order = ["auto", "on", "off"];
  const next = order[(order.indexOf(state.settings.thinking || "auto") + 1) % 3];
  state.settings = await api("/api/settings", { method: "PUT", body: { thinking: next } });
  renderThinkChip();
  toast(THINK_TIP[next].split(" — ")[0] + " — " + THINK_TIP[next].split(" — ")[1]);
}

async function loadModels() {
  const sel = $("#modelSelect");
  const current = state.settings.llm_model;
  try {
    const { models } = await api("/api/models");
    const list = models.includes(current) || !current ? models : [current, ...models];
    sel.innerHTML = list.map((m) => `<option ${m === current ? "selected" : ""}>${esc(m)}</option>`).join("")
      || `<option value="">Nessun modello</option>`;
    $("#statusDot").className = "dot ok";
    $("#modelSelectWrap").title = `Connesso a ${state.settings.llm_base_url}`;
    if (!current && models.length) {
      state.settings.llm_model = models[0];
      await api("/api/settings", { method: "PUT", body: { llm_model: models[0] } });
    }
  } catch (e) {
    sel.innerHTML = `<option>${esc(current || "Endpoint non raggiungibile")}</option>`;
    $("#statusDot").className = "dot err";
    $("#modelSelectWrap").title = e.message;
  }
}

async function loadServers() {
  state.servers = await api("/api/mcp/servers");
  const n = state.servers.filter((s) => s.enabled).reduce((a, s) => a + s.tools.filter((t) => t.enabled).length, 0);
  $("#toolCount").textContent = n;
  $("#toolBadge").textContent = n;
}

async function loadDocs() {
  state.docs = await api("/api/documents");
  $("#docCount").textContent = state.docs.filter((d) => d.status === "ready").length;
  if (state.panel === "docs") renderDocsList();
  if (state.docs.some((d) => d.status === "processing")) setTimeout(loadDocs, 1500);
}

async function loadConvs() {
  state.convs = await api("/api/conversations");
  renderConvs();
}

/* ===================== Sidebar ===================== */
function renderConvs() {
  const f = $("#convSearch").value.toLowerCase();
  const list = state.convs.filter((c) => (c.title || "").toLowerCase().includes(f));
  const groups = {};
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()) / 1000;
  list.forEach((c) => {
    const g = c.updated_at >= startToday ? "Oggi" : c.updated_at >= startToday - 86400 ? "Ieri"
      : c.updated_at >= startToday - 7 * 86400 ? "Ultimi 7 giorni" : "Meno recenti";
    (groups[g] ||= []).push(c);
  });
  const el = $("#convList");
  el.innerHTML = list.length ? "" : `<div class="empty" style="padding:20px 0;font-size:13px">Nessuna conversazione</div>`;
  for (const [g, items] of Object.entries(groups)) {
    el.insertAdjacentHTML("beforeend", `<div class="conv-group">${g}</div>`);
    items.forEach((c) => {
      const d = document.createElement("div");
      d.className = "conv" + (c.id === state.convId ? " active" : "");
      d.innerHTML = `<span class="t">${esc(c.title || "Senza titolo")}</span>
        <span class="acts"><button data-a="ren" title="Rinomina">${ICON.edit}</button><button data-a="del" title="Elimina">${ICON.trash}</button></span>`;
      d.onclick = async (ev) => {
        const a = ev.target.closest("button")?.dataset.a;
        if (a === "del") {
          ev.stopPropagation();
          if (!confirm("Eliminare questa conversazione?")) return;
          await api(`/api/conversations/${c.id}`, { method: "DELETE" });
          if (state.convId === c.id) newChat();
          return loadConvs();
        }
        if (a === "ren") {
          ev.stopPropagation();
          const t = prompt("Nuovo titolo", c.title);
          if (t) { await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { title: t } }); loadConvs(); }
          return;
        }
        openConv(c.id);
      };
      el.appendChild(d);
    });
  }
}

function newChat() {
  if (state.busy) stop();
  state.convId = null;
  $("#messages").innerHTML = "";
  $("#welcome").classList.remove("hidden");
  renderCtxWidget(null);
  renderConvs();
  $("#input").focus();
  $("#sidebar").classList.remove("open");
}

async function openConv(id) {
  if (state.busy) stop();
  const conv = await api(`/api/conversations/${id}`);
  state.convId = id;
  $("#messages").innerHTML = "";
  $("#welcome").classList.add("hidden");
  conv.messages.forEach((m) => {
    if (m.role === "user") addUserMsg(m.content);
    else {
      const v = addAssistantMsg();
      (m.meta.reasoning) && appendReasoning(v, m.meta.reasoning, true);
      (m.meta.tools || []).forEach((t) => { addToolCard(v, t); finishToolCard(v, t); });
      setAssistantText(v, m.content, true);
      if (m.meta.sources?.length) renderSources(v, m.meta.sources);
      if (m.meta.usage) renderUsage(v, m.meta.usage);
      if (m.meta.empty_reason) v.body.insertAdjacentHTML("beforeend", `<div class="notice">${esc(m.meta.empty_reason)}</div>`);
      if (m.meta.error) v.body.insertAdjacentHTML("beforeend", `<div class="err-box">${esc(m.meta.error)}</div>`);
    }
  });
  renderConvs();
  scrollBottom(true);
  $("#sidebar").classList.remove("open");
  const last = [...conv.messages].reverse().find((m) => m.role === "assistant" && m.meta?.usage);
  renderCtxWidget(last ? last.meta.usage : null);
}

/* ===================== Messaggi ===================== */
function scrollBottom(force = false) {
  const c = $("#chat");
  if (force || c.scrollHeight - c.scrollTop - c.clientHeight < 160) c.scrollTop = c.scrollHeight;
}

function addUserMsg(text) {
  const d = document.createElement("div");
  d.className = "msg user";
  d.innerHTML = `<div class="avatar">Tu</div><div class="body"><div class="bubble">${esc(text)}</div></div>`;
  $("#messages").appendChild(d);
}

function addAssistantMsg() {
  const d = document.createElement("div");
  d.className = "msg assistant";
  d.innerHTML = `<div class="avatar">${ICON.logo}</div><div class="body"><div class="steps"></div><div class="bubble"></div>
    <div class="msg-actions"><button title="Copia">${ICON.copy}</button></div></div>`;
  $("#messages").appendChild(d);
  const view = { el: d, body: $(".body", d), steps: $(".steps", d), bubble: $(".bubble", d), text: "", tools: {} };
  $(".msg-actions button", d).onclick = () => { navigator.clipboard.writeText(view.text); toast("Copiato negli appunti", "ok"); };
  return view;
}

function setAssistantText(v, text, final = false) {
  v.text = text;
  v.bubble.innerHTML = renderMd(text);
  v.bubble.classList.toggle("cursor", !final);
  if (final) enhanceCode(v.bubble);
}

function showTyping(v, on) {
  if (on && !v.text) v.bubble.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
}

function appendReasoning(v, text, closed = false) {
  if (!v.reasoning) {
    const det = document.createElement("details");
    det.className = "reasoning";
    if (!closed) det.open = true;
    det.innerHTML = `<summary>${ICON.brain}<span>Ragionamento</span></summary><div class="rbody"></div>`;
    v.steps.appendChild(det);
    v.reasoning = det;
  }
  $(".rbody", v.reasoning).textContent += text;
}

function addToolCard(v, t) {
  const det = document.createElement("details");
  det.className = "tool-card";
  det.innerHTML = `<summary><span class="ticon">${ICON.tool}</span><span class="tname">${esc(t.name)}</span>
      <span class="tsrv">· ${esc(t.server)}</span><span class="tstatus"><span class="spinner"></span>In esecuzione</span></summary>
    <div class="tbody">${t.note ? `<div class="tnote">${esc(t.note)}</div>` : ""}<div class="tlabel">Argomenti</div><pre>${esc(JSON.stringify(t.args, null, 2))}</pre>
      <div class="tres hidden"><div class="tlabel">Risultato</div><pre></pre></div></div>`;
  v.steps.appendChild(det);
  v.tools[t.id] = det;
  if (v.reasoning) v.reasoning.open = false;
}

function finishToolCard(v, t) {
  const det = v.tools[t.id];
  if (!det) return;
  if (t.note && !det.querySelector(".tnote")) $(".tbody", det).insertAdjacentHTML("afterbegin", `<div class="tnote">${esc(t.note)}</div>`);
  else if (t.note) det.querySelector(".tnote").textContent = t.note;
  det.classList.toggle("error", !!t.error);
  $(".tstatus", det).innerHTML = t.error ? `${ICON.x}Errore` : `${ICON.check}Completato`;
  $(".tres", det).classList.remove("hidden");
  let out = t.result ?? "";
  try { out = JSON.stringify(JSON.parse(out), null, 2); } catch {}
  $(".tres pre", det).textContent = out;
}

/* ===================== Riquadro contesto ===================== */
const CTX_PARTS = [
  ["system", "Istruzioni di sistema", "#8b5cf6"],
  ["tools", "Definizioni dei tool", "#22d3ee"],
  ["history", "Storico conversazione", "#6366f1"],
  ["rag", "Estratti documenti (RAG)", "#34d399"],
  ["message", "Tuo messaggio", "#f472b6"],
  ["tool_results", "Risultati dei tool", "#fbbf24"],
];
const fmt = (n) => String(Math.round(Number(n || 0))).replace(/\B(?=(\d{3})+(?!\d))/g, ".");

function renderCtxWidget(u) {
  const w = $("#ctxWidget");
  const body = $("#ctxBody");
  w.classList.remove("high", "full");
  if (!u) {
    $("#ctxPillText").textContent = "Contesto";
    $("#ctxRing").style.strokeDashoffset = 94.25;
    body.innerHTML = `<div class="ctx-empty">Invia un messaggio per misurare quanto contesto viene usato.</div>`;
    return;
  }
  const ctx = u.ctx || 0;
  const pct = ctx ? Math.min(100, Math.round((u.prompt / ctx) * 100)) : null;
  if (u.truncated || pct >= 98) w.classList.add("full"); else if (pct >= 85) w.classList.add("high");
  $("#ctxPillText").textContent = pct !== null ? `${pct}% · ${fmt(u.prompt)} tok` : `${fmt(u.prompt)} tok`;
  $("#ctxRing").style.strokeDashoffset = 94.25 * (1 - (pct ?? 0) / 100);
  const parts = u.parts || {};
  const denom = Math.max(ctx, u.prompt) || 1;
  const bar = CTX_PARTS.map(([k, , c]) => parts[k] ? `<span style="width:${(parts[k] / denom) * 100}%;background:${c}" title="${k}"></span>` : "").join("");
  const legend = CTX_PARTS.filter(([k]) => parts[k]).map(([k, label, c]) =>
    `<div><i style="background:${c}"></i>${label}<em>${fmt(parts[k])}</em></div>`).join("");
  const free = ctx ? Math.max(0, ctx - u.prompt) : null;
  const metas = [];
  metas.push(u.ctx_source === "ollama" ? "Finestra rilevata da Ollama" : u.ctx_source === "impostazioni" ? "Finestra da Impostazioni (non rilevata)" : "Finestra sconosciuta: impostala nelle Impostazioni");
  metas.push(u.estimated ? "Token stimati (l'endpoint non li riporta)" : "Token reali riportati dal modello");
  if (u.model) metas.push(`Modello: ${esc(u.model)}${u.round > 1 ? ` · round tool ${u.round}` : ""}`);
  if (u.history_dropped) metas.push(`<span class="warn">Storico ridotto: ${u.history_dropped} messaggi esclusi</span>`);
  if (u.truncated) metas.push(`<span class="err">Prompt troncato: il modello non ha visto tutto</span>`);
  else if (pct >= 85) metas.push(`<span class="warn">Quasi pieno: apri una nuova chat o disattiva tool non necessari</span>`);
  body.innerHTML = `
    <div class="ctx-big"><span class="n">${fmt(u.prompt)}</span><span class="of">/ ${ctx ? fmt(ctx) : "?"} token</span>${pct !== null ? `<span class="pct">${pct}%</span>` : ""}</div>
    <div class="ctx-bar">${bar}</div>
    <div class="ctx-legend">${legend}${free !== null ? `<div class="free"><i style="background:rgba(255,255,255,.1)"></i>Spazio libero<em>${fmt(free)}</em></div>` : ""}
      ${u.completion ? `<div class="free"><i style="background:transparent;border:1px solid var(--border-strong)"></i>Risposta generata<em>${fmt(u.completion)}</em></div>` : ""}</div>
    <div class="ctx-meta">${metas.map((m) => `<div>${m}</div>`).join("")}</div>`;
}

function initCtxWidget() {
  const w = $("#ctxWidget");
  let open = false;
  try { open = localStorage.getItem("nexus.ctxOpen") === "1"; } catch {}
  w.classList.toggle("collapsed", !open);
  const setOpen = (o) => { w.classList.toggle("collapsed", !o); try { localStorage.setItem("nexus.ctxOpen", o ? "1" : "0"); } catch {} };
  $("#ctxPill").onclick = () => setOpen(true);
  $("#ctxClose").onclick = () => setOpen(false);
  renderCtxWidget(null);
}

function renderUsage(v, u) {
  const acts = $(".msg-actions", v.body);
  acts.querySelector(".usage")?.remove();
  const pct = u.ctx ? Math.round((u.prompt / u.ctx) * 100) : null;
  acts.insertAdjacentHTML("beforeend", `<span class="usage ${u.truncated ? "trunc" : pct > 85 ? "high" : ""}"
    title="Token del prompt / finestra di contesto effettiva del modello">${fmt(u.prompt)} tok${u.ctx ? ` / ${fmt(u.ctx)} (${pct}%)` : ""}${u.truncated ? " · troncato" : ""}${u.history_dropped ? ` · storico ridotto (−${u.history_dropped})` : ""}</span>`);
}

function renderSources(v, sources) {
  v.body.querySelector(".sources")?.remove();
  const box = document.createElement("div");
  box.className = "sources";
  box.innerHTML = sources.map((s) => `<span class="source"><b>[${s.n}]</b>${esc(s.name)} · ${(s.score * 100).toFixed(0)}%
      <span class="tip">${esc(s.text)}…</span></span>`).join("");
  v.body.insertBefore(box, $(".msg-actions", v.body));
}

/* ===================== Invio / streaming ===================== */
function setBusy(b) {
  state.busy = b;
  $("#sendBtn").classList.toggle("busy", b);
  $("#sendBtn").title = b ? "Interrompi" : "Invia";
}

function stop() {
  state.userAborted = true;
  state.abort?.abort();
}

async function send(textArg) {
  if (state.busy) return; // Invio durante la generazione non interrompe: per fermare usa il pulsante ■
  const input = $("#input");
  const text = (textArg ?? input.value).trim();
  if (!text) return;
  input.value = "";
  autoGrow();
  $("#welcome").classList.add("hidden");
  addUserMsg(text);
  const v = addAssistantMsg();
  showTyping(v, true);
  scrollBottom(true);
  setBusy(true);
  state.userAborted = false;
  let gotDone = false;
  state.abort = new AbortController();
  let raf = null;
  const flush = () => { raf = null; setAssistantText(v, v.text); scrollBottom(); };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: state.abort.signal,
      body: JSON.stringify({
        message: text,
        conversation_id: state.convId,
        use_rag: $("#useRag").checked,
        use_tools: $("#useTools").checked,
        model: $("#modelSelect").value || null,
        thinking: state.settings.thinking || "auto",
        web_search: $("#useWeb").checked,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const ev = (raw.match(/^event: (.*)$/m) || [])[1];
        const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
        if (!ev || !dataLine) continue;
        const data = JSON.parse(dataLine);
        switch (ev) {
          case "meta":
            if (!state.convId) { state.convId = data.conversation_id; loadConvs(); }
            break;
          case "token":
            v.text += data.text;
            if (!raf) raf = requestAnimationFrame(flush);
            break;
          case "reasoning":
            appendReasoning(v, data.text);
            scrollBottom();
            break;
          case "sources":
            renderSources(v, data);
            break;
          case "tool_call":
            addToolCard(v, data);
            scrollBottom();
            break;
          case "tool_result":
            finishToolCard(v, data);
            showTyping(v, true);
            break;
          case "done":
            gotDone = true;
            break;
          case "empty":
            v.body.insertAdjacentHTML("beforeend", `<div class="notice">${esc(data.text)}</div>`);
            break;
          case "usage":
            renderUsage(v, data);
            renderCtxWidget(data);
            break;
          case "notice":
            v.steps.insertAdjacentHTML("beforeend", `<div class="notice">${esc(data.text)}</div>`);
            break;
          case "error":
            v.body.insertAdjacentHTML("beforeend", `<div class="err-box">${esc(data.text)}</div>`);
            break;
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") v.body.insertAdjacentHTML("beforeend", `<div class="err-box">${esc(e.message)}</div>`);
  } finally {
    if (raf) cancelAnimationFrame(raf);
    setAssistantText(v, v.text, true);
    const hasInfo = v.body.querySelector(".err-box, .notice");
    if (state.userAborted) {
      if (!v.text) v.bubble.innerHTML = `<span style="color:var(--faint)">(interrotto da te)</span>`;
    } else if (!gotDone && !v.body.querySelector(".err-box")) {
      v.body.insertAdjacentHTML("beforeend", `<div class="err-box">La connessione con il server si è chiusa prima della fine della risposta (timeout di un proxy, riavvio del container o errore del backend). Controlla i log: docker logs nexus-chat</div>`);
    }
    if (!v.text && !Object.keys(v.tools).length && !hasInfo && !state.userAborted && gotDone)
      v.bubble.innerHTML = `<span style="color:var(--faint)">(risposta vuota)</span>`;
    if (!v.text && (hasInfo || v.body.querySelector(".err-box"))) v.bubble.innerHTML = "";
    setBusy(false);
    loadConvs();
    scrollBottom();
  }
}

function autoGrow() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

/* ===================== Drawer ===================== */
function openPanel(name) {
  state.panel = name;
  $("#drawer").classList.add("open");
  $("#drawerBackdrop").classList.add("open");
  ({ settings: renderSettings, mcp: renderMcp, docs: renderDocs, appearance: renderAppearance })[name]();
}
function closePanel() {
  state.panel = null;
  $("#drawer").classList.remove("open");
  $("#drawerBackdrop").classList.remove("open");
}

/* ---------- Impostazioni ---------- */
function renderSettings() {
  const s = state.settings;
  $("#drawerTitle").textContent = "Impostazioni";
  $("#drawerBody").innerHTML = `
    <div class="section"><h3>Endpoint LLM</h3>
      <div class="field"><label>Preset</label>
        <div class="seg" id="presets">
          <button data-url="http://host.docker.internal:11434/v1" data-key="ollama">Ollama</button>
          <button data-url="https://api.openai.com/v1" data-key="">OpenAI</button>
          <button data-url="http://host.docker.internal:1234/v1" data-key="lm-studio">LM Studio</button>
          <button data-url="http://host.docker.internal:8000/v1" data-key="">vLLM</button>
        </div></div>
      <div class="field"><label>Base URL (OpenAI-compatibile)</label><input class="inp" id="s_llm_base_url" value="${esc(s.llm_base_url)}" placeholder="http://host:11434/v1" />
        <small>Per Ollama usa il suffisso <code>/v1</code>. Dal container, l'host è raggiungibile come <code>host.docker.internal</code>.</small></div>
      <div class="field"><label>API key</label><input class="inp" id="s_llm_api_key" type="password" value="${esc(s.llm_api_key)}" placeholder="opzionale per Ollama" /></div>
      <div class="field"><label>Modello</label>
        <div class="inp-group"><select class="inp" id="s_llm_model"><option>${esc(s.llm_model || "")}</option></select>
        <button class="btn" id="fetchModels">${ICON.refresh}Carica</button></div></div>
      <div class="row">
        <div class="field"><label>Temperatura: <b id="tempVal">${s.temperature}</b></label><input type="range" min="0" max="2" step="0.05" id="s_temperature" value="${s.temperature}" /></div>
        <div class="field"><label>Max token risposta (0 = default)</label><input class="inp" type="number" id="s_max_tokens" value="${s.max_tokens}" /></div>
      </div>
      <div class="field"><label>System prompt</label><textarea class="inp" id="s_system_prompt" rows="4">${esc(s.system_prompt)}</textarea></div>
      <div class="row">
        <div class="field"><label>Round massimi di tool calling</label><input class="inp" type="number" min="1" max="30" id="s_max_tool_rounds" value="${s.max_tool_rounds}" /></div>
        <div class="field"><label>Lunghezza max risultato tool (caratteri)</label><input class="inp" type="number" min="1000" step="1000" id="s_tool_result_max_chars" value="${s.tool_result_max_chars}" /></div>
      </div>
      <div class="row">
        <div class="field"><label>Messaggi di storico inviati al modello</label><input class="inp" type="number" min="0" max="200" id="s_history_max_messages" value="${s.history_max_messages}" />
          <small>Ulteriormente ridotti in automatico se il contesto è pieno.</small></div>
        <div class="field"><label>Caratteri dei risultati tool nello storico</label><input class="inp" type="number" min="0" step="100" id="s_history_tool_result_chars" value="${s.history_tool_result_chars}" /></div>
      </div>
      <label class="check"><input type="checkbox" id="s_entity_resolution" ${s.entity_resolution ? "checked" : ""}>
        <span><b>Interpreta i nomi delle entità</b><small>Se il modello usa un nome approssimativo («lampada del tavolo»), Nexus lo abbina all'entità esposta più simile; se è ambiguo fa chiedere a te quale intendi.</small></span></label>
      <div class="row">
        <label class="check" style="margin:0"><input type="checkbox" id="s_entity_semantic" ${s.entity_semantic ? "checked" : ""}>
          <span><b>Anche per significato</b><small>Usa gli embedding (es. «condizionatore» ≈ «clima»).</small></span></label>
        <div class="field" style="margin:0"><label>Soglia di affidabilità: <b id="entThr">${s.entity_match_threshold}</b></label>
          <input type="range" min="0.4" max="0.9" step="0.05" id="s_entity_match_threshold" value="${s.entity_match_threshold}" /></div>
      </div>
      <div style="height:8px"></div>
      <label class="check"><input type="checkbox" id="s_block_broad_actions" ${s.block_broad_actions ? "checked" : ""}>
        <span><b>Blocca azioni senza destinatario</b><small>Impedisce chiamate come «spegni tutti gli switch»: gli intent di Home Assistant devono indicare name o area.</small></span></label>
      <label class="check"><input type="checkbox" id="s_compact_tool_results" ${s.compact_tool_results ? "checked" : ""}>
        <span><b>Compatta i risultati dei tool</b><small>Estrae il testo dai JSON e riduce il Live Context di Home Assistant a una riga per entità (circa ⅓ dello spazio, stesse informazioni).</small></span></label>
      <label class="check"><input type="checkbox" id="s_history_tool_results" ${s.history_tool_results ? "checked" : ""}>
        <span><b>Tool nello storico</b><small>Il modello vede le chiamate ai tool dei turni precedenti: evita che smetta di usarli e risponda "a memoria".</small></span></label>
      <label class="check"><input type="checkbox" id="s_tool_reminder" ${s.tool_reminder ? "checked" : ""}>
        <span><b>Promemoria tool a ogni messaggio</b><small>Ricorda al modello di chiamare i tool per stati e azioni, invece di riusare risposte vecchie.</small></span></label>
      <div class="field"><label>Finestra di contesto del modello (token)</label><input class="inp" type="number" min="0" step="1024" id="s_context_window" value="${s.context_window}" />
        <small>Usata solo per avvisarti quando il prompt rischia di essere troncato (0 = nessun avviso). Con Ollama il valore reale si imposta sul server con <code>OLLAMA_CONTEXT_LENGTH</code>.</small></div>
      <label class="check"><input type="checkbox" id="s_use_server_instructions" ${s.use_server_instructions ? "checked" : ""}>
        <span><b>Istruzioni dei server MCP</b><small>Passa al modello le istruzioni d'uso fornite dai server (es. Home Assistant).</small></span></label>
    </div>

    <div class="section"><h3>Thinking (ragionamento)</h3>
      <div class="row">
        <div class="field"><label>Modalità</label>
          <select class="inp" id="s_thinking">
            ${["auto", "on", "off"].map((m) => `<option value="${m}" ${s.thinking === m ? "selected" : ""}>${{ auto: "Auto — decide il modello", on: "On — forza il ragionamento", off: "Off — disattiva il ragionamento" }[m]}</option>`).join("")}
          </select><small>Si cambia al volo anche dal pulsante <b>Thinking</b> in alto.</small></div>
        <div class="field"><label>Intensità quando è On</label>
          <select class="inp" id="s_reasoning_effort">
            ${["low", "medium", "high"].map((m) => `<option ${s.reasoning_effort === m ? "selected" : ""}>${m}</option>`).join("")}
          </select><small>Usata solo con il metodo <code>reasoning_effort</code>.</small></div>
      </div>
      <div class="field"><label>Metodi da usare (dipendono dal backend)</label>
        <label class="check"><input type="checkbox" id="s_think_reasoning_effort" ${s.think_reasoning_effort ? "checked" : ""}>
          <span><b>reasoning_effort</b><small>Standard OpenAI. Consigliato per Ollama (/v1) e llama.cpp: Off invia <code>"none"</code>.</small></span></label>
        <label class="check"><input type="checkbox" id="s_think_template_kwargs" ${s.think_template_kwargs ? "checked" : ""}>
          <span><b>chat_template_kwargs.enable_thinking</b><small>Per vLLM, SGLang e llama.cpp con Qwen3 e modelli simili.</small></span></label>
        <label class="check"><input type="checkbox" id="s_think_ollama_flag" ${s.think_ollama_flag ? "checked" : ""}>
          <span><b>think: true/false</b><small>Campo nativo di Ollama, per proxy che lo inoltrano.</small></span></label>
        <label class="check"><input type="checkbox" id="s_think_prompt_tag" ${s.think_prompt_tag ? "checked" : ""}>
          <span><b>/think · /no_think</b><small>Tag aggiunto al messaggio: funziona con Qwen3 su backend che non supportano i parametri.</small></span></label>
        <small>Se l'endpoint rifiuta un parametro, la risposta viene rigenerata senza e compare un avviso in chat.</small>
      </div>
    </div>

    <div class="section"><h3>Ricerca web</h3>
      <label class="check"><input type="checkbox" id="s_web_search_enabled" ${s.web_search_enabled ? "checked" : ""}>
        <span><b>Ricerca web attiva</b><small>Il modello può usare <code>web_search</code> e <code>fetch_url</code> quando gli manca un'informazione. Si attiva anche dal pulsante <b>Web</b> in alto.</small></span></label>
      <div class="row">
        <div class="field"><label>Motore di ricerca</label>
          <select class="inp" id="s_web_provider">
            ${[["auto", "Automatico (SearXNG, poi DuckDuckGo)"], ["searxng", "SearXNG (consigliato)"], ["duckduckgo", "DuckDuckGo (senza chiave)"], ["brave", "Brave Search API"], ["tavily", "Tavily API"]]
              .map(([v, l]) => `<option value="${v}" ${s.web_provider === v ? "selected" : ""}>${l}</option>`).join("")}
          </select></div>
        <div class="field"><label>Lingua / regione</label><input class="inp" id="s_web_language" value="${esc(s.web_language)}" placeholder="it" /></div>
      </div>
      <div class="field"><label>URL di SearXNG</label><input class="inp" id="s_searxng_url" value="${esc(s.searxng_url)}" placeholder="http://searxng:8080" />
        <small>Con il docker-compose incluso è <code>http://searxng:8080</code>.</small></div>
      <div class="field"><label>Chiave API (Brave o Tavily)</label><input class="inp" type="password" id="s_web_api_key" value="${esc(s.web_api_key)}" placeholder="solo per Brave/Tavily" /></div>
      <div class="row">
        <div class="field"><label>Risultati per ricerca</label><input class="inp" type="number" min="1" max="10" id="s_web_max_results" value="${s.web_max_results}" /></div>
        <div class="field"><label>Caratteri max per pagina letta</label><input class="inp" type="number" min="1000" step="1000" id="s_web_fetch_max_chars" value="${s.web_fetch_max_chars}" /></div>
      </div>
      <label class="check"><input type="checkbox" id="s_web_allow_private" ${s.web_allow_private ? "checked" : ""}>
        <span><b>Consenti pagine della rete locale</b><small>Sconsigliato: permetterebbe al modello di leggere servizi interni (router, NAS, Home Assistant) tramite <code>fetch_url</code>.</small></span></label>
      <div class="field"><div class="inp-group" style="align-items:center"><button class="btn" id="testWeb">${ICON.search}Prova ricerca</button><span id="webRes" style="font-size:13px;color:var(--muted)"></span></div></div>
    </div>

    <div class="section"><h3>Embedding &amp; RAG</h3>
      <div class="field"><label>Base URL embedding</label><input class="inp" id="s_embed_base_url" value="${esc(s.embed_base_url)}" placeholder="vuoto = stesso endpoint LLM" /></div>
      <div class="row">
        <div class="field"><label>API key embedding</label><input class="inp" type="password" id="s_embed_api_key" value="${esc(s.embed_api_key)}" placeholder="vuoto = stessa dell'LLM" /></div>
        <div class="field"><label>Modello embedding</label>
          <div class="inp-group"><input class="inp" id="s_embed_model" list="embedList" value="${esc(s.embed_model)}" /><datalist id="embedList"></datalist>
          <button class="btn" id="fetchEmbed" title="Carica modelli">${ICON.refresh}</button></div>
          <small>Es. <code>nomic-embed-text</code>, <code>bge-m3</code>, <code>text-embedding-3-small</code></small></div>
      </div>
      <div class="field"><div class="inp-group" style="align-items:center"><button class="btn" id="testEmbed">${ICON.play}Prova embedding</button><span id="embedRes" style="font-size:13px;color:var(--muted)"></span></div></div>
      <div class="row3">
        <div class="field"><label>Dim. chunk (caratteri)</label><input class="inp" type="number" id="s_chunk_size" value="${s.chunk_size}" /></div>
        <div class="field"><label>Overlap</label><input class="inp" type="number" id="s_chunk_overlap" value="${s.chunk_overlap}" /></div>
        <div class="field"><label>Top-K</label><input class="inp" type="number" id="s_rag_top_k" value="${s.rag_top_k}" /></div>
      </div>
      <div class="field"><label>Dove inserire gli estratti dei documenti</label>
        <select class="inp" id="s_rag_inject">
          <option value="user" ${s.rag_inject !== "system" ? "selected" : ""}>Nel messaggio dell'utente (consigliato: i modelli lo seguono meglio)</option>
          <option value="system" ${s.rag_inject === "system" ? "selected" : ""}>Nel prompt di sistema</option>
        </select></div>
      <div class="field"><label>Soglia minima di similarità: <b id="minVal">${s.rag_min_score}</b></label><input type="range" min="0" max="0.9" step="0.01" id="s_rag_min_score" value="${s.rag_min_score}" /></div>
    </div>
    <div class="actions"><button class="btn primary" id="saveSettings">${ICON.check}Salva impostazioni</button></div>`;

  $$("#presets button").forEach((b) => (b.onclick = () => {
    $("#s_llm_base_url").value = b.dataset.url;
    if (b.dataset.key) $("#s_llm_api_key").value = b.dataset.key;
    $$("#presets button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#s_temperature").oninput = (e) => ($("#tempVal").textContent = e.target.value);
  $("#s_rag_min_score").oninput = (e) => ($("#minVal").textContent = e.target.value);
  $("#s_entity_match_threshold").oninput = (e) => ($("#entThr").textContent = e.target.value);

  const collect = () => {
    const num = ["entity_match_threshold", "temperature", "max_tokens", "chunk_size", "chunk_overlap", "rag_top_k", "rag_min_score", "max_tool_rounds"];
    const out = {};
    $$("[id^=s_]").forEach((el) => {
      const k = el.id.slice(2);
      out[k] = el.type === "checkbox" ? el.checked : (num.includes(k) || el.type === "number" || el.type === "range") ? Number(el.value) : el.value;
    });
    return out;
  };
  const save = async (silent) => {
    state.settings = await api("/api/settings", { method: "PUT", body: collect() });
    renderThinkChip();
    renderWebChip();
    if (!silent) toast("Impostazioni salvate", "ok");
  };

  $("#fetchModels").onclick = async () => {
    try {
      await save(true);
      const { models } = await api("/api/models");
      const cur = $("#s_llm_model").value;
      $("#s_llm_model").innerHTML = models.map((m) => `<option ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join("");
      toast(`${models.length} modelli trovati`, "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  $("#fetchEmbed").onclick = async () => {
    try {
      await save(true);
      const { models } = await api("/api/models?target=embed");
      $("#embedList").innerHTML = models.map((m) => `<option value="${esc(m)}">`).join("");
      toast(`${models.length} modelli disponibili: scegli dal campo`, "ok");
      $("#s_embed_model").focus();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#testWeb").onclick = async () => {
    const out = $("#webRes");
    out.innerHTML = '<span class="spinner" style="display:inline-block"></span>';
    try {
      await save(true);
      const r = await api("/api/web/test", { method: "POST" });
      out.innerHTML = r.ok ? `<span style="color:var(--ok)">✓ ${esc(r.provider)}: ${r.count} risultati (es. «${esc(r.first)}»)</span>`
        : `<span style="color:var(--err)">${esc(r.error)}</span>`;
    } catch (e) { out.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; }
  };
  $("#testEmbed").onclick = async () => {
    const out = $("#embedRes");
    out.innerHTML = '<span class="spinner" style="display:inline-block"></span>';
    try {
      await save(true);
      const r = await api("/api/embeddings/test", { method: "POST" });
      out.innerHTML = r.ok ? `<span style="color:var(--ok)">✓ ${esc(r.model)} funziona (${r.dim} dimensioni)</span>`
        : `<span style="color:var(--err)">${esc(r.error)}</span>`;
    } catch (e) { out.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; }
  };
  $("#saveSettings").onclick = async () => {
    try { await save(); loadModels(); } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------- MCP ---------- */
function renderMcp() {
  $("#drawerTitle").textContent = "Server MCP";
  const body = $("#drawerBody");
  body.innerHTML = `
    <div class="section"><h3>Server collegati</h3><div id="srvList"></div>
      <div class="inp-group"><button class="btn" id="addSrvBtn">${ICON.plus}Aggiungi server MCP</button>
        <button class="btn" id="importSrvBtn">${ICON.upload}Importa JSON</button></div></div>
    <div class="section hidden" id="srvFormWrap"></div>`;
  $("#addSrvBtn").onclick = () => serverForm();
  $("#importSrvBtn").onclick = () => importForm();
  renderServerList();
}

function renderServerList() {
  const el = $("#srvList");
  if (!el) return;
  if (!state.servers.length) {
    el.innerHTML = `<div class="empty">${ICON.plug}<br>Nessun server MCP configurato.<br>Aggiungine uno per dare nuovi strumenti al modello.</div>`;
    return;
  }
  el.innerHTML = "";
  state.servers.forEach((s) => {
    const enabledCount = s.tools.filter((t) => t.enabled).length;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head">
        <div class="srv-icon">${ICON.server}</div>
        <div style="flex:1;min-width:0">
          <div class="card-title">${esc(s.name)} <span class="badge ${s.status}">${s.status}</span><span class="badge tr">${s.transport}</span>${s.has_token ? `<span class="badge tr" title="Autenticazione Bearer configurata" style="display:inline-flex;gap:4px;align-items:center">${ICON.lock.replace('<svg', '<svg style="width:11px;height:11px"')}token</span>` : ""}</div>
          <div class="card-sub">${esc(s.transport === "stdio" ? `${s.command} ${s.args.join(" ")}` : s.url)}</div>
        </div>
        <button class="icon-btn" data-a="refresh" title="Ricarica tool">${ICON.refresh}</button>
        <button class="icon-btn" data-a="edit" title="Modifica">${ICON.edit}</button>
        <button class="icon-btn" data-a="del" title="Elimina">${ICON.trash}</button>
        <label class="switch" title="Abilita server"><input type="checkbox" data-a="srv" ${s.enabled ? "checked" : ""}><span></span></label>
      </div>
      ${s.last_error ? `<div class="card-err">${esc(s.last_error)}</div>` : ""}
      ${s.tools.some((t) => t.name === "GetLiveContext") ? `<div class="srv-catalog" data-sid="${s.id}">
        <span class="cat-count">Catalogo entità: …</span>
        <input class="inp" placeholder="Prova un nome, es. «luce tv»" data-a="resolve-input">
        <button class="btn sm" data-a="resolve">Interpreta</button>
        <div class="cat-res"></div></div>` : ""}
      ${s.instructions ? `<details class="srv-instr"><summary>Istruzioni fornite dal server</summary><pre>${esc(s.instructions)}</pre></details>` : ""}
      ${s.tools.length ? `
        <div class="tools-bar">${ICON.tool}<span><b>${enabledCount}</b> / ${s.tools.length} tool abilitati</span><span class="spacer"></span>
          <input class="inp" style="width:150px;padding:5px 9px;font-size:12px" placeholder="Filtra…" data-a="filter">
          <button class="btn sm" data-a="all">Tutti</button><button class="btn sm" data-a="none">Nessuno</button></div>
        <div class="tools-list">${s.tools.map((t) => `
          <div class="tool-row" data-name="${esc(t.name)}">
            <div style="flex:1;min-width:0"><div class="tn">${esc(t.name)}</div><div class="td" title="${esc(t.description)}">${esc(t.description || "—")}</div></div>
            <button class="icon-btn" data-a="try" data-name="${esc(t.name)}" title="Prova il tool">${ICON.play}</button>
            <label class="switch"><input type="checkbox" data-tool="${esc(t.name)}" ${t.enabled ? "checked" : ""}><span></span></label>
          </div>`).join("")}</div>` : ""}`;
    card.onclick = async (ev) => {
      const a = ev.target.closest("[data-a]")?.dataset.a;
      try {
        if (a === "refresh") {
          const b = ev.target.closest("button");
          b.innerHTML = '<span class="spinner"></span>';
          const r = await api(`/api/mcp/servers/${s.id}/refresh`, { method: "POST" });
          toast(r.ok ? `${r.count} tool caricati` : r.error, r.ok ? "ok" : "err");
          await loadServers(); renderServerList();
        } else if (a === "resolve") {
          const inp = card.querySelector("[data-a=resolve-input]");
          const out = card.querySelector(".cat-res");
          if (!inp.value.trim()) return;
          out.innerHTML = '<span class="spinner" style="display:inline-block"></span>';
          const r = await api(`/api/mcp/servers/${s.id}/resolve`, { method: "POST", body: { name: inp.value, user_text: inp.value } });
          out.innerHTML = r.ambiguous ? `<span style="color:var(--warn)">Ambiguo — ${esc(r.ambiguous.split("Candidati più simili: ")[1]?.split(". Se")[0] || r.ambiguous)}</span>`
            : r.note ? `<span style="color:var(--ok)">${esc(r.note)}</span>` : `<span style="color:var(--ok)">Nome esatto: «${esc(r.args.name)}»</span>`;
        } else if (a === "try") {
          toolTester(card, s, ev.target.closest("[data-a]").dataset.name);
        } else if (a === "edit") serverForm(s);
        else if (a === "del") {
          if (!confirm(`Eliminare il server "${s.name}"?`)) return;
          await api(`/api/mcp/servers/${s.id}`, { method: "DELETE" });
          await loadServers(); renderServerList();
        } else if (a === "all" || a === "none") {
          await api(`/api/mcp/servers/${s.id}/tools`, { method: "PUT", body: { enabled: a === "all" } });
          await loadServers(); renderServerList();
        }
      } catch (e) { toast(e.message, "err"); }
    };
    card.onchange = async (ev) => {
      const t = ev.target;
      try {
        if (t.dataset.a === "srv") await api(`/api/mcp/servers/${s.id}`, { method: "PATCH", body: { enabled: t.checked } });
        else if (t.dataset.tool) await api(`/api/mcp/servers/${s.id}/tools/${encodeURIComponent(t.dataset.tool)}`, { method: "PUT", body: { enabled: t.checked } });
        await loadServers();
        const bar = card.querySelector(".tools-bar b");
        if (bar) bar.textContent = state.servers.find((x) => x.id === s.id).tools.filter((x) => x.enabled).length;
      } catch (e) { toast(e.message, "err"); }
    };
    card.oninput = (ev) => {
      if (ev.target.dataset.a !== "filter") return;
      const f = ev.target.value.toLowerCase();
      $$(".tool-row", card).forEach((r) => r.classList.toggle("hidden", !r.textContent.toLowerCase().includes(f)));
    };
    el.appendChild(card);
    const cat = card.querySelector(".srv-catalog");
    if (cat && s.enabled) api(`/api/mcp/servers/${s.id}/catalog`).then((r) => {
      cat.querySelector(".cat-count").innerHTML = r.count ? `Catalogo entità: <b>${r.count}</b>` : `<span style="color:var(--err)">Catalogo entità vuoto</span>`;
    }).catch(() => (cat.querySelector(".cat-count").textContent = "Catalogo non disponibile"));
    if (cat) cat.querySelector("[data-a=resolve-input]").onkeydown = (e) => { if (e.key === "Enter") cat.querySelector("[data-a=resolve]").click(); };
  });
}

function schemaSkeleton(schema) {
  const out = {};
  const req = schema?.required || [];
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (!req.includes(k)) continue;
    const t = p.type || (p.anyOf || [])[0]?.type;
    out[k] = p.enum ? p.enum[0] : t === "number" || t === "integer" ? 0 : t === "boolean" ? false : t === "array" ? [] : t === "object" ? {} : "";
  }
  return out;
}

function toolTester(card, srv, name) {
  const row = $(`.tool-row[data-name="${CSS.escape(name)}"]`, card);
  const next = row.nextElementSibling;
  if (next?.classList.contains("tool-test")) return next.remove();
  const tool = srv.tools.find((t) => t.name === name);
  const box = document.createElement("div");
  box.className = "tool-test";
  const props = Object.entries(tool.input_schema?.properties || {});
  box.innerHTML = `<div class="tlabel">Argomenti JSON${props.length ? ` · disponibili: ${props.map(([k]) => `<code>${esc(k)}</code>`).join(" ")}` : ""}</div>
    <textarea class="inp" rows="3" spellcheck="false">${esc(JSON.stringify(schemaSkeleton(tool.input_schema), null, 2))}</textarea>
    <div class="inp-group" style="align-items:center"><button class="btn sm primary">${ICON.play}Esegui</button><span class="tt-meta"></span></div>
    <pre class="hidden"></pre>`;
  row.after(box);
  $("button", box).onclick = async () => {
    let args;
    try { args = JSON.parse($("textarea", box).value || "{}"); } catch { return toast("JSON non valido", "err"); }
    $(".tt-meta", box).innerHTML = '<span class="spinner" style="display:inline-block"></span>';
    try {
      const r = await api(`/api/mcp/servers/${srv.id}/tools/${encodeURIComponent(name)}/call`, { method: "POST", body: { args } });
      $(".tt-meta", box).innerHTML = `${r.error ? '<span style="color:var(--err)">Errore</span>' : '<span style="color:var(--ok)">OK</span>'} · ${r.ms} ms · ${r.chars.toLocaleString()} caratteri`
        + (r.chars > (state.settings.tool_result_max_chars || 12000) ? ` · <span style="color:var(--warn)">oltre il limite inviato al modello</span>` : "");
      let out = r.result; try { out = JSON.stringify(JSON.parse(out), null, 2); } catch {}
      $("pre", box).textContent = out;
      $("pre", box).classList.remove("hidden");
    } catch (e) { $(".tt-meta", box).textContent = e.message; }
  };
}

function kvRows(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) entries.push(["", ""]);
  return entries.map(([k, v]) => `<div class="kv"><input class="inp" placeholder="Nome" value="${esc(k)}"><input class="inp" placeholder="Valore" value="${esc(v)}"><button class="icon-btn" data-rm>${ICON.x}</button></div>`).join("");
}
function readKv(box) {
  const o = {};
  $$(".kv", box).forEach((r) => { const [k, v] = $$("input", r).map((i) => i.value.trim()); if (k) o[k] = v; });
  return o;
}

function importForm() {
  const w = $("#srvFormWrap");
  w.classList.remove("hidden");
  w.onclick = null;
  w.innerHTML = `<h3>Importa da JSON</h3>
    <div class="field"><label>Incolla la configurazione (formato Claude Desktop, Cursor o VS Code)</label>
      <textarea class="inp" id="imp_json" rows="9" spellcheck="false" style="font-family:var(--mono);font-size:12.5px" placeholder='{
  "mcpServers": {
    "yfmcp": { "command": "uvx", "args": ["yfmcp"] },
    "remoto": { "url": "https://esempio.it/mcp", "headers": { "Authorization": "Bearer …" } }
  }
}'></textarea>
      <small>Ogni server viene creato e collegato. I server <code>stdio</code> (<code>uvx</code>, <code>npx</code>…) girano dentro il container: alla prima esecuzione scaricano i pacchetti e possono servire fino a un paio di minuti.</small></div>
    <div id="imp_res"></div>
    <div class="actions"><button class="btn" id="imp_cancel">Annulla</button><button class="btn primary" id="imp_go">${ICON.upload}Importa e collega</button></div>`;
  $("#imp_cancel").onclick = () => w.classList.add("hidden");
  $("#imp_go").onclick = async () => {
    const btn = $("#imp_go");
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Collegamento in corso… (la prima volta può richiedere qualche minuto)';
    try {
      const r = await api("/api/mcp/import", { method: "POST", body: { config: $("#imp_json").value } });
      $("#imp_res").innerHTML = r.servers.map((x) => `<div class="${x.ok ? "notice" : "err-box"}" style="${x.ok ? "color:var(--ok-fg);background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.3)" : ""}">
        <b>${esc(x.name)}</b> (${esc(x.transport)}): ${x.ok ? `collegato, ${x.count} tool` : esc(x.error)}</div>`).join("");
      await loadServers(); renderServerList();
      if (r.servers.every((x) => x.ok)) { toast(`${r.servers.length} server importati`, "ok"); setTimeout(() => w.classList.add("hidden"), 2500); }
    } catch (e) { $("#imp_res").innerHTML = `<div class="err-box">${esc(e.message)}</div>`; }
    btn.disabled = false; btn.innerHTML = `${ICON.upload}Importa e collega`;
  };
  w.scrollIntoView({ behavior: "smooth" });
  $("#imp_json").focus();
}

function serverForm(srv = null) {
  const w = $("#srvFormWrap");
  w.classList.remove("hidden");
  const s = srv || { name: "", transport: "http", url: "", headers: {}, command: "", args: [], env: {}, auth_token: "", enabled: true };
  w.innerHTML = `<h3>${srv ? "Modifica server" : "Nuovo server MCP"}</h3>
    <div class="field"><label>Nome</label><input class="inp" id="f_name" value="${esc(s.name)}" placeholder="es. filesystem, github, jira" /></div>
    <div class="field"><label>Trasporto</label>
      <div class="seg" id="f_tr">
        <button data-t="http" class="${s.transport === "http" ? "on" : ""}">Streamable HTTP</button>
        <button data-t="sse" class="${s.transport === "sse" ? "on" : ""}">SSE (legacy)</button>
        <button data-t="stdio" class="${s.transport === "stdio" ? "on" : ""}">stdio</button>
      </div></div>
    <div id="f_remote">
      <div class="field"><label>URL endpoint</label><input class="inp" id="f_url" value="${esc(s.url)}" placeholder="http://host.docker.internal:8000/mcp" /></div>
      <div class="field"><label>Bearer token</label>
        <div class="inp-group"><input class="inp" id="f_token" type="password" autocomplete="off" value="${esc(s.auth_token || "")}" placeholder="opzionale — es. ghp_xxx, sk-xxx, JWT…" />
        <button class="btn" id="f_token_eye" type="button" title="Mostra/nascondi">${ICON.eye}</button>
        ${s.has_token ? `<button class="btn danger" id="f_token_clear" type="button" title="Rimuovi token">${ICON.trash}</button>` : ""}</div>
        <small>Inviato come <code>Authorization: Bearer &lt;token&gt;</code>. ${s.has_token ? "Il token salvato è nascosto: lascialo così per mantenerlo o incollane uno nuovo." : "Puoi incollarlo anche con il prefisso “Bearer”."}</small></div>
      <div class="field"><label>Altri header HTTP (es. X-API-Key)</label><div id="f_headers">${kvRows(s.headers)}</div>
        <button class="btn sm" data-add="f_headers">${ICON.plus}Header</button></div>
    </div>
    <div id="f_local">
      <div class="field"><label>Comando</label><input class="inp" id="f_cmd" value="${esc(s.command)}" placeholder="uvx" /></div>
      <div class="field"><label>Argomenti (uno per riga)</label><textarea class="inp" id="f_args" rows="3" placeholder="mcp-server-time">${esc(s.args.join("\n"))}</textarea>
        <small>Il comando viene eseguito <b>dentro il container</b>: <code>uvx</code> è già disponibile; per server Node usa l'immagine con Node (vedi README).</small></div>
      <div class="field"><label>Variabili d'ambiente</label><div id="f_env">${kvRows(s.env)}</div>
        <button class="btn sm" data-add="f_env">${ICON.plus}Variabile</button></div>
    </div>
    <div class="actions"><button class="btn" id="f_cancel">Annulla</button><button class="btn primary" id="f_save">${ICON.plug}${srv ? "Salva e ricollega" : "Collega"}</button></div>`;
  let tr = s.transport;
  const sync = () => { $("#f_remote").classList.toggle("hidden", tr === "stdio"); $("#f_local").classList.toggle("hidden", tr !== "stdio"); };
  sync();
  $$("#f_tr button").forEach((b) => (b.onclick = () => { tr = b.dataset.t; $$("#f_tr button").forEach((x) => x.classList.toggle("on", x === b)); sync(); }));
  w.onclick = (ev) => {
    const add = ev.target.closest("[data-add]");
    if (add) $("#" + add.dataset.add).insertAdjacentHTML("beforeend", kvRows({ "": "" }));
    const rm = ev.target.closest("[data-rm]");
    if (rm) rm.parentElement.remove();
  };
  $("#f_token_eye").onclick = () => {
    const t = $("#f_token");
    if (t.value.startsWith("••••")) return toast("Il token salvato non è visualizzabile: puoi solo sostituirlo", "");
    t.type = t.type === "password" ? "text" : "password";
  };
  if ($("#f_token_clear")) $("#f_token_clear").onclick = () => { $("#f_token").value = ""; $("#f_token").focus(); toast("Il token verrà rimosso al salvataggio"); };
  $("#f_cancel").onclick = () => w.classList.add("hidden");
  $("#f_save").onclick = async () => {
    const body = {
      name: $("#f_name").value.trim() || "server", transport: tr, url: $("#f_url").value.trim(),
      headers: readKv($("#f_headers")), command: $("#f_cmd").value.trim(),
      args: $("#f_args").value.split("\n").map((x) => x.trim()).filter(Boolean), env: readKv($("#f_env")),
      auth_token: tr === "stdio" ? "" : $("#f_token").value.trim(),
      enabled: srv ? srv.enabled : true,
    };
    const btn = $("#f_save");
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Connessione…';
    try {
      const r = await api(srv ? `/api/mcp/servers/${srv.id}` : "/api/mcp/servers", { method: srv ? "PUT" : "POST", body });
      toast(r.ok ? `Connesso: ${r.count} tool disponibili` : `Salvato, ma connessione fallita: ${r.error}`, r.ok ? "ok" : "err");
      w.classList.add("hidden");
      await loadServers(); renderServerList();
    } catch (e) { toast(e.message, "err"); btn.disabled = false; btn.textContent = "Riprova"; }
  };
  w.scrollIntoView({ behavior: "smooth" });
  $("#f_name").focus();
}

/* ---------- Documenti ---------- */
function renderDocs() {
  $("#drawerTitle").textContent = "Knowledge base";
  $("#drawerBody").innerHTML = `
    <div class="section"><h3>Carica documenti</h3>
      <div class="dropzone" id="dz">${ICON.upload}<div><b>Trascina qui i file</b> o clicca per selezionarli</div>
        <small style="color:var(--faint)">PDF, DOCX, TXT, MD, CSV, HTML, JSON, codice sorgente…</small></div>
      <small style="display:block;color:var(--faint);margin-top:8px">Modello di embedding: <code>${esc(state.settings.embed_model)}</code></small></div>
    <div class="section"><h3>Prova la ricerca semantica</h3>
      <div class="inp-group"><input class="inp" id="ragQ" placeholder="Scrivi una domanda…"><button class="btn" id="ragGo">${ICON.search}Cerca</button></div>
      <div class="search-results" id="ragRes" style="margin-top:10px"></div></div>
    <div class="section"><h3>Documenti indicizzati</h3>
      <div class="inp-group" style="margin-bottom:12px;justify-content:flex-end">
        <button class="btn sm" id="retryErr">${ICON.refresh}Riprova quelli in errore</button>
        <button class="btn sm" id="reindexAll" title="Da usare dopo aver cambiato modello di embedding o dimensione dei chunk">${ICON.refresh}Reindicizza tutto</button>
      </div><div id="docList"></div></div>`;
  const dz = $("#dz");
  dz.onclick = () => $("#fileInput").click();
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("drag"); };
  dz.ondragleave = () => dz.classList.remove("drag");
  dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("drag"); uploadFiles(e.dataTransfer.files); };
  const go = async () => {
    const q = $("#ragQ").value.trim();
    if (!q) return;
    $("#ragRes").innerHTML = '<span class="spinner"></span>';
    try {
      const hits = await api("/api/rag/search", { method: "POST", body: { query: q } });
      $("#ragRes").innerHTML = hits.length ? hits.map((h) => `<div class="hit"><div class="hh"><span>${esc(h.name)} · chunk ${h.idx}</span><b>${(h.score * 100).toFixed(1)}%</b></div><div class="hb">${esc(h.text)}</div></div>`).join("")
        : `<div class="empty">Nessun risultato</div>`;
    } catch (e) { $("#ragRes").innerHTML = `<div class="err-box">${esc(e.message)}</div>`; }
  };
  $("#ragGo").onclick = go;
  const reidx = async (onlyErrors) => {
    if (!onlyErrors && !confirm("Reindicizzare tutti i documenti con le impostazioni attuali?")) return;
    const r = await api("/api/documents/reindex", { method: "POST", body: { only_errors: onlyErrors } });
    toast(`${r.count} documenti in indicizzazione`, "ok");
    loadDocs();
  };
  $("#retryErr").onclick = () => reidx(true);
  $("#reindexAll").onclick = () => reidx(false);
  $("#ragQ").onkeydown = (e) => e.key === "Enter" && go();
  renderDocsList();
}

function fmtSize(b) { return b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : b > 1e3 ? (b / 1e3).toFixed(0) + " KB" : b + " B"; }

function renderDocsList() {
  const el = $("#docList");
  if (!el) return;
  if (!state.docs.length) { el.innerHTML = `<div class="empty">Nessun documento. Caricane uno per abilitare il RAG.</div>`; return; }
  el.innerHTML = state.docs.map((d) => {
    const ext = (d.name.split(".").pop() || "").slice(0, 4);
    const meta = d.status === "processing" ? `<span class="spinner" style="display:inline-block;vertical-align:-2px"></span> Indicizzazione in corso…`
      : d.status === "error" ? esc(d.error) : `${d.chunks} chunk · ${fmtSize(d.size)} · ${esc(d.embed_model)}`;
    return `<div class="doc" data-id="${d.id}"><div class="ext">${esc(ext)}</div>
      <div class="info"><div class="nm" title="${esc(d.name)}">${esc(d.name)}</div><div class="mt ${d.status === "error" ? "err" : ""}">${meta}</div></div>
      ${d.status === "error" ? `<button class="btn sm" data-retry>${ICON.refresh}Riprova</button>` : ""}
      <label class="switch" title="Includi nella ricerca"><input type="checkbox" ${d.enabled ? "checked" : ""}><span></span></label>
      <button class="icon-btn" data-del title="Elimina">${ICON.trash}</button></div>`;
  }).join("");
  $$(".doc", el).forEach((row) => {
    const id = row.dataset.id;
    $("input", row).onchange = (e) => api(`/api/documents/${id}`, { method: "PATCH", body: { enabled: e.target.checked } });
    const retry = $("[data-retry]", row);
    if (retry) retry.onclick = async () => { await api(`/api/documents/${id}/reindex`, { method: "POST" }); loadDocs(); };
    $("[data-del]", row).onclick = async () => {
      if (!confirm("Eliminare il documento dalla knowledge base?")) return;
      await api(`/api/documents/${id}`, { method: "DELETE" });
      loadDocs();
    };
  });
}

async function uploadFiles(files) {
  // copia subito l'elenco: il FileList del selettore è "vivo" e si svuota quando il campo viene azzerato
  const list = [...(files || [])];
  if (!list.length) return;
  const fd = new FormData();
  list.forEach((f) => fd.append("files", f));
  const names = list.map((f) => f.name);
  toast(`Caricamento di ${names.length === 1 ? `«${names[0]}»` : `${names.length} file`}…`);
  try {
    const res = await api("/api/documents", { method: "POST", body: fd });
    const n = Array.isArray(res) ? res.length : list.length;
    toast(n === 1 ? `«${res[0]?.name || names[0]}» caricato: indicizzazione in corso` : `${n} file caricati: indicizzazione in corso`, "ok");
    loadDocs();
  } catch (e) { toast(`Caricamento non riuscito: ${e.message}`, "err"); }
}

/* ===================== Binding ===================== */
function bindUI() {
  $("#newChat").onclick = newChat;
  $("#convSearch").oninput = renderConvs;
  $("#sendBtn").onclick = () => (state.busy ? stop() : send());
  $("#input").addEventListener("input", autoGrow);
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $$(".suggestions button").forEach((b) => (b.onclick = () => send(b.textContent)));
  $$(".side-nav button").forEach((b) => (b.onclick = () => { openPanel(b.dataset.panel); $("#sidebar").classList.remove("open"); }));
  $("#drawerClose").onclick = closePanel;
  $("#drawerBackdrop").onclick = closePanel;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
  $("#thinkChip").onclick = () => cycleThinking().catch((e) => toast(e.message, "err"));
  $("#useWeb").onchange = async (e) => {
    try {
      state.settings = await api("/api/settings", { method: "PUT", body: { web_search_enabled: e.target.checked } });
      renderWebChip();
      toast(e.target.checked ? "Ricerca web attiva: il modello cercherà online quando gli manca un'informazione" : "Ricerca web disattivata");
    } catch (err) { toast(err.message, "err"); }
  };
  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("open");
  $("#modelSelect").onchange = async (e) => {
    state.settings = await api("/api/settings", { method: "PUT", body: { llm_model: e.target.value } });
    toast(`Modello: ${e.target.value}`, "ok");
  };
  $("#attachBtn").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => { uploadFiles(e.target.files); e.target.value = ""; };
  const comp = $("#composer");
  comp.ondragover = (e) => { e.preventDefault(); comp.classList.add("drag"); };
  comp.ondragleave = () => comp.classList.remove("drag");
  comp.ondrop = (e) => { e.preventDefault(); comp.classList.remove("drag"); uploadFiles(e.dataTransfer.files); };
}

init().catch((e) => toast(e.message, "err"));
