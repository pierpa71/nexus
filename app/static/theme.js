/* Nexus — tema e aspetto dell'interfaccia.
   Caricato nel <head> (sincrono): applica subito le preferenze salvate nel browser, evitando lo "sfarfallio"
   del tema predefinito. app.js poi sincronizza con le preferenze salvate sul server. */
(function () {
  const PRESETS = {
    nexus:    { label: "Nexus",    accent: "#8b5cf6", accent2: "#22d3ee", accent3: "#db2777" },
    smeraldo: { label: "Smeraldo", accent: "#10b981", accent2: "#84cc16", accent3: "#06b6d4" },
    oceano:   { label: "Oceano",   accent: "#3b82f6", accent2: "#06b6d4", accent3: "#6366f1" },
    tramonto: { label: "Tramonto", accent: "#f97316", accent2: "#ec4899", accent3: "#f59e0b" },
    rosa:     { label: "Rosa",     accent: "#ec4899", accent2: "#a855f7", accent3: "#f43f5e" },
    ambra:    { label: "Ambra",    accent: "#f59e0b", accent2: "#ef4444", accent3: "#eab308" },
    grafite:  { label: "Grafite",  accent: "#94a3b8", accent2: "#e2e8f0", accent3: "#64748b" },
  };
  const FONTS = {
    inter:   { label: "Inter",        stack: '"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif' },
    system:  { label: "Di sistema",   stack: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif' },
    rounded: { label: "Arrotondato",  stack: 'ui-rounded, "SF Pro Rounded", "Nunito", "Quicksand", "Varela Round", system-ui, sans-serif' },
    serif:   { label: "Serif",        stack: '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif' },
    mono:    { label: "Monospace",    stack: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
  };
  const DEFAULTS = {
    theme: "dark", preset: "nexus", accent: "#8b5cf6", accent2: "#22d3ee", accent3: "#db2777",
    bg: "aurora", bgColor: "#07070d", bgImage: "", bgDim: 0.45,
    auroraIntensity: 0.38, auroraSpeed: 22, glass: 0.62, grain: true,
    font: "inter", scale: 1, corners: "round", density: "normal", width: "normal",
    motion: true, bubble: "gradient",
  };
  const KEY = "nexus.ui";

  const rgb = (hex) => {
    const h = String(hex || "").replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return isNaN(n) ? "139, 92, 246" : `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
  };
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function apply(input) {
    const ui = Object.assign({}, DEFAULTS, input || {});
    const root = document.documentElement;
    const st = root.style;
    const theme = ui.theme === "system" ? (mq && mq.matches ? "light" : "dark") : ui.theme;
    root.dataset.theme = theme;
    root.dataset.bg = ui.bg;
    root.dataset.corners = ui.corners;
    root.dataset.bubble = ui.bubble;
    root.dataset.motion = ui.motion ? "on" : "off";
    st.setProperty("--accent-rgb", rgb(ui.accent));
    st.setProperty("--accent2-rgb", rgb(ui.accent2));
    st.setProperty("--accent3", ui.accent3);
    st.setProperty("--glass", String(ui.glass));
    st.setProperty("--aurora-opacity", String(theme === "light" ? ui.auroraIntensity * 0.6 : ui.auroraIntensity));
    st.setProperty("--aurora-speed", `${ui.auroraSpeed}s`);
    st.setProperty("--grain", ui.grain ? "0.035" : "0");
    st.setProperty("--font", (FONTS[ui.font] || FONTS.inter).stack);
    st.setProperty("--scale", String(ui.scale));
    st.setProperty("--msg-gap", { compact: "14px", normal: "26px", relaxed: "38px" }[ui.density] || "26px");
    st.setProperty("--chat-width", { normal: "860px", wide: "1120px", full: "100%" }[ui.width] || "860px");
    st.setProperty("--bg-dim", String(ui.bgDim));
    if (ui.bg === "solid") st.setProperty("--bg-solid", ui.bgColor); else st.removeProperty("--bg-solid");
    st.setProperty("--bg-image", ui.bgImage ? `url("${String(ui.bgImage).replace(/"/g, "%22")}")` : "none");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "light" ? "#f4f4f9" : "#07070d";
    return ui;
  }

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function store(ui) {
    try { localStorage.setItem(KEY, JSON.stringify(ui)); } catch (e) { /* storage non disponibile */ }
  }

  let current = apply(load());
  if (mq && mq.addEventListener) mq.addEventListener("change", () => current.theme === "system" && apply(current));

  window.NexusTheme = {
    PRESETS, FONTS, DEFAULTS,
    get: () => Object.assign({}, current),
    set(ui, persistLocal = true) {
      current = apply(ui);
      if (persistLocal) store(current);
      return current;
    },
  };
})();
