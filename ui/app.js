/* 屏幕问答 · WebView 前端 */
const $ = (id) => document.getElementById(id);
let state = { cfg: {}, busy: false, last: null, items: [], idx: 0 };

/* ---------- 与 Python 通信 ---------- */
async function py(method, ...args) {
  if (!window.pywebview || !window.pywebview.api) throw new Error("后端未连接");
  return window.pywebview.api[method](...args);
}
function toast(msg, ms = 1600) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function setStatus(s) { $("status").textContent = s; }
function setBusy(b) {
  state.busy = b;
  document.querySelectorAll(".actions .btn, #btnRecalc").forEach((el) => {
    if (["btnCont"].includes(el.id)) return;
    el.disabled = b;
  });
  if (b) setStatus("作答中…");
}

/* ---------- 渲染 ---------- */
function applyTheme(cfg) {
  const body = document.body;
  body.className = "";
  body.classList.add("theme-" + (cfg.theme || "light"));
  body.classList.add("accent-" + (cfg.accent || "blue"));
  if (cfg.font_scale) document.documentElement.style.fontSize = (13 * cfg.font_scale / 100) + "px";
}
function chipFor(conf) {
  const el = $("confChip");
  el.hidden = false; el.className = "chip";
  if (conf === "高") { el.textContent = "🟢 高置信度"; }
  else if (conf === "中") { el.textContent = "🟡 中置信度"; el.classList.add("mid"); }
  else { el.textContent = "🔴 存疑"; el.classList.add("low"); }
}
function renderResult(payload) {
  state.last = payload;
  const items = payload.items || (payload.result ? [payload.result] : []);
  state.items = items; state.idx = 0;
  const r = items[0];
  if (!r) { return; }
  $("answer").textContent = (items.length > 1 ? "第 1/" + items.length + " 题：" : "") + (r.answer || "—");
  chipFor(r.confidence);
  const pct = Math.max(0, Math.min(1, r.agreement || 0)) * 100;
  const bar = $("agreeBar");
  bar.style.width = pct + "%";
  bar.style.background = r.confidence === "高" ? "var(--green)"
    : r.confidence === "中" ? "var(--amber)" : "var(--red)";

  const bits = [];
  if (r.check === "ok") bits.push(["✅ 代入验算", "ok"]);
  if (r.check === "fail") bits.push(["⚠ 代入验算不通过", "warn"]);
  if (r.adaptive) bits.push(["🎯 一击命中", "ok"]);
  if (r.dual) bits.push([r.dual_note && r.dual_note.includes("不一致") ? "🤝 不一致" : "🤝 双模型一致", r.dual_note && r.dual_note.includes("不一致") ? "warn" : "ok"]);
  if (payload.cached) bits.push(["⚡ 缓存", ""]);
  if (payload.cost_num) bits.push(["¥" + payload.cost_num.toFixed(4), ""]);
  $("badges").innerHTML = bits.map(([t, k]) => `<span class="badge ${k}">${t}</span>`).join("");

  const meta = [];
  meta.push("一致 " + Math.round((r.agreement || 0) * 100) + "%");
  meta.push((r.calls || 0) + " 次调用");
  meta.push(((r.elapsed || 0)).toFixed(1) + "s");
  if (payload.backend) meta.push(payload.backend);
  $("meta").textContent = meta.join(" · ");

  const detail = [];
  if (r.fill) detail.push("填入答案：" + r.fill);
  if (r.latex) detail.push("LaTeX：" + r.latex);
  if (r.check_note) detail.push("验算：" + r.check_note);
  if (r.reasoning) detail.push("\n" + r.reasoning);
  if ((payload.warnings || []).length) detail.push("\n⚠ " + payload.warnings.join("\n⚠ "));
  $("detail").textContent = detail.join("\n") || "（无）";

  if (payload.text || payload.pretty_q) {
    $("qCard").hidden = false;
    $("qText").value = payload.pretty_q || payload.text || "";
    if (payload.translation) { $("trans").hidden = false; $("trans").textContent = "翻译：" + payload.translation; }
    else { $("trans").hidden = true; }
  }
  const st = [];
  if (r.qno) st.push("第 " + r.qno + " 题");
  st.push((r.confidence || "—") + "置信度");
  st.push("总 " + ((payload.elapsed || 0)).toFixed(1) + "s");
  if (payload.cached) st.push("缓存秒出");
  setStatus(st.join(" · "));
  setBusy(false);
}
function renderError(payload) {
  setBusy(false);
  $("answer").textContent = "失败";
  $("confChip").hidden = true;
  $("agreeBar").style.width = "0";
  $("meta").textContent = payload.stage ? "阶段：" + payload.stage : "";
  $("detail").textContent = "❌ " + (payload.error || "未知错误") +
    "\n\n尝试：① 把题目框大一点 ② Ctrl+Alt+T 直接用文字作答 ③ 检查 Key 与网络";
  setStatus("失败 · 可点「重算」重试");
}


/* ---------- 通用弹层 ---------- */
function openModal({ title, html = "", input = "", okText = "", onOk = null }) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = html;
  const inp = $("modalInput");
  const ok = $("modalOk");
  inp.hidden = !input;
  if (input) { inp.value = ""; inp.placeholder = input; setTimeout(() => inp.focus(), 30); }
  ok.hidden = !onOk;
  ok.textContent = okText || "确定";
  ok.onclick = async () => {
    if (!onOk) { closeModal(); return; }
    const val = inp.value.trim();
    ok.disabled = true;
    try { await onOk(val); } finally { ok.disabled = false; }
  };
  inp.onkeydown = (e) => { if (e.key === "Enter" && onOk) ok.click(); };
  $("modal").hidden = false; $("modalMask").hidden = false;
}
function closeModal() { $("modal").hidden = true; $("modalMask").hidden = true; }
function showText(title, text) { openModal({ title, html: `<pre>${escapeHtml(text)}</pre>` }); }
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
async function withBusy(label, fn) {
  setBusy(true); setStatus(label);
  try { return await fn(); } finally { setBusy(false); }
}

/* ---------- 动作 ---------- */
async function doShot(mode) {
  setBusy(true); setStatus(mode === "region" ? "圈选题目…" : "整屏识别…");
  try {
    const res = await py("capture", mode);
    if (res && res.ok === false) { renderError(res); return; }
    if (res && res.cancelled) { setBusy(false); setStatus("已取消"); return; }
    renderResult(res);
  } catch (e) { renderError({ error: String(e) }); }
}
function openTyped() {
  $("qCard").hidden = false;
  $("qText").value = "";
  $("qText").focus();
  setStatus("把题面粘进下面的框，然后点「用这段重算」");
}
async function doPaste() {
  setBusy(true); setStatus("读取剪贴板…");
  try { renderResult(await py("paste")); } catch (e) { renderError({ error: String(e) }); }
}
async function copyAnswer() {
  const r = state.items[state.idx];
  const val = (r && (r.fill || r.answer)) || "";
  if (!val) { toast("还没有答案"); return; }
  await py("copy", val); toast("已复制：" + val);
}
async function loadCfg() {
  const cfg = await py("get_cfg");
  state.cfg = cfg;
  applyTheme(cfg);
  $("cfgKey").value = cfg.api_key_masked ? "" : (cfg.api_key || "");
  $("cfgKey").placeholder = cfg.api_key_masked ? "（已保存：" + cfg.api_key_masked + "）" : "sk-...";
  $("cfgBase").value = cfg.base_url || "";
  $("cfgTheme").value = cfg.theme || "light";
  $("cfgAccent").value = cfg.accent || "blue";
  $("cfgFont").value = cfg.font_scale || 100;
  $("cfgOpacity").value = cfg.opacity || 100;
  $("cfgAutoPaste").checked = !!cfg.auto_paste;
  $("cfgEnter").checked = !!cfg.auto_enter;
  $("cfgDual").checked = !!cfg.dual;
  $("cfgAdaptive").checked = !!cfg.adaptive;
  $("cfgSound").checked = !!cfg.sound;
  $("cfgTop").checked = !!cfg.always_on_top;
  $("reader").value = (cfg.reader_order || ["winocr", "modlens"]).join(",");
  $("model").value = cfg.model || "deepseek-chat";
  $("samples").value = String(cfg.samples || 3);
  $("verify").checked = !!cfg.verify;
  $("intervalLabel").textContent = cfg.interval || 3;
  $("regionLabel").textContent = cfg.cont_bbox ? "已圈定区域" : "整个屏幕";
  $("meta2").textContent = "v" + (cfg.version || "");
}
async function saveCfg() {
  const patch = {
    base_url: $("cfgBase").value.trim(),
    theme: $("cfgTheme").value,
    accent: $("cfgAccent").value,
    font_scale: parseInt($("cfgFont").value, 10) || 100,
    opacity: parseInt($("cfgOpacity").value, 10) || 100,
    auto_paste: $("cfgAutoPaste").checked,
    auto_enter: $("cfgEnter").checked,
    dual: $("cfgDual").checked,
    adaptive: $("cfgAdaptive").checked,
    sound: $("cfgSound").checked,
    always_on_top: $("cfgTop").checked,
    reader_order: $("reader").value.split(","),
    model: $("model").value,
    samples: parseInt($("samples").value, 10) || 3,
    verify: $("verify").checked,
  };
  const key = $("cfgKey").value.trim();
  if (key) patch.api_key = key;
  const out = await py("save_cfg", patch);
  state.cfg = out.cfg || state.cfg;
  applyTheme(state.cfg);
  await py("apply_window", state.cfg.always_on_top, state.cfg.opacity);
  $("drawer").hidden = true; $("mask").hidden = true;
  toast("设置已保存");
  setStatus("设置已保存");
}

/* ---------- 事件绑定 ---------- */
function bind() {
  $("btnRegion").onclick = () => doShot("region");
  $("btnFull").onclick = () => doShot("full");
  $("btnPaste").onclick = doPaste;
  $("btnCopy").onclick = copyAnswer;
  $("btnRedo").onclick = async () => {
    setBusy(true); setStatus("重算（跳过缓存）…");
    try { renderResult(await py("redo")); } catch (e) { renderError({ error: String(e) }); }
  };
  $("btnCont").onclick = async () => {
    const on = await py("toggle_continuous");
    $("btnCont").textContent = on ? "⏸ 停止连续" : "▶ 连续截图";
    toast(on ? "连续截图已开启" : "连续截图已停止");
  };
  $("btnTyped").onclick = openTyped;
  $("btnOpen").onclick = async () => {
    setBusy(true); setStatus("打开图片…");
    try { renderResult(await py("open_image")); } catch (e) { renderError({ error: String(e) }); }
  };
  $("btnLatex").onclick = async () => {
    const r = state.items[state.idx];
    if (!r || !r.latex) { toast("这题没有 LaTeX"); return; }
    await py("copy", r.latex); toast("LaTeX 已复制");
  };
  $("btnAlt").onclick = async () => {
    setBusy(true); setStatus("换一种解法…");
    try {
      const out = await py("alt_method");
      $("detail").textContent = out.text || out.error || "";
      setStatus("已换一种解法");
    } catch (e) { toast(String(e)); }
    setBusy(false);
  };
  $("btnBad").onclick = async () => {
    const msg = await py("mark_error");
    toast(msg || "已记录"); setStatus(msg || "");
  };
  $("btnSelfcheck").onclick = async () => {
    setStatus("自检中…");
    const text = await py("selfcheck");
    $("detail").textContent = text; setStatus("自检完成");
  };
  $("btnBalance").onclick = async () => {
    setStatus("查余额…");
    const text = await py("balance_text");
    $("detail").textContent = text; setStatus("余额已更新");
    const m = text.match(/¥?([\d.]+)/);
    if (m) $("meta2").textContent = "¥" + m[1] + " · v" + (state.cfg.version || "");
  };
  $("btnRecalc").onclick = async () => {
    const t = $("qText").value.trim();
    if (!t) { toast("题面是空的"); return; }
    setBusy(true); setStatus("按改过的题面重算…");
    try { renderResult(await py("solve_text", t)); } catch (e) { renderError({ error: String(e) }); }
  };
  $("btnSettings").onclick = async () => {
    await loadCfg();
    $("drawer").hidden = false; $("mask").hidden = false;
  };
  $("btnCloseDrawer").onclick = $("btnCancelCfg").onclick = () => {
    $("drawer").hidden = true; $("mask").hidden = true;
  };
  $("btnSaveCfg").onclick = saveCfg;

  $("btnJudge").onclick = () => {
    const r = state.items[state.idx];
    if (!r) { toast("先做一道题"); return; }
    openModal({
      title: "判卷：你填的答案是？", input: "例如 6 或 2, 3", okText: "判一下",
      onOk: async (val) => {
        closeModal();
        await withBusy("判卷中…", async () => {
          const out = await py("judge", val);
          if (out.ok) showText("判卷结果", out.text);
          else toast(out.error || "判卷失败");
        });
      },
    });
  };
  $("btnPractice").onclick = () => withBusy("生成同类练习…", async () => {
    const out = await py("practice");
    if (out.ok) showText("同类练习（3 道）", out.text);
    else toast(out.error || "生成失败");
  });
  $("btnLedger").onclick = () => withBusy("统计中…", async () => {
    const d = await py("ledger");
    if (!d.ok) { toast(d.error || "账本读取失败"); return; }
    const s0 = d.summary || {};
    const bm = d.by_model || [];
    const arr = Array.isArray(bm) ? bm : Object.entries(bm).map(([k, v]) => ({ model: k, n: (v && v.n) != null ? v.n : v }));
    const num = (v) => (v && typeof v === "object" ? (v.n != null ? v.n : Object.values(v)[0]) : v);
    const rows = arr.map((m) => `<div class="kv"><span>${escapeHtml(m.model || m[0] || "—")}</span><b>${num(m.n)} 题</b></div>`).join("");
    openModal({
      title: "账本（近 30 天）",
      html: `<img src="${d.url}" alt="30 天柱状图">
        <div class="kv"><span>累计</span><b>${s0.total_n || 0} 题 · ¥${(s0.total_cost || 0).toFixed(4)}</b></div>
        <div class="kv"><span>今日</span><b>${s0.today_n || 0} 题 · ¥${(s0.today_cost || 0).toFixed(4)}</b></div>
        ${rows}`,
    });
  });
  $("btnHistory").onclick = () => withBusy("读历史…", async () => {
    const list = await py("history");
    if (!list.length) { toast("还没有历史记录"); return; }
    const html = `<div class="list">${list.map((h) => `<div class="it"><span>${escapeHtml(h.title || h.name)}</span><small>${h.time}</small></div>`).join("")}</div>`;
    openModal({ title: "历史（最近 40 条）", html });
  });
  $("btnRecent").onclick = () => withBusy("读最近题图…", async () => {
    const list = await py("recent");
    if (!list.length) { toast("还没有做题截图"); return; }
    const html = `<div class="grid-img">${list.map((r) => `<img src="${r.url}" title="${r.name}">`).join("")}</div>`;
    openModal({ title: "最近的题目截图", html });
  });
  $("btnPlot").onclick = () => withBusy("画图中…", async () => {
    const d = await py("plot");
    if (!d.ok) { toast(d.error || "画不出来"); return; }
    openModal({ title: "函数图像 · " + (d.exprs || []).join(" / "), html: `<img src="${d.url}">` });
  });
  $("btnCard").onclick = () => withBusy("导出卡片…", async () => {
    const d = await py("export_card");
    if (d.ok) { toast("卡片已保存"); showText("单题卡片", d.path); }
    else toast(d.error || "导出失败");
  });
  $("btnExport").onclick = () => withBusy("导出 CSV…", async () => {
    const d = await py("export_csv");
    if (d.ok) toast(`已导出 ${d.count} 条 → records.csv`);
    else toast(d.error || "导出失败");
  });
  $("btnWarm").onclick = () => withBusy("自检中…", async () => {
    showText("考前热身 / 自检", await py("warmup"));
  });
  $("modalX").onclick = $("modalCancel").onclick = closeModal;
  $("modalMask").onclick = closeModal;
  $("btnMin").onclick = () => py("win_minimize");
  $("btnClose").onclick = () => py("win_close");
  $("btnPin").onclick = async () => {
    const on = await py("toggle_pin");
    $("btnPin").style.opacity = on ? 1 : 0.45;
    toast(on ? "已置顶" : "取消置顶");
  };
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
      document.querySelectorAll("[data-page-body]").forEach((p) => {
        p.hidden = p.dataset.pageBody !== t.dataset.page;
      });
    };
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("drawer").hidden = true; $("mask").hidden = true; closeModal(); }
    if (e.ctrlKey && e.key === "Enter") { $("btnRecalc").click(); }
    if (/^[1-9]$/.test(e.key) && state.items.length > 1 && document.activeElement.tagName !== "TEXTAREA") {
      const i = parseInt(e.key, 10) - 1;
      if (state.items[i]) { state.idx = i; renderResult({ ...state.last, items: state.items }); toast("已切到第 " + (i + 1) + " 题"); }
    }
  });
}

/* ---------- 启动 ---------- */
window.addEventListener("pywebviewready", async () => {
  bind();
  try {
    await loadCfg();
    setStatus("就绪 · Ctrl+Alt+Q 框选 · Ctrl+Alt+T 文字作答");
  } catch (e) { setStatus("后端未就绪：" + e); }
});
