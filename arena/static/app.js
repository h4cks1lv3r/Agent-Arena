"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const COLORS = ["#66e2c4", "#baa3ff", "#f3c17b", "#77b9fa", "#f38ec9", "#bdd781"];
  const TERMINAL = new Set(["filled", "canceled", "cancelled", "expired", "rejected"]);
  const PROVIDERS = { rules: "Rules baseline", openai: "OpenAI", anthropic: "Claude", manual: "Manual observation" };
  const MONEY = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let state = null;
  let token = "";
  let authenticated = false;
  let remoteSession = false;
  let refreshInFlight = false;
  let lastSnapshotReceivedAt = 0;
  let snapshotFailed = false;
  let lastSnapshotError = "";
  let lastRenderedPayload = "";
  let stateEpoch = 0;
  let loginBusy = false;
  let serverJob = null;
  const submittedJobs = new Map();
  const agentControlBusy = new Set();
  let busy = false;
  let haltBusy = false;
  let restartPreferenceBusy = false;
  let haltEpoch = 0;
  let formLoaded = false;
  let currentView = "overview";
  let resolutionTarget = null;
  let noticeTimer;

  const TWO_AGENT_PRESET = [
    { id: "openai", name: "Astra", provider: "openai", model: "gpt-6-astra", weight: 1, input_price: 10, output_price: 50 },
    { id: "claude", name: "Claude", provider: "anthropic", model: "claude-opus-5-5", weight: 1, input_price: 4, output_price: 20 }
  ];

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const money = (value) => MONEY.format(number(value));
  const pct = (value, signed = false) => `${signed && number(value) > 0 ? "+" : ""}${number(value).toFixed(2)}%`;
  const tone = (value) => number(value) > 0.00001 ? "positive" : number(value) < -0.00001 ? "negative" : "muted";
  const qty = (value) => number(value).toLocaleString("en-US", { maximumFractionDigits: 6 });
  const sum = (items, key) => items.reduce((total, item) => total + number(item[key]), 0);
  const agentName = (id) => state?.agents?.find((agent) => agent.id === id)?.name || id || "System";
  const autonomousMode = () => state?.experiment?.agent_mode === "autonomous";
  const providerLabel = (provider) => ["openai", "anthropic"].includes(provider) ? `${PROVIDERS[provider]} ${autonomousMode() ? "strategist" : "reviewer"}` : (PROVIDERS[provider] || provider);

  function timestamp(value, compact = false) {
    if (!value) return "Not yet recorded";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, compact ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function message(text, isError = false) {
    clearTimeout(noticeTimer);
    const target = $(isError ? "error-banner" : "notice-banner");
    $(isError ? "notice-banner" : "error-banner").hidden = true;
    target.textContent = text;
    target.hidden = false;
    if (!isError) noticeTimer = setTimeout(() => { target.hidden = true; }, 9000);
  }

  async function request(path, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), path === "/api/export" ? 60000 : 15000);
    try {
      const options = { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: { Accept: "application/json" } };
      if (payload !== undefined) {
        options.method = "POST";
        options.headers["Content-Type"] = "application/json";
        options.headers["X-Arena-Token"] = token;
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(path, options);
      let data;
      try { data = await response.json(); } catch { throw new Error(`The server returned an unreadable response (${response.status}).`); }
      if (!response.ok || data.error) {
        const error = new Error(data.error || `Request failed (${response.status}).`);
        error.status = response.status;
        error.loginRequired = response.status === 401 && Boolean(data.login_required || remoteSession);
        if (error.loginRequired && path !== "/api/login") showLogin("Your session has ended. Enter your server access token to reconnect.");
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Its outcome may be unknown. Refresh and reconcile before retrying a paper order action.");
      if (error instanceof TypeError) throw new Error("Cannot reach your arena server. Agents and paper orders may still be active. Reconnect to check state, or use the broker website if needed.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  async function mutate(path, payload = {}, success = "", resetForm = false) {
    if (busy || !authenticated) return;
    const startingHaltEpoch = haltEpoch;
    busy = true;
    $("error-banner").hidden = true;
    updateControls();
    try {
      const data = await request(path, payload);
      // An independent halt can finish while a cycle is awaiting a provider.
      // Never paint an older cycle response over the newer halt state.
      const latest = startingHaltEpoch !== haltEpoch ? await request("/api/state") : data;
      stateEpoch++;
      state = latest.state || latest;
      serverJob = state.server_job || latest.job || null;
      if (latest.job) submittedJobs.set(latest.job.id, success);
      lastSnapshotReceivedAt = Date.now();
      snapshotFailed = false;
      if (resetForm) formLoaded = false;
      render();
      if (success && startingHaltEpoch === haltEpoch) {
        message(latest.job ? "Request accepted. Check the operation status for its result. Monitoring and stop controls remain available." : success);
        reportJobResult();
      }
    } catch (error) { message(error.message, true); }
    finally {
      busy = false;
      updateControls();
    }
  }

  async function haltIndependently() {
    if (haltBusy || !authenticated) return;
    haltBusy = true;
    haltEpoch++;
    $("halt-button").disabled = true;
    message("Halt requested. Waiting for the server to confirm; existing broker orders remain active.");
    try {
      // Deliberately bypass the normal action mutex. A slow cycle must never
      // prevent the operator from requesting a persistent halt.
      const data = await request("/api/halt", {});
      stateEpoch++;
      state = data.state || data;
      render();
      message("Automation halted, including automatic exits. Existing broker orders remain; cancel orders or request closes separately.");
    } catch (error) {
      message(`Halt was not confirmed. ${error.message} Use Stop_New_Entries.bat or the broker paper account website.`, true);
    } finally {
      haltBusy = false;
      updateControls();
    }
  }

  async function changeAgentControl(agentId, pause) {
    if (!authenticated || agentControlBusy.has(agentId)) return;
    agentControlBusy.add(agentId);
    if (pause) haltEpoch++;
    const startingHaltEpoch = haltEpoch;
    updateControls();
    try {
      // Agent pauses are independent of normal mutations and long AI cycles.
      const data = await request(pause ? "/api/agent/halt" : "/api/agent/resume", { agent_id: agentId });
      const latest = startingHaltEpoch !== haltEpoch ? await request("/api/state") : data;
      stateEpoch++;
      state = latest.state || latest;
      serverJob = state.server_job || data.job || serverJob;
      if (data.job) submittedJobs.set(data.job.id, `${agentName(agentId)} resumed. Global controls and the review schedule still apply.`);
      render();
      if (startingHaltEpoch !== haltEpoch) return;
      if (data.job) { message("Resume check accepted. Check the operation status for its result."); reportJobResult(); }
      else message(pause ? `${agentName(agentId)} paused. Automatic research, entries and exits stop; existing paper orders remain.` : `${agentName(agentId)} resumed. Global controls and the agent's review schedule still apply.`);
    } catch (error) {
      if (!error.loginRequired) message(`${pause ? "Pause" : "Resume"} was not confirmed. ${error.message}`, true);
    } finally { agentControlBusy.delete(agentId); updateControls(); }
  }

  function showLogin(reason = "") {
    authenticated = false;
    token = "";
    $("app-content").hidden = true;
    $("freshness-bar").hidden = true;
    $("loading").hidden = true;
    $("login-overlay").hidden = false;
    document.querySelector(".app-shell").inert = true;
    $("login-error").textContent = reason;
    $("login-error").hidden = !reason;
    $("logout-button").hidden = true;
    $("access-token").focus();
  }

  function acceptSession(session) {
    if (!session.token) throw new Error("The server did not provide a session token. Reload to reconnect.");
    token = session.token;
    authenticated = true;
    remoteSession = Boolean(session.remote);
    $("login-overlay").hidden = true;
    document.querySelector(".app-shell").inert = false;
    $("logout-button").hidden = !remoteSession;
    $("access-mode-label").textContent = remoteSession ? "Private server · Paper only" : "Local · Paper only";
    $("login-error").hidden = true;
    $("access-token").value = "";
    $("loading").hidden = Boolean(state);
  }

  function setView(view) {
    if (!["overview", "research", "activity", "setup"].includes(view)) view = "overview";
    currentView = view;
    document.querySelectorAll(".view").forEach((el) => { el.hidden = el.id !== `view-${view}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    const labels = {
      overview: ["The arena", "Two independent agents. One transparent competition."],
      research: ["Inside each strategy", "Inspect each agent's research, plan, evidence, and next review."],
      activity: ["The evidence", "Trace each decision from its recorded inputs to the resulting order."],
      setup: ["Your experiment", "Set the boundaries before interpreting the results."]
    };
    $("page-title").textContent = labels[view][0];
    $("page-subtitle").textContent = labels[view][1];
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  }

  function render() {
    if (!state?.experiment || !Array.isArray(state.agents)) throw new Error("The server state is incomplete. Refresh before taking another action.");
    const experiment = state.experiment;
    const agents = state.agents;
    const demo = experiment.mode === "demo";
    const equity = sum(agents, "equity");
    const capital = number(experiment.total_capital);
    const pnl = equity - capital;
    const fees = sum(agents, "fees");
    const modelCost = sum(agents, "model_cost");
    const pending = (state.orders || []).filter((order) => !TERMINAL.has(String(order.status).toLowerCase()));
    const unknown = pending.filter((order) => String(order.status).toLowerCase().includes("unknown"));
    $("loading").hidden = true;
    $("app-content").hidden = false;
    $("source-badge").textContent = demo ? "SYNTHETIC DEMO" : "ALPACA PAPER";
    $("source-badge").className = `badge ${demo ? "neutral" : "purple"}`;
    $("scope-description").textContent = demo ? "Synthetic prices and simulated fills. No real funds or AI calls." : "Broker paper orders use virtual funds. Enabled model API calls may cost real money.";
    $("strategy-scope").textContent = autonomousMode() ? "AI agents choose strategies, sizes and exits for broker-verified, eligible US stocks and ETFs. Only the rules baseline uses SMA20/SMA50. No borrowing, shorts, options, or crypto." : "Legacy mode: AI agents review entries from the shared SMA20/SMA50 strategy on SPY, QQQ and IWM. Switch to autonomous strategies in a new experiment for independent research and trade planning.";
    $("first-run").hidden = number(experiment.step) > 0 || (state.decisions || []).length > 0;
    $("onboarding-title").textContent = demo ? "Synthetic mode does not run AI agents." : "Connect your agents. Start the competition.";
    $("onboarding-copy").textContent = demo ? "This mode uses invented prices and only trades rules agents. Choose Alpaca paper accounts to run real model research with virtual broker funds." : `${money(capital)} total is split across ${agents.length} agents by weight. Configure separate paper accounts and model keys on the server, then reconcile and enable automation.`;
    $("total-equity").textContent = money(equity);
    $("total-change").textContent = `${pnl >= 0 ? "+" : "−"}${money(Math.abs(pnl))} (${pct(capital ? pnl / capital * 100 : 0, true)}) from ${money(capital)}`;
    $("total-change").className = `metric-detail ${tone(pnl)}`;
    $("target-value").textContent = money(experiment.target);
    const multiple = capital ? number(experiment.target) / capital : 0;
    $("target-detail").textContent = `${multiple.toFixed(multiple % 1 ? 1 : 0)}× starting capital · goal, not a forecast`;
    const tradingPnl = agents.reduce((total, agent) => total + number(agent.trading_pnl, number(agent.net_pnl) + number(agent.model_cost)), 0);
    const includesModelCosts = Boolean(experiment.loss_limit_includes_model_costs);
    const lossPnl = includesModelCosts ? pnl : tradingPnl;
    $("loss-remaining").textContent = money(Math.max(0, number(experiment.loss_limit) + Math.min(0, lossPnl)));
    $("loss-detail").textContent = `${money(experiment.loss_limit)} ${includesModelCosts ? "net-loss threshold · includes model costs" : "trading-loss threshold · API costs use a separate budget"}`;
    $("total-cost").textContent = money(fees + modelCost);
    $("cost-detail").textContent = `${money(fees)} simulated fees + ${money(modelCost)} estimated model charges`;
    const statusText = { ready: "ENTRIES ENABLED", paused: "PAUSED", halted: "HALTED", recovering: "CHECKING ACCOUNTS" };
    $("system-status").textContent = statusText[experiment.status] || String(experiment.status || "UNKNOWN").toUpperCase();
    $("system-status").className = `badge ${experiment.status === "halted" ? "danger" : experiment.status === "ready" ? "" : "warn"}`;
    $("halt-reason").hidden = !experiment.halt_reason;
    $("halt-reason").textContent = experiment.halt_reason || "";
    $("pending-count").textContent = String(pending.length);
    $("unknown-count").textContent = String(unknown.length);
    $("unknown-count").className = unknown.length ? "negative" : "";
    $("market-source").textContent = state.market?.source || (demo ? "Synthetic · not yet run" : "No data received");
    $("market-as-of").textContent = timestamp(state.market?.as_of, true);
    $("app-version").textContent = state.version || "";
    $("chart-observations").textContent = `${(state.history || []).length} chart points${state.view_limits?.truncated?.history ? ` of ${state.view_limits.full_counts.history} observations` : ""}`;
    $("chart-caption").textContent = `${demo ? "Synthetic demo only. Inactive AI agents are shown flat. " : "Prospective paper results. "}${state.view_limits?.truncated?.history ? "Chart samples the full period and keeps bucket highs and lows. Full records are in the export. " : ""}Returns include estimated execution and model costs. Taxes not calculated.`;
    renderScoreboard();
    renderAgents();
    renderChart();
    renderOrders();
    renderDecisions();
    renderEvents();
    renderConnections();
    renderResearch();
    if (!formLoaded) loadForm();
    updateControls();
    updateFreshness();
  }

  function updateControls() {
    if (!state) return;
    const experiment = state.experiment;
    const demo = experiment.mode === "demo";
    $("demo-controls").hidden = !demo;
    $("paper-controls").hidden = demo;
    const job = state.server_job || serverJob;
    const jobActive = ["queued", "running"].includes(job?.status);
    const operationBusy = busy || jobActive;
    const intraday = experiment.trading_style === "aggressive_intraday";
    $("trading-style-button").disabled = operationBusy || demo || !autonomousMode();
    $("trading-style-button").textContent = intraday ? "Use balanced trading" : "Use aggressive intraday";
    $("trading-style-detail").textContent = intraday
      ? "Aggressive intraday: 1-minute AI reviews, exit checks about every 5 seconds, 30-minute maximum holds, and stock exits 10 minutes before session close. Existing risk and API budget limits apply."
      : "Balanced trading permits longer holds. Switch to intraday for faster reviews and enforced short holding times.";
    $("resume-button").disabled = operationBusy || experiment.status === "ready";
    $("halt-button").disabled = haltBusy || experiment.status === "halted";
    $("demo-button").disabled = busy || !demo || experiment.status !== "ready";
    $("cycle-button").disabled = busy || demo || experiment.status !== "ready" || jobActive;
    $("cycle-button").textContent = jobActive && job.kind === "cycle" ? "Research cycle running…" : "Run paper cycle";
    $("reconcile-button").disabled = operationBusy || demo;
    $("activity-reconcile").disabled = operationBusy || demo;
    $("autopilot-toggle").checked = Boolean(experiment.autopilot);
    $("autopilot-toggle").disabled = demo || (!experiment.autopilot && (operationBusy || experiment.status !== "ready"));
    $("auto-resume-toggle").checked = experiment.auto_resume !== false;
    $("auto-resume-toggle").disabled = demo || restartPreferenceBusy || (experiment.auto_resume === false && operationBusy);
    $("test-connections").disabled = operationBusy || demo;
    $("test-paid-model").disabled = operationBusy || demo;
    $("refresh-button").disabled = !authenticated;
    $("add-agent").disabled = busy || document.querySelectorAll(".agent-edit-row").length >= 6;
    const started = Boolean(experiment.started) || number(experiment.step) > 0 || (state.orders || []).length > 0 || (state.decisions || []).length > 0;
    $("save-config").disabled = operationBusy || started;
    $("new-experiment").disabled = operationBusy;
    document.querySelectorAll("[data-close-agent], [data-cancel-order], [data-resolve-order]").forEach((button) => { button.disabled = !authenticated || operationBusy || demo; });
    $("resolution-submit").disabled = !authenticated || operationBusy || demo || !resolutionTarget;
    document.querySelectorAll("[data-agent-halt]").forEach((button) => { button.disabled = !authenticated || agentControlBusy.has(button.dataset.agentHalt) || Boolean(state.agent_controls?.[button.dataset.agentHalt]?.paused && !["transient", "unknown_order"].includes(state.agent_controls?.[button.dataset.agentHalt]?.pause_kind)); });
    document.querySelectorAll("[data-agent-resume]").forEach((button) => { button.disabled = !authenticated || operationBusy || agentControlBusy.has(button.dataset.agentResume) || !state.agent_controls?.[button.dataset.agentResume]?.paused; });
    $("config-lock-note").textContent = started ? "This experiment has started. Configuration changes require archiving it and starting a new one. Pending orders must be resolved first." : "Save settings before starting. Changing the execution environment requires a new experiment.";
  }

  function providerBadge(agent) {
    const connection = state.connections?.[agent.id] || {};
    if (agent.provider === "manual") return ["Manual · no automated entries", "neutral"];
    if (agent.provider === "rules") return state.experiment.mode === "demo" ? ["Local rules · demo enabled", ""] : connection.alpaca ? ["Paper credentials configured", "neutral"] : ["Paper credentials missing", "warn"];
    if (state.experiment.mode === "demo") return ["AI inactive in synthetic demo", "neutral"];
    if (!connection.model) return ["Model credentials missing", "warn"];
    if (!agent.model) return ["Model ID required", "warn"];
    if (number(state.experiment.monthly_model_budget) <= 0) return ["AI disabled · $0 budget", "warn"];
    if (!connection.alpaca) return ["Paper credentials missing", "warn"];
    return ["Credentials configured · check in Setup", "neutral"];
  }

  function agentStatus(agent) {
    const control = state.agent_controls?.[agent.id] || {};
    if (state.experiment.mode === "demo") return [agent.provider === "rules" ? "Synthetic rules" : "AI inactive · demo", "neutral"];
    if (control.paused && ["transient", "unknown_order"].includes(control.pause_kind)) return [control.pause_kind === "unknown_order" ? "Checking order outcome" : "Recovering connection", "warn"];
    if (control.paused) return ["Agent paused", "warn"];
    if (state.experiment.status === "recovering") return ["Checking accounts after restart", "warn"];
    if (state.experiment.status === "halted") return ["Global halt", "danger"];
    if (state.experiment.status === "paused") return ["Experiment paused", "warn"];
    const [connectionStatus, connectionClass] = providerBadge(agent);
    if (connectionClass === "warn") return [connectionStatus, connectionClass];
    const activity = state.autonomy?.[agent.id] || {};
    if (activity.status === "researching") return ["Researching", ""];
    if (activity.status && /fail|error|blocked|unavailable|invalid|exhausted/.test(activity.status)) return [String(activity.status).replaceAll("_", " "), "warn"];
    if (control.last_reconciled_at) return [state.experiment.autopilot ? "Monitoring enabled" : "Manual cycles", "neutral"];
    return ["Awaiting connection check", "neutral"];
  }

  function renderScoreboard() {
    const agents = state.agents;
    const returns = agents.map((agent) => number(agent.return_pct));
    const best = Math.max(...returns);
    const leaders = agents.filter((agent) => Math.abs(number(agent.return_pct) - best) < 0.005);
    const meaningful = (state.orders || []).some((order) => number(order.filled_qty) > 0) || agents.some((agent) => Math.abs(number(agent.net_pnl)) >= 0.005);
    const uniqueLeader = meaningful && agents.length > 1 && leaders.length === 1 ? leaders[0].id : null;
    $("competition-title").textContent = agents.length === 2 ? `${agents[0].name} vs ${agents[1].name}` : `${agents.length} agents. One arena.`;
    $("leader-label").textContent = uniqueLeader ? `${agentName(uniqueLeader)} leads · net return` : meaningful ? "Tied on net return" : "No lead established";
    $("leader-label").className = `badge ${uniqueLeader ? "" : "neutral"}`;
    $("competition-scoreboard").innerHTML = agents.map((agent, index) => {
      const [status, statusClass] = agentStatus(agent);
      const control = state.agent_controls?.[agent.id] || {};
      const activity = state.autonomy?.[agent.id] || {};
      const strategy = activity.strategy || activity.plan?.strategy || {};
      const allocation = number(agent.allocation);
      const target = agent.target_equity !== undefined ? number(agent.target_equity) : number(state.experiment.total_capital) > 0 ? number(state.experiment.target) * allocation / number(state.experiment.total_capital) : 0;
      const equity = number(agent.equity);
      const progress = target > 0 ? Math.max(0, Math.min(100, equity / target * 100)) : 0;
      const ownOrders = (state.orders || []).filter((order) => order.agent_id === agent.id);
      const pending = agent.working_order_count ?? ownOrders.filter((order) => !TERMINAL.has(String(order.status).toLowerCase())).length;
      const filled = agent.trade_count ?? ownOrders.filter((order) => number(order.filled_qty) > 0).length;
      const invested = Math.max(0, number(agent.trading_equity, equity + number(agent.model_cost)) - number(agent.cash));
      const lastDecision = [...(state.decisions || [])].reverse().find((decision) => decision.agent_id === agent.id);
      const recovering = control.paused && ["transient", "unknown_order"].includes(control.pause_kind);
      const pauseAction = recovering ? `<button class="button secondary" data-agent-halt="${esc(agent.id)}">Stop recovery</button>` : control.paused ? `<button class="button secondary" data-agent-resume="${esc(agent.id)}">Resume agent</button>` : `<button class="button secondary" data-agent-halt="${esc(agent.id)}">Pause agent</button>`;
      return `<article class="scorecard ${uniqueLeader === agent.id ? "is-leading" : ""}" style="--agent-color:${COLORS[index % COLORS.length]}" aria-label="${esc(agent.name)} performance"><div class="scorecard-top"><span class="scorecard-provider">${esc(agent.provider === "openai" ? "OPENAI" : agent.provider === "anthropic" ? "ANTHROPIC" : agent.provider.toUpperCase())}</span><span class="scorecard-place">${uniqueLeader === agent.id ? "LEADING" : "PAPER"}</span></div><h3>${esc(agent.name)}</h3><div class="scorecard-model" title="${esc(agent.model || "No model selected")}">${esc(agent.model || (agent.provider === "rules" ? "Fixed rules baseline" : "No model selected"))}</div><div class="scorecard-status badge ${statusClass}" title="${esc(control.reason || status)}">${esc(status)}</div><div class="scorecard-equity">${money(equity)}</div><div class="scorecard-return ${tone(agent.net_pnl)}"><strong>${pct(agent.return_pct, true)}</strong><span>${number(agent.net_pnl) >= 0 ? "+" : "−"}${money(Math.abs(number(agent.net_pnl)))} net P&L</span></div><dl class="scorecard-stats"><div><dt>Cash</dt><dd>${money(agent.cash)}</dd></div><div><dt>Invested</dt><dd>${money(invested)}</dd></div><div><dt>Drawdown</dt><dd>${pct(Math.abs(number(agent.max_drawdown_pct)))}</dd></div><div><dt>Model cost*</dt><dd>${money(agent.model_cost)}</dd></div><div><dt>Trading P&amp;L</dt><dd>${money(number(agent.trading_pnl, number(agent.net_pnl) + number(agent.model_cost)))}</dd></div><div><dt>Orders with fills</dt><dd>${filled}</dd></div><div><dt>Open orders</dt><dd>${pending}</dd></div></dl><div class="scorecard-goal"><div><span>${money(allocation)} start</span><strong>${money(target)} goal</strong></div><div class="goal-track" role="progressbar" aria-label="${esc(agent.name)} goal progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.toFixed(2)}"><span style="width:${progress}%"></span></div><small>${progress.toFixed(1)}% of goal · no return promised</small></div><div class="scorecard-plan"><span>STRATEGY</span><p>${esc(strategy.name || (agent.provider === "rules" ? "SMA20 / SMA50" : "No research yet"))}</p><span>LATEST DECISION</span><p title="${esc(lastDecision?.reason || "")}">${lastDecision ? `${esc(String(lastDecision.action || "record").toUpperCase())}${lastDecision.symbol ? ` ${esc(lastDecision.symbol)}` : ""} · ${esc(lastDecision.reason || lastDecision.status || "Recorded")}` : "No decision recorded"}</p></div>${recovering ? `<p class="small muted">${esc(control.recovery?.last_error || control.reason || "Account verification pending.")} Next check: ${esc(timestamp(control.recovery?.next_retry_at, true))}. No order is resubmitted during recovery.</p>` : ""}<div class="scorecard-freshness"><span>Quote <time data-agent-quote-time="${esc(agent.id)}">—</time></span><span>Orders checked <time data-agent-reconcile-time="${esc(agent.id)}">—</time></span></div><div class="scorecard-controls">${pauseAction}<button class="text-button" data-agent-research="${esc(agent.id)}">View research →</button></div></article>`;
    }).join("");
    $("scoreboard-note").textContent = `${state.experiment.mode === "demo" ? "Synthetic demo only; AI agents are inactive. " : "Paper portfolio marks, not live-money results. "}Net returns include estimated execution and model costs*. A lead is not evidence of skill.`;
  }

  function updateFreshness() {
    if (!state || !authenticated) return;
    $("freshness-bar").hidden = false;
    const now = Date.now();
    const serverClock = Date.parse(state.server_time || "");
    const estimatedServerNow = Number.isFinite(serverClock) ? serverClock + Math.max(0, now - lastSnapshotReceivedAt) : now;
    const snapshot = state.snapshot_at || (lastSnapshotReceivedAt ? new Date(lastSnapshotReceivedAt).toISOString() : "");
    const snapshotDate = Date.parse(snapshot);
    const holdingQuotes = state.agents.flatMap((agent) => Object.entries(agent.positions || {})
      .filter(([, position]) => number(position.qty) > 0.0000001)
      .map(([symbol]) => ({ agent: agent.name, symbol, at: state.agent_controls?.[agent.id]?.quote_times?.[symbol] })));
    const missingQuotes = holdingQuotes.filter((item) => !Number.isFinite(Date.parse(item.at || "")));
    const quote = holdingQuotes.length ? holdingQuotes.filter((item) => Number.isFinite(Date.parse(item.at || ""))).map((item) => item.at).sort((a, b) => Date.parse(a) - Date.parse(b))[0] : state.market?.as_of;
    const quoteDate = Date.parse(quote || "");
    const snapshotAge = Number.isFinite(snapshotDate) ? Math.max(0, estimatedServerNow - snapshotDate) : Infinity;
    const quoteAge = Number.isFinite(quoteDate) ? Math.max(0, estimatedServerNow - quoteDate) : Infinity;
    const staleSnapshot = snapshotAge > 45000;
    const staleQuote = state.experiment.mode === "paper" && (quoteAge > 120000 || missingQuotes.length > 0);
    const ageText = (age) => !Number.isFinite(age) ? "not received" : age < 1000 ? "just now" : age < 60000 ? `${Math.floor(age / 1000)}s ago` : age < 3600000 ? `${Math.floor(age / 60000)}m ago` : `${Math.floor(age / 3600000)}h ago`;
    const exact = (value) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Not received";
    $("snapshot-time").textContent = `${exact(snapshot)} · ${ageText(snapshotAge)}`;
    $("snapshot-time").title = `Server snapshot: ${timestamp(snapshot)}. Last successful browser fetch: ${lastSnapshotReceivedAt ? new Date(lastSnapshotReceivedAt).toLocaleString() : "none"}.`;
    $("quote-time").textContent = state.experiment.mode === "demo" ? "Synthetic · not a live quote" : missingQuotes.length ? `${missingQuotes.length} holding price${missingQuotes.length === 1 ? "" : "s"} missing` : `${exact(quote)}${quote ? ` · ${ageText(quoteAge)}${holdingQuotes.length ? " · oldest holding" : ""}` : ""}`;
    $("quote-time").title = missingQuotes.length ? `Missing verified prices: ${missingQuotes.map((item) => `${item.agent}: ${item.symbol}`).join("; ")}.` : quote ? `${holdingQuotes.length ? "Oldest current holding price" : "Market update"}: ${timestamp(quote)}. Monitor last success: ${timestamp(state.monitor?.last_success)}.` : "No quote timestamp has been received.";
    $("quote-time").className = staleQuote ? "freshness-warning" : "";
    $("snapshot-time").className = staleSnapshot ? "freshness-warning" : "";
    $("feed-status").textContent = snapshotFailed ? "Disconnected · last known state" : staleSnapshot ? "Server snapshot is stale" : missingQuotes.length ? "Connected · holding prices missing" : staleQuote ? quote ? "Connected · prices are stale" : "Connected · awaiting prices" : "Server connected";
    $("feed-dot").className = `status-dot ${snapshotFailed ? "offline-dot" : staleSnapshot || staleQuote ? "stale-dot" : ""}`;
    const job = state.server_job || serverJob;
    const jobActive = ["queued", "running"].includes(job?.status);
    $("job-status").hidden = !job?.id;
    $("job-status").textContent = job?.id ? `${String(job.action || "cycle").replaceAll("/", " ").toUpperCase()} · ${String(job.status).toUpperCase()}` : "";
    $("job-status").title = job?.error || "The last server operation. Conflicting commands are not queued.";
    if (state.monitor?.reconciliation_deferred) {
      $("job-status").hidden = false;
      $("job-status").textContent = "ORDER CHECKS DEFERRED · RESEARCH BUSY";
      $("job-status").className = "badge warn";
    } else $("job-status").className = `badge ${job?.status === "error" ? "warn" : "neutral"}`;
    document.querySelectorAll("[data-agent-quote-time]").forEach((element) => {
      const control = state.agent_controls?.[element.dataset.agentQuoteTime] || {};
      const agent = state.agents.find((item) => item.id === element.dataset.agentQuoteTime);
      const symbols = Object.entries(agent?.positions || {}).filter(([, position]) => number(position.qty) > 0.0000001).map(([symbol]) => symbol);
      const missing = symbols.filter((symbol) => !Number.isFinite(Date.parse(control.quote_times?.[symbol] || "")));
      const times = symbols.map((symbol) => control.quote_times?.[symbol]).filter((value) => Number.isFinite(Date.parse(value)));
      const quoteAt = times.sort((a, b) => Date.parse(a) - Date.parse(b))[0];
      const age = quoteAt ? Math.max(0, estimatedServerNow - Date.parse(quoteAt)) : Infinity;
      element.textContent = state.experiment.mode === "demo" ? "Synthetic" : !symbols.length ? "No holdings" : missing.length ? `Missing: ${missing.join(", ")}` : `${exact(quoteAt)}${age > 120000 ? " · stale" : " · oldest"}`;
      element.className = symbols.length && (missing.length || age > 120000) && state.experiment.mode !== "demo" ? "freshness-warning" : "";
      element.title = symbols.length ? `Current holdings: ${symbols.map((symbol) => `${symbol}: ${timestamp(control.quote_times?.[symbol])}`).join("; ")}. ${control.recovery?.quote_warning || ""}` : "No current holdings require a valuation mark.";
    });
    document.querySelectorAll("[data-agent-reconcile-time]").forEach((element) => {
      const at = state.agent_controls?.[element.dataset.agentReconcileTime]?.last_reconciled_at;
      const age = at ? Math.max(0, estimatedServerNow - Date.parse(at)) : Infinity;
      element.textContent = `${exact(at)}${state.monitor?.reconciliation_deferred ? " · deferred" : age > 45000 && Number.isFinite(age) ? " · aged" : ""}`;
      element.className = state.monitor?.reconciliation_deferred || age > 45000 ? "freshness-warning" : "";
      element.title = `Last confirmed order reconciliation: ${timestamp(at)}. Quote updates do not imply that fills have been reconciled.`;
    });
    $("offline-banner").hidden = !snapshotFailed;
    $("offline-banner").textContent = snapshotFailed ? `Connection lost. Showing the last snapshot received ${lastSnapshotReceivedAt ? new Date(lastSnapshotReceivedAt).toLocaleTimeString() : "before disconnect"}. Your agents may still run on the server. ${lastSnapshotError}` : "";
  }

  function holdingMarkMarkup(agentId, symbol) {
    if (state.experiment.mode !== "paper") return "";
    const control = state.agent_controls?.[agentId] || {};
    const mark = control.quote_details?.[symbol] || {};
    const at = control.quote_times?.[symbol];
    const source = [mark.feed, mark.price_source].filter(Boolean).join(" · ");
    return `<small class="holding-mark">${at ? `${esc(source || "Valuation mark")} · ${esc(timestamp(at, true))}` : "Valuation price missing"}${mark.execution_eligible === false ? " · valuation only" : ""}</small>`;
  }

  function renderAgents() {
    $("agent-grid").innerHTML = state.agents.map((agent, index) => {
      const [badge, badgeClass] = providerBadge(agent);
      const positions = Object.entries(agent.positions || {}).filter(([, position]) => number(position.qty) > 0.0000001);
      const positionHtml = positions.length ? positions.map(([symbol, position]) => `<div class="position-row"><span><strong>${esc(symbol)}</strong> · ${qty(position.qty)} shares${holdingMarkMarkup(agent.id, symbol)}</span><button class="button secondary" data-close-agent="${esc(agent.id)}" data-close-symbol="${esc(symbol)}" ${state.experiment.mode !== "paper" ? "disabled title=\"Position-close requests require Alpaca paper mode\"" : ""}>Request close</button></div>`).join("") : '<p class="no-positions">No open positions</p>';
      const activity = state.autonomy?.[agent.id] || {};
      const strategy = activity.strategy || activity.plan?.strategy || {};
      const strategySummary = agent.provider === "rules" ? '<div class="agent-strategy"><span class="small-label">FIXED BASELINE</span><strong>SMA20 / SMA50</strong><p>Long-only rules on SPY, QQQ and IWM. No model calls.</p></div>' : `<div class="agent-strategy"><span class="small-label">${autonomousMode() ? "AGENT STRATEGY" : "LEGACY REVIEWER"}</span><strong>${esc(strategy.name || (autonomousMode() ? "Awaiting first research cycle" : "SMA entry review"))}</strong><p>${esc(strategy.thesis || (state.experiment.mode === "demo" ? "AI research is inactive in the synthetic demo." : autonomousMode() ? "The agent will record its own thesis and evidence before placing paper orders." : "Reviews baseline buy candidates. It does not choose an independent strategy."))}</p><button class="text-button" data-agent-research="${esc(agent.id)}">View strategy & research →</button></div>`;
      return `<article class="panel agent-card" style="--agent-color:${COLORS[index % COLORS.length]}"><div class="agent-header"><span class="agent-avatar" aria-hidden="true">${esc(agent.provider === "rules" ? "R" : agent.provider === "openai" ? "O" : agent.provider === "anthropic" ? "C" : "M")}</span><div class="agent-name"><h3>${esc(agent.name)}</h3><small>${esc(providerLabel(agent.provider))}${agent.model ? ` · ${esc(agent.model)}` : ""}</small></div></div><span class="badge ${badgeClass}">${esc(badge)}</span><div class="agent-equity">${money(agent.equity)}</div><div class="agent-return ${tone(agent.net_pnl)}">${number(agent.net_pnl) >= 0 ? "+" : "−"}${money(Math.abs(number(agent.net_pnl)))} · ${pct(agent.return_pct, true)}</div><dl class="agent-stats"><div><dt>Starting allocation</dt><dd>${money(agent.allocation)}</dd></div><div><dt>Virtual cash</dt><dd>${money(agent.cash)}</dd></div><div><dt>Max drawdown</dt><dd>${pct(Math.abs(number(agent.max_drawdown_pct)))}</dd></div><div><dt>Estimated costs</dt><dd>${money(number(agent.fees) + number(agent.model_cost))}</dd></div></dl>${strategySummary}${exitRetryMarkup(agent.id)}<div class="agent-positions">${positionHtml}</div></article>`;
    }).join("");
  }

  function renderChart() {
    $("chart-legend").innerHTML = state.agents.map((agent, index) => `<span class="legend-item"><i class="legend-swatch" style="background:${COLORS[index % COLORS.length]}"></i>${esc(agent.name)}</span>`).join("");
    // The server supplies a bounded full-period extrema sample. A second
    // stride here would discard the exact highs/lows that sample preserves.
    const historyRows = state.history || [];
    if (historyRows.length < 2) {
      $("equity-chart").innerHTML = '<div class="chart-empty"><strong>Your first observations start here.</strong>Resume entries and run a demo, or connect dedicated paper accounts.</div>';
      $("equity-chart").setAttribute("aria-label", "No return series yet. At least two observations are needed.");
      return;
    }
    const width = 700, height = 265, left = 48, right = 15, top = 20, bottom = 36;
    const series = state.agents.map((agent) => historyRows.map((row) => {
      const recorded = row.agents?.[agent.id];
      const value = recorded === undefined || recorded === null ? number(agent.allocation) : number(recorded);
      return number(agent.allocation) ? (value / number(agent.allocation) - 1) * 100 : 0;
    }));
    const allValues = series.flat();
    let low = Math.min(0, ...allValues), high = Math.max(0, ...allValues);
    if (high - low < 0.3) { low -= 0.3; high += 0.3; }
    const margin = (high - low) * 0.13;
    low -= margin; high += margin;
    const times = historyRows.map((row) => Date.parse(row.at || ""));
    const timeSpan = times.at(-1) - times[0];
    const useTime = state.experiment.mode !== "demo" && times.every(Number.isFinite) && timeSpan > 0;
    const x = (index) => left + (useTime ? (times[index] - times[0]) / timeSpan : index / Math.max(1, historyRows.length - 1)) * (width - left - right);
    const y = (value) => top + (high - value) / (high - low) * (height - top - bottom);
    let svg = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#66e2c4" stop-opacity=".12"/><stop offset="100%" stop-color="#66e2c4" stop-opacity="0"/></linearGradient></defs>`;
    for (let index = 0; index < 5; index++) {
      const value = low + (high - low) * index / 4;
      const position = y(value);
      svg += `<line x1="${left}" y1="${position}" x2="${width - right}" y2="${position}" stroke="#263b50" stroke-width="1" stroke-dasharray="3 5"/><text x="${left - 10}" y="${position + 3}" text-anchor="end" fill="#99afc4" font-size="9" font-family="system-ui">${value.toFixed(1)}%</text>`;
    }
    svg += `<line x1="${left}" y1="${y(0)}" x2="${width - right}" y2="${y(0)}" stroke="#4a6175" stroke-width="1"/>`;
    series.forEach((values, index) => {
      const points = values.map((value, i) => `${x(i).toFixed(2)},${y(value).toFixed(2)}`).join(" ");
      if (index === 0) svg += `<polygon points="${x(0)},${y(0)} ${points} ${x(values.length - 1)},${y(0)}" fill="url(#chart-fill)"/>`;
      svg += `<polyline points="${points}" fill="none" stroke="${COLORS[index % COLORS.length]}" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"${index > 0 ? ` stroke-dasharray="${index % 2 ? "5 4" : "2 4"}"` : ""}/><circle cx="${x(values.length - 1)}" cy="${y(values.at(-1))}" r="3" fill="${COLORS[index % COLORS.length]}"/>`;
    });
    const end = historyRows.length - 1;
    [0, Math.floor(end / 2), end].filter((value, index, array) => array.indexOf(value) === index).forEach((index) => {
      const label = state.experiment.mode === "demo" ? `Observation ${index + 1}` : timestamp(historyRows[index].at, true);
      svg += `<text x="${x(index)}" y="${height - 7}" text-anchor="${index === 0 ? "start" : index === end ? "end" : "middle"}" fill="#99afc4" font-size="9" font-family="system-ui">${esc(label)}</text>`;
    });
    $("equity-chart").innerHTML = `${svg}</svg>`;
    $("equity-chart").setAttribute("aria-label", `${historyRows.length} recorded observations. Latest returns: ${state.agents.map((agent, index) => `${agent.name} ${pct(series[index].at(-1), true)}`).join("; ")}.`);
  }

  function renderOrders() {
    if (resolutionTarget && (resolutionTarget.experiment_id !== state.experiment.id || !(state.orders || []).some((order) => order.id === resolutionTarget.order_id && order.status === "unknown"))) dismissResolution();
    const allOrders = [...(state.orders || [])].reverse();
    const working = allOrders.filter((order) => !TERMINAL.has(String(order.status).toLowerCase()));
    const closed = allOrders.filter((order) => TERMINAL.has(String(order.status).toLowerCase()));
    const orders = [...working, ...closed.slice(0, 150)];
    if (!orders.length) { $("orders-table").innerHTML = '<div class="empty-state">No orders yet. Every submitted order will appear here.</div>'; return; }
    $("orders-table").innerHTML = `<p class="small muted">${working.length} working orders shown first · ${Math.min(closed.length, 150)} recent closed orders</p><table><thead><tr><th>Agent</th><th>Instrument</th><th>Side</th><th>Requested</th><th>Filled quantity</th><th>Reserved</th><th>Status</th><th>Control</th></tr></thead><tbody>${orders.map((order) => {
      const status = String(order.status || "unknown").toLowerCase();
      const terminal = TERMINAL.has(status);
      const requested = number(order.notional) > 0 ? money(order.notional) : `${qty(order.qty)} shares`;
      const badgeClass = status.includes("unknown") || status === "rejected" ? "danger" : status === "filled" ? "" : "neutral";
      const resolution = status === "unknown" && state.agent_controls?.[order.agent_id]?.paused ? `<button class="text-button" data-resolve-order="${esc(order.id)}">Record broker confirmation</button>` : "";
      return `<tr><td>${esc(agentName(order.agent_id))}</td><td><strong>${esc(order.symbol)}</strong></td><td>${esc(String(order.side || "").toUpperCase())}</td><td>${requested}</td><td>${qty(order.filled_qty)}</td><td>${money(order.reserved)}</td><td><span class="badge ${badgeClass}">${esc(status.replaceAll("_", " "))}</span></td><td>${!terminal ? `<button class="button secondary compact" data-cancel-order="${esc(order.id)}" ${state.experiment.mode === "demo" ? "disabled" : ""}>Request cancel</button>${resolution}` : "—"}</td></tr>`;
    }).join("")}</tbody></table>${(orders.length > 150 || state.view_limits) ? '<p class="small muted">Showing recent orders. Export includes the full recorded evidence.</p>' : ""}`;
  }

  function dismissResolution() {
    resolutionTarget = null;
    $("order-resolution-panel").hidden = true;
    $("order-resolution-form").reset();
  }

  function openResolution(orderId) {
    const order = (state.orders || []).find((item) => item.id === orderId);
    if (!order || order.status !== "unknown" || !state.agent_controls?.[order.agent_id]?.paused) return;
    dismissResolution();
    resolutionTarget = { experiment_id: state.experiment.id, agent_id: order.agent_id, order_id: order.id, client_order_id: order.client_order_id };
    $("resolution-client-id").value = order.client_order_id;
    $("order-resolution-detail").textContent = `${agentName(order.agent_id)} · ${String(order.side).toUpperCase()} ${order.symbol} · ${money(order.reserved)} reserved`;
    $("order-resolution-panel").hidden = false;
    $("resolution-confirmation").focus();
    $("order-resolution-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    updateControls();
  }

  function renderDecisions() {
    const decisions = [...(state.decisions || [])].reverse();
    $("decision-count").textContent = `${decisions.length} recent decisions${state.view_limits?.truncated?.decisions ? ` of ${state.view_limits.full_counts.decisions}` : ""}`;
    $("decisions-list").innerHTML = decisions.length ? decisions.slice(0, 100).map((decision) => `<article class="decision-row"><div class="decision-meta"><strong>${esc(agentName(decision.agent_id))}</strong><span class="badge ${decision.action === "buy" ? "" : decision.action === "sell" ? "warn" : "neutral"}">${esc(String(decision.action || "record").toUpperCase())}${decision.symbol ? ` · ${esc(decision.symbol)}` : ""}</span>${decision.status ? `<span class="small muted">${esc(decision.status)}</span>` : ""}<time datetime="${esc(decision.at)}">${esc(timestamp(decision.at, true))}</time></div><p>${esc(decision.reason || "No reason was recorded.")}</p></article>`).join("") + ((decisions.length > 100 || state.view_limits) ? '<p class="small muted">Showing recent decisions. Download the export for the complete record.</p>' : "") : '<div class="empty-state">No decisions yet. Recorded reasons will appear here, including skipped or blocked entries.</div>';
  }

  function renderEvents() {
    const events = [...(state.events || [])].reverse();
    $("event-list").innerHTML = events.length ? events.slice(0, 100).map((event) => `<div class="event-row"><time datetime="${esc(event.at)}">${esc(timestamp(event.at, true))}</time><span class="event-level ${esc(["error", "critical", "warn", "warning"].includes(event.level) ? event.level : "info")}">${esc(event.level || "info")}</span><span class="event-message">${esc(event.message)}</span></div>`).join("") : '<div class="empty-state">No system events recorded.</div>';
  }

  function renderConnections() {
    $("connection-list").innerHTML = state.agents.map((agent) => {
      const connection = state.connections?.[agent.id] || {};
      const prefix = connection.env_prefix || `ALPACA_${String(agent.id).toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
      return `<div class="connection-item"><div><strong>${esc(agent.name)}</strong><small>${esc(providerLabel(agent.provider))}</small></div><div class="credential-names"><code>${esc(prefix)}_KEY</code><code>${esc(prefix)}_SECRET</code></div><span class="badge ${connection.alpaca ? "neutral" : "warn"}">${connection.alpaca ? "Paper credentials configured · unverified" : "Paper credentials missing"}</span></div>`;
    }).join("");
    const estimates = state.agents.filter((agent) => ["openai", "anthropic"].includes(agent.provider)).map((agent) => state.connections?.[agent.id]?.model_test_estimate);
    const available = estimates.length > 0 && estimates.every((estimate) => estimate && Number.isFinite(Number(estimate.reserved_usd)));
    const estimate = available ? estimates.reduce((total, item) => total + Number(item.reserved_usd), 0) : 0;
    $("connection-test-estimate").textContent = available ? `Estimated maximum for all model tests: $${estimate.toFixed(4)}, using saved model rates. Each test reserves its cost from the same monthly and agent budgets as research. Provider billing can differ.` : "Model test cost estimate is unavailable. Save valid AI models and token rates before testing.";
    $("connection-test-budget").textContent = `Shared monthly model budget: ${money(state.experiment.monthly_model_budget)} · spent or reserved: ${money(state.model_spend_month)} · remaining: ${money(Math.max(0, number(state.experiment.monthly_model_budget) - number(state.model_spend_month)))}`;
    const jobs = [...(state.server_jobs || []), ...(state.server_job ? [state.server_job] : [])];
    const diagnostic = jobs.reverse().find((job) => job.action === "test-connections" && job.experiment_id === state.experiment.id && ["complete", "error"].includes(job.status));
    const report = diagnostic?.result;
    if (diagnostic?.status === "error") $("connection-test-result").innerHTML = `<p class="research-error">${esc(diagnostic.error || "The connection check did not complete.")}</p>`;
    else if (report) {
      const checkMarkup = (name, check) => `<p><strong>${name}: ${esc(check?.status || "not checked")}</strong>${check?.message || check?.error || check?.reason ? ` · ${esc(check.message || check.error || check.reason)}` : ""}${check?.actual_model ? `<br><span class="small muted">Returned model: ${esc(check.actual_model)}</span>` : check?.model ? `<br><span class="small muted">Requested model: ${esc(check.model)}; returned model ID was not reported.</span>` : ""}</p>`;
      $("connection-test-result").innerHTML = `<p class="small muted">${report.paid_model ? "Broker and paid model checks" : "Broker checks only"} · ${esc(timestamp(report.at || diagnostic.finished_at))}. These checks do not establish trading readiness or profit.</p>${(report.checks || []).map((check) => `<div class="diagnostic-result"><strong>${esc(check.name || agentName(check.agent_id))}</strong>${checkMarkup("Broker account and clock", check.broker)}${checkMarkup("Stock data", check.market_data)}${check.crypto_data ? checkMarkup("24/7 crypto data", check.crypto_data) : ""}${check.market_data?.age_seconds !== undefined ? `<p class="small muted">Sample mark age at check: ${qty(Math.max(0, number(check.market_data.age_seconds)))} seconds · ${esc(check.market_data.feed || "feed not reported")} · ${esc(check.market_data.source || "source not reported")} · ${check.market_data.execution_eligible ? "execution quote available at check; each order requires a fresh check" : "valuation only; not an execution quote"}</p>` : ""}${checkMarkup("Model", check.model)}${check.model?.cost_usd !== undefined ? `<p class="small muted">Estimated model charge: $${number(check.model.cost_usd).toFixed(4)}</p>` : ""}</div>`).join("")}`;
    } else $("connection-test-result").textContent = "";
  }

  function safeSourceLink(value, label) {
    if (typeof value !== "string") return "";
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password) return "";
      return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(label || url.hostname)} ↗</a>`;
    } catch { return ""; }
  }

  function evidenceLinks(record) {
    const links = new Map();
    let visited = 0;
    const visit = (item, depth = 0) => {
      if (!item || depth > 5 || visited++ > 400 || links.size >= 12) return;
      if (Array.isArray(item)) { item.slice(0, 40).forEach((entry) => visit(entry, depth + 1)); return; }
      if (typeof item !== "object") return;
      if (typeof item.url === "string") {
        const link = safeSourceLink(item.url, typeof item.headline === "string" ? item.headline : undefined);
        if (link) links.set(item.url, link);
      }
      Object.values(item).forEach((value) => { if (value && typeof value === "object") visit(value, depth + 1); });
    };
    const sourceLink = safeSourceLink(record.source);
    if (sourceLink) links.set(record.source, sourceLink);
    visit(record.data);
    return links.size ? `<div class="evidence-links"><span class="small-label">SOURCE LINKS</span>${[...links.values()].join("")}</div>` : "";
  }

  function evidenceMarkup(record, index, agentId) {
    const status = record.status || "unknown";
    const successful = status === "ok";
    const key = `evidence-${agentId}-${record.id || index}`;
    let raw = JSON.stringify(record.data ?? {}, null, 2);
    const clipped = raw.length > 16000;
    if (clipped) raw = `${raw.slice(0, 16000)}\n… Display shortened. Export the full record.`;
    const request = record.request || {};
    const symbols = Array.isArray(request.symbols) ? request.symbols.join(", ") : "";
    return `<details class="evidence-record" data-detail-key="${esc(key)}"><summary><span class="record-kind">${esc(String(record.kind || "record").replaceAll("_", " "))}${symbols ? ` · ${esc(symbols)}` : ""}</span><span class="badge ${successful ? "neutral" : "warn"}">${esc(status)}</span><time>${esc(timestamp(record.at, true))}</time></summary><div class="record-body"><div class="record-meta"><span><strong>Record</strong> <code>${esc(record.id || "Not assigned")}</code></span><span><strong>Source</strong> ${esc(record.source || "Not specified")}</span></div>${request.query ? `<p class="small"><strong>Query:</strong> ${esc(request.query)}</p>` : ""}${record.error ? `<p class="research-error">${esc(record.error)}</p>` : ""}${record.truncated ? '<p class="small muted">The source record was bounded or truncated by the research tool.</p>' : ""}${evidenceLinks(record)}<pre class="evidence-data">${esc(raw)}</pre></div></details>`;
  }

  function exitMarkup(exits) {
    const entries = Array.isArray(exits) ? exits : Object.entries(exits || {}).map(([symbol, exit]) => ({ symbol, ...exit }));
    if (!entries.length) return '<p class="small muted">No local exit plan recorded.</p>';
    return `<div class="table-scroll"><table class="exit-table"><thead><tr><th>Symbol</th><th>Stop threshold</th><th>Profit threshold</th><th>Maximum holding time</th><th>Recorded rationale</th></tr></thead><tbody>${entries.map((exit) => `<tr><td><strong>${esc(exit.symbol)}</strong></td><td>${number(exit.stop_loss) > 0 ? money(exit.stop_loss) : "Not set"}</td><td>${number(exit.take_profit) > 0 ? money(exit.take_profit) : "Not set"}</td><td>${number(exit.max_hold_hours) > 0 ? `${qty(exit.max_hold_hours)} hours` : "Not set"}${exit.opened_at ? `<small>Opened ${esc(timestamp(exit.opened_at, true))}</small>` : ""}</td><td class="exit-reason">${esc(exit.reason || "No rationale recorded.")}</td></tr>`).join("")}</tbody></table></div>`;
  }

  function exitRetryMarkup(agentId) {
    const warning = state.agent_controls?.[agentId]?.recovery?.execution_warning;
    const deferred = warning ? `<div class="exit-retry-status" role="status"><strong>Last execution deferred</strong><p>${esc(warning)} Execution checks will run again while automation is enabled. The position remains open until a sell fills.</p></div>` : "";
    const retries = Object.entries(state.autonomy?.[agentId]?.exit_retries || {});
    if (!retries.length) return deferred;
    return deferred + `<div class="exit-retry-status" role="status"><strong>Exit retry pending</strong>${retries.map(([symbol, retry]) => `<p><strong>${esc(symbol)}</strong> · ${esc(retry.message || retry.last_error || retry.error || "The broker rejected the last exit request.")}<br><span class="small">Next allowed automatic attempt: ${esc(timestamp(retry.next_retry_at, true))}. The position remains open until a sell fills. Stop controls still apply.</span></p>`).join("")}</div>`;
  }

  function turnMarkup(turn, index, agentId) {
    const response = turn.response || {};
    const research = Array.isArray(response.research) ? response.research : [];
    const orders = Array.isArray(response.orders) ? response.orders : [];
    return `<details class="model-turn" data-detail-key="${esc(`turn-${agentId}-${turn.at || index}-${turn.round || index}`)}"><summary><strong>Round ${esc(turn.round ?? index + 1)}</strong><span class="badge ${response.phase === "plan" ? "" : "neutral"}">${esc(response.phase || "response")}</span><time>${esc(timestamp(turn.at, true))}</time></summary><div class="record-body"><p class="model-rationale">${esc(response.reason || "No brief rationale was recorded.")}</p>${research.length ? `<ul class="research-requests">${research.map((request) => `<li><strong>${esc(String(request.kind || "research").replaceAll("_", " "))}</strong>${request.query ? ` · ${esc(request.query)}` : ""}${Array.isArray(request.symbols) && request.symbols.length ? ` · ${esc(request.symbols.join(", "))}` : ""}</li>`).join("")}</ul>` : ""}${response.phase === "plan" && !orders.length ? '<p class="small muted">Final plan contains no orders: hold. This does not itself establish that the strategy is effective.</p>' : ""}${orders.length ? `<div class="planned-orders">${orders.map((order) => `<div><strong>${esc(String(order.side || "").toUpperCase())} ${esc(order.symbol)}</strong> <span>${order.side === "buy" ? money(order.notional) : `${qty(order.qty)} shares`}</span><p>${esc(order.reason || "")}</p><small>Evidence: ${esc(Array.isArray(order.evidence_ids) ? order.evidence_ids.join(", ") : "Not recorded")}</small></div>`).join("")}</div><p class="small muted">These are model proposals. Check Decisions & orders for validation, submission, and fill outcomes.</p>` : ""}</div></details>`;
  }

  function renderResearch() {
    const openDetails = new Set([...$("research-panels").querySelectorAll("details[open]")].map((element) => element.dataset.detailKey));
    const demo = state.experiment.mode === "demo";
    $("research-mode-note").textContent = demo ? "Synthetic demo: no AI calls, invented research, or AI trade results. The panels below will populate only from recorded paper-mode research." : autonomousMode() ? "Autonomous mode: each AI agent researches and plans independently within its allocation. Source text is treated as untrusted data. Research requests can be unavailable because of provider permissions or connectivity." : "Legacy reviewer mode: AI filters the fixed baseline's entry candidates. Independent research and strategy planning are available when you select Autonomous strategies for a new experiment.";
    $("research-panels").innerHTML = state.agents.map((agent, index) => {
      const activity = state.autonomy?.[agent.id] || {};
      const strategy = activity.strategy || activity.plan?.strategy || {};
      const evidence = Array.isArray(activity.evidence) ? activity.evidence : Array.isArray(activity.research) ? activity.research : [];
      const turns = Array.isArray(activity.turns) ? activity.turns : [];
      const watchlist = Array.isArray(activity.watchlist) ? activity.watchlist : [];
      const isRules = agent.provider === "rules";
      const manual = agent.provider === "manual";
      const [badge, badgeClass] = providerBadge(agent);
      const strategyDetails = isRules ? '<div class="strategy-fields"><div><span class="small-label">FIXED STRATEGY</span><p>SMA20/SMA50 long-only baseline on SPY, QQQ and IWM. Buy when the latest closed daily price is above SMA20 and SMA20 is above SMA50. Exit when price is below SMA50; otherwise hold. Allocation and risk controls still apply.</p></div><div><span class="small-label">PURPOSE</span><p>A fixed comparison strategy. It does not use model research and has no demonstrated advantage in this experiment.</p></div></div>' : manual ? '<p class="muted small">This agent does not run an automated strategy or submit model-generated orders.</p>' : `<h3 class="strategy-name">${esc(strategy.name || "No strategy recorded yet")}</h3><div class="strategy-fields">${[["THESIS", strategy.thesis], ["INVALIDATION", strategy.invalidation], ["LESSONS / MEMORY", strategy.lessons]].map(([label, value]) => `<div><span class="small-label">${label}</span><p>${esc(value || "Not recorded.")}</p></div>`).join("")}</div>`;
      return `<section class="panel research-agent" data-research-agent="${esc(agent.id)}" style="--agent-color:${COLORS[index % COLORS.length]}"><div class="panel-heading"><div><span class="eyebrow">${esc(providerLabel(agent.provider))}</span><h2>${esc(agent.name)}</h2></div><span class="badge ${badgeClass}">${esc(badge)}</span></div>${strategyDetails}${!isRules && !manual ? `<dl class="research-schedule"><div><dt>Last cycle status</dt><dd>${esc(activity.status || "Not run")}</dd></div><div><dt>Last research cycle</dt><dd>${esc(timestamp(activity.last_cycle, true))}</dd></div><div><dt>Next scheduled review</dt><dd>${esc(timestamp(activity.next_due, true))}</dd></div><div><dt>Profit compounding</dt><dd>${state.experiment.compound_profits ? "Enabled" : "Disabled"}</dd></div></dl><div class="research-subsection"><h3>Watchlist</h3><div class="watchlist">${watchlist.length ? watchlist.map((symbol) => `<span>${esc(symbol)}</span>`).join("") : '<p class="small muted">No watchlist recorded.</p>'}</div></div><div class="research-subsection"><h3>Planned exit thresholds</h3><p class="small muted">Local triggers, not broker stop orders. They require this app and autopilot to run. Halting automation also stops these exits. A trigger does not guarantee a fill price.</p>${exitMarkup(activity.effective_exits || activity.exits || activity.plan?.exits)}${exitRetryMarkup(agent.id)}</div><div class="research-subsection"><div class="subsection-heading"><h3>Model research & rationale</h3><span class="small muted">${turns.length} recorded turns</span></div>${turns.length ? turns.map((turn, turnIndex) => turnMarkup(turn, turnIndex, agent.id)).join("") : '<div class="empty-state">No prospective model responses recorded.</div>'}</div><div class="research-subsection"><div class="subsection-heading"><h3>Research evidence</h3><span class="small muted">${evidence.length} source records</span></div>${evidence.length ? evidence.map((record, evidenceIndex) => evidenceMarkup(record, evidenceIndex, agent.id)).join("") : '<div class="empty-state">No research records. Missing access is reported explicitly; it is never replaced with fabricated data.</div>'}</div>` : ""}</section>`;
    }).join("");
    $("research-panels").querySelectorAll("details").forEach((element) => { element.open = openDetails.has(element.dataset.detailKey); });
  }

  function syncTradingStyleForm(preset = false) {
    const form = $("config-form");
    const intraday = form.elements.namedItem("trading_style").value === "aggressive_intraday";
    form.elements.namedItem("cycle_minutes").min = intraday ? "1" : "15";
    form.elements.namedItem("max_cycles_per_day").max = intraday ? "1440" : "24";
    if (preset) {
      form.elements.namedItem("cycle_minutes").value = intraday ? "1" : "60";
      form.elements.namedItem("max_cycles_per_day").value = intraday ? "390" : "4";
      if (intraday) form.elements.namedItem("agent_mode").value = "autonomous";
    }
  }

  function loadForm() {
    const experiment = state.experiment;
    const defaults = { trading_style: "balanced", agent_mode: "reviewer", asset_scope: "equities", research_rounds: 3, cycle_minutes: 60, max_cycles_per_day: 4, max_orders_per_cycle: 3, compound_profits: false, auto_resume: true, loss_limit_includes_model_costs: false, output_token_limit: 8192 };
    for (const key of ["mode", "agent_mode", "trading_style", "asset_scope", "total_capital", "target", "loss_limit", "position_cap_pct", "exposure_cap_pct", "slippage_bps", "fee_bps", "monthly_model_budget", "research_rounds", "cycle_minutes", "max_cycles_per_day", "max_orders_per_cycle", "output_token_limit"]) {
      const input = $("config-form").elements.namedItem(key);
      if (input) input.value = experiment[key] ?? defaults[key] ?? "";
    }
    $("config-form").elements.namedItem("compound_profits").checked = experiment.compound_profits ?? defaults.compound_profits;
    for (const key of ["auto_resume", "loss_limit_includes_model_costs"]) $("config-form").elements.namedItem(key).checked = experiment[key] ?? defaults[key];
    $("agent-editor").innerHTML = state.agents.map(agentEditorRow).join("");
    syncTradingStyleForm();
    formLoaded = true;
  }

  function agentEditorRow(agent) {
    return `<div class="agent-edit-row" data-agent-id="${esc(agent.id)}"><div class="agent-edit-top"><strong>Agent <code>${esc(agent.id)}</code></strong><button type="button" class="remove-agent" data-remove-agent="${esc(agent.id)}" aria-label="Remove ${esc(agent.name)}">Remove</button></div><div class="agent-edit-fields"><label>Display name<input data-field="name" type="text" value="${esc(agent.name)}" maxlength="60" required></label><label>Provider<select data-field="provider">${Object.entries(PROVIDERS).map(([value, label]) => `<option value="${value}" ${agent.provider === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>Exact model ID<input data-field="model" type="text" value="${esc(agent.model || "")}" placeholder="Required for AI" maxlength="150" autocomplete="off"></label><label>Allocation weight<input data-field="weight" type="number" value="${number(agent.weight, 1)}" min="0.001" step="any" required></label><label>Input USD / 1M tokens<input data-field="input_price" type="number" value="${number(agent.input_price)}" min="0" step="any" required></label><label>Output USD / 1M tokens<input data-field="output_price" type="number" value="${number(agent.output_price)}" min="0" step="any" required></label></div></div>`;
  }

  function readConfig() {
    const form = $("config-form");
    if (!form.reportValidity()) return null;
    const config = { mode: form.elements.namedItem("mode").value, agent_mode: form.elements.namedItem("agent_mode").value, asset_scope: form.elements.namedItem("asset_scope").value, trading_style: form.elements.namedItem("trading_style").value, compound_profits: form.elements.namedItem("compound_profits").checked, auto_resume: form.elements.namedItem("auto_resume").checked, loss_limit_includes_model_costs: form.elements.namedItem("loss_limit_includes_model_costs").checked };
    for (const key of ["total_capital", "target", "loss_limit", "position_cap_pct", "exposure_cap_pct", "slippage_bps", "fee_bps", "monthly_model_budget", "research_rounds", "cycle_minutes", "max_cycles_per_day", "max_orders_per_cycle", "output_token_limit"]) config[key] = Number(form.elements.namedItem(key).value);
    config.agents = [...document.querySelectorAll(".agent-edit-row")].map((row) => {
      const agent = { id: row.dataset.agentId };
      for (const field of ["name", "provider", "model", "weight", "input_price", "output_price"]) {
        const value = row.querySelector(`[data-field="${field}"]`).value;
        agent[field] = ["weight", "input_price", "output_price"].includes(field) ? Number(value) : value.trim();
      }
      return agent;
    });
    if (!config.agents.length) { message("Keep at least one agent in the experiment.", true); return null; }
    return config;
  }

  function reportJobResult() {
    const latest = state?.server_job || serverJob;
    const jobs = [...(state?.server_jobs || []), ...(latest ? [latest] : [])];
    for (const job of jobs) {
      if (!submittedJobs.has(job.id) || !["complete", "error"].includes(job.status)) continue;
      const success = submittedJobs.get(job.id);
      submittedJobs.delete(job.id);
      if (job.status === "error") message(`${job.action || "Operation"}: ${job.error || "The operation did not complete. Check state before retrying."}`, true);
      else if (success && state.experiment.status !== "halted") message(success);
    }
  }

  async function disableAutopilot() {
    if (!authenticated) return;
    const startingHaltEpoch = ++haltEpoch;
    try {
      const data = await request("/api/autopilot", { enabled: false });
      const latest = startingHaltEpoch !== haltEpoch ? await request("/api/state") : data;
      stateEpoch++;
      state = latest.state || latest;
      render();
      if (startingHaltEpoch !== haltEpoch) return;
      message("Automatic cycles disabled. Existing broker orders remain. An in-progress call may finish; use Halt automation to stop follow-up work.");
    } catch (error) { message(error.message, true); updateControls(); }
  }

  async function changeRestartPreference(enabled) {
    if (!authenticated || restartPreferenceBusy) return;
    if (enabled) {
      await mutate("/api/auto-resume", { enabled: true }, "Restart recovery enabled. Only previously enabled automation can resume after account checks.");
      if (state?.experiment) $("config-form").elements.namedItem("auto_resume").checked = state.experiment.auto_resume !== false;
      return;
    }
    // Opting out must reach the server while a recovery call is in flight.
    // Its newer generation prevents that old call from restoring automation.
    const startingHaltEpoch = ++haltEpoch;
    restartPreferenceBusy = true;
    updateControls();
    try {
      const data = await request("/api/auto-resume", { enabled: false });
      const latest = startingHaltEpoch !== haltEpoch ? await request("/api/state") : data;
      stateEpoch++;
      state = latest.state || latest;
      $("config-form").elements.namedItem("auto_resume").checked = state.experiment.auto_resume !== false;
      render();
      if (startingHaltEpoch === haltEpoch) message("Restart recovery disabled. After a restart, automation waits for your action.");
    } catch (error) { message(error.message, true); }
    finally { restartPreferenceBusy = false; updateControls(); }
  }

  async function refresh(silent = false) {
    if (refreshInFlight || !authenticated) return;
    refreshInFlight = true;
    const startingEpoch = stateEpoch;
    try {
      const data = await request("/api/state");
      if (startingEpoch !== stateEpoch || !authenticated) return;
      state = data.state || data;
      serverJob = state.server_job || null;
      reportJobResult();
      lastSnapshotReceivedAt = Date.now();
      snapshotFailed = false;
      lastSnapshotError = "";
      const payload = JSON.stringify({ ...state, server_time: undefined, snapshot_at: undefined });
      if (payload !== lastRenderedPayload || $("app-content").hidden) {
        lastRenderedPayload = payload;
        render();
      } else { updateControls(); updateFreshness(); }
      if (!silent) message("Experiment state refreshed.");
    } catch (error) {
      $("loading").hidden = true;
      if (error.loginRequired) return;
      snapshotFailed = true;
      lastSnapshotError = error.message;
      if (state) updateFreshness();
      else message(error.message, true);
    } finally { refreshInFlight = false; }
  }

  document.querySelectorAll("[data-view], [data-go-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view || button.dataset.goView)));
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
  $("refresh-button").addEventListener("click", () => authenticated ? refresh() : connectSession());
  $("trading-style-button").addEventListener("click", () => mutate("/api/trading-style",
    { style: state.experiment.trading_style === "aggressive_intraday" ? "balanced" : "aggressive_intraday" },
    "Trading style updated. Existing positions, history, risk caps and model budget were retained.", true));
  $("resume-button").addEventListener("click", () => mutate("/api/resume", {}, "New entries enabled. The next demo or paper cycle can evaluate the strategy."));
  $("halt-button").addEventListener("click", haltIndependently);
  $("demo-button").addEventListener("click", () => mutate("/api/demo", { count: Number($("demo-count").value) }, "Synthetic sessions recorded. No AI or broker calls were made."));
  $("cycle-button").addEventListener("click", () => mutate("/api/cycle", {}, "Paper cycle accepted. Follow its progress in Research & strategy."));
  $("reconcile-button").addEventListener("click", () => mutate("/api/reconcile", {}, "Reconciliation completed. Review system events before resuming."));
  $("activity-reconcile").addEventListener("click", () => mutate("/api/reconcile", {}, "Reconciliation completed. Review system events before resuming."));
  $("autopilot-toggle").addEventListener("change", (event) => event.target.checked ? mutate("/api/autopilot", { enabled: true }, "Automatic paper checks enabled on the server.") : disableAutopilot());
  $("auto-resume-toggle").addEventListener("change", (event) => changeRestartPreference(event.target.checked));
  $("test-paid-model").addEventListener("change", (event) => {
    $("test-connections").textContent = event.target.checked ? "Check paper + paid model connections" : "Check paper connections";
  });
  $("test-connections").addEventListener("click", () => mutate("/api/test-connections", { paid_model: $("test-paid-model").checked }, "Connection check finished. Read each result in Setup; a completed check can still report a failed connection."));
  $("config-form").elements.namedItem("trading_style").addEventListener("change", () => syncTradingStyleForm(true));
  $("config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const config = readConfig();
    if (!config) return;
    if (config.mode !== state.experiment.mode) { message("Changing between synthetic demo and Alpaca paper requires “Archive & start new experiment.”", true); return; }
    mutate("/api/config", config, "Experiment settings saved.", true);
  });
  $("new-experiment").addEventListener("click", () => {
    const config = readConfig();
    if (!config) return;
    const label = config.mode === "paper" ? "Alpaca paper accounts" : "a local synthetic demo";
    if (!confirm(`Archive the current experiment and create a new one using ${label}?\n\nThe new total virtual allocation will be ${money(config.total_capital)} across ${config.agents.length} agents. The current record will be archived. Existing positions are not automatically closed and no funds are moved. Pending orders must be resolved first.`)) return;
    mutate("/api/new", config, "New experiment created and paused. Review connections before resuming.", true);
  });
  $("add-agent").addEventListener("click", () => {
    const rows = [...document.querySelectorAll(".agent-edit-row")];
    if (rows.length >= 6) return;
    const ids = new Set(rows.map((row) => row.dataset.agentId));
    let index = 1;
    while (ids.has(`agent${index}`)) index++;
    const id = `agent${index}`;
    $("agent-editor").insertAdjacentHTML("beforeend", agentEditorRow({ id, name: `Agent ${index}`, provider: "rules", model: "", weight: 1, input_price: 0, output_price: 0 }));
    updateControls();
    document.querySelector(`[data-agent-id="${id}"] input`).focus();
  });
  $("two-agent-preset").addEventListener("click", () => {
    $("agent-editor").innerHTML = TWO_AGENT_PRESET.map(agentEditorRow).join("");
    $("config-form").elements.namedItem("mode").value = "paper";
    $("config-form").elements.namedItem("agent_mode").value = "autonomous";
    updateControls();
    message("Astra vs Claude preset loaded into the form only. Capital and operating limits are unchanged. Save before starting, or archive and create a new experiment to apply it.");
  });
  $("agent-editor").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-agent]");
    if (!button) return;
    if (document.querySelectorAll(".agent-edit-row").length <= 1) { message("At least one agent is required.", true); return; }
    button.closest(".agent-edit-row").remove();
    updateControls();
  });
  $("orders-table").addEventListener("click", (event) => {
    const resolve = event.target.closest("[data-resolve-order]");
    if (resolve) { openResolution(resolve.dataset.resolveOrder); return; }
    const button = event.target.closest("[data-cancel-order]");
    if (!button) return;
    if (!confirm("Request cancellation of this paper order? It may fill before cancellation is confirmed. Reserved funds remain held until the broker reports a final state.")) return;
    mutate("/api/cancel", { order_id: button.dataset.cancelOrder }, "Paper cancellation requested. Reconcile to confirm the final order state.");
  });
  $("resolution-dismiss").addEventListener("click", () => { dismissResolution(); updateControls(); });
  $("order-resolution-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!resolutionTarget || !$("order-resolution-form").reportValidity() || !$("resolution-attestation").checked) return;
    const broker_confirmation = $("resolution-confirmation").value.trim();
    if (broker_confirmation.length < 8) { message("Enter the broker confirmation reference and details (at least 8 characters).", true); return; }
    mutate("/api/orders/resolve-unknown", { ...resolutionTarget, broker_confirmation, confirmed_not_accepted: true }, "Broker confirmation recorded and reservation released. The agent remains paused; reconcile and resume it when ready.");
  });
  $("agent-grid").addEventListener("click", (event) => {
    const researchButton = event.target.closest("[data-agent-research]");
    if (researchButton) {
      setView("research");
      const panel = [...document.querySelectorAll("[data-research-agent]")].find((element) => element.dataset.researchAgent === researchButton.dataset.agentResearch);
      if (panel) panel.scrollIntoView({ block: "start", behavior: "instant" });
      return;
    }
    const button = event.target.closest("[data-close-agent]");
    if (!button || button.disabled) return;
    const agent = button.dataset.closeAgent;
    const symbol = button.dataset.closeSymbol;
    if (!confirm(`Request a paper sell to close ${symbol} for ${agentName(agent)}?\n\nThis is separate from halting entries and cancelling orders. A fill is not guaranteed. No real-money order can be placed by this app.`)) return;
    if (!confirm(`Confirm the paper close request for ${agentName(agent)} / ${symbol}.\n\nThe request will be submitted to the agent's dedicated Alpaca paper account. Continue?`)) return;
    mutate("/api/close", { agent_id: agent, symbol }, "Paper close requested. Check and reconcile the resulting order.");
  });

  $("competition-scoreboard").addEventListener("click", (event) => {
    const halt = event.target.closest("[data-agent-halt]");
    if (halt && !halt.disabled) { changeAgentControl(halt.dataset.agentHalt, true); return; }
    const resume = event.target.closest("[data-agent-resume]");
    if (resume && !resume.disabled) { changeAgentControl(resume.dataset.agentResume, false); return; }
    const research = event.target.closest("[data-agent-research]");
    if (research) {
      setView("research");
      const panel = [...document.querySelectorAll("[data-research-agent]")].find((element) => element.dataset.researchAgent === research.dataset.agentResearch);
      panel?.scrollIntoView({ block: "start", behavior: "instant" });
    }
  });

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (loginBusy || !$("login-form").reportValidity()) return;
    loginBusy = true;
    $("login-button").disabled = true;
    $("login-button").textContent = "Connecting…";
    $("login-error").hidden = true;
    try {
      const session = await request("/api/login", { access_token: $("access-token").value });
      $("access-token").value = "";
      acceptSession(session);
      await refresh(true);
    } catch (error) {
      $("access-token").value = "";
      $("login-error").textContent = error.message;
      $("login-error").hidden = false;
      $("access-token").focus();
    } finally {
      loginBusy = false;
      $("login-button").disabled = false;
      $("login-button").textContent = "Open dashboard →";
    }
  });
  $("logout-button").addEventListener("click", async () => {
    $("logout-button").disabled = true;
    try {
      await request("/api/logout", {});
      stateEpoch++;
      state = null;
      formLoaded = false;
      lastRenderedPayload = "";
      submittedJobs.clear();
      $("competition-scoreboard").innerHTML = "";
      $("agent-grid").innerHTML = "";
      $("research-panels").innerHTML = "";
      showLogin("Signed out. Agents can still run on your server.");
    } catch (error) { if (!error.loginRequired) message(`Sign-out was not confirmed. ${error.message}`, true); }
    finally { $("logout-button").disabled = false; }
  });

  async function connectSession() {
    try {
      const session = await request("/api/session");
      acceptSession(session);
      await refresh(true);
    } catch (error) {
      $("loading").hidden = true;
      if (error.loginRequired) showLogin();
      else message(error.message, true);
    }
  }

  async function initialize() {
    setView(location.hash.slice(1) || currentView);
    await connectSession();
    setInterval(() => { if (!document.hidden && authenticated) refresh(true); }, 3000);
    setInterval(() => { if (!document.hidden && authenticated) updateFreshness(); }, 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && authenticated) refresh(true); });
    window.addEventListener("online", () => { if (authenticated) refresh(true); });
  }
  initialize();
})();
