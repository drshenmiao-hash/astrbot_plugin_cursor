const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const dimensions = { model: "模型", session: "匿名会话", code: "结果" };
const transports = { auto: "自动检测", local: "本地隔离", ssh: "SSH 运行器" };
const codes = {
  ok: "成功",
  cancelled: "请求取消",
  closed: "插件停止",
  not_logged_in: "Cursor 未登录",
  quota_exhausted: "额度不足",
  rate_limited: "触发限流",
  queue_timeout: "排队超时",
  context_limit: "上下文超限",
  startup_timeout: "启动超时",
  timeout: "响应超时",
  connection_stalled: "连接停滞",
  ping_timeout: "心跳超时",
  upstream_retryable: "上游短暂故障",
  cli_failed: "CLI 请求失败",
  model_unavailable: "模型不可用",
  image_unsupported: "不支持图片输入",
  image_unavailable: "上游拒绝图片",
  ask_unavailable: "Ask 模式不可用",
  runner_in_use: "运行器被占用",
  ssh_auth_denied: "SSH 认证失败",
  ssh_host_key_mismatch: "SSH 主机公钥不匹配",
  ssh_unreachable: "SSH 连接失败",
  sandbox_unavailable: "隔离环境不可用",
};
const count = (value) => number.format(value ?? 0);
let current;
let csvSelection = 0;

function write(id, value) {
  byId(id).textContent = value;
}

function bytes(value) {
  if (value < 1024) return `${count(value)} B`;
  if (value < 1024 ** 2) return `${count(value / 1024)} KiB`;
  return `${count(value / 1024 ** 2)} MiB`;
}

function resultLabel(code) {
  return Object.hasOwn(codes, code) ? codes[code] : code || "—";
}

function alert(message) {
  write("error", message);
  byId("error").hidden = !message;
}

function table(id, rows, columns) {
  const body = byId(id);
  body.replaceChildren();
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = columns;
    cell.textContent = "该时段暂无请求";
    return;
  }
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.insertCell().textContent = value;
  }
}

async function showCursorCsv(event) {
  const input = event.currentTarget;
  const file = input.files[0];
  if (!file) return;
  input.value = "";
  const selection = ++csvSelection;
  const error = byId("csv-error");
  const result = byId("csv-result");
  result.hidden = error.hidden = true;
  write("csv-total", "");
  byId("csv-models").replaceChildren();
  write("csv-status", "正在读取 CSV…");
  try {
    if (file.size > 20 * 1024 ** 2) {
      throw new Error("CSV 超过 20 MiB，请在 Cursor 用量页缩短导出时段。");
    }
    const source = await file.text().catch(() => {
      throw new Error("无法读取 CSV 文件。");
    });
    if (selection !== csvSelection) return;
    const report = parseCursorUsageCsv(source);
    const missing = report.events - report.reported;
    write("csv-status", report.events
      ? `文件中有 ${count(report.events)} 条用量事件${missing ? `，其中 ${count(missing)} 条未提供 Token 数` : ""}。`
      : "文件中没有用量事件。");
    write("csv-total", report.reported ? count(report.total) : "—");
    if (!report.events) return;
    table("csv-models", report.models.map((model) => [
      model.label || "模型未注明",
      count(model.events),
      count(model.reported),
      model.reported ? count(model.total) : "—",
    ]), 4);
    result.hidden = false;
  } catch (failure) {
    if (selection !== csvSelection) return;
    write("csv-status", "");
    error.textContent = failure.message;
    error.hidden = false;
  }
}

function renderGroups() {
  if (!current) return;
  const dimension = byId("dimension").value;
  write("dimension-title", dimensions[dimension]);
  const rows = current.groups[dimension].map((row) => {
    let label = row.label;
    if (dimension === "code") label = resultLabel(label);
    if (!label) label = dimension === "session" ? "未识别会话" : "未记录 ACP 发送";
    return [label, count(row.requests), count(row.failed), count(row.reused)];
  });
  table("groups", rows, 4);
}

function renderTrend(days) {
  const max = Math.max(1, ...days.map((day) => day.requests));
  const bars = document.createDocumentFragment();
  for (const day of days) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.title = `${day.label} · ${count(day.requests)} 次 · ${count(day.failed)} 次失败`;
    for (const [quantity, style] of [[day.requests - day.failed, ""], [day.failed, "fail"]]) {
      const segment = document.createElement("span");
      segment.className = style;
      segment.style.height = `${quantity / max * 100}%`;
      bar.append(segment);
    }
    bars.append(bar);
  }
  byId("chart").replaceChildren(bars);
  table("daily", days.map((day) => [day.label, count(day.requests), count(day.failed)]), 3);
}

const diagnosticLabels = { passed: "通过", failed: "失败", skipped: "跳过", unknown: "无法确认" };
const diagnosticSteps = { runner: "Runner", cli: "Cursor CLI", acp: "ACP 握手", auth: "认证", cleanup: "诊断资源清理" };
const utcTime = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" });
let diagnosticBridge;
let diagnosing = false;

function recentRequest(request) {
  byId("recent-status").hidden = Boolean(request);
  byId("recent-request").hidden = !request;
  byId("recent-reason").hidden = true;
  if (!request) {
    write("recent-status", "暂无请求");
    return;
  }
  const recordedAt = Date.parse(request.finished_at);
  write("recent-time", Number.isNaN(recordedAt) ? request.day || "未记录" : utcTime.format(recordedAt));
  const notSent = request.delivery === "not_sent";
  write("recent-model", request.model_id || (notSent ? "未调用" : "未记录"));
  write("recent-runner", transports[request.transport] || "未记录");
  write("recent-result", request.outcome);
  write("recent-duration", `${count(request.duration_ms)} ms`);
  const delivery = notSent && !request.outcome.includes("未发送")
    ? "Cursor 请求未发送。"
    : request.delivery === "unconfirmed" ? "无法确认 Cursor 是否收到请求。" : "";
  const reason = [delivery, request.reason].filter(Boolean).join(" ");
  write("recent-reason", reason);
  byId("recent-reason").hidden = !reason;
}

function setDiagnosticButtons() {
  for (const id of ["check-connection", "test-model"]) byId(id).disabled = diagnosing || !diagnosticBridge;
}

function diagnosticDetails(list, entries) {
  list.replaceChildren();
  for (const [label, value] of entries) {
    const row = document.createElement("div");
    const title = document.createElement("dt");
    const description = document.createElement("dd");
    title.textContent = label;
    description.textContent = value;
    row.append(title, description);
    list.append(row);
  }
  list.hidden = false;
}

async function diagnose(action) {
  if (diagnosing || !diagnosticBridge) return;
  diagnosing = true;
  setDiagnosticButtons();
  const status = byId(`${action}-status`);
  const details = byId(`${action}-result`);
  details.hidden = true;
  status.hidden = false;
  status.textContent = action === "check" ? "正在检查连接…" : "正在测试模型…";
  try {
    const data = await diagnosticBridge.apiPost("usage/dashboard", { action });
    if (action === "check") {
      status.textContent = `检查完成 · ${transports[data.transport] || "Runner 未确认"}`;
      diagnosticDetails(details, Object.entries(diagnosticSteps)
        .filter(([key]) => data.steps[key])
        .map(([key, title]) => [title, `${diagnosticLabels[data.steps[key].status]} · ${data.steps[key].reason}`]));
    } else {
      status.textContent = `${diagnosticLabels[data.status]} · ${data.reason}`;
      diagnosticDetails(details, [
        ["实际 ACP 模型", data.model_id || "未确认"],
        ["Runner", transports[data.transport] || "未确认"],
        ["耗时", Number.isFinite(data.duration_ms) ? `${count(data.duration_ms)} ms` : "—"],
      ]);
    }
  } catch (error) {
    status.textContent = error?.message || "诊断未完成，请稍后重试。";
  } finally {
    diagnosing = false;
    setDiagnosticButtons();
  }
}

function render(data) {
  current = data;
  recentRequest(data.recent_request);
  const stats = data.totals;
  const live = data.runtime;
  write("model", data.model || "未配置");
  write("transport", transports[data.transport] || "—");
  const startupError = data.startup_error;
  const runtimeState = live.process_alive ? "CLI 运行中" : data.validated ? "等待请求" : "模型待校验";
  write("runtime-state", startupError ? "启动失败" : runtimeState);
  byId("startup-error").hidden = !startupError;
  if (startupError) write("startup-error", `[${startupError.code}] ${startupError.message}`);
  write("active", `${count(live.active)} / ${count(data.concurrency_limit)}`);
  write("queued", count(live.queue_depth));
  write("sessions", count(live.cached_sessions));
  write("last-failure", resultLabel(live.last_failure));
  write("requests", count(stats.requests));
  write("failed", `失败 ${count(stats.failed)} 次`);
  write("success", stats.requests ? `${count(stats.success / stats.requests * 100)}%` : "—");
  write("success-count", `成功 ${count(stats.success)} 次`);
  write("duration", stats.requests ? `${count(stats.duration_ms / stats.requests)} ms` : "—");
  write("queue-time", stats.requests ? `平均排队 ${count(stats.queue_ms / stats.requests)} ms` : "平均排队 —");
  write("bytes", `${bytes(stats.sent_input_bytes)} / ${bytes(stats.output_bytes)}`);
  write("reuse", `会话复用 ${count(stats.reused)} 次`);
  write("input-bytes", bytes(stats.input_bytes));
  write("wire-bytes", bytes(stats.wire_bytes));
  write("process-reused", count(stats.process_reused));
  write("interval", `${data.start} 至 ${data.end}`);
  write("axis-start", data.start.slice(5));
  write("axis-end", data.end.slice(5));
  renderTrend(data.trend);
  renderGroups();
  byId("dashboard").hidden = false;
  if (data.write_failures) {
    alert(`本次运行有 ${count(data.write_failures)} 次统计写入失败，数据可能不完整。`);
  }
}

async function refresh() {
  const button = byId("refresh");
  const range = byId("range");
  button.disabled = range.disabled = true;
  alert("");
  byId("status").hidden = false;
  write("status", "正在读取本地统计…");
  try {
    const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!bridge || window.parent === window) {
      throw new Error("请从 AstrBot 插件管理页打开此页面。");
    }
    await bridge.ready();
    diagnosticBridge = bridge;
    byId("connectivity").hidden = false;
    setDiagnosticButtons();
    const data = await bridge.apiPost("usage/dashboard", { days: Number(range.value) });
    if (!data?.totals) throw new Error("无法读取统计，请刷新重试。");
    render(data);
    const total = data.totals.requests;
    write("status", total
      ? `已更新 · ${new Intl.DateTimeFormat("zh-CN", {timeStyle: "medium"}).format(new Date())}`
      : "该时段暂无用量记录。Cursor 处理请求后可刷新查看。");
  } catch (error) {
    byId("dashboard").hidden = true;
    byId("recent-request").hidden = true;
    byId("recent-reason").hidden = true;
    byId("recent-status").hidden = false;
    write("recent-status", "读取请求记录失败。");
    byId("status").hidden = true;
    alert(error?.message || "读取失败，请刷新重试。");
  } finally {
    button.disabled = range.disabled = false;
  }
}

byId("check-connection").addEventListener("click", () => diagnose("check"));
byId("test-model").addEventListener("click", () => diagnose("test"));
byId("refresh").addEventListener("click", refresh);
byId("range").addEventListener("change", refresh);
byId("dimension").addEventListener("change", renderGroups);
byId("cursor-csv").addEventListener("change", showCursorCsv);
byId("select-usage-url").addEventListener("click", () => {
  const url = byId("official-usage-url");
  url.focus();
  url.select();
});
refresh();
