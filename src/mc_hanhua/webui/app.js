const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 80;
const ui = {
  mode: "logs",
  records: [],
  logs: [],
  openIds: new Set(),
  drafts: {},
  editing: {},
  scroll: {logs: 0, preview: 0, review: 0},
  loadedCount: PAGE_SIZE,
  snapshot: null,
  backend: null,
  fixture: true,
  modelOptions: [],
  taskId: null,
  pendingSave: null,
};

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
function setModelOptions(models) {
  ui.modelOptions = models.map((model) => String(model));
  renderModelList("");
}
function renderModelList(query) {
  const q = (query === undefined ? $("connModel").value.trim().toLowerCase() : query);
  const items = ui.modelOptions.filter((model) => !q || model.toLowerCase().includes(q));
  $("modelList").innerHTML = items.length
    ? items.map((model) => "<button type=\"button\" data-model=\"" + esc(model) + "\">" + esc(model) + "</button>").join("")
    : "<span class=\"none\">没有匹配的模型</span>";
}
function openModelList() {
  if (!ui.modelOptions.length) return;
  // Opening always shows the full list; only typing while open narrows it —
  // otherwise the already-typed model name would filter out every suggestion.
  renderModelList("");
  $("modelList").hidden = false;
}
function closeModelList() {
  $("modelList").hidden = true;
}
function note(s) {
  $("toast").textContent = s;
  $("toast").style.display = "block";
  setTimeout(() => { $("toast").style.display = "none"; }, 2300);
}
function rememberScroll() {
  const box = $("content");
  ui.scroll[ui.mode] = box.scrollTop;
}
function restoreScroll() {
  $("content").scrollTop = ui.scroll[ui.mode] || 0;
}
function tab(name) {
  rememberScroll();
  ui.mode = name;
  document.querySelectorAll("nav button[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    if (active) $("content").setAttribute("aria-labelledby", button.id);
  });
  render();
  restoreScroll();
}
function call(command, payload) {
  if (!ui.backend || typeof ui.backend.call !== "function") return;
  const request = JSON.stringify(Object.assign({command}, payload || {}));
  ui.backend.call(request, (reply) => {
    if (typeof reply === "string" && reply) applySnapshot(JSON.parse(reply));
  });
}
function applySnapshot(data) {
  if (!data) return;
  const content = $("content");
  const previousTop = content.scrollTop;
  const followLogs = ui.mode === "logs" && $("followLogs").checked && content.scrollHeight - previousTop - content.clientHeight <= 40;
  ui.fixture = false;
  ui.snapshot = data;
  // A10: drafts, open cards and edit states belong to one task; a new task
  // id must not inherit the previous task's in-page state.
  if (data.task_id && ui.taskId && data.task_id !== ui.taskId) {
    ui.drafts = {};
    ui.openIds.clear();
    ui.editing = {};
    ui.pendingSave = null;
  }
  ui.taskId = data.task_id || ui.taskId;
  if (Array.isArray(data.records)) ui.records = data.records;
  if (Array.isArray(data.logs)) ui.logs = data.logs;
  $("filename").textContent = data.input_name || "尚未选择输入资源";
  $("filename").title = data.input_detail || data.input_name || "";
  $("inputDetail").textContent = data.input_detail || "支持 zip / jar / mrpack，以及地图或整合包目录";
  $("inputDetail").title = $("inputDetail").textContent;
  $("out").value = data.output_path || "";
  $("status").textContent = data.status_title || "等待开始";
  $("desc").textContent = data.status_desc || "";
  $("desc").title = data.status_desc || "";
  $("taskState").textContent = data.status_title || "等待开始";
  $("taskState").title = data.stage_text || data.status_desc || "";
  const connection = data.connection || {};
  $("connectionState").textContent = connection.has_api_key ? "API 已配置 · " + (connection.model || "未指定模型") : "本地词库模式";
  $("connectionState").title = connection.has_api_key ? "配置已保存；实际请求状态请查看处理日志。" : "未使用 API Key";
  document.querySelector(".meter").setAttribute("aria-valuenow", String(data.percent || 0));
  $("choose").disabled = !!data.busy;
  $("changeOut").disabled = !!data.busy;
  $("out").disabled = !!data.busy;
  $("npc").disabled = !!data.busy;
  $("openConnection").disabled = !!data.busy;
  $("fill").style.width = (data.percent || 0) + "%";
  $("percent").textContent = (data.percent || 0) + "%";
  $("count").textContent = String((data.stats || {}).scanned || 0);
  $("groups").textContent = String((data.stats || {}).groups || 0);
  $("passed").textContent = String((data.stats || {}).ai_passed || 0);
  $("issues").textContent = String((data.stats || {}).issues || 0);
  $("badge").textContent = String((data.stats || {}).issues || 0);
  $("npc").checked = data.npc !== false;
  // Keep in-progress dialog edits intact while snapshot pushes stream in
  // (e.g. the async model list arriving after fetch_models).
  if (!$("connection").open) {
    $("connUrl").value = connection.base_url || "";
    $("connModel").value = connection.model || "";
    $("connWorkers").value = connection.workers || 1;
    $("connMaxAi").value = connection.max_ai || 0;
    $("connRemember").checked = !!connection.remember_api_key;
    if (!connection.has_api_key) $("connKey").value = "";
  }
  if (Array.isArray(data.models)) { setModelOptions(data.models); openModelList(); }
  else if (Array.isArray(data.cached_models)) setModelOptions(data.cached_models);
  if (typeof data.model_status === "string") {
    $("modelStatus").textContent = data.model_status;
    $("fetchModels").disabled = data.model_status.indexOf("正在拉取") === 0;
  } else if ($("connection").open && $("fetchModels").disabled) {
    // A finished-but-statusless snapshot (e.g. an outdated fetch) must never
    // leave the button locked in "正在拉取…" forever.
    $("fetchModels").disabled = false;
    $("modelStatus").textContent = "拉取未完成，请重试。";
  }
  document.body.classList.toggle("maximized", !!data.window_maximized);
  const buttons = data.buttons || {};
  $("scan").disabled = buttons.scan === "disabled";
  $("run").disabled = buttons.run === "disabled";
  $("run").textContent = buttons.run_label || "开始汉化";
  $("reset").disabled = buttons.reset === "disabled";
  if (data.view) tab(data.view);
  if (data.toast) note(data.toast);
  // A10: close the revision editor only when the backend accepted the save.
  if (ui.pendingSave) {
    if (data.ok !== false) delete ui.editing[ui.pendingSave];
    ui.pendingSave = null;
  }
  if (ui.mode === "logs" || Array.isArray(data.records) || data.view) {
    render();
    if (!data.view) {
      content.scrollTop = followLogs ? content.scrollHeight : previousTop;
    }
  }
}
function filteredRecords() {
  const query = $("search").value.toLowerCase();
  const filter = $("filter").value;
  return ui.records.filter((record) => {
    if (ui.mode === "review" && !record.issue && record.type !== "unknown") return false;
    if (filter !== "all" && record.type !== filter) return false;
    if (!query) return true;
    const blob = [record.source, record.target, record.why, record.role, record.path].join(" ").toLowerCase();
    return blob.includes(query);
  });
}
function renderLogs() {
  const query = $("search").value.toLowerCase();
  const level = $("logLevel").value;
  const logs = ui.logs.filter((item) => (level === "all" || logLevel(item) === level)
    && (!query || String(item.text || item).toLowerCase().includes(query)));
  $("recordCount").textContent = "显示 " + logs.length + " / " + ui.logs.length + " 条日志";
  if (!logs.length) {
    $("content").innerHTML = ui.logs.length
      ? "<div class=\"empty\">没有匹配的日志，请调整关键词或级别。</div>"
      : "<div class=\"empty\">选择资源后，这里显示任务阶段与处理结果。<br>日志可以搜索，任务可以停止与继续。</div>";
    return;
  }
  $("content").innerHTML = logs.map((item) => "<div class=\"entry\"><time>" + esc(item.time || "")
    + "</time><span class=\"log-level " + logLevel(item) + "\">" + (logLevel(item) === "warning" ? "WARN" : logLevel(item).toUpperCase())
    + "</span><span>" + esc(item.text || item) + "</span></div>").join("");
}
function logLevel(item) {
  const level = String(item.level || "info").toLowerCase();
  return level === "warn" || level === "warning" ? "warning" : level === "error" ? "error" : "info";
}
function renderCards() {
  const list = filteredRecords();
  $("recordCount").textContent = "显示 " + Math.min(list.length, ui.loadedCount) + " / " + list.length + " 条记录";
  if (!list.length) {
    $("content").innerHTML = ui.records.length
      ? "<div class=\"empty\">没有符合筛选条件的内容。</div>"
      : "<div class=\"empty\">完成扫描后，这里会展示可翻译内容及保留原因。</div>";
    return;
  }
  const visible = list.slice(0, ui.loadedCount);
  $("content").innerHTML = visible.map((record) => {
    const open = ui.openIds.has(record.id);
    const draft = ui.drafts[record.id] || record.target || "";
    const editing = !!ui.editing[record.id];
    const translation = record.preference_keep || record.type !== "display"
      ? "保留原文"
      : (draft ? (ui.drafts[record.id] ? "手工修订：" : "译文：") + draft : "等待翻译");
    return "<article class=\"item\" data-id=\"" + esc(record.id) + "\"><header><b>" + esc(record.role) + "</b><span class=\"tag " + (record.type !== "display" || record.preference_keep ? "lock" : "") + "\">"
      + esc(record.tag) + "</span></header><div class=\"source\">" + esc(record.source) + "</div><div class=\"translation\">"
      + esc(translation) + "</div><small>" + esc(record.why) + "</small><details data-id=\"" + esc(record.id) + "\"" + (open ? " open" : "") + "><summary>查看位置与处理依据</summary><small>"
      + esc(record.full_path || record.path) + "<br>" + esc(record.locator) + "</small></details>"
      + (record.editable ? "<button type=\"button\" data-edit=\"" + esc(record.id) + "\">修订译文</button>" : "")
      + (editing ? "<div><textarea data-draft=\"" + esc(record.id) + "\" aria-label=\"修订译文\">" + esc(draft) + "</textarea><button type=\"button\" data-save=\"" + esc(record.id) + "\">保存修订</button></div>" : "")
      + "</article>";
  }).join("");
}
function render() {
  $("logLevel").hidden = ui.mode !== "logs";
  $("logColumns").hidden = ui.mode !== "logs";
  $("followLabel").hidden = ui.mode !== "logs";
  $("filter").hidden = ui.mode === "logs";
  if (ui.mode === "logs") renderLogs();
  else renderCards();
}
function maybeLoadMore() {
  const box = $("content");
  if (ui.mode === "logs") return;
  if (box.scrollTop + box.clientHeight >= box.scrollHeight - 40) {
    const total = filteredRecords().length;
    if (ui.loadedCount < total) {
      ui.loadedCount += PAGE_SIZE;
      const top = box.scrollTop;
      render();
      box.scrollTop = top;
    }
  }
}

document.addEventListener("click", (event) => {
  const tabButton = event.target.closest("nav button[data-tab]");
  if (tabButton) { tab(tabButton.dataset.tab); return; }
  if (event.target.closest("article .source, article .translation")) return;
  if (event.target.id === "winMin") { call("window_minimize", {}); return; }
  if (event.target.id === "winMax") { call("window_toggle_maximize", {}); return; }
  if (event.target.id === "winClose") { call("window_close", {}); return; }
  if (event.target.id === "openConnection") { $("connection").showModal(); $("connUrl").focus(); return; }
  if (event.target.id === "fetchModels") {
    $("fetchModels").disabled = true;
    $("modelStatus").textContent = "正在拉取可用模型…";
    call("fetch_models", {base_url: $("connUrl").value, api_key: $("connKey").value});
    return;
  }
  if (event.target.id === "closeConnection") {
    $("connection").close();
    call("apply_connection", {base_url: $("connUrl").value, model: $("connModel").value, workers: Number($("connWorkers").value), max_ai: Number($("connMaxAi").value), api_key: $("connKey").value, remember_api_key: $("connRemember").checked});
    // A05: a typed key must not linger in the field after it was applied.
    $("connKey").value = "";
    return;
  }
  if (event.target.id === "openHistory") { renderHistory(); $("historyDialog").showModal(); return; }
  if (event.target.id === "closeHistory") { $("historyDialog").close(); $("openHistory").focus(); return; }
  if (event.target.id === "openSummary" || (event.target.id === "run" && ui.snapshot && ui.snapshot.state === "complete")) {
    renderSummary(); $("summary").showModal(); return;
  }
  if (event.target.id === "closeSummary") { $("summary").close(); $("openSummary").focus(); return; }
  if (event.target.id === "choose") { call("choose_resource", {}); if (ui.fixture) window.applyFixture && window.applyFixture("loaded"); return; }
  if (event.target.id === "changeOut") { call("choose_output", {current: $("out").value}); $("out").focus(); return; }
  if (event.target.id === "scan") { call("scan", {}); if (ui.fixture) window.applyFixture && window.applyFixture("scanned"); return; }
  if (event.target.id === "run") {
    const label = $("run").textContent;
    if (label === "查看质量摘要") { renderSummary(); $("summary").showModal(); return; }
    if (label === "停止汉化") call("stop", {});
    else if (label === "继续汉化") call("start", {resume: true});
    else if (label === "重新审计并打包") call("reaudit", {});
    else call("start", {});
    return;
  }
  if (event.target.id === "reset") { call("reset", {}); if (ui.fixture) window.applyFixture && window.applyFixture("initial"); return; }
  if (event.target.id === "copyDraft") { call("save_draft", {}); return; }
  if (event.target.id === "makeCopy") { call("export_copy", {}); $("summary").close(); return; }
  if (event.target.id === "openReport") { call("open_report", {}); return; }
  if (event.target.id === "openWorkspace") { call("open_workspace", {}); return; }
  if (event.target.id === "openBlocked") { $("filter").value = "unknown"; $("summary").close(); tab("review"); return; }
  const restore = event.target.getAttribute("data-restore");
  if (restore) { call("restore_task", {index: Number(restore)}); $("historyDialog").close(); return; }
  const editId = event.target.getAttribute("data-edit");
  if (editId) { ui.editing[editId] = true; render(); const area = document.querySelector("textarea[data-draft=\"" + editId + "\"]"); if (area) area.focus(); return; }
  const saveId = event.target.getAttribute("data-save");
  if (saveId) {
    const area = document.querySelector("textarea[data-draft=\"" + saveId + "\"]");
    const value = area ? area.value : "";
    ui.drafts[saveId] = value;
    ui.pendingSave = saveId;
    call("save_revision", {id: saveId, target: value});
    render();
  }
});
document.addEventListener("toggle", (event) => {
  const id = event.target.getAttribute("data-id");
  if (!id || event.target.tagName !== "DETAILS") return;
  if (event.target.open) ui.openIds.add(id); else ui.openIds.delete(id);
}, true);
document.addEventListener("input", (event) => {
  if (event.target.id === "search") { ui.loadedCount = PAGE_SIZE; render(); return; }
  if (event.target.dataset && event.target.dataset.draft) ui.drafts[event.target.dataset.draft] = event.target.value;
});
$("filter").addEventListener("change", () => { ui.loadedCount = PAGE_SIZE; render(); });
$("logLevel").addEventListener("change", render);
$("followLogs").addEventListener("change", () => {
  if ($("followLogs").checked && ui.mode === "logs") $("content").scrollTop = $("content").scrollHeight;
});
document.querySelector("nav").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const names = ["logs", "preview", "review"];
  const index = names.indexOf(ui.mode);
  const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
  tab(names[next]);
  document.querySelector("nav button.active").focus();
});
$("npc").addEventListener("change", () => call("set_npc", {enabled: $("npc").checked}));
$("content").addEventListener("scroll", () => { ui.scroll[ui.mode] = $("content").scrollTop; maybeLoadMore(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeModelList();
    ["connection", "historyDialog", "summary"].forEach((id) => { if ($(id).open) $(id).close(); });
  }
});
$("out").addEventListener("change", () => call("set_output_text", {path: $("out").value}));

// Model list popup: full fetched list, themed, filterable as you type.
$("modelToggle").addEventListener("click", () => { if ($("modelList").hidden) openModelList(); else closeModelList(); });
$("connModel").addEventListener("focus", openModelList);
$("connModel").addEventListener("input", () => { if (!$("modelList").hidden) renderModelList(); });
$("modelList").addEventListener("mousedown", (event) => {
  const item = event.target.closest("[data-model]");
  if (!item) return;
  event.preventDefault();
  $("connModel").value = item.dataset.model;
  closeModelList();
});
document.addEventListener("mousedown", (event) => {
  if (!event.target.closest(".model-field")) closeModelList();
}, true);

// Window drag/close are owned by Qt (event filter on the view); the edge
// strips still come through here.
document.addEventListener("mousedown", (event) => {
  const strip = event.target.closest(".rz");
  if (strip && event.button === 0) { call("window_resize", {edge: strip.dataset.edge}); }
});

function renderHistory() {
  const items = (ui.snapshot && ui.snapshot.history) || [];
  if (!items.length) { $("historyText").textContent = "还没有任务。请先选择资源。"; return; }
  $("historyText").innerHTML = items.map((item, index) => "<p>" + esc(item.name || item.output || "") + " · " + esc(item.state || "")
    + (item.missing ? " · " + esc(item.reason) : " <button type=\"button\" data-restore=\"" + index + "\">打开</button>") + "</p>").join("");
}
function renderSummary() {
  const summary = (ui.snapshot && ui.snapshot.summary) || {};
  const recovery = (ui.snapshot && ui.snapshot.recovery) || {};
  const blocked = ui.snapshot && (ui.snapshot.state === "blocked" || recovery.available);
  $("summaryText").innerHTML = (summary.html || "<p>任务尚未完成。完成后可查看统计与保留原因。</p>")
    + (blocked ? "<p>译文已暂存；发布被阻止。</p><p><button type=\"button\" id=\"openReport\">打开报告</button> <button type=\"button\" id=\"openWorkspace\">打开工作目录</button> <button type=\"button\" id=\"openBlocked\">查看阻断项</button></p>" : "")
    + (summary.ready ? "<p><button type=\"button\" id=\"copyDraft\">保存草稿</button> <button type=\"button\" id=\"makeCopy\">生成修订副本</button></p>" : "");
}

window.applySnapshot = applySnapshot;
window.applyFixture = function applyFixture(name) {
  ui.fixture = true;
  ui.records = [];
  ui.logs = [];
  ui.openIds.clear();
  ui.drafts = {};
  ui.editing = {};
  const demo = [
    {id:"1",source:"Flower Merchant",target:"花卉商人",type:"display",role:"商人头顶标签",why:"示例：没有发现名称匹配引用。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"2",source:"Welcome to this guide, dear cavers!",target:"欢迎阅读这份指南，亲爱的洞穴探险者！",type:"display",role:"书页正文",why:"书页按页分组，保持阅读顺序。",path:"书本 / pages / 0",full_path:"书本 / pages / 0",locator:"pages/0",tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"3",source:"Guard Captain",target:"Guard Captain",type:"lock",role:"实体名称",why:"示例：@e[name=\"Guard Captain\"] 引用了该名称，必须保持原样。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"机制锁定",editable:false,issue:false,preference_keep:false},
    {id:"4",source:"Welcome!! Please read this ^^",target:"欢迎！！请阅读这里 ^^",type:"display",role:"浮动文字",why:"TextDisplay 可见内容。",path:"实体 / text",full_path:"实体 / text",locator:"text",tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"5",source:"Trader_01",target:"Trader_01",type:"unknown",role:"未知身份名",why:"含数字与下划线，疑似标识符；保留原文，等待确认。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"保留待确认",editable:false,issue:true,preference_keep:false}
  ];
  $("connection").close(); $("historyDialog").close(); $("summary").close();
  $("npc").checked = true; $("prefs").open = false; $("search").value = ""; $("filter").value = "all";
  $("scan").disabled = true; $("run").disabled = true; $("run").textContent = "开始汉化";
  $("filename").textContent = "尚未选择输入资源"; $("out").value = ""; $("status").textContent = "等待开始";
  $("desc").textContent = "选择输入资源后即可开始"; $("count").textContent = "0"; $("groups").textContent = "0";
  $("passed").textContent = "0"; $("issues").textContent = "0"; $("badge").textContent = "0"; $("fill").style.width = "0%"; $("percent").textContent = "0%";
  ui.mode = "logs";
  if (name === "initial") { render(); return name; }
  $("filename").textContent = "The Ruralist · 回归示例"; $("out").value = "data/output/Ruralist_zh_cn.zip"; $("scan").disabled = false; $("run").disabled = false;
  if (name === "loaded") { ui.logs = [{time:"", text:"已载入固定对照数据。"}]; tab("logs"); return name; }
  ui.records = demo; $("count").textContent = "424"; $("status").textContent = "扫描完成"; $("desc").textContent = "展示 5 条代表性样例，包含商人标签与机制锁定。";
  if (name === "scanned") { tab("preview"); return name; }
  if (name === "card-expanded-edit") { ui.openIds.add("1"); ui.editing["1"] = true; ui.drafts["1"] = "花卉商人"; tab("preview"); return name; }
  if (name === "prefs-open") { $("prefs").open = true; tab("preview"); return name; }
  if (name === "connection-dialog") { $("connection").showModal(); return name; }
  if (name === "history-dialog") { $("historyText").textContent = "Ruralist · 当前对照任务，可以回到主界面继续。"; $("historyDialog").showModal(); return name; }
  $("fill").style.width = "100%"; $("percent").textContent = "100%"; $("groups").textContent = "118"; $("passed").textContent = "117"; $("issues").textContent = "1"; $("badge").textContent = "1";
  $("status").textContent = "汉化完成"; $("desc").textContent = "审计通过，有 1 条身份名保留原文；可以查看质量摘要。"; $("run").textContent = "查看质量摘要";
  if (name === "summary-dialog") { $("summaryText").innerHTML = "<p>对照数据：审计通过 · 118 组已处理</p>"; $("summary").showModal(); tab("preview"); return name; }
  if (name === "complete") { tab("preview"); return name; }
  if (name === "filter-lock") { $("filter").value = "lock"; tab("preview"); return name; }
  return "unknown";
};
window.collectMetrics = function collectMetrics() {
  const app = document.querySelector(".app");
  const layout = document.querySelector(".layout");
  const panel = document.querySelector(".panel");
  const title = document.querySelector(".title");
  return {
    href: location.href,
    viewport: {innerWidth, innerHeight},
    devicePixelRatio,
    badge: (document.querySelector(".badge") || {}).textContent || "",
    choose: $("choose").textContent,
    reset: $("reset").textContent,
    filename: $("filename").textContent,
    status: $("status").textContent,
    run: $("run").textContent,
    filter: $("filter").value,
    cards: [...document.querySelectorAll("article.item header b")].map((el) => el.textContent),
    prefsOpen: $("prefs").open,
    dialogs: {connection: $("connection").open, history: $("historyDialog").open, summary: $("summary").open},
    textarea: !!document.querySelector("article.item textarea"),
    detailsOpen: !!document.querySelector("article.item details[open]"),
    fonts: {family: getComputedStyle(document.documentElement).fontFamily, title: title && getComputedStyle(title).fontSize, h2: getComputedStyle(document.querySelector(".panel h2")).fontSize, h3: getComputedStyle(document.querySelector(".panel h3")).fontSize},
    app: {maxWidth: getComputedStyle(app).maxWidth, padding: getComputedStyle(app).padding, background: getComputedStyle(document.body).backgroundColor},
    layout: {columns: getComputedStyle(layout).gridTemplateColumns, gap: getComputedStyle(layout).gap},
    panel: {padding: getComputedStyle(panel).padding, radius: getComputedStyle(panel).borderRadius, borderWidth: getComputedStyle(panel).borderTopWidth},
    pageOverflow: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1 || document.body.scrollHeight > document.body.clientHeight + 1,
    pageOverflowY: getComputedStyle(document.documentElement).overflowY,
    contentOverflowY: getComputedStyle($("content")).overflowY,
    contentMaxHeight: getComputedStyle($("content")).maxHeight,
    bottomVisible: (function () {
      const box = document.querySelector(".bottom-actions");
      if (!box) return false;
      const rect = box.getBoundingClientRect();
      return rect.bottom <= innerHeight + 1 && rect.top >= 0;
    })()
  };
};
window.__toggleCard = function (id, times) {
  const details = document.querySelector("article.item[data-id=\"" + id + "\"] details");
  if (!details) return 0;
  let count = 0;
  for (let index = 0; index < times; index += 1) { details.open = !details.open; count += 1; }
  return count;
};

function connectBridge() {
  if (typeof qt === "undefined" || !qt.webChannelTransport) {
    document.title = "SHELL_READY";
    return;
  }
  new QWebChannel(qt.webChannelTransport, (channel) => {
    ui.backend = channel.objects.backend;
    document.title = "SHELL_READY";
    call("hello", {});
  });
}

render();
connectBridge();
