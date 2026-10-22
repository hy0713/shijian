const $ = (id) => document.getElementById(id);
document.querySelectorAll("svg.icon").forEach(icon => icon.setAttribute("aria-hidden", "true"));
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const labels = {
  pending: "排队中",
  running: "研究中",
  completed: "执行结束",
  failed: "失败",
  cancelled: "已取消",
};
const outcomes = {
  answered: "已形成有据回答",
  partial: "部分核查完成",
  insufficient_evidence: "证据不足",
  incomplete: "研究未完成",
};
const indexLabels = {
  unrequested: "快照未索引",
  pending: "等待索引",
  indexing: "正在索引",
  ready: "可检索",
  failed: "索引失败",
};
let routeVersion = 0,
  current = null,
  socket = null,
  reconnectTimer = null,
  cursor = 0,
  terminal = false;
let taskEvidence = [],
  selectedEvidence = new Set(),
  activeReference = null,
  activeSource = null,
  libraryOffset = 0,
  libraryVersion = 0;
let stageIndex = -1,
  webCandidates = new Map(),
  webReadPages = new Map(),
  webKnownCost = 0,
  webKnownRequests = 0,
  seenStages = new Set(),
  task = null,
  config = null,
  evaluationSelection = null,
  importBusy = false;
let activeCollection = null, collectionOffset = 0, collectionSourceOffset = 0,
  collectionTaskOffset = 0, editingCollection = null, pickerTarget = null;
let focusComposer = false;
function error(msg) {
  $("globalError").hidden = !msg;
  $("globalError").textContent = msg || "";
}
function guarded(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      error(e.message);
    }
  };
}
async function api(path, options = {}) {
  const r = await fetch(path, options);
  if (!r.ok) {
    let b;
    try {
      b = await r.json();
    } catch {
      b = { detail: "服务暂时不可用" };
    }
    throw Error(
      typeof b.detail === "string"
        ? b.detail
        : "请求格式不符合要求，请检查输入字段",
    );
  }
  return r.json();
}
const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
function link(url, text) {
  const a = el("a", text);
  try {
    const u = new URL(url);
    if (!["http:", "https:"].includes(u.protocol))
      return el("span", "来源地址未提供或无效");
    a.href = u.href;
  } catch {
    return el("span", "来源地址未提供或无效");
  }
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function button(text, fn, cls = "quiet") {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = guarded(fn);
  return b;
}
function metadata(root, rows) {
  const dl = el("dl", undefined, "metadata");
  for (const [k, v] of rows) {
    dl.append(el("dt", k), el("dd", v || "未标注"));
  }
  root.append(dl);
}
function disconnect() {
  clearTimeout(reconnectTimer);
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
}
function showView(name) {
  document
    .querySelectorAll(".view")
    .forEach((n) => (n.hidden = n.id !== name + "View"));
  document.body.dataset.view = name;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    a.classList.toggle(
      "active",
      a.dataset.nav ===
        (name === "source"
          ? "library"
          : name.startsWith("evaluation")
            ? "evaluation"
            : name === "research"
              ? "home"
              : name),
    );
  });
  setNavigation(false);
}
async function route() {
  const version = ++routeVersion;
  disconnect();
  error("");
  current = null;
  task = null;
  const [path, query = ""] = (location.hash.slice(1) || "/").split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  const name = parts[0] || "home";
  showView(
    name === "sources"
      ? "source"
      : name === "evaluation" && parts.length === 4
        ? "evaluationCase"
        : name,
  );
  $("pageTitle").textContent =
    {
      home: "研究工作台",
      research: "研究详情",
      library: "文献资料库",
      sources: "原文阅读",
      evaluation: "效果评测",
      collections: "研究专题",
    }[name] || "研究工作台";
  $("pageSubtitle").textContent =
    name === "research" ? "核查 · 阅读 · 引证" : "从问题到证据";
  try {
    if (name === "home") {
      if (params.has("q")) $("question").value = params.get("q");
      if (focusComposer) { $("question").focus(); focusComposer = false; }
      await loadResearchScope(params.get("collection"));
    } else if (name === "research" && parts[1])
      await openTask(parts[1], version);
    else if (name === "library") {
      $("libraryFilters").elements.namedItem("query").value = params.get("query") || "";
      libraryOffset = 0;
      await loadLibrary();
    } else if (name === "sources" && parts[1])
      await loadSource(parts[1], version, params.has("at") ? Number(params.get("at")) : null);
    else if (name === "collections") {
      collectionSourceOffset = collectionTaskOffset = 0;
      $("collectionForm").hidden = true;
      $("collectionAttachResults").replaceChildren();
      await loadCollections(parts[1] || null);
      if (params.has("new")) showCollectionEditor(null);
    }
    else if (name === "evaluation" && parts.length === 4)
      await loadEvaluationCase(parts.slice(1), version);
    else if (name === "evaluation") await loadEvaluations(params.get("run"));
    else if (name !== "home") {
      location.hash = "/";
    }
  } catch (e) {
    if (version === routeVersion) error(e.message);
  }
}
async function refreshHistory() {
  const tasks = await api(
    "/api/tasks?" +
      new URLSearchParams({ limit: 30, query: $("historyQuery").value }),
  );
  $("history").replaceChildren(
    ...tasks.map((t) => {
      const a = el("a", undefined, "history-item");
      a.href = "#/research/" + t.task_id;
      if (t.task_id === current) a.classList.add("selected");
      a.append(
        el("span", labels[t.status], "history-status " + t.status),
        el("span", t.question),
      );
      a.title = t.question;
      return a;
    }),
  );
  if (!tasks.length) $("history").append(el("p", "还没有研究记录", "muted"));
  $("historyCount").textContent = tasks.length ? String(tasks.length) : "";
}
async function createResearch(question, mode, scope = {}) {
  const t = await post("/api/tasks", { question, mode, ...scope });
  location.hash = "/research/" + t.task_id;
  refreshHistory().catch((e) => error(e.message));
}
async function openTask(id, version) {
  current = id;
  cursor = 0;
  terminal = false;
  taskEvidence = [];
  selectedEvidence.clear();
  activeReference = null;
  seenStages.clear();
  stageIndex = -1;
  webCandidates.clear();
  webReadPages.clear();
  $("webSourcesPanel").hidden = true;
  $("leadPublisher").value = "all";
  $("leadLanguage").value = "all";
  webKnownCost = 0;
  webKnownRequests = 0;
  $("events").replaceChildren();
  $("answer").replaceChildren();
  $("taskDiagnostics").hidden = true;
  $("diagnosticData").textContent = "";
  $("taskUsage").hidden = true;
  $("webUsage").hidden = true;
  $("evidenceReader").hidden = true;
  $("evidenceList").hidden = false;
  $("backEvidence").hidden = true;
  $("eventCount").textContent = "";
  $("currentStep").textContent = "正在读取已保存进度";
  $("taskQuestion").textContent = "正在载入研究…";
  const t = await api("/api/tasks/" + id);
  if (version !== routeVersion) return;
  showTask(t);
  await loadTaskEvidence(id);
  if (version !== routeVersion) return;
  connect(id);
  refreshHistory().catch((e) => error(e.message));
}
function showTask(t) {
  if (t.task_id !== current) return;
  task = t;
  terminal = ["completed", "failed", "cancelled"].includes(t.status);
  $("taskQuestion").textContent = t.question;
  $("taskMeta").replaceChildren(
    el(
      "span",
      {
        online: "全网历史研究",
        local: "限定资料研究",
        hybrid: "资料库 + 联网（旧模式）",
      }[t.mode] || t.mode,
    ),
    el("span", new Date(t.created_at).toLocaleString("zh-CN")),
    el("span", "任务 " + t.task_id.slice(0, 8)),
  );
  if (t.source_ids !== null) $("taskMeta").append(el("span", "固定范围 · " + t.source_ids.length + " 份资料"));
  $("taskStatus").textContent =
    t.status === "completed" && t.result?.research_outcome
      ? outcomes[t.result.research_outcome.status]
      : labels[t.status];
  $("taskStatus").className = "status-pill " + t.status;
  $("cancel").hidden = terminal;
  $("retry").hidden = !terminal;
  $("exportAnswer").hidden = !t.result;
  $("exportMarkdown").hidden = !t.result;
  $("exportMarkdown").href = "/api/tasks/" + t.task_id + "/export";
  $("runningHint").hidden = terminal;
  $("taskFailure").hidden = !["failed", "cancelled"].includes(t.status);
  $("taskFailure").replaceChildren();
  if (t.status === "failed")
    $("taskFailure").append(
      el("strong", "这次研究未完成"),
      el("p", t.error || "执行失败；已保存的原文仍可在右侧查看。"),
      el("p", "修复配置或连接后可重新研究；将创建新任务，原记录保留。"),
    );
  if (t.status === "cancelled")
    $("taskFailure").append(
      el("strong", "研究已取消"),
      el("p", "已读取的证据保留。可修改问题或重新创建研究。"),
    );
  if (t.result) renderAnswer(t.result, $("answer"), (ref) => showEvidence(ref));
  updateElapsed();
  renderStages();
}
function updateElapsed() {
  if (!task) return;
  const seconds =
    task.result?.metrics?.elapsed_seconds ??
    Math.max(
      0,
      Math.round(
        ((terminal ? Date.parse(task.updated_at) : Date.now()) -
          Date.parse(task.created_at)) /
          1000,
      ),
    );
  $("elapsed").textContent =
    "用时 " +
    Math.floor(seconds / 60) +
    "分 " +
    Math.floor(seconds % 60) +
    "秒";
}
const stageNames = [
  "分析前提",
  "检索资料",
  "阅读原文",
  "评估缺口",
  "组织回答",
  "核对引用",
];
function renderStages() {
  const root = $("stages");
  root.replaceChildren(
    ...stageNames.map((name, i) => {
      const n = el(
        "li",
        undefined,
        i === stageIndex
          ? ["failed", "cancelled"].includes(task?.status)
            ? "interrupted"
            : terminal
              ? "done"
              : "active"
          : seenStages.has(i)
            ? "done"
            : "",
      );
      n.append(
        el(
          "span",
          (seenStages.has(i) && i !== stageIndex) ||
            (terminal && task?.status === "completed" && i === stageIndex)
            ? "✓"
            : String(i + 1),
          "stage-number",
        ),
        el("span", name),
      );
      return n;
    }),
  );
}
function progress(event) {
  if (event.event_id) {
    if (event.event_id <= cursor) return;
    cursor = event.event_id;
  }
  if (event.type === "snapshot") {
    showTask(event.data);
    loadTaskEvidence(current).catch((e) => error(e.message));
    return;
  }
  const d = event.data;
  if (event.type === "status" && !terminal && task && labels[d.status]) {
    task.status = d.status;
    $("taskStatus").textContent = d.cancel_requested
      ? "正在取消"
      : labels[d.status];
    $("taskStatus").className = "status-pill " + d.status;
  }
  let summary = "";
  if (event.type === "step") {
    const s = d.step;
    let index = s.includes("分析")
      ? 0
      : s.includes("选择")
        ? 1
        : s.includes("评估")
          ? 3
          : s.includes("核对")
            ? 5
            : s.includes("组织") || s.includes("修订")
              ? 4
              : stageIndex;
    if (stageIndex >= 0) seenStages.add(stageIndex);
    stageIndex = index;
    $("currentStep").textContent = s;
    summary = s + (d.round ? " · 第 " + d.round + " 轮" : "");
  } else if (event.type === "tool") {
    if (stageIndex >= 0) seenStages.add(stageIndex);
    stageIndex = ["read_passage", "fetch_page"].includes(d.name) ? 2 : 1;
    summary = d.name + " · " + d.summary;
  } else if (event.type === "plan")
    summary = "核查计划：" + d.subquestions.join("；");
  else if (event.type === "assessment")
    summary =
      "证据评估：" +
      d.summary +
      "\n缺口：" +
      (d.gaps.join("；") || "模型判断当前材料已足够");
  else if (event.type === "source") {
    summary = "新证据：" + d.title + " " + (d.chapter || "");
    loadTaskEvidence(current).catch((e) => error(e.message));
  } else if (event.type === "search_results") {
    for (const hit of d.leads) webCandidates.set(hit.url, hit);
    renderWebSources();
    summary = "候选来源 · " + d.query_language + " · " + d.query + " · " + d.leads.length + " 条，尚不可引用";
  } else if (event.type === "page_read") {
    webReadPages.set(d.url, d);
    renderWebSources();
    summary = "网页阅读 · " + d.title + " · " + d.language + (d.possible_reprint_of.length ? " · 可能转载" : "");
  } else if (event.type === "model_usage") {
    summary =
      "模型第 " +
      d.request_number +
      " 次 · " +
      d.stage +
      " · " +
      (d.total_tokens == null
        ? "未返回 Token 用量"
        : "输入 " +
          d.input_tokens +
          " / 输出 " +
          d.output_tokens +
          " / 合计 " +
          d.total_tokens);
    $("taskUsage").hidden = false;
    $("taskUsage").textContent =
      "已请求 " +
      d.request_number +
      " 次 · 已观测累计 " +
      d.observed_cumulative_tokens.toLocaleString() +
      " Token" +
      (d.usage_missing_requests
        ? " · " + d.usage_missing_requests + " 次未返回用量"
        : "") +
      " · 未估算费用";
  } else if (event.type === "external_usage") {
    const u = d.summary;
    if (d.estimated_cost_cny != null) {
      webKnownCost += Number(d.estimated_cost_cny);
      webKnownRequests++;
    }
    summary =
      "外部请求 " +
      d.request_number +
      " · " +
      d.kind +
      " · " +
      d.outcome +
      (d.estimated_cost_cny == null
        ? " · 工具费未知"
        : " · 工具费估算 ¥" + d.estimated_cost_cny);
    $("webUsage").hidden = false;
    $("webUsage").textContent =
      "搜索尝试 " +
      u.search_attempts +
      " 次 · 正文抓取尝试 " +
      u.fetch_attempts +
      " 次 · 工具费估算 " +
      (u.estimated_tool_cost_cny == null
        ? webKnownRequests
          ? "已知部分 ¥" + webKnownCost.toFixed(3) + "；总额未知"
          : "未知"
        : "¥" + u.estimated_tool_cost_cny) +
      "（不含模型费用）" +
      (u.unknown_billing_requests ? " · 有计费状态未知的请求" : "");
  } else if (event.type === "cache_hit") {
    summary = d.name + " · " + d.note;
  } else if (event.type === "diagnostic") {
    $("taskDiagnostics").hidden = false;
    $("diagnosticData").textContent = JSON.stringify(d, null, 2);
    summary = "运行失败诊断已保存，可展开查看；原记录和证据保留";
  } else if (event.type === "tool_error")
    summary =
      d.name + " · " + (d.message || "调用失败（" + d.error_type + "）");
  else if (event.type === "status" || labels[event.type]) {
    summary = labels[d.status] || event.type;
    if (d.cancel_requested) summary += "，已请求取消";
  }
  if (summary) {
    const n = el("li");
    n.append(
      el("time", new Date(event.time).toLocaleTimeString("zh-CN")),
      el("span", summary),
    );
    $("events").append(n);
    $("eventCount").textContent = $("events").children.length + " 条";
  }
  renderStages();
  if (labels[event.type] && !["pending", "running"].includes(event.type)) {
    const id = current;
    api("/api/tasks/" + id)
      .then((t) => {
        if (current === id) showTask(t);
      })
      .catch((e) => error(e.message));
  }
}
function connect(id) {
  if (current !== id) return;
  socket = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host +
      "/ws/tasks/" +
      id +
      "?after=" +
      cursor,
  );
  socket.onmessage = (e) => {
    if (current === id) progress(JSON.parse(e.data));
  };
  socket.onclose = () => {
    if (current === id && !terminal)
      reconnectTimer = setTimeout(() => connect(id), 1500);
  };
  socket.onerror = () => {
    if (current === id && !terminal)
      $("currentStep").textContent = "连接中断，正在恢复；后台任务继续执行";
  };
}
async function loadTaskEvidence(id) {
  if (!id) return;
  const rows = await api("/api/tasks/" + id + "/evidence");
  if (current !== id) return;
  taskEvidence = rows;
  renderEvidenceList();
}
function renderWebSources() {
  $("webSourcesPanel").hidden = !webCandidates.size && !webReadPages.size;
  const publisher = $("leadPublisher").value, language = $("leadLanguage").value;
  const rows = [...webCandidates.values()].filter(h =>
    (publisher === "all" || h.publisher_hint === "机构或学术域名线索") &&
    (language === "all" || h.language_hint === language));
  $("webCandidates").replaceChildren(...rows.map(h => {
    const n = el("article", undefined, "candidate-card");
    n.append(link(h.url, h.title + " ↗"), el("small", h.publisher_hint + " · " + h.language_hint + " · " + (webReadPages.has(h.url) ? "已取得正文" : "未读候选")), el("p", h.snippet));
    return n;
  }));
  if (!rows.length) $("webCandidates").append(el("p", "没有符合筛选条件的候选。", "muted"));
  $("webReadPages").replaceChildren(...[...webReadPages.values()].map(p => {
    const n = el("article", undefined, "candidate-card");
    n.append(el("strong", "已读网页 · " + p.title), el("small", p.language + " · " + p.publisher_hint));
    if (p.duplicate || p.possible_reprint_of.length) n.append(el("p", p.duplicate ? "复用库中同正文存储；来源独立性另行核查。" : "正文高度相似，可能转载；须人工核查独立性。", "alert"));
    const a = el("a", "浏览保存正文与目录 ↗"); a.href = "#/sources/" + p.source_id; n.append(a);
    n.append(saveButton(p.source_id), button("加入专题", () => openCollectionPicker("sources", p.source_id)));
    return n;
  }));
}
$("leadPublisher").onchange = renderWebSources;
$("leadLanguage").onchange = renderWebSources;
function renderEvidenceList() {
  $("evidenceCount").textContent = taskEvidence.length;
  $("evidenceList").replaceChildren();
  for (const e of taskEvidence) {
    const card = el("div", undefined, "evidence-card");
    if (activeReference?.evidence_id === e.evidence_id)
      card.classList.add("selected");
    const select = el("input");
    select.type = "checkbox";
    select.setAttribute(
      "aria-label",
      "对照 " + e.title + " " + (e.chapter || "") + " 片段 " + e.start,
    );
    select.checked = selectedEvidence.has(e.evidence_id);
    select.disabled = selectedEvidence.size >= 2 && !select.checked;
    select.onchange = () => {
      if (select.checked) selectedEvidence.add(e.evidence_id);
      else selectedEvidence.delete(e.evidence_id);
      renderEvidenceList();
    };
    const b = button(
      e.title + " " + (e.chapter || ""),
      () => showEvidence({ evidence_id: e.evidence_id }),
      "evidence-title",
    );
    card.append(
      select,
      b,
      el("p", e.text.slice(0, 95) + "…"),
      el("small", e.source_type + " · 原文位置 " + e.start + "–" + e.end),
    );
    if (e.language) card.append(el("small", "语种线索：" + e.language + (e.section_heading ? " · " + e.section_heading : "")));
    if (e.possible_reprint_of?.length) card.append(el("small", "可能转载，不作为独立佐证", "failure-text"));
    $("evidenceList").append(card);
  }
  if (!taskEvidence.length)
    $("evidenceList").append(
      el(
        "div",
        "尚无已读证据。检索命中只有经过阅读才会出现在这里。",
        "empty-state",
      ),
    );
  $("compareEvidence").textContent =
    "对照已选证据（" + selectedEvidence.size + "/2）";
  $("compareEvidence").disabled = selectedEvidence.size !== 2;
}
function originalNode(text, start, end) {
  const node = el("div", undefined, "original");
  const chars = Array.from(text);
  if (
    Number.isInteger(start) &&
    start >= 0 &&
    end <= chars.length &&
    end > start
  ) {
    node.append(
      document.createTextNode(chars.slice(0, start).join("")),
      el("mark", chars.slice(start, end).join("")),
      document.createTextNode(chars.slice(end).join("")),
    );
  } else node.textContent = text;
  return node;
}
async function showEvidence(ref) {
  const id = current;
  let e = taskEvidence.find((e) => e.evidence_id === ref.evidence_id);
  if (!e) e = await api("/api/tasks/" + id + "/evidence/" + ref.evidence_id);
  if (current !== id) return;
  activeReference = ref;
  const root = $("evidenceReader");
  root.replaceChildren(el("h3", e.title + " " + (e.chapter || "")));
  metadata(root, [
    ["作者 / 编纂者", e.author],
    ["任务快照版本", e.edition],
    ["本次网页读取", e.retrieved_at || "历史记录未提供"],
    ["资料类型", e.source_type],
    ["语种线索（可能出错）", e.language || "历史记录未标注"],
    ["来源线索", e.publisher_hint || "待核查"],
    ["所在节标题", e.section_heading || "未识别，勿视为卷章信息"],
    ["原文位置", e.start + "–" + e.end],
  ]);
  root.append(link(e.url, "查看保存时的来源 ↗"));
  const reading = task?.result?.citation_audit?.literal_readings?.find(
    (q) => q.evidence_id === e.evidence_id && q.quote === ref.quote,
  );
  if (reading?.literal_meaning) {
    const d = el("details");
    d.append(
      el("summary", "中文译述 / 释读（模型辅助，不是原文）"),
      el("p", reading.literal_meaning),
    );
    root.append(d);
  }
  if (ref.quote) {
    const actual = Array.from(e.text)
      .slice(ref.start - e.start, ref.end - e.start)
      .join("");
    if (actual !== ref.quote) {
      root.append(
        el("p", "引用位置与快照不一致，未标成有效高亮。", "alert danger"),
      );
    } else root.append(el("p", "朱砂色标记为本条引用的连续原文。", "muted"));
  }
  for (const [label, text] of [
    ["前一片段", e.before],
    ["引用所在原文", e.text],
    ["后一片段", e.after],
  ]) {
    if (!text) continue;
    const details = el("details");
    details.open = label === "引用所在原文";
    details.append(
      el("summary", label),
      label === "引用所在原文"
        ? originalNode(
            text,
            ref.quote ? ref.start - e.start : null,
            ref.quote ? ref.end - e.start : null,
          )
        : originalNode(text),
    );
    root.append(details);
  }
  root.append(el("p", e.provenance_note, "panel-note"));
  if (e.possible_reprint_of?.length) root.append(el("p", "本段来源与本次其他正文高度相似，可能转载，不能简单计为独立佐证。", "alert"));
  const a = el("a", "打开整份原文 ↗", "text-link");
  a.href = "#/sources/" + e.source_id;
  root.append(a);
  root.append(saveButton(e.source_id), button("加入专题", () => openCollectionPicker("sources", e.source_id)));
  root.hidden = false;
  $("evidenceList").hidden = true;
  $("backEvidence").hidden = false;
  renderEvidenceList();
  root.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function renderAnswer(r, root, onCitation) {
  root.replaceChildren();
  if (r.research_outcome) {
    const banner = el(
      "section",
      undefined,
      "research-outcome " + r.research_outcome.status,
    );
    banner.append(
      el("strong", outcomes[r.research_outcome.status]),
      el("p", r.research_outcome.reasons.join("；")),
      el("small", r.research_outcome.note),
    );
    root.append(banner);
  }
  function section(title, items, cls = "") {
    if (!items.length) return;
    const block = el("details", undefined, "answer-section " + cls);
    block.open = true;
    block.append(el("summary", title));
    for (const c of items) {
      const claim = el("div", undefined, "claim");
      claim.append(el("span", c.kind, "kind"), el("p", c.text));
      const refs = el("div", undefined, "citation-row");
      for (const ref of c.citations || []) {
        const b = button(
          ref.title + " " + (ref.chapter || "") + " ↗",
          () => onCitation(ref),
          "citation",
        );
        b.title = ref.quote;
        refs.append(b);
      }
      claim.append(refs);
      if (c.support_review?.facts?.length) {
        const d = el("details", undefined, "fact-review");
        d.append(el("summary", "逐项证据核查（模型辅助）"));
        for (const fact of c.support_review.facts) {
          const row = el("div");
          row.append(
            el("p", fact.fragment),
            el("small", fact.verdict + " · " + fact.reason),
          );
          for (const id of fact.quotation_ids) {
            const ref = c.citations.find((x) => x.quotation_id === id);
            if (ref)
              row.append(
                button("查看原文 " + id, () => onCitation(ref), "citation"),
              );
          }
          d.append(row);
        }
        claim.append(d);
      }
      block.append(claim);
    }
    root.append(block);
  }
  section("提问前提", [r.premise]);
  section("核心结论", r.conclusions);
  section("关键事件与解释", r.events, "event-thread");
  section("来源分歧与局限", r.disagreements);
  if (!r.conclusions.length)
    root.append(
      el(
        "p",
        "没有形成通过引用审核的核心结论。可缩小问题范围，或先补充相关卷章再研究。",
        "alert",
      ),
    );
  if (r.limitations.length) {
    const d = el("details", undefined, "answer-section");
    d.open = true;
    d.append(el("summary", "证据不足与限制"));
    const ul = el("ul");
    for (const text of r.limitations) ul.append(el("li", text));
    d.append(ul);
    root.append(d);
  }
  const audit = el("details", undefined, "audit");
  audit.append(el("summary", "运行统计与引用审核"));
  const m = r.metrics || {};
  metadata(audit, [
    ["研究轮数", String(m.rounds ?? "—")],
    ["工具调用", String(m.tool_calls ?? "—")],
    [
      "Token 用量",
      m.token_usage
        ? Object.values(m.token_usage)
            .reduce((n, x) => n + x.total_tokens, 0)
            .toLocaleString()
        : "未提供",
    ],
    ["模型费用", m.cost == null ? "未配置模型价格，未估算" : String(m.cost)],
  ]);
  audit.append(
    el("p", r.citation_audit.note, "muted"),
    el(
      "pre",
      JSON.stringify(
        {
          rejected: r.rejected_claims,
          rejected_limitations: r.rejected_limitations,
          requests: m.model_requests,
          external_requests: m.external_requests,
          web_usage: m.web_usage,
        },
        null,
        2,
      ),
    ),
  );
  root.append(audit);
}
async function loadLibrary() {
  const version = ++libraryVersion;
  const data = new FormData($("libraryFilters"));
  const q = new URLSearchParams([...data.entries()].filter(([, v]) => v));
  q.set("limit", "12");
  q.set("offset", libraryOffset);
  const rows = await api("/api/sources?" + q);
  if (version !== libraryVersion) return;
  $("sourceGrid").replaceChildren(
    ...rows.map((s) => {
      const card = el("article", undefined, "source-card");
      card.append(el("small", s.source_type, "eyebrow"));
      const a = el("a", s.title + " " + (s.chapter || ""));
      a.href = "#/sources/" + s.source_id;
      card.append(
        a,
        el("p", s.author || "编纂者未标注", "muted"),
        el("p", (s.people || []).join(" · "), "source-people"),
      );
      const pill = el(
        "span",
        indexLabels[s.index_status],
        "status-pill " + s.index_status,
      );
      card.append(
        pill,
        el(
          "small",
          s.index_status === "indexing"
            ? s.indexed_passages + "/" + s.index_total + " 段"
            : s.edition || "版本未标注",
        ),
      );
      if (s.index_error) card.append(el("p", s.index_error, "failure-text"));
      return card;
    }),
  );
  if (!rows.length)
    $("sourceGrid").append(
      el(
        "div",
        "没有符合筛选条件的资料。试试其他人物、主题，或导入所需卷章。",
        "empty-state",
      ),
    );
  $("libraryCount").textContent = "本页 " + rows.length + " 份资料";
  $("libraryPage").textContent = "第 " + (libraryOffset / 12 + 1) + " 页";
  $("libraryPrev").disabled = libraryOffset === 0;
  $("libraryNext").disabled = rows.length < 12;
}
function indexInfo(s) {
  const root = $("indexInfo");
  root.replaceChildren();
  const card = el("section", undefined, "index-card");
  card.append(el("strong", indexLabels[s.index_status]));
  const progress = el("progress");
  progress.max = Math.max(s.index_total, 1);
  progress.value =
    s.index_status === "ready" ? progress.max : s.indexed_passages;
  progress.setAttribute("aria-label", "片段索引进度");
  card.append(
    progress,
    el("span", s.indexed_passages + " / " + s.index_total + " 段"),
  );
  if (s.index_error) card.append(el("p", s.index_error, "failure-text"));
  if (s.index_status === "pending")
    card.append(
      el(
        "p",
        "已持久化，等待索引 worker；Redis 通知丢失时会自动补投。",
        "muted",
      ),
    );
  if (s.saved && !["pending", "indexing"].includes(s.index_status))
    card.append(
      button(
        s.index_status === "failed" ? "重试索引" : "重建索引",
        async () => {
          await post("/api/sources/" + s.id + "/index", {});
          await pollSource();
        },
      ),
    );
  root.append(card);
  if (!s.saved) root.append(el("p", "研究快照已保留。收藏后会异步建立索引，供以后的限定资料研究使用。取消收藏不会删除历史引用。", "subtle-notice"));
}
async function loadSource(id, version, at = null) {
  activeSource = null;
  const s = await api("/api/sources/" + id);
  if (version !== routeVersion) return;
  activeSource = s;
  $("sourceHeading").replaceChildren(
    el("div", s.source_type, "eyebrow"),
    el("h1", s.title + " " + (s.chapter || "")),
  );
  metadata($("sourceHeading"), [
    ["作者 / 编纂者", s.author],
    ["版本", s.edition],
    ["导入时间", new Date(s.imported_at).toLocaleString("zh-CN")],
    ["人物", s.people.join("、")],
    ["主题", s.topics.join("、")],
  ]);
  $("sourceHeading").append(
    link(s.source_url, "原始来源 ↗"),
    el("p", s.provenance_note, "panel-note"),
    el("code", "SHA-256 · " + s.content_hash, "hash-text"),
  );
  indexInfo(s);
  renderSourceSaveActions(s);
  $("sourceOriginal").replaceChildren(document.createTextNode(s.raw_text));
  $("originalMatch").textContent =
    "保存的完整原文，引用位置按 Unicode 字符计数。";
  $("sourceOutline").hidden = !s.outline?.length;
  $("sourceOutlineLinks").replaceChildren(...(s.outline || []).map(h => {
    const a = el("a", h.heading); a.href = "#/sources/" + id + "?at=" + h.start; return a;
  }));
  if (Number.isInteger(at) && at >= 0 && at < Array.from(s.raw_text).length) {
    const heading = s.outline?.find(h => h.start === at);
    $("sourceOriginal").replaceChildren(originalNode(s.raw_text, at, at + (heading ? Array.from(heading.heading).length : 1)));
    $("originalMatch").textContent = "已定位保存原文位置 " + at + "；此导航不生成研究引用。";
    $("sourceOriginal").querySelector("mark")?.scrollIntoView({block:"center"});
  }
  $("metadataForm").replaceChildren();
  const typeLabel = el("label", "资料类型");
  const typeSelect = el("select");
  typeSelect.name = "source_type";
  for (const name of ["历史文献", "现代研究", "通俗文章", "网页", "其他"]) {
    const option = el("option", name);
    option.value = name;
    typeSelect.append(option);
  }
  typeSelect.value = s.source_type;
  typeLabel.append(typeSelect);
  $("metadataForm").append(typeLabel);
  for (const [name, label] of [
    ["title", "标题"],
    ["author", "作者 / 编纂者"],
    ["chapter", "卷章"],
    ["edition", "版本"],
    ["people", "人物（逗号分隔）"],
    ["topics", "主题（逗号分隔）"],
    ["provenance_note", "来源说明"],
  ]) {
    const l = el("label", label);
    const input = el(name === "provenance_note" ? "textarea" : "input");
    input.name = name;
    input.value = Array.isArray(s[name]) ? s[name].join("，") : s[name] || "";
    input.required = name === "title";
    l.append(input);
    $("metadataForm").append(l);
  }
  const submit = el("button", "保存元数据", "primary");
  submit.type = "submit";
  $("metadataForm").append(
    submit,
    el("span", "原文与历史任务快照保持不变。", "muted"),
  );
}
async function pollSource() {
  const id = activeSource?.id;
  if (!id || document.body.dataset.view !== "source") return;
  const s = await api("/api/sources/" + id);
  if (activeSource?.id !== id) return;
  activeSource = { ...activeSource, ...s };
  indexInfo(s);
  renderSourceSaveActions(s);
}
const put = (path, body = {}) => api(path, {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
function saveButton(id, saved = null, after = null) {
  const b = button(saved === true ? "取消收藏" : "收藏资料 · 异步索引", async () => {
    b.disabled = true;
    try {
      const result = await put("/api/sources/" + id + "/bookmark", { saved: saved !== true });
      saved = result.saved;
      b.textContent = saved ? "已收藏 · 取消收藏" : "收藏资料 · 异步索引";
      if (after) await after();
    } finally { b.disabled = false; }
  });
  return b;
}
function renderSourceSaveActions(s) {
  $("sourceSaveActions").replaceChildren(
    saveButton(s.id, s.saved, pollSource),
    button("加入专题", () => openCollectionPicker("sources", s.id)),
    el("span", s.saved ? "已收藏 · 索引就绪后可复用" : "本次研究快照 · 尚未收藏", "muted"),
  );
}
async function loadResearchScope(selected = null) {
  const version = routeVersion;
  $("start").disabled = true;
  if (selected) $("mode").value = "local";
  $("scopeControl").hidden = $("mode").value !== "local";
  let rows;
  try { rows = await api("/api/collections?limit=100"); }
  catch (e) { if (version === routeVersion) error("资料范围未加载，已暂停提交：" + e.message); return; }
  if (version !== routeVersion) return;
  $("researchCollection").replaceChildren(el("option", "全部已收藏且可检索的资料"));
  $("researchCollection").firstChild.value = "";
  for (const r of rows) { const o = el("option", r.name + " · " + r.source_count + " 份资料"); o.value = r.id; $("researchCollection").append(o); }
  if (selected && !rows.some(r => r.id === selected)) {
    const r = await api("/api/collections/" + selected);
    if (version !== routeVersion) return;
    const o = el("option", r.name); o.value = r.id; $("researchCollection").append(o);
  }
  $("researchCollection").value = selected || "";
  if (selected) $("mode").value = "local";
  $("scopeControl").hidden = $("mode").value !== "local";
  $("start").disabled = false;
}
$("mode").addEventListener("change", () => { $("scopeControl").hidden = $("mode").value !== "local"; });
async function openCollectionPicker(kind, id) {
  pickerTarget = { kind, id };
  $("pickerQuery").value = "";
  $("pickerNotice").textContent = "";
  $("collectionPicker").showModal();
  await loadPicker();
}
async function loadPicker() {
  const rows = await api("/api/collections?" + new URLSearchParams({ query: $("pickerQuery").value, limit: 100 }));
  $("pickerCollection").replaceChildren(...rows.map(r => { const o = el("option", r.name); o.value = r.id; return o; }));
  $("pickerSubmit").disabled = !rows.length;
  $("pickerNotice").textContent = rows.length ? "选择专题后确认加入；可搜索更多专题。" : "没有匹配专题，请先新建。";
}
$("pickerSearch").onsubmit = guarded(async e => { e.preventDefault(); await loadPicker(); });
$("pickerForm").onsubmit = guarded(async e => {
  e.preventDefault(); $("pickerSubmit").disabled = true;
  try {
    const r = await put("/api/collections/" + $("pickerCollection").value + "/" + pickerTarget.kind + "/" + pickerTarget.id);
    $("pickerNotice").textContent = "已加入专题" + (r.index_status ? " · " + indexLabels[r.index_status] : " · 原研究记录已保留");
    if (activeSource && document.body.dataset.view === "source") await pollSource();
  } finally { $("pickerSubmit").disabled = false; }
});
$("pickerNew").onclick = () => { $("collectionPicker").close(); location.hash = "/collections?new=1"; };
$("archiveTask").onclick = guarded(() => openCollectionPicker("tasks", current));
function showCollectionEditor(row) {
  editingCollection = row?.id || null;
  $("collectionForm").reset();
  if (row) for (const key of ["name", "description", "people", "events", "regions"]) {
    $("collectionForm").elements.namedItem(key).value = Array.isArray(row[key]) ? row[key].join("，") : row[key];
  }
  $("collectionForm").hidden = false;
  $("collectionForm").elements.namedItem("name").focus();
}
$("newCollection").onclick = () => showCollectionEditor(null);
$("closeCollectionForm").onclick = () => { $("collectionForm").hidden = true; };
$("collectionForm").onsubmit = guarded(async e => {
  e.preventDefault(); const values = Object.fromEntries(new FormData(e.target));
  for (const k of ["people", "events", "regions"]) values[k] = splitTags(values[k]);
  const row = editingCollection ? await put("/api/collections/" + editingCollection, values) : await post("/api/collections", values);
  $("collectionForm").hidden = true;
  if (location.hash === "#/collections/" + row.id) await loadCollections(row.id);
  else location.hash = "/collections/" + row.id;
});
async function loadCollections(id = null) {
  const version = routeVersion;
  activeCollection = id;
  $("collectionDirectory").hidden = !!id; $("collectionDetail").hidden = !id;
  if (id) { await loadCollectionDetail(id, version); return; }
  const q = new URLSearchParams([...new FormData($("collectionFilters")).entries()].filter(([,v]) => v));
  q.set("limit", 12); q.set("offset", collectionOffset);
  const rows = await api("/api/collections?" + q);
  if (version !== routeVersion) return;
  $("collectionGrid").replaceChildren(...rows.map(r => {
    const n = el("article", undefined, "source-card collection-card"), a = el("a", r.name); a.href = "#/collections/" + r.id;
    n.append(el("small", "研究专题", "eyebrow"), a, el("p", r.description || "等待新的问题与线索", "muted"), el("p", [...r.people, ...r.events, ...r.regions].join(" · "), "source-people"), el("small", r.source_count + " 份文献 · " + r.task_count + " 次研究")); return n;
  }));
  if (!rows.length) $("collectionGrid").append(el("div", "还没有匹配的专题。创建一个专题，把分散的证据整理起来。", "empty-state"));
  $("collectionPage").textContent = "第 " + (collectionOffset / 12 + 1) + " 页";
  $("collectionPrev").disabled = !collectionOffset; $("collectionNext").disabled = rows.length < 12;
}
async function loadCollectionDetail(id, version = routeVersion) {
  const r = await api("/api/collections/" + id + "?" + new URLSearchParams({source_offset: collectionSourceOffset, task_offset: collectionTaskOffset}));
  if (version !== routeVersion || activeCollection !== id) return;
  const root = $("collectionHeading"); root.replaceChildren(el("h2", r.name), el("p", r.description, "panel-note"));
  metadata(root, [["人物", r.people.join("、")], ["事件", r.events.join("、")], ["地区", r.regions.join("、")]]);
  $("collectionActions").replaceChildren(
    button("使用专题资料研究 ↗", () => { location.hash = "/?collection=" + id; }, "primary"),
    button("编辑专题", () => showCollectionEditor(r)),
    button("移除专题…", () => {
      const n = $("collectionActions");
      n.append(el("span", "仅移除分类；文献与研究记录保留。", "muted"), button("确认移除", async () => {
        await api("/api/collections/" + id, {method:"DELETE"}); location.hash = "/collections";
      }));
    }),
  );
  $("collectionSourceCount").textContent = "专题文献 · " + r.source_count;
  $("collectionTaskCount").textContent = "专题研究 · " + r.task_count;
  $("collectionSources").replaceChildren(...r.sources.map(s => {
    const n = el("article", undefined, "source-card"), a = el("a", s.title + " " + (s.chapter || "")); a.href = "#/sources/" + s.source_id;
    n.append(a, el("p", s.saved ? "已收藏 · " + indexLabels[s.index_status] : "已取消收藏 · 未纳入本地检索", "muted"));
    if (s.index_error) n.append(el("p", s.index_error, "failure-text"));
    if (!s.saved) n.append(saveButton(s.source_id, false, () => loadCollectionDetail(id)));
    n.append(button("移出专题", () => removeMembership("sources", s.source_id))); return n;
  }));
  $("collectionTasks").replaceChildren(...r.tasks.map(t => {
    const n = el("article", undefined, "candidate-card"), a = el("a", t.question); a.href = "#/research/" + t.task_id;
    n.append(a, el("small", labels[t.status] + " · " + new Date(t.created_at).toLocaleDateString("zh-CN")), button("移出专题", () => removeMembership("tasks", t.task_id))); return n;
  }));
  if (!r.sources.length) $("collectionSources").append(el("p", "从资料库或研究引证中加入文献。", "empty-state"));
  if (!r.tasks.length) $("collectionTasks").append(el("p", "在研究详情中点击“加入专题”，或在上方查找研究记录。", "empty-state"));
  for (const kind of ["Source", "Task"]) {
    const offset = kind === "Source" ? collectionSourceOffset : collectionTaskOffset;
    const total = kind === "Source" ? r.source_count : r.task_count;
    const move = async delta => { if (kind === "Source") collectionSourceOffset = Math.max(0, offset + delta); else collectionTaskOffset = Math.max(0, offset + delta); await loadCollectionDetail(id); };
    const prev = button("上一页", () => move(-30)), next = button("下一页", () => move(30)); prev.disabled = !offset; next.disabled = offset + 30 >= total;
    $("collection" + kind + "Pages").replaceChildren(prev, el("span", "第 " + (offset/30+1) + " 页"), next);
  }
}
async function removeMembership(kind, id) {
  await api("/api/collections/" + activeCollection + "/" + kind + "/" + id, {method:"DELETE"});
  await loadCollectionDetail(activeCollection);
}
$("collectionAttachForm").onsubmit = guarded(async e => {
  e.preventDefault(); const kind = $("collectionAttachKind").value, id = activeCollection;
  const rows = await api("/api/" + kind + "?" + new URLSearchParams({query: $("collectionAttachQuery").value, limit: 30}));
  if (id !== activeCollection) return;
  $("collectionAttachResults").replaceChildren(...rows.map(r => {
    const n = el("div", undefined, "candidate-card"), key = r.source_id || r.task_id;
    n.append(el("span", r.title || r.question), button("加入", async () => {
      await put("/api/collections/" + id + "/" + kind + "/" + key);
      await loadCollectionDetail(id); n.replaceChildren(el("span", "已加入 · " + (r.title || r.question)));
    })); return n;
  }));
  if (!rows.length) $("collectionAttachResults").append(el("p", "未找到匹配项；请输入其他关键词。", "muted"));
});
$("collectionFilters").onsubmit = guarded(async e => { e.preventDefault(); collectionOffset = 0; await loadCollections(); });
$("collectionFilters").onreset = () => setTimeout(guarded(async () => { collectionOffset = 0; await loadCollections(); }), 0);
$("collectionPrev").onclick = guarded(async () => { collectionOffset = Math.max(0, collectionOffset - 12); await loadCollections(); });
$("collectionNext").onclick = guarded(async () => { collectionOffset += 12; await loadCollections(); });
const splitTags = (text) =>
  text
    .split(/[,，、]/)
    .map((x) => x.trim())
    .filter(Boolean);
async function loadEvaluations(selected) {
  const rows = await api("/api/evaluations");
  $("evaluationRun").replaceChildren(
    ...rows.map((r) => {
      const o = el("option", r.run_id + " · " + r.records + " 条");
      o.value = r.run_id;
      return o;
    }),
  );
  if (!rows.length) {
    $("evaluationSummary").replaceChildren(
      el(
        "p",
        "暂无已运行的评测。运行 eval/run.py 后，结果将显示于此；系统不会生成示例分数。",
        "empty-state",
      ),
    );
    $("evaluationTable").replaceChildren();
    return;
  }
  if (selected && rows.some((r) => r.run_id === selected))
    $("evaluationRun").value = selected;
  await loadEvaluationRun();
}
async function loadEvaluationRun() {
  const runId = $("evaluationRun").value;
  if (!runId) return;
  const row = await api("/api/evaluations/" + encodeURIComponent(runId));
  $("evaluationProtocol").textContent = row.manifest?.mode === "online"
    ? "跨主题在线研究记录；不是同语料三方案对比。" + (row.manifest.not_run_ids ? "题集仍有 " + row.manifest.not_run_ids.length + " 题未运行。" : "")
    : "同模型、同语料，比较直接回答、一次检索 RAG 与研究 Agent。";
  const root = $("evaluationSummary");
  root.replaceChildren();
  const summary = el("div", undefined, "evaluation-stats");
  for (const method of ["direct", "rag", "agent"]) {
    const cases = row.records.filter((r) => r.method === method);
    const reviewed = cases.filter((r) => r.human_review?.reviewer);
    const valid = cases.filter((r) => r.status !== "failed");
    const card = el("div", undefined, "stat-card");
    card.append(
      el(
        "strong",
        { direct: "直接回答", rag: "一次检索 RAG", agent: "研究 Agent" }[
          method
        ],
      ),
      el(
        "span",
        cases.length ? valid.length + " 条记录 / " + (cases.length - valid.length) + " 次执行失败" : "未运行",
      ),
      el("small", "人工已核查 " + reviewed.length + " 条"),
    );
    const insufficient = cases.filter(r => ["insufficient_evidence", "incomplete"].includes(r.outcome?.status)).length;
    if (insufficient) card.append(el("small", "证据不足 / 未完成核查：" + insufficient + " 条"));
    summary.append(card);
  }
  root.append(summary);
  if (row.manifest) {
    const d = el("details");
    d.append(
      el("summary", "固定实验配置与语料清单"),
      el("pre", JSON.stringify(row.manifest, null, 2)),
    );
    root.append(d);
  }
  const table = el("table");
  const head = el("tr");
  [
    "题号 / 类型",
    "方法",
    "执行 / 证据覆盖",
    "耗时",
    "Token",
    "工具",
    "锚点读取",
    "人工评分",
    "查看",
  ].forEach((t) => head.append(el("th", t)));
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const tbody = el("tbody");
  for (const r of row.records) {
    const tr = el("tr");
    const review = r.human_review;
    const vals = [
      r.question_id + " / " + (r.category || ""),
      r.method,
      r.status === "failed"
        ? "失败 · " + r.error_type
        : outcomes[r.outcome?.status] || "已记录",
      r.elapsed_seconds == null ? "—" : r.elapsed_seconds + " s",
      r.total_tokens == null ? "—" : r.total_tokens.toLocaleString(),
      r.tool_calls ?? "—",
      r.anchor_recall
        ? r.anchor_recall.matched + "/" + r.anchor_recall.expected
        : r.status === "failed"
          ? "未完成"
          : "不适用",
      review?.reviewer
        ? [
            review.evidence_support_0_to_2,
            review.completeness_0_to_2,
            review.premise_recognition_0_to_2,
          ].join(" / ")
        : "未评分",
    ];
    for (const value of vals) tr.append(el("td", String(value)));
    const td = el("td");
    const a = el("a", "核查 ↗");
    a.href = "#/evaluation/" + runId + "/" + r.question_id + "/" + r.method;
    td.append(a);
    tr.append(td);
    tbody.append(tr);
  }
  table.append(tbody);
  $("evaluationTable").replaceChildren(table);
}
async function loadEvaluationCase(parts, version) {
  const [runId, id, method] = parts;
  const row = await api(
    "/api/evaluations/" + parts.map(encodeURIComponent).join("/"),
  );
  if (version !== routeVersion) return;
  evaluationSelection = parts;
  $("evaluationBack").href = "#/evaluation?run=" + encodeURIComponent(runId);
  const root = $("evaluationCase");
  root.replaceChildren(
    el("div", runId + " / " + id + " / " + method, "eyebrow"),
    el("h1", row.question || id),
  );
  $("humanReviewSection").hidden = row.status === "failed";
  $("reviewNotice").textContent = "";
  if (row.status === "failed") {
    root.append(
      el("p", "实际失败：" + row.error_type, "alert danger"),
      el("pre", JSON.stringify(row.metrics, null, 2)),
    );
    return;
  }
  root.append(el("p", "人工评分要点：" + row.human_rubric, "alert"));
  const answer = el("article", undefined, "answer-reader");
  if (method === "direct")
    answer.append(el("p", row.result.answer_text, "direct-answer"));
  else
    renderAnswer(row.result, answer, (ref) => {
      const e = row.evidence.find((x) => x.evidence_id === ref.evidence_id);
      if (e) {
        const target = $("eval-e-" + e.evidence_id);
        target.open = true;
        target.replaceChildren(
          el("summary", e.title + " " + (e.chapter || "")),
          originalNode(e.text, ref.start - e.start, ref.end - e.start),
          link(e.url, "查看来源 ↗"),
        );
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  root.append(answer, el("h2", "本题已读证据"));
  for (const e of row.evidence) {
    const d = el("details", undefined, "evaluation-evidence");
    d.id = "eval-e-" + e.evidence_id;
    d.append(
      el(
        "summary",
        e.title + " " + (e.chapter || "") + " · " + e.start + "–" + e.end,
      ),
      el("p", e.edition || "版本未标注", "muted"),
      originalNode(e.text),
      link(e.url, "查看来源 ↗"),
    );
    root.append(d);
  }
  if (!row.evidence.length)
    root.append(
      el(
        "p",
        "该方法未读取资料，不能将模型给出的出处当成已核查引用。",
        "muted",
      ),
    );
  const assisted = el("details", undefined, "audit");
  assisted.append(
    el("summary", "模型辅助审核（与人工评分分开）"),
    el("pre", JSON.stringify(row.model_assisted, null, 2)),
  );
  root.append(assisted);
  for (const [k, v] of Object.entries(row.human_review))
    if ($("humanReviewForm").elements.namedItem(k))
      $("humanReviewForm").elements.namedItem(k).value = v ?? "";
}
$("researchForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  $("start").disabled = true;
  try {
    const collection = $("researchCollection").value;
    await createResearch($("question").value.trim(), $("mode").value,
      $("mode").value === "local" && collection ? {collection_id: collection} : {});
  } finally {
    $("start").disabled = false;
  }
});
$("retry").onclick = guarded(async () => {
  const t = task;
  $("retry").disabled = true;
  try {
    await createResearch(t.question, t.mode, t.source_ids === null ? {} : {source_ids: t.source_ids});
  } finally {
    $("retry").disabled = false;
  }
});
$("editQuestion").onclick = () => {
  if (task) location.hash = "/?q=" + encodeURIComponent(task.question);
};
$("cancel").onclick = guarded(async () => {
  await post("/api/tasks/" + current + "/cancel", {});
  $("currentStep").textContent = "已请求取消；进行中的外部请求返回或超时后停止";
});
$("exportAnswer").onclick = () => {
  if (!task?.result) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(task.result, null, 2)], {
      type: "application/json",
    }),
  );
  const a = el("a");
  a.href = url;
  a.download = "shijian-" + current + ".json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("backEvidence").onclick = () => {
  $("evidenceList").hidden = false;
  $("evidenceReader").hidden = true;
  $("backEvidence").hidden = true;
  activeReference = null;
  renderEvidenceList();
};
$("compareEvidence").onclick = () => {
  const rows = taskEvidence.filter((e) => selectedEvidence.has(e.evidence_id));
  $("comparison").replaceChildren(
    ...rows.map((e) => {
      const n = el("section");
      n.append(el("h3", e.title + " " + (e.chapter || "")));
      metadata(n, [
        ["版本", e.edition],
        ["类型", e.source_type],
        ["位置", e.start + "–" + e.end],
        ["语种线索（可能出错）", e.language || "未标注"],
        ["来源线索", e.publisher_hint || "待核查"],
      ]);
      n.append(
        link(e.url, "查看来源 ↗"),
        originalNode(e.text),
        el("p", e.provenance_note, "muted"),
      );
      for (const q of task?.result?.citation_audit?.literal_readings || []) {
        if (q.evidence_id === e.evidence_id && q.literal_meaning) n.append(el("p", "中文译述 / 释读（模型辅助，不是原文）：" + q.literal_meaning, "translation-note"));
      }
      return n;
    }),
  );
  if (rows[0].source_id === rows[1].source_id || rows.some(e => e.possible_reprint_of?.some(r => rows.some(other => other.source_id === r.source_id))))
    $("comparison").prepend(el("p", "这两段属于同份正文或疑似转载，不能按两份独立来源计算。", "alert"));
  $("compareDialog").showModal();
};
$("libraryFilters").onsubmit = guarded(async (e) => {
  e.preventDefault();
  libraryOffset = 0;
  await loadLibrary();
});
$("libraryFilters").onreset = () =>
  setTimeout(
    guarded(async () => {
      libraryOffset = 0;
      await loadLibrary();
    }),
    0,
  );
$("libraryPrev").onclick = guarded(async () => {
  libraryOffset = Math.max(0, libraryOffset - 12);
  await loadLibrary();
});
$("libraryNext").onclick = guarded(async () => {
  libraryOffset += 12;
  await loadLibrary();
});
$("metadataForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  const values = Object.fromEntries(new FormData(e.target));
  for (const k of ["people", "topics"]) values[k] = splitTags(values[k]);
  await api("/api/sources/" + activeSource.id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  await loadSource(activeSource.id, routeVersion);
});
$("findOriginal").onsubmit = (e) => {
  e.preventDefault();
  if (!activeSource) return;
  const q = $("originalQuery").value;
  const index = q ? activeSource.raw_text.indexOf(q) : -1;
  const start =
    index >= 0
      ? Array.from(activeSource.raw_text.slice(0, index)).length
      : null;
  const text = originalNode(
    activeSource.raw_text,
    start,
    start == null ? null : start + Array.from(q).length,
  );
  $("sourceOriginal").replaceChildren(...text.childNodes);
  $("originalMatch").textContent =
    index >= 0
      ? "已定位首个匹配。"
      : q
        ? "原文中未找到该词句。"
        : "保存的完整原文。";
  $("sourceOriginal")
    .querySelector("mark")
    ?.scrollIntoView({ block: "center", behavior: "smooth" });
};
$("openImport").onclick = () => {
  $("importNotice").replaceChildren();
  $("importDialog").showModal();
};
document
  .querySelectorAll("[data-close]")
  .forEach((b) => (b.onclick = () => $(b.dataset.close).close()));
$("file").onchange = () => {
  $("importTitle").required = !$("file")
    .files[0]?.name.toLowerCase()
    .endsWith(".json");
};
function importedNotice(rows) {
  const list = Array.isArray(rows) ? rows : [rows];
  const notice = $("importNotice");
  notice.replaceChildren(el("p", "原文已保存，索引在后台执行。"));
  for (const r of list) {
    const a = el(
      "a",
      (r.duplicate ? "已存在的资料" : "查看资料") +
        " · " +
        indexLabels[r.index_status],
    );
    a.href = "#/sources/" + r.source_id;
    a.onclick = () => $("importDialog").close();
    notice.append(a, el("br"));
  }
}
$("importForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  if (importBusy) return;
  importBusy = true;
  const submit = e.submitter;
  submit.disabled = true;
  $("importNotice").textContent = "正在保存原文…";
  try {
    const f = new FormData();
    f.append("file", $("file").files[0]);
    const m = {
      title: $("importTitle").value,
      author: $("importAuthor").value || null,
      chapter: $("importChapter").value || null,
      edition: $("importEdition").value || null,
      source_type: $("importType").value,
      source_url: $("importUrl").value || null,
      people: splitTags($("importPeople").value),
      topics: splitTags($("importTopics").value),
    };
    importedNotice(
      await api(
        "/api/sources/upload?metadata=" + encodeURIComponent(JSON.stringify(m)),
        { method: "POST", body: f },
      ),
    );
    if (document.body.dataset.view === "library") await loadLibrary();
  } catch (err) {
    $("importNotice").textContent = err.message;
  } finally {
    submit.disabled = false;
    importBusy = false;
  }
});
$("pageForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  if (importBusy) return;
  importBusy = true;
  e.submitter.disabled = true;
  try {
    $("importNotice").textContent = "正在读取网页正文…";
    importedNotice(
      await post("/api/sources/page", { url: $("pageUrl").value }),
    );
    if (document.body.dataset.view === "library") await loadLibrary();
  } catch (err) {
    $("importNotice").textContent = err.message;
  } finally {
    e.submitter.disabled = false;
    importBusy = false;
  }
});
$("bookSearchForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  e.submitter.disabled = true;
  $("bookNotice").textContent = "正在查询维基文库目录…";
  try {
    const data = await api(
      "/api/books/search?query=" + encodeURIComponent($("bookQuery").value),
    );
    $("bookResults").replaceChildren(
      ...data.results.map((r) => {
        const n = el("article", undefined, "book-result");
        n.append(
          link(r.url, r.title + " ↗"),
          el("p", r.snippet),
          button("导入此卷章", async () => {
            const b = n.querySelector("button");
            b.disabled = true;
            try {
              const saved = await post("/api/sources/page", { url: r.url });
              const a = el("a", "已保存，查看索引进度 ↗");
              a.href = "#/sources/" + saved.source_id;
              n.append(a);
              b.remove();
            } finally {
              b.disabled = false;
            }
          }),
        );
        return n;
      }),
    );
    $("bookNotice").textContent = data.results.length
      ? data.notice
      : "未找到候选页面，请改用书名和人物短词。";
  } catch (err) {
    $("bookNotice").textContent =
      "目录查询失败：" + err.message + "；没有使用虚构结果。";
  } finally {
    e.submitter.disabled = false;
  }
});
$("refreshHistory").onclick = guarded(refreshHistory);
let historyTimer;
$("historyQuery").oninput = () => {
  clearTimeout(historyTimer);
  historyTimer = setTimeout(guarded(refreshHistory), 250);
};
$("refreshEvaluations").onclick = guarded(() =>
  loadEvaluations($("evaluationRun").value),
);
$("evaluationRun").onchange = guarded(loadEvaluationRun);
$("humanReviewForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  for (const k of [
    "evidence_support_0_to_2",
    "completeness_0_to_2",
    "premise_recognition_0_to_2",
  ])
    data[k] = Number(data[k]);
  await api(
    "/api/evaluations/" +
      evaluationSelection.map(encodeURIComponent).join("/") +
      "/review",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    },
  );
  $("reviewNotice").textContent = "人工评分已保存。";
});
const narrowNavigation = matchMedia("(max-width: 720px)");
function setNavigation(open) {
  document.body.classList.toggle("sidebar-open", open);
  $("menuToggle").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = narrowNavigation.matches && !open;
}
setNavigation(false);
narrowNavigation.addEventListener("change", () => setNavigation(false));
$("menuToggle").onclick = () => {
  const open = !document.body.classList.contains("sidebar-open");
  setNavigation(open);
  if (open) document.querySelector(".new-study").focus();
};
$("navBackdrop").onclick = () => {
  setNavigation(false);
  $("menuToggle").focus();
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) $("navBackdrop").click();
  if (e.target.closest("input, textarea, select, [contenteditable], dialog")) return;
  if (e.key.toLowerCase() === "n" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault(); $("question").value = "";
    if (document.body.dataset.view === "home") $("question").focus();
    else { focusComposer = true; location.hash = "/"; }
  }
});
$("question").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !$("start").disabled) {
    e.preventDefault(); $("researchForm").requestSubmit();
  }
});
document.querySelectorAll("[data-question]").forEach(
  (b) =>
    (b.onclick = () => {
      $("question").value = b.dataset.question;
      $("question").focus();
    }),
);
document.querySelector(".skip-link").onclick = (e) => {
  e.preventDefault();
  $("mainContent").focus();
};
window.addEventListener("hashchange", route);
setInterval(() => {
  updateElapsed();
  if (
    document.body.dataset.view === "source" &&
    activeSource &&
    ["pending", "indexing"].includes(activeSource.index_status)
  )
    pollSource().catch((e) => error(e.message));
}, 3000);
Promise.all([
  refreshHistory(),
  api("/api/config").then((c) => {
    config = c;
    document.querySelector(".connection-details").open = !!c.model_error || !c.web_ready;
    const notes = [];
    if (c.model_error) notes.push(c.model_error);
    if (!c.web_ready) {
      notes.push(
        (c.web_error || "联网工具未配置") +
          "。请完成配置或主动选择限定资料研究。",
      );
      for (const option of $("mode").options)
        if (option.value !== "local") option.disabled = true;
    } else if (c.search_provider === "wikisource")
      notes.push(
        "当前只配置了维基文库搜索，范围限于该站；通用全网研究请配置DeepSeek或Tavily。",
      );
    else if (c.search_provider === "deepseek")
      notes.push(
        "全网研究通过DeepSeek搜索与读取网页，不自动检索预装资料；每次请求单独记录用量。",
      );
    $("configNotice").hidden = !notes.length;
    $("configNotice").textContent = notes.join(" ");
  }),
])
  .then(route)
  .catch((e) => {
    error(e.message);
    route();
  });
