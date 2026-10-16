const $ = (id) => document.getElementById(id);
let current = null,
  socket = null,
  reconnectTimer = null,
  cursor = 0,
  terminal = false;
const labels = {
  pending: "排队中",
  running: "研究中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function error(msg) {
  $("error").hidden = !msg;
  $("error").textContent = msg;
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
      typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail),
    );
  }
  return r.json();
}
function link(url, title) {
  if (!url) return el("span", "来源 URL 未提供");
  const a = el("a", title || url);
  try {
    const u = new URL(url);
    if (!["http:", "https:"].includes(u.protocol))
      return el("span", "URL 无效");
    a.href = u.href;
  } catch {
    return el("span", "URL 无效");
  }
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
async function refresh() {
  const [tasks, sources] = await Promise.all([
    api("/api/tasks"),
    api("/api/sources"),
  ]);
  $("history").replaceChildren(
    ...tasks.map((t) => {
      const b = el(
        "button",
        `${labels[t.status]} · ${t.question}`,
        "history-item",
      );
      b.onclick = () => openTask(t.task_id);
      return b;
    }),
  );
  $("sources").replaceChildren(
    ...sources.map((s) => {
      const n = el("div", undefined, "source");
      n.append(
        el("strong", `${s.title} ${s.chapter || ""}`),
        el(
          "div",
          `${s.index_status === "ready" ? "已索引" : "待执行索引"} · ${s.source_type}`,
        ),
      );
      return n;
    }),
  );
}
function progress(e) {
  if (e.event_id) {
    if (e.event_id <= cursor) return;
    cursor = e.event_id;
  }
  if (e.type === "snapshot") {
    showTask(e.data);
    return;
  }
  const d = e.data;
  let text = "";
  if (e.type === "status" || labels[e.type]) {
    text = labels[d.status] || e.type;
    $("status").textContent = text;
    if (d.cancel_requested) text += "，已请求取消";
  } else if (e.type === "step") {
    text = d.step + (d.round ? `（第 ${d.round} 轮）` : "");
  } else if (e.type === "plan") {
    text = `前提：${d.premise_status}\n子问题：${d.subquestions.join("；")}\n查询：${d.search_queries.join("；")}`;
  } else if (e.type === "tool") {
    text = `工具 ${d.name} · ${d.summary}`;
  } else if (e.type === "source") {
    text = `已读：${d.title} ${d.chapter || ""}`;
  } else if (e.type === "assessment") {
    text = `证据评估：${d.summary}\n缺口：${d.gaps.join("；") || "模型判断当前材料已足够"}`;
  } else if (e.type === "tool_error") {
    text = `${d.name} 调用失败：${d.error_type}`;
  }
  if (text) {
    const n = el("li", text);
    $("events").append(n);
    n.scrollIntoView({ block: "nearest" });
  }
  if (labels[e.type] && e.type !== "running" && e.type !== "pending")
    api(`/api/tasks/${current}`)
      .then(showTask)
      .catch((e) => error(e.message));
}
function connect(taskId) {
  if (terminal || current !== taskId) return;
  socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/tasks/${taskId}?after=${cursor}`,
  );
  socket.onmessage = (e) => {
    if (current === taskId) progress(JSON.parse(e.data));
  };
  socket.onclose = () => {
    if (current === taskId && !terminal)
      reconnectTimer = setTimeout(() => connect(taskId), 1500);
  };
  socket.onerror = () => {
    $("status").textContent = "连接中断，正在重连；任务仍在后台执行";
  };
}
async function openTask(taskId) {
  try {
    current = taskId;
    terminal = false;
    cursor = 0;
    clearTimeout(reconnectTimer);
    if (socket) {
      socket.onclose = null;
      socket.close();
    }
    error("");
    $("events").replaceChildren();
    $("answer").replaceChildren();
    const t = await api(`/api/tasks/${taskId}`);
    showTask(t); // Always connect once to replay existing events, even for finished tasks.
    terminal = false;
    connect(taskId);
  } catch (e) {
    error(e.message);
  }
}
function showTask(t) {
  if (t.task_id !== current) return;
  $("question").value = t.question;
  $("mode").value = t.mode;
  $("status").textContent = labels[t.status];
  terminal = ["completed", "failed", "cancelled"].includes(t.status);
  $("cancel").disabled = terminal;
  $("start").disabled = !terminal;
  if (t.error) error(t.error);
  if (t.result) renderAnswer(t.result);
  if (terminal) refresh().catch((e) => error(e.message));
}
function renderAnswer(r) {
  const root = $("answer");
  root.replaceChildren();
  function section(title, items) {
    root.append(el("h2", title));
    for (const c of items) {
      const n = el("div", undefined, "claim");
      n.append(el("span", c.kind, "kind"), el("p", c.text));
      for (const ref of c.citations || []) {
        const b = el(
          "button",
          `${ref.title} ${ref.chapter || ""} ↗ 原文`,
          "citation",
        );
        b.onclick = () => showEvidence(ref.evidence_id);
        n.append(b);
      }
      root.append(n);
    }
  }
  section("提问前提", [r.premise]);
  section("核心结论", r.conclusions);
  section("关键事件与解释", r.events);
  if (r.disagreements.length) section("来源分歧与局限", r.disagreements);
  if (r.limitations.length) {
    root.append(el("h2", "证据不足与限制"));
    const ul = el("ul");
    for (const text of r.limitations) ul.append(el("li", text));
    root.append(ul);
  }
  root.append(el("h2", "已读来源"));
  for (const s of r.sources) {
    const n = el("div", undefined, "source");
    n.append(
      el("strong", `${s.title} ${s.chapter || ""}`),
      el("div", s.edition || "版本未知"),
      link(s.url, "查看来源"),
      el("p", s.provenance_note),
    );
    root.append(n);
  }
  const audit = el("details");
  audit.append(
    el("summary", "引用审核与运行统计"),
    el("p", r.citation_audit.note),
    el(
      "pre",
      JSON.stringify(
        { metrics: r.metrics, rejected: r.rejected_claims },
        null,
        2,
      ),
    ),
  );
  root.append(audit);
}
async function showEvidence(id) {
  try {
    const e = await api(`/api/tasks/${current}/evidence/${id}`);
    const root = $("evidenceBody");
    root.replaceChildren(
      el("h2", `${e.title} ${e.chapter || ""}`),
      el("p", `${e.edition || "版本未知"} · 原文位置 ${e.start}–${e.end}`),
      link(e.url, "查看来源页面"),
    );
    for (const [title, text] of [
      ["前一片段", e.before],
      ["引用所在原文", e.text],
      ["后一片段", e.after],
    ]) {
      if (!text) continue;
      const d = el("details");
      d.open = title === "引用所在原文";
      d.append(el("summary", title), el("div", text, "original"));
      root.append(d);
    }
    root.append(el("p", e.provenance_note));
    document.querySelector(".library").prepend($("evidencePanel"));
    $("evidencePanel").hidden = false;
    $("evidencePanel").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) {
    error(e.message);
  }
}
$("researchForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    error("");
    const t = await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: $("question").value,
        mode: $("mode").value,
      }),
    });
    await openTask(t.task_id);
  } catch (e) {
    error(e.message);
  }
};
$("cancel").onclick = async () => {
  try {
    await api(`/api/tasks/${current}/cancel`, { method: "POST" });
    $("status").textContent =
      "已请求取消；正在进行的外部请求将在超时或返回后停止";
  } catch (e) {
    error(e.message);
  }
};
$("refresh").onclick = () => refresh().catch((e) => error(e.message));
$("closeEvidence").onclick = () => { $("evidencePanel").hidden = true; };
document.querySelectorAll("[data-question]").forEach(button => { button.onclick = () => { $("question").value = button.dataset.question; $("question").focus(); }; });
$("importForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData();
  f.append("file", $("file").files[0]);
  const metadata = {
    title: $("importTitle").value,
    author: $("importAuthor").value || null,
    chapter: $("importChapter").value || null,
    source_url: $("importUrl").value || null,
  };
  try {
    $("importNotice").textContent =
      JSON.stringify(
        await api(
          "/api/sources/upload?metadata=" +
            encodeURIComponent(JSON.stringify(metadata)),
          { method: "POST", body: f },
        ),
        null,
        2,
      ) + "\n已保存；执行 shijian reindex 后参与检索。";
    await refresh();
  } catch (e) {
    $("importNotice").textContent = e.message;
  }
};
$("pageForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    $("importNotice").textContent = JSON.stringify(
      await api("/api/sources/page", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: $("pageUrl").value }),
      }),
      null,
      2,
    );
    await refresh();
  } catch (e) {
    $("importNotice").textContent = e.message;
  }
};
Promise.all([
  refresh(),
  api("/api/config").then((c) => {
    const notes = [];
    if (c.model_error) notes.push(c.model_error);
    if (!c.web_ready) notes.push("独立搜索服务尚未配置，联网模式不可用。");
    if (notes.length) {
      $("configNotice").hidden = false;
      $("configNotice").textContent = notes.join(" ");
    }
  }),
]).catch((e) => error(e.message));
