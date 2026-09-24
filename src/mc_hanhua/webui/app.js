const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 80;
const ACTIVE_STATES = ["idle", "ready", "scanned", "scanning", "translating", "stopping"];
// Default API address shipped for each provider mode; used only to decide
// whether a provider switch may rewrite the address field.
const CHAT_DEFAULT_URL = "https://token-plan-cn.xiaomimimo.com/v1";
const QWEN_MT_DEFAULT_URL = "https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/compatible-mode/v1";
// 日志级别徽标：三个配色都定义在 design-base.css（.level.info|warn|error）。
const LOG_BADGES = {info: "INFO", warning: "WARN", error: "ERROR"};
const ui = {
  mode: "logs",
  records: [],
  runtimeProblems: [],
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
  lastIssues: -1,
  queueLength: 0,
  logRevision: 0,
  logIds: new Set(),
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
    : "<div class=\"none\">没有匹配的模型</div>";
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
// Qwen-MT reads its instructions from the API's translation_options, not from
// a prompt, so the two modes differ in more than a model name. The hint states
// what actually changes; the safety validation gate is identical.
function renderProviderHint() {
  const qwenMt = $("connProvider").value === "qwen-mt";
  // Name what actually differs between the modes. Terminology consistency and
  // the safety gate are identical in both: canonical reuse, glossary terms and
  // official-term validation all run outside the provider.
  $("providerHint").textContent = qwenMt
    ? "翻译专用模型：整句直译，术语走原生术语表；不做二次审校，疑难排版退回逐句直译并由校验门把关。"
    : "对话模型：批量分组翻译 + 审校，支持复杂排版重排。";
}
function applyProviderDefaults() {
  renderProviderHint();
  // Switching the model type must leave a working pair behind: an MT provider
  // pointed at a chat model name (or the reverse) fails every request. A model
  // name that already belongs to the selected mode is kept as typed.
  const model = $("connModel");
  if ($("connProvider").value === "qwen-mt") {
    if (!model.value.trim().toLowerCase().startsWith("qwen-mt")) model.value = "qwen-mt-flash";
    // Only replace the address the app shipped with; an address the user typed
    // for their own workspace is never overwritten.
    const current = $("connUrl").value.trim();
    if (!current || current === CHAT_DEFAULT_URL) $("connUrl").value = QWEN_MT_DEFAULT_URL;
    return;
  }
  if (model.value.trim().toLowerCase().startsWith("qwen-mt")) model.value = "mimo-v2.5";
}
// The left panel shows one view at a time: the current task, or the queue.
// Switching is purely presentational — a drop still works from either view.
function setTaskView(name) {
  const queued = name === "queue";
  $("viewSingle").style.display = queued ? "none" : "flex";
  $("viewQueue").style.display = queued ? "flex" : "none";
  $("modeSingle").classList.toggle("active", !queued);
  $("modeQueue").classList.toggle("active", queued);
  $("modeSingle").setAttribute("aria-selected", queued ? "false" : "true");
  $("modeQueue").setAttribute("aria-selected", queued ? "true" : "false");
}
// Called from the desktop layer: the page never sees drag events of its own,
// because the window (not the web view) accepts the drop.
function applyDropState(state, detail) {
  const drop = $("dropZone");
  drop.classList.toggle("over", state === "over");
  drop.classList.toggle("queued", state === "queue");
  const hint = $("dropHint");
  hint.textContent = detail || "";
  hint.hidden = !detail;
}
function note(s) {
  const box = $("toast");
  box.textContent = s;
  box.classList.add("show");
  clearTimeout(note._timer);
  note._timer = setTimeout(() => box.classList.remove("show"), 2300);
}
function showBanner(notify) {
  const kind = String(notify.kind || "complete");
  $("bannerTitle").textContent = notify.title || "任务状态更新";
  $("bannerBody").textContent = notify.body || "";
  $("banner").className = "banner " + kind;
  $("banner").hidden = false;
}
function hideBanner() {
  $("banner").hidden = true;
}
function pulseBadge() {
  const badge = $("badge");
  badge.classList.remove("pulse");
  void badge.offsetWidth;
  badge.classList.add("pulse");
  setTimeout(() => badge.classList.remove("pulse"), 1300);
}
function flashElement(element) {
  if (!element) return;
  element.classList.remove("flash");
  void element.offsetWidth;
  element.classList.add("flash");
  element.scrollIntoView({block: "center"});
  setTimeout(() => element.classList.remove("flash"), 1700);
}
function jumpToLog(ref) {
  tab("logs");
  requestAnimationFrame(() => {
    const entry = document.querySelector(".log-row[data-ref=\"" + CSS.escape(ref) + "\"]");
    if (entry) flashElement(entry);
    else note("对应日志已滚出缓冲区，可按关键词搜索。");
  });
}
// 队列行的第二行是输出路径（设计稿把它做成等宽小字），第一行是任务名。
function renderQueue(data) {
  const queue = Array.isArray(data.queue) ? data.queue : [];
  const session = data.session || {};
  // The queue lives behind its own view: the badge carries the count so the
  // panel does not have to be open to see that work is waiting.
  $("queueBadge").textContent = String(queue.length);
  $("queueBadge").hidden = queue.length === 0;
  ui.queueLength = queue.length;
  $("queueList").innerHTML = queue.length
    ? queue.map((item, index) => "<li title=\"" + esc(item.input || "") + "\">"
      + "<span class=\"q-index\">" + (index + 1) + "</span>"
      + "<span class=\"q-text\"><span class=\"q-name\">" + esc(item.name || "") + "</span>"
      + "<span class=\"q-path\">" + esc(item.output ? "输出 " + item.output : item.input || "") + "</span></span>"
      // 队首就是「自动继续」会启动的那一个：不标出来，用户看不出队列从哪儿开始。
      // 标记放在 li 的弹性行里，不能塞进 .q-name（那里有 ellipsis，会被裁掉）。
      + (index === 0 ? "<span class=\"q-next\">下一项</span>" : "")
      + "<button type=\"button\" class=\"q-remove\" data-qremove=\"" + index + "\" title=\"移除 " + esc(item.name || "") + "\" aria-label=\"移除 " + esc(item.name || "") + "\">✕</button></li>").join("")
    : "<li class=\"queue-empty\"><span class=\"line\">还没有排队任务</span>"
      + "<span class=\"sub\">把文件拖到左栏：第一个设为当前输入，之后拖入的依次排队</span></li>";
  const done = Number(session.tasks || 0);
  // 「开始下一个」是完成态下唯一的队列启动入口：完成态的运行按钮走摘要/队列动作，
  // 而自动继续只由完成回调触发，关掉它时没有别的入口能把队列推起来。
  // 可用性由后端给出（`buttons.queue_start`）：当前输入还没开始处理时必须禁用，
  // 否则队首会顶掉尚未启动的首包，而那个包不会回到队列。
  const startButton = $("startQueue");
  if (startButton) {
    const allowed = ((data.buttons || {}).queue_start || "disabled") === "enabled";
    startButton.disabled = !allowed || queue.length === 0;
    startButton.title = queue.length
      ? (allowed
          ? "开始处理队列中的第一个任务：" + (queue[0].name || "")
          : "当前输入还没开始处理，队列不会跳过它")
      : "队列为空";
  }
  $("queueSummary").textContent = "队列中共有 " + queue.length + " 个任务" + (done ? " · 已完成 " + done + " 个" : "");
  $("queueSummary").title = queue.map((item) => item.name || "").join("，");
  if (session.tasks) {
    const totals = ["耗时 " + (session.elapsed_ms ? Math.round(session.elapsed_ms / 1000) : 0) + " 秒"];
    if (session.prompt_tokens || session.completion_tokens) totals.push("Token " + Number((session.prompt_tokens || 0) + (session.completion_tokens || 0)).toLocaleString());
    if (session.cost) totals.push("预计花费 " + Number(session.cost).toFixed(2) + " 元");
    $("queueSummary").title = ($("queueSummary").title ? $("queueSummary").title + "，" : "") + totals.join("，");
  }
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
  document.querySelectorAll(".tabs button[data-tab]").forEach((button) => {
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
  // 每次请求都带上「已收到的日志游标」：后端只回新增的日志行，快照体积从
  // 每帧 ~243 KB 降到几百字节（见 controller.logs_since）。
  const request = JSON.stringify(Object.assign({command, log_cursor: ui.logRevision}, payload || {}));
  ui.backend.call(request, (reply) => {
    if (typeof reply === "string" && reply) applySnapshot(JSON.parse(reply));
  });
}
// 指标行：设计稿把它做成 2×2 的小格（标签在上、数值在下）。后端给的是三段
// 文案（耗时/用量/花费），用量里的「输入 …，输出 …」拆成两格正好四格。
// 指标行是**单行文字**（2026-09-21 用户反馈：2×2 格子装三个数字又高又空）。
// 输入/输出合并成一条，币种符号跟数字一起写，保证一行放得下、不折行——折行会
// 把执行卡撑高，就回到旧问题了。
function metricParts(stats) {
  const parts = [];
  if (stats.elapsed_text) parts.push({label: "耗时", value: String(stats.elapsed_text)});
  if (stats.usage_text) {
    const segs = String(stats.usage_text).split(/[，,]/).map((s) => s.trim()).filter(Boolean);
    if (segs.length === 2) {
      parts.push({label: "Token", value: segs[0].replace(/^输入\s*/, "") + " / " + segs[1].replace(/^输出\s*/, "")});
    } else {
      parts.push({label: "Token", value: segs.join(" · ")});
    }
  }
  if (stats.cost_text) parts.push({label: "费用", value: String(stats.cost_text).replace(/^预计花费\s*/, "")});
  return parts;
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
  if (Array.isArray(data.runtime_problems)) ui.runtimeProblems = data.runtime_problems;
  // 日志按 id 去重后追加：后端可能重复送同一批行（进度推送与命令回包是两条
  // 路径，Qt 侧只负责裁剪体积，正确性由这里的 id 集合保证）。任务换新时游标
  // 回退，说明页面状态落后，直接清空重来。
  if (typeof data.log_revision === "number") {
    if (data.log_revision < ui.logRevision || (data.task_id && ui.taskId && data.task_id !== ui.taskId)) {
      ui.logs = [];
      ui.logIds = new Set();
    }
    if (Array.isArray(data.logs)) {
      for (const entry of data.logs) {
        const id = String(entry && entry.id || "");
        if (id && ui.logIds.has(id)) continue;
        if (id) ui.logIds.add(id);
        ui.logs.push(entry);
      }
    }
    if (ui.logs.length > 2000) {
      ui.logs = ui.logs.slice(-2000);
      ui.logIds = new Set(ui.logs.map((entry) => String(entry && entry.id || "")));
    }
    ui.logRevision = data.log_revision;
  } else if (Array.isArray(data.logs)) {
    ui.logs = data.logs;
    ui.logIds = new Set(ui.logs.map((entry) => String(entry && entry.id || "")));
  }
  $("filename").textContent = data.input_name || "拖拽文件至此处，或点击选择";
  $("filename").title = data.input_detail || data.input_name || "";
  $("inputDetail").textContent = data.input_name ? "资源已选择 · 原始文件保持不变" : "支持 zip / jar / mrpack，以及地图或整合包目录";
  $("inputDetail").title = data.input_detail || $("inputDetail").textContent;
  $("out").value = data.output_path || "";
  $("status").textContent = data.status_title || "等待开始";
  $("desc").textContent = data.busy ? "" : (data.status_desc || "");
  $("desc").title = data.status_desc || "";
  $("taskState").textContent = data.status_title || "等待开始";
  $("taskState").title = data.stage_text || data.status_desc || "";
  const connection = data.connection || {};
  $("connectionState").textContent = connection.has_api_key ? "API 已配置 · " + (connection.model || "未指定模型") : "本地词库模式";
  $("connectionState").title = connection.has_api_key ? "配置已保存；实际请求状态请查看处理日志。" : "未使用 API Key";
  document.querySelector(".progress").setAttribute("aria-valuenow", String(data.percent || 0));
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
  const issues = Number((data.stats || {}).issues || 0);
  $("issues").textContent = String(issues);
  // 设计稿把计数写在页签标题里（「问题审阅 (0)」），因此徽标连同括号一起更新。
  $("badge").textContent = "(" + issues + ")";
  if (ui.lastIssues >= 0 && issues > ui.lastIssues) pulseBadge();
  ui.lastIssues = issues;
  const stats = data.stats || {};
  const parts = metricParts(stats);
  $("metrics").innerHTML = parts.map((m) => "<span class=\"m\"><span>" + esc(m.label) + "</span><b>" + esc(m.value) + "</b></span>").join("");
  $("metrics").hidden = !parts.length;
  $("metrics").title = stats.usage_detail || "";
  renderQueue(data);
  $("autoContinue").checked = data.auto_continue !== false;
  const prefs = data.prefs || {};
  $("notifySystem").checked = prefs.notify_system !== false;
  $("notifySound").checked = !!prefs.notify_sound;
  $("npc").checked = data.npc !== false;
  // Keep in-progress dialog edits intact while snapshot pushes stream in
  // (e.g. the async model list arriving after fetch_models).
  if (!$("connection").open) {
    $("connUrl").value = connection.base_url || "";
    $("connModel").value = connection.model || "";
    $("connProvider").value = connection.provider === "qwen-mt" ? "qwen-mt" : "chat";
    renderProviderHint();
    $("connWorkers").value = connection.workers || 1;
    $("connMaxAi").value = connection.max_ai || 0;
    $("connRemember").checked = !!connection.remember_api_key;
    const price = connection.price || {};
    $("connPriceIn").value = price.input_per_m != null ? price.input_per_m : "";
    $("connPriceOut").value = price.output_per_m != null ? price.output_per_m : "";
    $("connCurrency").value = price.currency || "CNY";
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
  $("run").classList.toggle("primary", buttons.run !== "stop");
  if (data.view) tab(data.view);
  // Terminal outcomes never raise an in-app banner: the status line, the log,
  // the problem review and the Windows notification all carry the same news,
  // so a page-level banner only repeats it (2026-09-21 feedback). The banner
  // element stays for non-terminal messages and is hidden here.
  if (data.notify || ACTIVE_STATES.includes(data.state)) hideBanner();
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
  // Review mode merges scan/report records with the live runtime problem
  // feed so validation failures and service errors show up while running.
  const base = ui.mode === "review" ? ui.records.concat(ui.runtimeProblems || []) : ui.records;
  return base.filter((record) => {
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
  $("content").innerHTML = "<div class=\"records-head\"><span>时间</span><span>级别</span><span>处理内容</span></div>"
    + logs.map((item) => {
      const level = logLevel(item);
      return "<div class=\"log-row\" data-log-id=\"" + esc(item.id || "") + "\""
        + (item.ref ? " data-ref=\"" + esc(item.ref) + "\"" : "") + "><time>" + esc(item.time || "")
        + "</time><span><span class=\"level " + level + "\">" + LOG_BADGES[level] + "</span></span>"
        + "<span class=\"text\">" + esc(item.text || item) + "</span></div>";
    }).join("");
}
function logLevel(item) {
  const level = String(item.level || "info").toLowerCase();
  return level === "warn" || level === "warning" ? "warn" : level === "error" ? "error" : "info";
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
    if (record.record_kind === "runtime") {
      return "<article class=\"rec runtime\" data-record-id=\"" + esc(record.id) + "\" style=\"border-left-color:var(--danger-line)\">"
        + "<div class=\"rec-head\"><span class=\"rec-role\">" + esc(record.role) + "</span>"
        + "<span class=\"tag fail\">" + esc(record.tag) + "</span></div>"
        + "<div class=\"rec-text\">" + esc(record.source) + "</div>"
        + "<div class=\"rec-why\">" + esc(record.why) + (record.time ? " · " + esc(record.time) : "") + "</div>"
        + "<div class=\"rec-actions\"><button type=\"button\" class=\"jump\" data-logjump=\"" + esc(record.id) + "\">查看日志</button></div></article>";
    }
    const open = ui.openIds.has(record.id);
    const draft = ui.drafts[record.id] || record.target || "";
    const editing = !!ui.editing[record.id];
    const kept = record.preference_keep || record.type !== "display";
    const tagClass = kept ? "tag keep" : "tag";
    const translation = kept
      ? "<div class=\"rec-trans pending\"><span class=\"k\">译文</span><span class=\"v\">保留原文</span></div>"
      : (draft
          ? "<div class=\"rec-trans\"><span class=\"k\">译文</span><span class=\"v\">" + esc(draft) + "</span></div>"
          : "<div class=\"rec-trans pending\"><span class=\"k\">译文</span><span class=\"v\">等待翻译</span></div>");
    return "<article class=\"rec\" data-record-id=\"" + esc(record.id) + "\">"
      + "<div class=\"rec-head\"><span class=\"rec-role\">" + esc(record.role) + "</span>"
      + "<span class=\"" + tagClass + "\">" + esc(record.tag) + "</span></div>"
      + "<div class=\"rec-text\">" + esc(record.source) + "</div>"
      + translation
      + "<div class=\"rec-why\">" + esc(record.why) + "</div>"
      + "<details class=\"rec-more\" data-id=\"" + esc(record.id) + "\"" + (open ? " open" : "") + ">"
      + "<summary>查看位置与处理依据</summary><div class=\"rec-meta\">"
      + "<div class=\"row\"><span class=\"k\">文件</span><span class=\"v\">" + esc(record.full_path || record.path) + "</span></div>"
      + "<div class=\"row\"><span class=\"k\">定位</span><span class=\"v\">" + esc(record.locator) + "</span></div>"
      + "</div></details>"
      + (record.editable
          ? (editing
              ? "<div class=\"rec-edit\"><textarea data-draft=\"" + esc(record.id) + "\" aria-label=\"修订译文\">" + esc(draft) + "</textarea>"
                + "<div class=\"rec-actions\"><button type=\"button\" class=\"primary\" data-save=\"" + esc(record.id) + "\">保存修订</button>"
                + "<button type=\"button\" data-cancel=\"" + esc(record.id) + "\">取消</button></div></div>"
              : "<div class=\"rec-actions\"><button type=\"button\" data-edit=\"" + esc(record.id) + "\">修订译文</button></div>")
          : "")
      + "</article>";
  }).join("");
}
function render() {
  $("logLevel").hidden = ui.mode !== "logs";
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
  const tabButton = event.target.closest(".tabs button[data-tab]");
  if (tabButton) { tab(tabButton.dataset.tab); return; }
  if (event.target.closest("article .rec-text, article .rec-trans")) return;
  // 标题栏按钮：图标是 <svg>/<path>，点击时 event.target 可能是它们而不是
  // <button> 本身，所以按 closest 判定，不能比较 id——否则只有恰好点在
  // 按钮留白上才有反应（"点好几次才生效"）。
  const winButton = event.target.closest("#winMin, #winMax, #winClose");
  if (winButton) {
    if (winButton.id === "winMin") call("window_minimize", {});
    else if (winButton.id === "winMax") call("window_toggle_maximize", {});
    else call("window_close", {});
    return;
  }
  if (event.target.id === "modeSingle") { setTaskView("single"); return; }
  if (event.target.id === "modeQueue") { setTaskView("queue"); return; }
  if (event.target.id === "openConnection") { $("connection").showModal(); renderProviderHint(); $("connUrl").focus(); return; }
  if (event.target.id === "fetchModels") {
    $("fetchModels").disabled = true;
    $("modelStatus").textContent = "正在拉取可用模型…";
    call("fetch_models", {base_url: $("connUrl").value, api_key: $("connKey").value});
    return;
  }
  if (event.target.id === "closeConnection") {
    $("connection").close();
    call("apply_connection", {base_url: $("connUrl").value, model: $("connModel").value, provider: $("connProvider").value, workers: Number($("connWorkers").value), max_ai: Number($("connMaxAi").value), api_key: $("connKey").value, remember_api_key: $("connRemember").checked, price_input: $("connPriceIn").value, price_output: $("connPriceOut").value, price_currency: $("connCurrency").value});
    // A05: a typed key must not linger in the field after it was applied.
    $("connKey").value = "";
    return;
  }
  if (event.target.id === "openPrefs") { $("prefsDialog").showModal(); return; }
  if (event.target.id === "closePrefs") { $("prefsDialog").close(); $("openPrefs").focus(); return; }
  if (event.target.id === "openHistory") { renderHistory(); $("historyDialog").showModal(); return; }
  if (event.target.id === "closeHistory") { $("historyDialog").close(); $("openHistory").focus(); return; }
  if (event.target.id === "openSummary") {
    renderSummary(); $("summaryDialog").showModal(); return;
  }
  if (event.target.id === "closeSummary") { $("summaryDialog").close(); $("openSummary").focus(); return; }
  if (event.target.id === "choose") { call("choose_resource", {}); if (ui.fixture) window.applyFixture && window.applyFixture("loaded"); return; }
  if (event.target.id === "enqueue") {
    // The file dialog is modal, so the call returns only after the user has
    // chosen or cancelled; react to the queue having grown rather than to the
    // click, or a cancelled dialog would still switch the view.
    const before = ui.queueLength || 0;
    call("choose_enqueue", {});
    if ((ui.queueLength || 0) > before) setTaskView("queue");
    return;
  }
  if (event.target.id === "clearQueue") { call("queue_clear", {}); return; }
  if (event.target.id === "startQueue") { call("start_next", {}); return; }
  if (event.target.id === "bannerClose") { hideBanner(); return; }
  if (event.target.id === "changeOut") { call("choose_output", {current: $("out").value}); $("out").focus(); return; }
  if (event.target.id === "scan") { call("scan", {}); if (ui.fixture) window.applyFixture && window.applyFixture("scanned"); return; }
  if (event.target.id === "run") {
    // 按动作令牌分发，不按按钮文案：文案是给用户看的，改成同义说法不该改变
    // 行为；"queue" 是完成态下还有排队项时唯一的启动入口（不自动开跑）。
    const action = ((ui.snapshot && ui.snapshot.buttons) || {}).run || "enabled";
    if (action === "summary") { renderSummary(); $("summaryDialog").showModal(); return; }
    if (action === "stop") { call("stop", {}); return; }
    if (action === "continue") { call("start", {resume: true}); return; }
    if (action === "reaudit") { call("reaudit", {}); return; }
    if (action === "queue") { call("start_next", {}); return; }
    call("start", {});
    return;
  }
  if (event.target.id === "copyDraft") { call("save_draft", {}); return; }
  if (event.target.id === "makeCopy") { call("export_copy", {}); $("summaryDialog").close(); return; }
  if (event.target.id === "openReport") { call("open_report", {}); return; }
  if (event.target.id === "openWorkspace") { call("open_workspace", {}); return; }
  if (event.target.id === "openBlocked") { $("filter").value = "unknown"; $("summaryDialog").close(); tab("review"); return; }
  const restore = event.target.getAttribute("data-restore");
  if (restore) { call("restore_task", {index: Number(restore)}); $("historyDialog").close(); return; }
  const qremove = event.target.getAttribute("data-qremove");
  if (qremove !== null) { call("queue_remove", {index: Number(qremove)}); return; }
  const logJump = event.target.getAttribute("data-logjump");
  if (logJump) { jumpToLog(logJump); return; }
  const editId = event.target.getAttribute("data-edit");
  if (editId) { ui.editing[editId] = true; render(); const area = document.querySelector("textarea[data-draft=\"" + CSS.escape(editId) + "\"]"); if (area) area.focus(); return; }
  const cancelId = event.target.getAttribute("data-cancel");
  if (cancelId) { delete ui.editing[cancelId]; render(); return; }
  const saveId = event.target.getAttribute("data-save");
  if (saveId) {
    const area = document.querySelector("textarea[data-draft=\"" + CSS.escape(saveId) + "\"]");
    const value = area ? area.value : "";
    ui.drafts[saveId] = value;
    ui.pendingSave = saveId;
    call("save_revision", {id: saveId, target: value});
    render();
  }
});
// details 的原生展开发生在 click 之后，click 里读 open 拿到的是旧值；
// 用 toggle（不冒泡，需 capture）记录，且**不重渲染**——重渲染会把它关回去。
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (!details.classList || !details.classList.contains("rec-more")) return;
  const id = details.getAttribute("data-id");
  if (!id) return;
  if (details.open) ui.openIds.add(id); else ui.openIds.delete(id);
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
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const names = ["logs", "preview", "review"];
  const index = names.indexOf(ui.mode);
  const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
  tab(names[next]);
  document.querySelector(".tabs button.active").focus();
});
$("npc").addEventListener("change", () => call("set_npc", {enabled: $("npc").checked}));
$("autoContinue").addEventListener("change", () => call("set_auto_continue", {enabled: $("autoContinue").checked}));
$("notifySystem").addEventListener("change", () => call("set_notify", {system: $("notifySystem").checked}));
$("notifySound").addEventListener("change", () => call("set_notify", {sound: $("notifySound").checked}));
$("content").addEventListener("scroll", () => { ui.scroll[ui.mode] = $("content").scrollTop; maybeLoadMore(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeModelList();
    ["connection", "historyDialog", "prefsDialog", "summaryDialog"].forEach((id) => { if ($(id).open) $(id).close(); });
  }
});
$("out").addEventListener("change", () => call("set_output_text", {path: $("out").value}));

// Model list popup: full fetched list, themed, filterable as you type.
$("modelToggle").addEventListener("click", () => { if ($("modelList").hidden) openModelList(); else closeModelList(); });
$("connProvider").addEventListener("change", applyProviderDefaults);
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
  if (!event.target.closest(".model-picker") && !event.target.closest("#modelToggle")) closeModelList();
}, true);

// 页面缩放由 Qt 的 WebEngine zoomFactor 承担（等比缩放整页，而不是拉大某一块）：
// 页内不再出现 zoom 样式。Ctrl + 滚轮在 Qt 侧拦截，这里补键盘快捷键。
document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey)) return;
  const step = {"+": 0.1, "=": 0.1, "-": -0.1, "_": -0.1}[event.key];
  if (step !== undefined) { event.preventDefault(); call("window_zoom_step", {delta: step}); return; }
  if (event.key === "0") { event.preventDefault(); call("window_zoom", {factor: 1}); }
});

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
  ui.runtimeProblems = [];
  ui.logs = [];
  ui.logRevision = 0;
  ui.logIds = new Set();
  ui.openIds.clear();
  ui.drafts = {};
  ui.editing = {};
  ui.lastIssues = -1;
  $("metrics").hidden = true;
  applyDropState("idle", "");
  renderQueue({queue: [], session: {}});
  hideBanner();
  const demo = [
    {id:"1",source:"Sneak",target:"潜行",type:"display",role:"NBT 文本",why:"计分板显示名，未被命令引用。",path:"scoreboard.dat",full_path:"COCHLEA (26.3)/data/minecraft/scoreboard.dat",locator:'{"path": ["data", "Objectives", 0, "DisplayName"]}',tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"2",source:"Flower Merchant",target:"花卉商人",type:"display",role:"商人头顶标签",why:"示例：没有发现名称匹配引用。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"3",source:"Welcome to this guide, dear cavers!",target:"欢迎阅读这份指南，亲爱的洞穴探险者！",type:"display",role:"书页正文",why:"书页按页分组，保持阅读顺序。",path:"书本 / pages / 0",full_path:"书本 / pages / 0",locator:"pages/0",tag:"可翻译",editable:true,issue:false,preference_keep:false},
    {id:"4",source:"Guard Captain",target:"Guard Captain",type:"lock",role:"实体名称",why:"示例：@e[name=\"Guard Captain\"] 引用了该名称，必须保持原样。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"机制锁定",editable:false,issue:false,preference_keep:false},
    {id:"5",source:"Trader_01",target:"Trader_01",type:"unknown",role:"未知身份名",why:"含数字与下划线，疑似标识符；保留原文，等待确认。",path:"实体 / CustomName",full_path:"实体 / CustomName",locator:"CustomName",tag:"保留待确认",editable:false,issue:true,preference_keep:false}
  ];
  $("connection").close(); $("historyDialog").close(); $("summaryDialog").close(); $("prefsDialog").close();
  $("npc").checked = true; $("search").value = ""; $("filter").value = "all";
  $("scan").disabled = true; $("run").disabled = true; $("run").textContent = "开始汉化"; $("run").classList.add("primary");
  $("filename").textContent = "拖拽文件至此处，或点击选择"; $("out").value = ""; $("status").textContent = "等待开始";
  $("desc").textContent = "选择输入资源后即可开始"; $("count").textContent = "0"; $("groups").textContent = "0";
  $("passed").textContent = "0"; $("issues").textContent = "0"; $("badge").textContent = "(0)"; $("fill").style.width = "0%"; $("percent").textContent = "0%";
  ui.mode = "logs";
  if (name === "initial") { setTaskView("single"); render(); return name; }
  // 翻译中夹具：指标行可见（执行卡比空闲态高一截）——这是输出行被压住的那个
  // 状态的唯一回归入口，布局检查必须能摆出它。字段要与真实快照一致：缺字段会
  // 被 applySnapshot 的默认值覆盖，夹具就测不到真实样子。
  if (name === "translating") {
    applySnapshot({
      ok: true, task_id: "fixture-translating", state: "translating", busy: true, percent: 24,
      status_title: "翻译与审校", status_desc: "", stage_text: "翻译与审校",
      input_name: "the_colourcube_v1.0.0.zip",
      input_detail: "D:/maps/the_colourcube_v1.0.0.zip",
      output_path: "D:/maps/the_colourcube_v1.0.0_zh_cn.zip",
      stats: {scanned: 9379, groups: 13, ai_passed: 5, issues: 0, elapsed_text: "13 秒", usage_text: "输入 1543，输出 46"},
      buttons: {scan: "disabled", run: "stop", run_label: "停止汉化"},
      connection: {}, history: [], summary: {}, queue: [], auto_continue: true, prefs: {},
      runtime_problems: [], session: {}, records: [],
      logs: [{id: "fixture-1", time: "21:45:06", level: "info", text: "[24%] 正在翻译并审校文本组"}],
    });
    ui.fixture = true;
    tab("logs");
    return name;
  }
  // Queue view fixtures: the queue is a view of its own now.
  if (name === "queue-empty" || name === "queue-filled" || name === "drop-over" || name === "drop-queued") {
    $("filename").textContent = "The Ruralist · 回归示例";
    $("out").value = "data/output/Ruralist_zh_cn.zip";
    $("scan").disabled = false; $("run").disabled = false;
    if (name === "queue-empty") {
      renderQueue({queue: [], session: {}});
    } else {
      renderQueue({
        queue: [
          {name: "Ruralist_zh_cn.zip", input: "D:/in/Ruralist.zip", output: "D:/out/Ruralist_zh_cn.zip"},
          {name: "build-to-unlock_zh_cn.zip", input: "D:/in/build-to-unlock.zip", output: "D:/out/build-to-unlock_zh_cn.zip"},
          {name: "RoH-1.0-1_zh_cn.zip", input: "D:/in/RoH-1.0-1.zip", output: "D:/out/RoH-1.0-1_zh_cn.zip"},
        ],
        session: {tasks: 2, elapsed_ms: 184000},
      });
    }
    // Drop feedback belongs to the input view: that is where the drop zone and
    // its hint live, so the fixture must show it or the state is invisible.
    setTaskView(name === "drop-over" || name === "drop-queued" ? "single" : "queue");
    if (name === "drop-over") applyDropState("over", "松开后设为当前输入：Ruralist.zip");
    else if (name === "drop-queued") applyDropState("queue", "松开后加入队列：a.zip、b.zip、c.zip 等 5 个");
    tab("logs");
    return name;
  }
  $("filename").textContent = "The Ruralist · 回归示例"; $("out").value = "data/output/Ruralist_zh_cn.zip"; $("scan").disabled = false; $("run").disabled = false;
  if (name === "loaded") { ui.logs = [{time:"", text:"已载入固定对照数据。"}]; tab("logs"); return name; }
  ui.records = demo; $("count").textContent = "424"; $("status").textContent = "扫描完成"; $("desc").textContent = "展示 5 条代表性样例，包含商人标签与机制锁定。";
  if (name === "scanned") { tab("preview"); return name; }
  if (name === "card-expanded-edit") { ui.openIds.add("1"); ui.editing["1"] = true; ui.drafts["1"] = "潜行"; tab("preview"); return name; }
  if (name === "prefs-open") { $("prefsDialog").showModal(); tab("preview"); return name; }
  if (name === "connection-dialog" || name === "connection-dialog-qwen-mt") {
    $("connProvider").value = name === "connection-dialog-qwen-mt" ? "qwen-mt" : "chat";
    applyProviderDefaults();
    $("connection").showModal();
    return name;
  }
  if (name === "history-dialog") { $("historyText").textContent = "Ruralist · 当前对照任务，可以回到主界面继续。"; $("historyDialog").showModal(); return name; }
  $("fill").style.width = "100%"; $("percent").textContent = "100%"; $("groups").textContent = "118"; $("passed").textContent = "117"; $("issues").textContent = "1"; $("badge").textContent = "(1)";
  $("status").textContent = "汉化完成"; $("desc").textContent = "审计通过，有 1 条身份名保留原文；可以查看质量摘要。"; $("run").textContent = "查看质量摘要";
  if (name === "summary-dialog") { $("summaryText").innerHTML = "<p>对照数据：审计通过 · 118 组已处理</p>"; $("summaryDialog").showModal(); tab("preview"); return name; }
  if (name === "complete") { tab("preview"); return name; }
  if (name === "filter-lock") { $("filter").value = "lock"; tab("preview"); return name; }
  // Terminal-state fixtures: which kinds still raise the in-app banner.
  if (name === "terminal-complete" || name === "terminal-failed" || name === "terminal-blocked" || name === "terminal-paused") {
    const kind = name.slice("terminal-".length);
    applySnapshot({state: kind, notify: {kind: kind, title: "示例通知", body: "示例正文"}, connection: {}, stats: {}, buttons: {}});
    tab("logs");
    return name;
  }
  return "unknown";
};
window.collectMetrics = function collectMetrics() {
  const app = document.querySelector(".app");
  const layout = document.querySelector(".layout");
  const panel = document.querySelector(".panel.left");
  const title = document.querySelector(".titlebar span");
  const h2 = document.querySelector(".panel h2");
  return {
    href: location.href,
    viewport: {innerWidth, innerHeight},
    devicePixelRatio,
    badge: (document.querySelector(".segmented .count") || {}).textContent || "",
    reviewBadge: $("badge").textContent,
    choose: $("choose").textContent,
    filename: $("filename").textContent,
    status: $("status").textContent,
    run: $("run").textContent,
    filter: $("filter").value,
    cards: [...document.querySelectorAll("article.rec .rec-role")].map((el) => el.textContent),
    prefsOpen: $("prefsDialog").open,
    dialogs: {connection: $("connection").open, history: $("historyDialog").open, prefs: $("prefsDialog").open, summary: $("summaryDialog").open},
    textarea: !!document.querySelector("article.rec textarea"),
    detailsOpen: !!document.querySelector("article.rec details[open]"),
    fonts: {family: getComputedStyle(document.documentElement).fontFamily, title: title && getComputedStyle(title).fontSize, h2: h2 && getComputedStyle(h2).fontSize, h3: null},
    app: {maxWidth: getComputedStyle(app).maxWidth, padding: getComputedStyle(app).padding, background: getComputedStyle(document.body).backgroundColor},
    layout: {columns: getComputedStyle(layout).gridTemplateColumns, gap: getComputedStyle(layout).gap},
    panel: {padding: getComputedStyle(panel).padding, radius: getComputedStyle(panel).borderRadius, borderWidth: getComputedStyle(panel).borderTopWidth},
    pageOverflow: document.documentElement.scrollHeight > document.documentElement.clientHeight + 1 || document.body.scrollHeight > document.body.clientHeight + 1,
    pageOverflowY: getComputedStyle(document.documentElement).overflowY,
    contentOverflowY: getComputedStyle($("content")).overflowY,
    contentMaxHeight: getComputedStyle($("content")).maxHeight,
    bottomVisible: (function () {
      const box = document.querySelector(".links");
      if (!box) return false;
      const rect = box.getBoundingClientRect();
      return rect.bottom <= innerHeight + 1 && rect.top >= 0;
    })()
  };
};
window.__toggleCard = function (id, times) {
  const details = document.querySelector("article.rec[data-record-id=\"" + id + "\"] details");
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
renderQueue({queue: [], session: {}});
connectBridge();
