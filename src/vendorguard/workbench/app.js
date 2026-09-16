/* 工作台前端: 原生 HTML/CSS/JS, 不依赖任何库或 CDN.
 *
 * 四条约定 (M4 方案 8.3):
 * - 所有来自接口的文本一律用 textContent 渲染, 不拼 innerHTML;
 * - Token 只放 sessionStorage, 收到 401 就清掉并回到登录状态;
 * - 一次操作只发一个请求, 请求进行中立刻禁用对应按钮;
 * - 可执行动作只看服务端的 allowed_actions, 前端不推导状态; 制度引用展开用本次运行
 *   保存的原文, 不在查看时重新检索 (方案 4.5).
 */

const TOKEN_KEY = "vendorguard.token";
const THEME_KEY = "vendorguard.theme";
const REVIEWS_API = "/api/reviews";

const STATUS_TEXT = {
  running: "审查中",
  question: "等待补充",
  completed: "报告完成",
  failed: "失败",
};
const STATUS_CLASS = { running: "", question: "warn", completed: "ok", failed: "danger" };
const KIND_TEXT = { answer: "报告", question: "追问", failed: "失败" };
const ACTION_TEXT = {
  supplement: "补充材料",
  feedback: "确认报告或要求重查",
  rerun: "用原记录重跑",
};
const BOUNDARY_NOTE =
  "初审报告不是准入决定: 它只说明这次材料与现行制度的核对结果; 这里的任何操作都不会调用准入审批, 也不会修改案件或供应商状态。";

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  current: null,
  reviews: [],
};

const dom = {
  error: document.getElementById("error-line"),
  loginView: document.getElementById("login-view"),
  loginForm: document.getElementById("login-form"),
  loginSubmit: document.getElementById("login-submit"),
  username: document.getElementById("username"),
  password: document.getElementById("password"),
  appView: document.getElementById("app-view"),
  currentUser: document.getElementById("current-user"),
  logout: document.getElementById("logout-button"),
  themeToggle: document.getElementById("theme-toggle"),
  historyState: document.getElementById("history-state"),
  historyList: document.getElementById("history-list"),
  reviewForm: document.getElementById("review-form"),
  reviewSubmit: document.getElementById("review-submit"),
  materialFile: document.getElementById("material-file"),
  requestText: document.getElementById("request-text"),
  referenceDate: document.getElementById("reference-date"),
  statusLine: document.getElementById("status-line"),
  actionHint: document.getElementById("action-hint"),
  feedbackNote: document.getElementById("feedback-note"),
  actionArea: document.getElementById("action-area"),
  supplementForm: document.getElementById("supplement-form"),
  supplementText: document.getElementById("supplement-text"),
  supplementSubmit: document.getElementById("supplement-submit"),
  feedbackForm: document.getElementById("feedback-form"),
  feedbackComment: document.getElementById("feedback-comment"),
  feedbackConfirm: document.getElementById("feedback-confirm"),
  feedbackRecheck: document.getElementById("feedback-recheck"),
  rerunButton: document.getElementById("rerun-button"),
  materialFacts: document.getElementById("material-facts"),
  roundsArea: document.getElementById("rounds-area"),
  reportArea: document.getElementById("report-area"),
  traceList: document.getElementById("trace-list"),
};

/* ---------- 小工具 ---------- */

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.className) node.className = options.className;
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) node.append(child);
  return node;
}

function setError(message) {
  dom.error.textContent = message || "";
  dom.error.hidden = !message;
}

function setBusy(button, busy, label) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.label;
}

function statusText(status) {
  return STATUS_TEXT[status] || status;
}

function formatTime(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function shortId(value) {
  return String(value).slice(0, 8);
}

function parseJson(text) {
  if (typeof text !== "string" || !text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch (cause) {
    return null;
  }
}

function prettyJson(value) {
  if (value === undefined || value === null) return "(无)";
  if (typeof value === "string") {
    const parsed = parseJson(value);
    return parsed === null ? value : JSON.stringify(parsed, null, 2);
  }
  return JSON.stringify(value, null, 2);
}

/* 定位符形状: "材料ID@page:N" 或 "user_supplement@round:N". */

function pageOf(locator) {
  const match = /^[^@]+@page:(\d+)$/.exec(String(locator));
  return match ? Number(match[1]) : null;
}

function supplementRoundOf(locator) {
  const match = /^user_supplement@round:(\d+)$/.exec(String(locator));
  return match ? Number(match[1]) : null;
}

/* ---------- 主题: 手动选择优先, 否则跟随系统 ---------- */

function systemTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "";
  } catch (cause) {
    // 隐私模式等拿不到 localStorage: 就当没手动选过
    return "";
  }
}

function resolvedTheme() {
  return storedTheme() || systemTheme();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  // 按钮显示"切过去会变成什么": 当前亮色就显示暗色
  dom.themeToggle.textContent = theme === "light" ? "暗色" : "亮色";
  dom.themeToggle.setAttribute("aria-label", theme === "light" ? "切换到暗色" : "切换到亮色");
}

function toggleTheme() {
  const next = resolvedTheme() === "light" ? "dark" : "light";
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (cause) {
    // 存不下就只对当前页面生效
  }
  applyTheme(next);
}

/* ---------- 会话 ---------- */

function forgetSession() {
  state.token = "";
  sessionStorage.removeItem(TOKEN_KEY);
}

function showLogin(message) {
  dom.appView.hidden = true;
  dom.loginView.hidden = false;
  dom.logout.hidden = true;
  dom.currentUser.textContent = "";
  dom.historyList.replaceChildren();
  state.current = null;
  state.reviews = [];
  if (message) setError(message);
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    forgetSession();
    showLogin("登录状态已失效, 请重新登录。");
    const failure = new Error("未认证");
    failure.unauthenticated = true;
    throw failure;
  }
  if (!response.ok) throw new Error(await errorMessage(response));
  return response;
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    const error = body && body.error;
    if (error && error.code) return `[${error.code}] ${error.message}`;
  } catch (cause) {
    // 响应不是统一错误体: 用状态码兜底
  }
  return `请求失败 (HTTP ${response.status})`;
}

/* ---------- 登录与列表 ---------- */

async function login(event) {
  event.preventDefault();
  setError("");
  setBusy(dom.loginSubmit, true, "登录中…");
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: dom.username.value.trim(),
        password: dom.password.value,
      }),
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    const body = await response.json();
    state.token = body.access_token;
    sessionStorage.setItem(TOKEN_KEY, state.token);
    dom.loginForm.reset();
    await enterApp();
  } catch (error) {
    setError(error.message);
  } finally {
    setBusy(dom.loginSubmit, false, "登录");
  }
}

async function enterApp() {
  dom.loginView.hidden = true;
  dom.appView.hidden = false;
  dom.logout.hidden = false;
  setError("");
  await Promise.all([loadMe(), loadReviews()]);
}

async function loadMe() {
  try {
    const response = await request("/auth/me");
    const user = await response.json();
    dom.currentUser.textContent = `${user.username} · ${user.role}`;
  } catch (error) {
    if (!error.unauthenticated) setError(error.message);
  }
}

async function loadReviews() {
  dom.historyState.hidden = false;
  dom.historyState.textContent = "加载中…";
  try {
    const response = await request(`${REVIEWS_API}?limit=20`);
    state.reviews = await response.json();
    renderHistory();
  } catch (error) {
    if (error.unauthenticated) return;
    dom.historyState.textContent = "列表加载失败。";
    setError(error.message);
  }
}

function renderHistory() {
  dom.historyList.replaceChildren();
  if (state.reviews.length === 0) {
    dom.historyState.hidden = false;
    dom.historyState.textContent = "还没有审查记录: 提交一份材料就能看到第一份报告。";
    return;
  }
  dom.historyState.hidden = true;
  for (const item of state.reviews) {
    const button = el(
      "button",
      { className: "history-item", attrs: { type: "button" } },
      [
        el("span", { className: "name" }, [
          el("span", {
            className: `badge ${STATUS_CLASS[item.status] || ""}`.trim(),
            text: statusText(item.status),
          }),
          el("span", { text: item.original_filename }),
        ]),
        el("span", {
          className: "meta",
          text: [
            formatTime(item.created_at),
            `发现 ${item.finding_count}`,
            item.retry_of_review_id ? `重跑自 ${shortId(item.retry_of_review_id)}` : "",
          ]
            .filter(Boolean)
            .join(" · "),
        }),
      ],
    );
    if (state.current && state.current.review_id === item.review_id) {
      button.setAttribute("aria-current", "true");
    }
    button.addEventListener("click", () => loadDetail(item.review_id));
    dom.historyList.append(button);
  }
}

/* ---------- 发起审查与详情 ---------- */

async function submitReview(event) {
  event.preventDefault();
  setError("");
  const file = dom.materialFile.files[0];
  if (!file) {
    setError("请选择一份 PDF 材料。");
    return;
  }
  const payload = new FormData();
  payload.set("material", file);
  payload.set("request_text", dom.requestText.value.trim());
  payload.set("reference_date", dom.referenceDate.value);
  setBusy(dom.reviewSubmit, true, "审查中…");
  dom.statusLine.textContent = "审查中: 这一轮要读取材料, 校验事实并检索制度。";
  dom.actionArea.hidden = true;
  try {
    const response = await request(REVIEWS_API, { method: "POST", body: payload });
    state.current = await response.json();
    renderDetail();
    await loadReviews();
  } catch (error) {
    if (error.unauthenticated) return;
    dom.statusLine.textContent = "这次审查没有跑完。";
    setError(error.message);
  } finally {
    setBusy(dom.reviewSubmit, false, "开始审查");
  }
}

async function loadDetail(reviewId) {
  setError("");
  dom.statusLine.textContent = "加载中…";
  try {
    const response = await request(`${REVIEWS_API}/${encodeURIComponent(reviewId)}`);
    state.current = await response.json();
    renderDetail();
    renderHistory();
  } catch (error) {
    if (error.unauthenticated) return;
    dom.statusLine.textContent = "详情加载失败。";
    setError(error.message);
  }
}

function renderDetail() {
  const record = state.current;
  if (!record) return;
  dom.statusLine.textContent = `${statusText(record.status)} · 更新于 ${formatTime(record.updated_at)}`;
  dom.feedbackNote.hidden = true;
  const actions = (record.allowed_actions || []).map((action) => ACTION_TEXT[action] || action);
  dom.actionHint.hidden = actions.length === 0;
  dom.actionHint.textContent = actions.length ? `可执行动作: ${actions.join(" / ")}` : "";
  applyActions(record);
  renderFacts(record);
  renderRounds(record);
  renderReport(record);
  renderTrace(record);
}

function applyActions(record) {
  const actions = record.allowed_actions || [];
  const has = (name) => actions.includes(name);
  const supplement = has("supplement");
  const feedback = has("feedback");
  const rerun = has("rerun");
  dom.supplementForm.hidden = !supplement;
  dom.feedbackForm.hidden = !feedback;
  dom.rerunButton.hidden = !rerun;
  dom.actionArea.hidden = !(supplement || feedback || rerun);
  if (!supplement) dom.supplementForm.reset();
  if (!feedback) dom.feedbackComment.value = "";
}

function renderFacts(record) {
  const rows = [
    ["上传文件名 (仅展示)", record.original_filename],
    ["材料 ID", record.material_id || "尚未登记"],
    ["页数", record.page_count ? `${record.page_count} 页` : "尚未登记"],
    ["材料指纹", record.material_sha256 ? record.material_sha256.slice(0, 16) : "尚未登记"],
    ["参考日期", record.reference_date],
    ["审查要求", record.request_text],
  ];
  if (record.supplements.length) {
    rows.push(["用户补充", record.supplements.join("\n")]);
  }
  if (record.retry_of_review_id) {
    rows.push(["重跑自", record.retry_of_review_id]);
  }
  if (record.feedback) {
    rows.push([
      "报告反馈",
      `${record.feedback.decision === "confirmed" ? "已确认" : "已要求重查"}${
        record.feedback.comment ? ` · ${record.feedback.comment}` : ""
      }`,
    ]);
  }
  if (record.failure) {
    rows.push([
      "失败",
      `[${record.failure.code}] ${record.failure.message}${
        record.failure.retryable ? " (可用原记录重跑)" : " (这类失败不能用原记录重跑)"
      }`,
    ]);
  }
  dom.materialFacts.replaceChildren(
    ...rows.flatMap(([label, value]) => [
      el("dt", { text: label }),
      el("dd", {
        text: value,
        className: label === "材料指纹" || label === "材料 ID" ? "mono" : "",
      }),
    ]),
  );
}

function renderRounds(record) {
  dom.roundsArea.replaceChildren();
  if (record.rounds.length === 0) {
    dom.roundsArea.append(el("p", { className: "muted small", text: "这次没有跑出轮次。" }));
    return;
  }
  for (const round of record.rounds) {
    const head = `第 ${round.number} 轮 · ${KIND_TEXT[round.kind] || round.kind} · ${round.model} · ` +
      `模型请求 ${round.model_requests} 次 / 工具调用 ${round.tool_attempts} 次`;
    dom.roundsArea.append(
      el("article", { className: "round" }, [
        el("p", { className: "head", text: head }),
        el("p", { className: "line", text: round.text }),
      ]),
    );
  }
}

/* ---------- 报告: 发现, 材料来源, 制度引用 ---------- */

function renderReport(record) {
  dom.reportArea.replaceChildren();
  const withReport = [...record.rounds].reverse().find((round) => round.report);
  const report = withReport ? withReport.report : null;
  if (!report) {
    dom.reportArea.append(
      el("p", {
        className: "muted small",
        text: record.status === "failed" ? "这次运行没有产出报告。" : "还没有结构化报告。",
      }),
    );
    return;
  }
  if (report.insufficient_evidence) {
    dom.reportArea.append(
      el("p", { className: "warn-text", text: `依据不足: ${report.missing_reason}` }),
    );
  }
  for (const finding of report.findings) {
    dom.reportArea.append(
      el("article", { className: "finding" }, [
        el("p", { className: "summary", text: finding.summary }),
        el("p", {
          className: "line",
          text: `已核对事实: ${finding.fact_fields.join(", ") || "无"}`,
        }),
        el("p", {
          className: "line",
          text: `规则结果: ${
            finding.rule_results.map((item) => `${item.rule_id}=${item.result}`).join(", ") || "无"
          }`,
        }),
        renderMaterialSources(finding, record),
        renderPolicyCitations(finding, withReport),
      ]),
    );
  }
  if (report.notes) {
    dom.reportArea.append(el("p", { className: "muted small", text: `备注: ${report.notes}` }));
  }
  dom.reportArea.append(el("p", { className: "note", text: BOUNDARY_NOTE }));
}

function renderMaterialSources(finding, record) {
  const box = el("div", { className: "sources" }, [
    el("p", { className: "line", text: "材料来源" }),
  ]);
  if (finding.material_sources.length === 0) {
    box.append(el("p", { className: "line", text: "无" }));
    return box;
  }
  for (const locator of finding.material_sources) {
    const roundNumber = supplementRoundOf(locator);
    if (roundNumber !== null) {
      const text = record.supplements[roundNumber - 1];
      box.append(
        el("div", { className: "source-item" }, [
          el("span", { className: "badge", text: `用户补充 第 ${roundNumber} 轮` }),
          el("p", {
            className: "quote",
            text: text || "(本次记录里没有保存这一轮补充的原文)",
          }),
        ]),
      );
      continue;
    }
    const page = pageOf(locator);
    const label = page === null ? "打开材料" : `查看材料 第 ${page} 页`;
    const button = el("button", {
      className: "button small-button",
      attrs: { type: "button" },
      text: label,
    });
    button.addEventListener("click", () => openMaterial(record.review_id, page, label, button));
    box.append(
      el("div", { className: "source-item" }, [
        el("span", { className: "mono", text: locator }),
        button,
      ]),
    );
  }
  return box;
}

function renderPolicyCitations(finding, round) {
  const box = el("div", { className: "citations" }, [
    el("p", { className: "line", text: "制度引用" }),
  ]);
  if (finding.policy_citations.length === 0) {
    box.append(el("p", { className: "line", text: "无" }));
    return box;
  }
  const saved = savedNodesOf(round);
  for (const nodeKey of finding.policy_citations) {
    const node = saved.get(nodeKey);
    if (!node) {
      box.append(
        el("p", { className: "line", text: `${nodeKey}: 本次运行没有保存这个节点的原文` }),
      );
      continue;
    }
    box.append(
      el("details", { className: "citation" }, [
        el("summary", {
          text: `${node.title || node.node_key} · ${(node.locator || []).join(" > ")}`,
        }),
        el("p", { className: "line mono", text: node.node_key }),
        el("p", { className: "line", text: `制度版本: ${node.edition_key}` }),
        el("pre", { className: "text-block", text: node.text }),
      ]),
    );
  }
  return box;
}

/* 本次运行保存的条款原文: 只读这一轮的 search_policy 事件, 不重新检索 (方案 4.5). */

function savedNodesOf(round) {
  const nodes = new Map();
  if (!round) return nodes;
  for (const event of round.tool_events || []) {
    if (event.name !== "search_policy" || event.status !== "ok") continue;
    const returned = (event.detail && event.detail.nodes) || [];
    for (const node of returned) nodes.set(node.node_key, node);
  }
  return nodes;
}

async function openMaterial(reviewId, page, label, button) {
  setError("");
  setBusy(button, true, "打开中…");
  try {
    const response = await request(
      `${REVIEWS_API}/${encodeURIComponent(reviewId)}/material`,
    );
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    // 页码定位靠浏览器的 PDF 阅读器: #page=N 让新标签页直接停在那一页
    window.open(page === null ? url : `${url}#page=${page}`, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    if (!error.unauthenticated) setError(error.message);
  } finally {
    setBusy(button, false, label);
  }
}

/* ---------- 工具轨迹 ---------- */

function renderTrace(record) {
  dom.traceList.replaceChildren();
  const items = record.rounds.flatMap((round) =>
    (round.tool_events || []).map((event) => ({ round: round.number, event })),
  );
  if (items.length === 0) {
    dom.traceList.append(el("li", { className: "muted small", text: "这次运行没有工具事件。" }));
    return;
  }
  for (const { round, event } of items) {
    const failed = event.status !== "ok";
    const details = el("details", { className: `trace-item${failed ? " failed" : ""}` });
    if (failed) details.setAttribute("open", "");
    details.append(
      el("summary", {}, [
        el("span", { className: "mono", text: String(event.name || "(未知)") }),
        el("span", { className: `badge ${failed ? "danger" : "ok"}`, text: failed ? "失败" : "成功" }),
        el("span", { className: "muted small", text: `第 ${round} 轮 · ${summarizeCall(event)}` }),
      ]),
      el("pre", { className: "text-block", text: `参数: ${prettyJson(event.arguments)}` }),
      el("pre", { className: "text-block", text: `结果: ${prettyJson(event.detail)}` }),
    );
    dom.traceList.append(el("li", {}, [details]));
  }
}

function summarizeCall(event) {
  const args = parseJson(event.arguments);
  if (args && typeof args === "object") {
    const parts = Object.entries(args)
      .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
      .slice(0, 2)
      .map(([key, value]) => `${key}=${String(value).slice(0, 40)}`);
    if (parts.length) return parts.join(" ");
  }
  return typeof event.arguments === "string" && event.arguments.trim() ? "参数见下" : "无参数";
}

/* ---------- 补充, 反馈, 重跑 ---------- */

async function submitSupplement(event) {
  event.preventDefault();
  const record = state.current;
  if (!record) return;
  if (!dom.supplementText.value.trim()) {
    setError("补充内容不能为空。");
    return;
  }
  setError("");
  setBusy(dom.supplementSubmit, true, "补充提交中…");
  try {
    const response = await request(
      `${REVIEWS_API}/${encodeURIComponent(record.review_id)}/supplements`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: dom.supplementText.value }),
      },
    );
    state.current = await response.json();
    dom.supplementForm.reset();
    renderDetail();
    await loadReviews();
  } catch (error) {
    if (error.unauthenticated) return;
    setError(error.message);
  } finally {
    setBusy(dom.supplementSubmit, false, "提交补充并重新审查");
  }
}

async function submitFeedback(decision) {
  const record = state.current;
  if (!record) return;
  const confirmed = decision === "confirmed";
  const button = confirmed ? dom.feedbackConfirm : dom.feedbackRecheck;
  const label = confirmed ? "确认报告" : "要求重查";
  setError("");
  setBusy(button, true, "提交中…");
  try {
    const response = await request(
      `${REVIEWS_API}/${encodeURIComponent(record.review_id)}/feedback`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, comment: dom.feedbackComment.value }),
      },
    );
    const body = await response.json();
    state.current = body;
    renderDetail();
    dom.feedbackNote.hidden = false;
    dom.feedbackNote.textContent = body.business_state_changed
      ? "反馈已记录。"
      : `反馈已记录 (${confirmed ? "确认报告" : "要求重查"}); 没有改动任何业务状态。`;
    await loadReviews();
  } catch (error) {
    if (error.unauthenticated) return;
    setError(error.message);
  } finally {
    setBusy(button, false, label);
  }
}

async function submitRerun() {
  const record = state.current;
  if (!record) return;
  setError("");
  setBusy(dom.rerunButton, true, "重跑中…");
  try {
    const response = await request(
      `${REVIEWS_API}/${encodeURIComponent(record.review_id)}/reruns`,
      { method: "POST" },
    );
    state.current = await response.json();
    renderDetail();
    await loadReviews();
  } catch (error) {
    if (error.unauthenticated) return;
    setError(error.message);
  } finally {
    setBusy(dom.rerunButton, false, "用原记录重跑");
  }
}

/* ---------- 启动 ---------- */

function init() {
  applyTheme(resolvedTheme());
  dom.themeToggle.addEventListener("click", toggleTheme);
  dom.loginForm.addEventListener("submit", login);
  dom.reviewForm.addEventListener("submit", submitReview);
  dom.supplementForm.addEventListener("submit", submitSupplement);
  dom.feedbackConfirm.addEventListener("click", () => submitFeedback("confirmed"));
  dom.feedbackRecheck.addEventListener("click", () => submitFeedback("recheck_requested"));
  dom.rerunButton.addEventListener("click", submitRerun);
  dom.logout.addEventListener("click", () => {
    forgetSession();
    showLogin("已退出登录。");
  });
  if (state.token) {
    enterApp().catch(() => showLogin("登录状态已失效, 请重新登录。"));
  } else {
    showLogin("");
  }
}

init();
