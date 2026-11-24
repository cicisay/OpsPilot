(() => {
  "use strict";

  const state = {
    me: null,
    csrf: "",
    tenants: [],
    tenantId: "",
    overview: null,
    selected: null,
    variantIndex: 0,
    memoryTab: "profile",
    replay: null,
    auditLogs: [],
    auditOpen: false,
  };

  const $ = (id) => document.getElementById(id);
  const problemLabels = {
    low_traffic: "流量低",
    low_inquiry: "询盘低",
    low_order: "下单低",
    high_negative: "差评高",
    stock_risk: "库存风险",
    overstock: "库存积压",
    low_margin: "毛利低",
  };
  const statusLabels = {
    preview: "已预览",
    approval_required: "待审批",
    invalid_approval: "令牌无效",
    busy: "发布中",
    published: "已发布",
    blocked: "被拦截",
    failed: "失败",
    no_change: "无变化",
  };

  const verdictLabels = {
    supported: "成立",
    rejected: "不成立",
    inconclusive: "不确定",
    insufficient: "样本不足",
    observing: "观察中",
  };

  // Compliance is four checks, not one boolean. The console shows all four so
  // a "warn" on one axis is never hidden behind a green overall badge.
  const checkLabels = {
    c1_absolute: "绝对化",
    c2_ip: "知识产权",
    c3_category: "类目资质",
    c4_market: "目标市场",
  };
  const checkOrder = ["c1_absolute", "c2_ip", "c3_category", "c4_market"];
  const severityLabels = { pass: "通过", warn: "提示", block: "否决", missing: "未审" };
  const severityTone = { pass: "ok", warn: "warn", block: "danger", missing: "muted" };
  const profileLabels = {
    store_slug: "店铺标识",
    active_products: "在售商品",
    catalogue_categories: "类目",
    median_order_rate_pct: "订单率中位数(%)",
    median_inquiry_rate_pct: "询盘率中位数(%)",
    last_run_id: "最近 run_id",
    last_selected_count: "上次选品数",
    updated_at: "更新时间",
    updated_by: "更新来源",
  };

  function pct(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return `${(Number(value) * 100).toFixed(digits)}%`;
  }

  function num(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("zh-CN");
  }

  // Memory holds raw JSON; a bare array dumped into a cell is unreadable.
  function fmtValue(value) {
    if (Array.isArray(value)) return value.join("、");
    if (value && typeof value === "object") return JSON.stringify(value);
    return String(value ?? "");
  }

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  // CSP 禁止内联 style，进度条宽度统一在渲染后用 CSSOM 赋值。
  function applyWidths(root = document) {
    root.querySelectorAll("[data-width]").forEach((node) => {
      const value = Number(node.dataset.width);
      node.style.width = `${Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0}%`;
    });
  }

  function money(cents) {
    const value = Number(cents || 0) / 100;
    return `$${value.toFixed(2)}`;
  }

  // 页头周期：ISO 周 + 本周一到周日。模板不改，只换文案。
  function weekLabel(now = new Date()) {
    const date = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
    const day = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    const week = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
    const monday = new Date(now);
    monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    const fmt = (item) => `${item.getMonth() + 1}/${String(item.getDate()).padStart(2, "0")}`;
    return `${now.getFullYear()} 第 ${week} 周 · ${fmt(monday)} – ${fmt(sunday)}`;
  }

  const sourceTone = { model: "model", fallback: "fallback", rule: "", legacy: "legacy" };

  function sourceBadge(meta, compact = false) {
    const source = meta && meta.source ? meta.source : "legacy";
    const label = compact
      ? (meta && meta.short) || "历史"
      : (meta && meta.label) || "历史数据 · 来源未记录";
    return `<span class="badge ${sourceTone[source] ?? "legacy"}">${esc(label)}</span>`;
  }

  function critiqueBadge(meta) {
    const critique = (meta && meta.critique) || {};
    if (critique.score === null || critique.score === undefined) return "";
    const passed = critique.verdict === "pass";
    return `<span class="badge ${passed ? "ok" : "warn"}">评审 ${Number(critique.score).toFixed(2)} · ${passed ? "通过" : "待改"}</span>`;
  }

  function proposalSourceSummary(proposals) {
    if (!proposals.length) return "可审批";
    const counts = { model: 0, fallback: 0, rule: 0, legacy: 0 };
    proposals.forEach((proposal) => {
      const source = proposal.agent_meta && proposal.agent_meta.source;
      counts[source] = (counts[source] || 0) + 1;
    });
    if (counts.legacy === proposals.length) return "历史方案 · 待审批";
    const parts = [];
    if (counts.model) parts.push(`模型 ${counts.model}`);
    if (counts.fallback) parts.push(`兜底 ${counts.fallback}`);
    if (counts.rule) parts.push(`规则 ${counts.rule}`);
    if (counts.legacy) parts.push(`历史 ${counts.legacy}`);
    return parts.join(" · ");
  }

  function toast(message, bad = false) {
    const el = $("toast");
    el.textContent = message;
    el.className = `toast ${bad ? "bad" : "ok"}`;
    el.classList.remove("hidden");
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(() => el.classList.add("hidden"), 4200);
  }

  function showOutput(text, bad = false) {
    const el = $("action-output");
    el.textContent = text;
    el.className = `output ${bad ? "bad" : ""}`;
    el.classList.remove("hidden");
  }

  async function api(path, method = "GET", body = null, options = {}) {
    const headers = {};
    if (body !== null) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === null ? undefined : JSON.stringify(body),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* ignore */ }
    if (response.status === 401 && !options.allow401) {
      showLogin();
      throw new Error("请先登录");
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  function showLogin() {
    $("login-view").classList.remove("hidden");
    $("app-view").classList.add("hidden");
    $("password-modal").classList.add("hidden");
    state.me = null;
    state.csrf = "";
    state.tenantId = "";
    state.overview = null;
    state.selected = null;
    window.setTimeout(() => $("username").focus(), 50);
  }

  function showApp() {
    $("login-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
    $("user-label").textContent = `${state.me.display_name} · ${state.me.role}`;
    const isAdmin = state.me.role === "admin";
    $("admin-panel").classList.toggle("hidden", !isAdmin);
    $("nav-admin").classList.toggle("hidden", !isAdmin);
  }

  // 左目录：滚动高亮当前区块；点击走原生锚点，避免额外滚动逻辑。
  function initRail() {
    const links = Array.from(document.querySelectorAll("[data-nav]"));
    const entries = links
      .map((link) => ({ link, section: document.getElementById(link.dataset.nav) }))
      .filter((entry) => entry.section);
    if (!entries.length) return;
    let ticking = false;
    const update = () => {
      ticking = false;
      const visible = entries.filter((entry) => entry.section.offsetParent !== null);
      if (!visible.length) return;
      const line = window.scrollY + 140;
      let current = visible[0];
      visible.forEach((entry) => {
        if (entry.section.getBoundingClientRect().top + window.scrollY <= line) current = entry;
      });
      links.forEach((link) => link.classList.toggle("active", link === current.link));
    };
    window.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }, { passive: true });
    update();
  }

  async function boot() {
    try {
      const me = await api("/api/me");
      state.me = me.user;
      state.csrf = me.csrf;
      showApp();
      await loadTenants();
    } catch (_) {
      showLogin();
    }
  }

  async function login(event) {
    event.preventDefault();
    $("login-error").textContent = "";
    try {
      const result = await api("/api/login", "POST", {
        username: $("username").value.trim(),
        password: $("password").value,
      }, { allow401: true });
      state.me = result.user;
      state.csrf = result.csrf;
      $("password").value = "";
      showApp();
      await loadTenants();
    } catch (error) {
      $("login-error").textContent = error.message;
    }
  }

  async function logout() {
    try { await api("/api/logout", "POST", {}); } catch (_) { /* ignore */ }
    showLogin();
  }

  async function loadTenants() {
    const data = await api("/api/tenants");
    state.tenants = data.tenants || [];
    const select = $("tenant-select");
    select.innerHTML = state.tenants
      .map((tenant) => `<option value="${esc(tenant.tenant_id)}">${esc(tenant.name)}</option>`)
      .join("");
    if (!state.tenants.length) {
      toast("当前账号没有可访问的店铺", true);
      return;
    }
    state.tenantId = state.tenants[0].tenant_id;
    select.value = state.tenantId;
    await loadOverview();
  }

  async function loadOverview() {
    if (!state.tenantId) return;
    const data = await api(`/api/tenants/${encodeURIComponent(state.tenantId)}/overview`);
    state.overview = data;
    state.selected = null;
    state.variantIndex = 0;
    state.replay = null;
    render();
    if (state.me && state.me.role === "admin") {
      loadUsers().catch((error) => toast(`用户列表加载失败：${error.message}`, true));
      loadAudit().catch((error) => toast(`审计加载失败：${error.message}`, true));
    }
  }

  function render() {
    const overview = state.overview;
    if (!overview) return;
    const tenant = overview.tenant || {};
    $("week-label").textContent = weekLabel();
    $("tenant-title").textContent = tenant.name || "运营总览";
    $("tenant-subtitle").textContent = `${overview.store.name} · ${overview.store.products} 个商品 / ${overview.store.skus} 个 SKU`;
    const status = $("site-status");
    if (overview.demo) {
      status.textContent = "演示模式 · 只读预览";
      status.className = "hero-badge demo";
    } else {
      status.textContent = tenant.site_configured ? "站点已连接" : "站点未配置";
      status.className = `hero-badge ${tenant.site_configured ? "ok" : ""}`;
    }

    const tasks = overview.task_orders || [];
    const proposals = overview.proposals || [];
    const variants = proposals.flatMap((proposal) => proposal.variants || []);
    const pending = variants.filter((variant) => {
      const compliance = variant.compliance || {};
      return variant.approvable !== false && compliance.severity && compliance.severity !== "block";
    }).length;
    $("pending-badge").textContent = `待审 ${pending} 版`;
    $("nav-tasks").textContent = tasks.length;
    $("nav-proposals").textContent = variants.length;
    $("tasks-count").textContent = `${tasks.length} 张`;
    $("proposals-count").textContent = `${variants.length} 版`;

    renderStats(overview);
    renderHealth(overview.checkup || {}, overview.metrics || {});
    renderTasks(tasks, proposals);
    renderProposals(proposals);
    // 默认选中第一个方案，右栏不再是空白；切换店铺时也不会残留上一家的详情。
    if (!state.selected && proposals.length) {
      state.selected = proposals[0];
      state.variantIndex = 0;
      renderProposals(proposals);
    }
    if (state.selected) {
      renderDetail();
    } else {
      const detail = $("detail");
      detail.className = "detail empty";
      detail.textContent = "先选一个方案";
    }
    renderRuns(overview);
    renderMemory(overview.memory || {});
    renderRailFoot(overview);
  }

  function renderRailFoot(overview) {
    const foot = $("rail-foot");
    const checkup = overview.checkup || {};
    if (!checkup.available) {
      foot.innerHTML = `<b>还没有扫描记录</b>先运行 director run`;
      return;
    }
    const generated = String(checkup.generated_at || "").replace("T", " ");
    const time = generated ? generated.slice(5, 16) : "—";
    const model = checkup.model || checkup.planner || "—";
    const calls = (overview.metrics && overview.metrics.trace && overview.metrics.trace.calls) || 0;
    foot.innerHTML = `<b>最近扫描 ${esc(time)}</b>
      ${esc(checkup.scanned ?? "—")} 个在售 · 命中 ${esc(checkup.matched ?? "—")}<br>
      ${esc(model)} · ${esc(calls)} 次调用`;
  }

  // 三个比率做主卡，其余压成一条辅助带；没有 metrics 的老数据退回旧卡片。
  const PRIMARY_METRICS = new Set(["completion", "block_rate", "verdict_rate"]);

  function renderStats(overview) {
    const metrics = (overview.metrics && overview.metrics.metrics) || [];
    const board = $("stats");
    const mini = $("stats-mini");

    if (!metrics.length) {
      const fallback = [
        ["商品", overview.store.products, "站点在售"],
        ["本周动作", (overview.task_orders || []).length, "TaskOrder"],
        ["方案", (overview.proposals || []).length, proposalSourceSummary(overview.proposals || [])],
      ];
      board.innerHTML = fallback.map(([label, value, note]) => `
        <div class="kpi">
          <div class="k-label"><span>${esc(label)}</span></div>
          <div class="k-value">${esc(value)}</div>
          <div class="k-note">${esc(note)}</div>
        </div>`).join("");
      mini.innerHTML = "";
      return;
    }

    const primary = metrics.filter((item) => PRIMARY_METRICS.has(item.key));
    const secondary = metrics.filter((item) => !PRIMARY_METRICS.has(item.key));
    board.innerHTML = primary.map((item) => {
      const ratio = Math.max(0, Math.min(1, Number(item.value) || 0));
      const alert = item.tone === "warn" ? " alert" : "";
      return `<div class="kpi${alert}">
        <div class="k-label"><span>${esc(item.label)}</span><span class="k-target">${esc(item.target)}</span></div>
        <div class="k-value">${esc(item.display)}</div>
        <div class="meter"><i data-width="${Math.round(ratio * 100)}"></i></div>
        <div class="k-note">${esc(item.note)}</div>
      </div>`;
    }).join("");

    applyWidths(board);

    mini.innerHTML = secondary.map((item) => `
      <span>${esc(item.label)} <b>${esc(item.display)}</b>
        <span class="hint">${esc(item.note)}</span>
      </span>`).join("");
  }

  function renderHealth(checkup, metrics) {
    const body = $("health-body");
    const badge = $("health-score-badge");
    if (!body) return;
    if (!checkup.available) {
      body.innerHTML = `<div class="empty">还没有体检记录，先运行 <code>director run</code>。</div>`;
      if (badge) badge.textContent = "";
      $("health-alerts").innerHTML = "";
      return;
    }
    const health = checkup.health || {};
    const quota = checkup.quota || {};
    const score = Number(health.score);
    const scoreText = Number.isFinite(score) && score >= 0 ? String(score) : "—";
    const tone = Number.isFinite(score) && score >= 0 ? (score >= 70 ? "ok" : score >= 50 ? "warn" : "danger") : "muted";
    const categories = checkup.categories || [];
    const weak = health.weak_categories || [];
    const selected = (checkup.matched || 0);
    if (badge) badge.textContent = `${scoreText} 分`;

    const categoryCards = categories.length
      ? categories.map((item) => `
        <div class="cat ${item.weak ? "weak" : ""}">
          <div class="c-name"><span>${esc(item.category)}</span>${item.weak ? "<em>弱</em>" : ""}</div>
          <div class="c-val">${esc(item.count ?? 0)} <small>个</small></div>
          ${item.problems.length
            ? `<div class="c-problems">${item.problems.map((p) => `${esc(p.label)}${p.count > 1 ? `×${p.count}` : ""}`).join("；")}</div>`
            : ""}
        </div>`).join("")
      : `<div class="cat"><div class="c-name"><span>没有类目异常</span></div><div class="c-val">0 <small>个</small></div></div>`;

    body.innerHTML = `
      <div class="health-top">
        <div class="score ${tone}">
          <b>${esc(scoreText)}</b>
          <span>健康度</span>
        </div>
        <div class="health-copy">
          <p class="health-summary">${esc(health.summary || "没有整体判断")}</p>
          ${health.top_problem ? `<p class="health-top"><span>最该动的</span>${esc(health.top_problem)}</p>` : ""}
          <p class="health-quota">
            <span>本周选品</span>
            <strong>${esc(quota.value ?? selected)}</strong>
            <span class="muted">（模型请求 ${esc(quota.requested ?? "—")}，区间 ${esc(quota.low ?? "?")}–${esc(quota.high ?? "?")}${quota.guarded ? "，已裁剪" : ""}）</span>
          </p>
          ${weak.length ? `<div class="weak">${weak.map((name) => `<span class="badge warn">${esc(name)}</span>`).join("")}</div>` : ""}
          ${(checkup.reasons || []).length ? `<details class="evidence"><summary>版本数理由 ${checkup.reasons.length} 条</summary><ul>${checkup.reasons.map((text) => `<li>${esc(text)}</li>`).join("")}</ul></details>` : ""}
          ${(checkup.notes || []).length ? `<details class="evidence"><summary>本轮备注</summary><ul>${checkup.notes.map((text) => `<li>${esc(text)}</li>`).join("")}</ul></details>` : ""}
        </div>
      </div>
      <div class="cat-grid">${categoryCards}</div>`;

    const alerts = (metrics.alerts || []).slice(0, 4);
    $("health-alerts").innerHTML = alerts.length
      ? alerts.map((text) => `<span class="alert-chip">${esc(text)}</span>`).join("")
      : `<span class="muted">四项运营指标均在阈值内</span>`;
  }

  function renderTasks(tasks, proposals = []) {
    const body = $("tasks-body");
    if (!tasks.length) {
      body.innerHTML = `<div class="empty">暂无任务单，先运行 supervisor</div>`;
      return;
    }
    const drafted = new Set((proposals || []).map((proposal) => String(proposal.product_id)));
    body.innerHTML = tasks.map((task, index) => {
      const band = Array.isArray(task.price_band_cents)
        ? `${money(task.price_band_cents[0])}–${money(task.price_band_cents[1])}`
        : "—";
      const meta = task.agent_meta || {};
      const strategy = meta.strategy || {};
      const evidence = (strategy.evidence || task.evidence || []);
      const points = Array.isArray(task.price_points) ? task.price_points : [];
      const label = task.problem_label || problemLabels[task.problem] || task.problem;
      const ready = drafted.has(String(task.product_id));
      return `<article class="task">
        <span class="idx">${String(index + 1).padStart(2, "0")}</span>
        <div class="t-body">
          <div class="t-title">${esc(task.title)}</div>
          <div class="t-meta">
            <span>${esc(label)}</span>
            <span>价格带 <i>${esc(band)}</i></span>
            <span>版数 <i>${esc(task.variant_min ?? "?")}–${esc(task.variant_max ?? "?")}</i></span>
            <span>评分 <i>${esc(Number(task.score || 0).toFixed(2))}</i></span>
          </div>
          <details class="evidence">
            <summary>假设与判据</summary>
            <dl class="task-grid">
              <div><dt>假设</dt><dd>${esc(task.hypothesis || "—")}</dd></div>
              <div><dt>成功判据</dt><dd>${esc(task.success_metric || "—")}${task.min_lift ? ` <span class="muted">（≥${Math.round(Number(task.min_lift) * 100)}%，${esc(task.window_days || "?")} 天）</span>` : ""}</dd></div>
              <div><dt>价位点</dt><dd>${points.length ? points.map((p) => money(p)).join(" / ") : "—"}</dd></div>
              <div><dt>版数理由</dt><dd>${esc(task.variant_reason || "—")}</dd></div>
            </dl>
            ${evidence.length ? `<ul>${evidence.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>` : ""}
          </details>
        </div>
        <div class="t-right">
          <span class="badge brand">${esc(task.variant_min ?? "?")}–${esc(task.variant_max ?? "?")} 版</span>
          ${sourceBadge(meta, true)}
          ${ready ? `<span class="badge ok">已出稿</span>` : `<span class="badge">待出稿</span>`}
        </div>
      </article>`;
    }).join("");
  }

  function renderProposals(proposals) {
    const list = $("proposals");
    if (!proposals.length) {
      list.innerHTML = `<div class="muted">暂无方案，先运行 workshop</div>`;
      return;
    }
    list.innerHTML = proposals.map((proposal) => {
      const active = state.selected && state.selected.product_id === proposal.product_id ? " active" : "";
      const selected = state.selected && state.selected.product_id === proposal.product_id;
      const summary = proposal.compliance_summary || {};
      const variants = proposal.variants || [];
      const total = summary.total ?? variants.length;
      const meta = proposal.agent_meta || {};
      // Problem types are model-invented since P1, so the static dictionary is
      // only a fallback for the legacy slugs; the task's own label wins.
      const problem = problemLabels[proposal.problem]
        || (proposal.task && proposal.task.problem_label)
        || proposal.problem;
      const prices = variants
        .map((item) => Number(item.price_cents))
        .filter((value) => Number.isFinite(value));
      const price = prices.length
        ? (Math.min(...prices) === Math.max(...prices)
          ? money(prices[0])
          : `${money(Math.min(...prices))}–${money(Math.max(...prices))}`)
        : "—";
      const dots = variants.slice(0, 6).map((item) => {
        const tone = (item.compliance || {}).severity || "missing";
        return `<i class="dot ${severityTone[tone] || "muted"}"></i>`;
      }).join("");
      const complianceText = summary.block
        ? `否决 ${summary.block}`
        : summary.missing
          ? `未审 ${summary.missing}`
          : summary.warn
            ? `提示 ${summary.warn}`
            : `合规 ${summary.approvable ?? total}/${total}`;
      const complianceTone = summary.block ? "danger" : summary.missing ? "muted" : summary.warn ? "warn" : "ok";
      return `<div class="proposal${active}" data-product="${esc(proposal.product_id)}"
        role="button" tabindex="0" aria-pressed="${selected ? "true" : "false"}">
        <div class="p-main">
          <div class="proposal-title">${esc(proposal.title)}</div>
          <div class="proposal-meta">
            <span class="badge warn">${esc(problem)}</span>
            <span>${total} 版</span>
            <span class="dots" aria-hidden="true">${dots}</span>
            <span class="badge ${complianceTone}">${esc(complianceText)}</span>
            ${(meta.source && meta.source !== "model") ? sourceBadge(meta, true) : ""}
          </div>
        </div>
        <div class="p-price">${esc(price)}</div>
      </div>`;
    }).join("");
    list.querySelectorAll("[data-product]").forEach((node) => {
      node.addEventListener("click", () => selectProposal(node.dataset.product));
      node.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
          event.preventDefault();
          selectProposal(node.dataset.product);
        }
      });
    });
  }

  function selectProposal(productId) {
    const proposal = (state.overview.proposals || []).find((item) => item.product_id === productId);
    if (!proposal) return;
    state.selected = proposal;
    state.variantIndex = 0;
    renderProposals(state.overview.proposals || []);
    renderDetail();
  }

  function renderAgentPanel(proposal) {
    const meta = proposal.agent_meta || {};
    const source = meta.source || "legacy";
    const strategy = meta.strategy || {};
    const critique = meta.critique || {};
    if (source === "legacy") {
      return `<p class="agent-note">该方案来自旧版本，未记录生成来源、策略与评审。重新运行 <code>agent run --planner llm</code> 后可查看完整链路。</p>`;
    }
    if (source === "rule") {
      return `<div class="agent-strip">${sourceBadge(meta)}<span class="hint">规则诊断与模板文案，未调用模型</span></div>`;
    }
    if (source === "fallback") {
      return `<div class="agent-strip">${sourceBadge(meta)}<span class="hint">模型未返回可用结果，本次保留规则诊断与规则文案</span></div>`;
    }

    const hasConfidence = strategy.confidence !== null && strategy.confidence !== undefined;
    const confidence = hasConfidence ? Number(strategy.confidence) : null;
    const problem = strategy.problem_label || problemLabels[strategy.problem] || strategy.problem || "";
    const angles = Array.isArray(strategy.angles) ? strategy.angles : [];
    const evidence = Array.isArray(strategy.evidence) ? strategy.evidence : [];
    const issues = Array.isArray(critique.issues) ? critique.issues : [];
    const badges = [
      sourceBadge(meta),
      critiqueBadge(meta),
      meta.revisions > 0 ? `<span class="badge">修订 ${esc(meta.revisions)} 轮</span>` : "",
      hasConfidence && Number.isFinite(confidence) ? `<span class="badge">置信度 ${Math.round(confidence * 100)}%</span>` : "",
    ].filter(Boolean).join("");

    const strategyBlock = strategy.hypothesis || strategy.action
      ? `<div class="strategy-box">
          <div class="strategy-head"><span class="strategy-kicker">模型策略</span><strong>${esc(problem)}</strong></div>
          ${strategy.hypothesis ? `<p class="strategy-line"><span>判断</span>${esc(strategy.hypothesis)}</p>` : ""}
          ${strategy.action ? `<p class="strategy-line"><span>动作</span>${esc(strategy.action)}</p>` : ""}
          ${angles.length ? `<ul class="angle-plan">${angles.map((item) =>
            `<li><strong>${esc(item.angle)}</strong>${item.focus ? `<span>${esc(item.focus)}</span>` : ""}</li>`
          ).join("")}</ul>` : ""}
          ${evidence.length ? `<details class="evidence"><summary>决策证据 ${evidence.length} 条</summary><ul>${evidence.map((text) => `<li>${esc(text)}</li>`).join("")}</ul></details>` : ""}
        </div>`
      : "";

    const critiqueBlock = critique.score !== null && critique.score !== undefined
      ? `<div class="critique-box">
          <div class="strategy-head"><span class="strategy-kicker">评审意见</span><strong>${critique.verdict === "pass" ? "通过" : "要求修订"}</strong></div>
          ${issues.length
            ? `<ul class="critique-issues">${issues.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>`
            : `<p class="hint">评审通过，没有遗留修改意见。</p>`}
        </div>`
      : "";

    return `<div class="agent-strip">${badges}</div>
      ${strategyBlock}
      ${critiqueBlock}`;
  }

  function renderDetail() {
    const detail = $("detail");
    const proposal = state.selected;
    if (!proposal) {
      detail.className = "detail empty";
      detail.textContent = "先选一个方案";
      return;
    }
    detail.className = "detail";
    const variants = proposal.variants || [];
    const index = Math.min(state.variantIndex, Math.max(variants.length - 1, 0));
    const variant = variants[index];
    if (!variant) {
      detail.textContent = "该方案没有版本";
      return;
    }
    const preview = variant.preview || {};
    const before = preview.title || {};
    const bullets = preview.bulletsEn || {};
    const price = preview.priceCents || {};
    const compliance = variant.compliance || {};
    const rows = variants.map((item, position) => {
      const tone = (item.compliance || {}).severity || "missing";
      const blocked = tone === "block" ? " blocked" : "";
      return `<button type="button" class="vrow ${position === index ? "active" : ""}${blocked}" data-variant="${position}" aria-pressed="${position === index ? "true" : "false"}">
        <b>v${position + 1}</b>
        <span>${esc(item.angle)}</span>
        <em>目标 +${Math.round(Number(item.expected_lift || 0) * 100)}%</em>
        <i class="dot ${severityTone[tone] || "muted"}" aria-hidden="true"></i>
      </button>`;
    }).join("");
    const canApprove = state.me && (state.me.role === "admin" || state.me.role === "approver");
    const approvable = variant.approvable !== false && compliance.severity && compliance.severity !== "block";
    const demo = Boolean(state.overview && state.overview.demo);

    const priceBefore = Number(price.before ?? 0);
    const priceAfter = Number(price.after ?? variant.price_cents ?? 0);
    const delta = priceBefore > 0 && priceAfter > 0
      ? `<em>${priceAfter >= priceBefore ? "+" : ""}${((priceAfter - priceBefore) / priceBefore * 100).toFixed(1)}%</em>`
      : "";

    detail.innerHTML = `
      <h3>${esc(proposal.title)}</h3>
      <p class="detail-sub">${esc(variant.angle)} · 第 ${index + 1}/${variants.length} 版</p>
      <div class="variant-list">${rows}</div>
      <div class="price-box">
        <b>${money(priceAfter)}</b>
        ${priceBefore ? `<span>原价 ${money(priceBefore)}</span>` : ""}
        ${delta}
      </div>
      ${renderComplianceMatrix(compliance)}
      <div class="detail-actions">
        ${canApprove
          ? `<button type="button" id="approve-button" class="warn" ${approvable ? "" : "disabled"}>签发审批令牌</button>`
          : ""}
        <button type="button" id="preview-button" class="ghost">预览服务端 diff</button>
      </div>
      ${canApprove && !approvable ? `<p class="hint">${compliance.severity === "block" ? "该版本被合规否决" : "该版本还没有合规结论"}，不能签发</p>` : ""}
      ${demo ? `<p class="hint">演示模式只读：可预览，不能发布。</p>` : `
      <div class="token-row">
        <input id="approval-token-input" placeholder="粘贴审批令牌 APR-..." autocomplete="off" aria-label="审批令牌">
        <button type="button" id="publish-button" class="good" ${approvable ? "" : "disabled"}>发布</button>
      </div>
      <p class="hint">发布需要有效令牌；没有令牌只能预览。</p>`}
      <details class="detail-details">
        <summary>完整文案与 diff</summary>
        <div class="diff-block">
          <div class="diff-label">标题</div>
          <div class="diff-before">${esc(before.before || "")}</div>
          <div class="diff-after">${esc(before.after || variant.title)}</div>
        </div>
        <div class="diff-block">
          <div class="diff-label">卖点</div>
          <div>${(bullets.after || variant.bullets || []).map((item) => `• ${esc(item)}`).join("<br>")}</div>
        </div>
        ${renderVariantNotes(variant)}
      </details>
      <details class="detail-details">
        <summary>方案依据与评审</summary>
        ${renderAgentPanel(proposal)}
      </details>
    `;

    detail.querySelectorAll("[data-variant]").forEach((node) => {
      node.addEventListener("click", () => {
        state.variantIndex = Number(node.dataset.variant);
        renderDetail();
      });
    });
    $("preview-button").addEventListener("click", () => previewProposal(proposal, index));
    if (canApprove && approvable) {
      $("approve-button").addEventListener("click", () => approveProposal(proposal, variant));
    }
    const publishButton = $("publish-button");
    if (publishButton && approvable) {
      publishButton.addEventListener("click", () => publishProposal(proposal, index));
    }
  }

  // Four checks, one row each. A collapsed single badge would let a warn on
  // one axis disappear behind three passes.
  function renderComplianceMatrix(compliance) {
    if (!compliance || !compliance.checked) {
      return `<div class="matrix unchecked">
        <span class="badge muted">未审</span>
        <span class="hint">该版本还没有合规结论，不能进入审批队列。运行 <code>review run</code> 后刷新。</span>
      </div>`;
    }
    const cells = checkOrder.map((key) => {
      const tone = compliance.checks[key] || "missing";
      return `<div class="matrix-cell ${severityTone[tone] || "muted"}">
        <span class="matrix-label">${esc(checkLabels[key])}</span>
        <span class="matrix-value">${esc(severityLabels[tone] || tone)}</span>
      </div>`;
    }).join("");
    const override = compliance.overridden_by === "code"
      ? `<div class="matrix-note danger">代码覆盖：模型判通过，硬禁改判否决</div>`
      : "";
    const findings = (compliance.findings || []).length
      ? `<ul class="findings">${compliance.findings.map((item) => `
          <li>
            <div class="finding-head"><span class="badge ${severityTone[compliance.checks[item.category]] || "muted"}">${esc(checkLabels[item.category] || item.category)}</span></div>
            ${item.quote ? `<div class="finding-quote">“${esc(item.quote)}”</div>` : ""}
            <div class="finding-rule">${esc(item.rule)}</div>
            ${item.suggestion ? `<div class="finding-fix">改法：${esc(item.suggestion)}</div>` : ""}
          </li>`).join("")}</ul>`
      : `<p class="hint">四类检查均无待处理项。</p>`;
    return `<div class="matrix">
      <div class="matrix-row">${cells}</div>
      ${override}
      ${findings}
    </div>`;
  }

  function renderVariantNotes(variant) {
    const brief = Array.isArray(variant.material_brief) ? variant.material_brief.filter(Boolean) : [];
    const check = variant.self_check && typeof variant.self_check === "object" ? variant.self_check : {};
    const checkLabelsSelf = { facts: "事实", differentiation: "差异", readability: "可读" };
    const checkRows = Object.keys(check)
      .filter((key) => check[key])
      .map((key) => `<li><span>${esc(checkLabelsSelf[key] || key)}</span>${esc(check[key])}</li>`)
      .join("");
    if (!brief.length && !variant.self_review && !checkRows) return "";
    return `
      ${brief.length ? `<details class="evidence" open><summary>素材 brief ${brief.length} 条</summary><ul>${brief.map((text) => `<li>${esc(text)}</li>`).join("")}</ul></details>` : ""}
      ${variant.self_review ? `<details class="evidence"><summary>自评与修订</summary><p>${esc(variant.self_review)}</p>${checkRows ? `<ul class="self-checks">${checkRows}</ul>` : ""}</details>` : ""}`;
  }

  async function approveProposal(proposal, variant) {
    try {
      const result = await api(`/api/tenants/${encodeURIComponent(state.tenantId)}/approve`, "POST", {
        product_id: proposal.product_id,
        variant_key: variant.variant_key,
        ttl: 86400,
      });
      const selectedId = proposal.product_id;
      const selectedIndex = Number(state.variantIndex || 0);
      showOutput(`审批令牌已签发（只显示这一次）\n${result.token}\n\n对象：${result.product_id} / ${result.variant_key}\n到期：${result.expires_at}`);
      toast("审批令牌已签发");
      await loadOverview();
      const next = (state.overview.proposals || []).find((item) => item.product_id === selectedId);
      if (next) {
        state.selected = next;
        state.variantIndex = selectedIndex;
        renderProposals(state.overview.proposals || []);
        renderDetail();
        const input = $("approval-token-input");
        if (input) input.value = result.token;
      }
    } catch (error) {
      showOutput(`签发失败：${error.message}`, true);
      toast(error.message, true);
    }
  }

  async function previewProposal(proposal, variantIndex) {
    try {
      const result = await api(`/api/tenants/${encodeURIComponent(state.tenantId)}/preview`, "POST", {
        product_id: proposal.product_id,
        variant_index: variantIndex,
      });
      showOutput(JSON.stringify(result, null, 2));
      toast("服务端 diff 已返回（未写入）");
    } catch (error) {
      showOutput(`预览失败：${error.message}`, true);
      toast(error.message, true);
    }
  }

  async function publishProposal(proposal, variantIndex) {
    const input = $("approval-token-input");
    const token = (input && input.value.trim()) || "";
    if (!token) {
      toast("请先粘贴审批令牌", true);
      return;
    }
    if (!window.confirm(`确认发布「${proposal.title}」？`)) return;
    try {
      const result = await api(`/api/tenants/${encodeURIComponent(state.tenantId)}/publish`, "POST", {
        product_id: proposal.product_id,
        variant_index: variantIndex,
        approval_token: token,
      });
      showOutput(JSON.stringify(result, null, 2), !result.ok);
      toast(result.ok ? "已发布" : `${statusLabels[result.status] || result.status}：${result.message}`, !result.ok);
      if (input) input.value = "";
      await loadOverview();
    } catch (error) {
      showOutput(`发布失败：${error.message}`, true);
      toast(error.message, true);
    }
  }

  // 发布与实验合并成一张表：同一个版本从“发出去”到“判出来”只占一行。
  function renderRuns(overview) {
    const publishes = overview.publishes || [];
    const board = overview.experiment_board || { observing: [], judged: [], summary: {} };
    const proposals = overview.proposals || [];
    const titles = new Map();
    const angles = new Map();
    proposals.forEach((proposal) => {
      titles.set(String(proposal.product_id), proposal.title);
      (proposal.variants || []).forEach((variant) => {
        if (variant.variant_key) angles.set(String(variant.variant_key), variant.angle);
      });
    });

    const rows = new Map();
    const ensure = (productId, variantKey) => {
      const key = `${productId}::${variantKey}`;
      if (!rows.has(key)) {
        rows.set(key, { productId, variantKey, experiment: null, publish: null, observing: false });
      }
      return rows.get(key);
    };

    (board.observing || []).forEach((item) => {
      if (!item.variant_key) return;
      const row = ensure(String(item.product_id), String(item.variant_key));
      row.experiment = item;
      row.observing = true;
    });
    (board.judged || []).forEach((item) => {
      if (!item.variant_key) return;
      ensure(String(item.product_id), String(item.variant_key)).experiment = item;
    });
    publishes.forEach((item) => {
      if (!item.product_id || !item.variant_key) return;
      ensure(String(item.product_id), String(item.variant_key)).publish = item;
    });

    const rank = (row) => (row.observing ? 0 : row.experiment ? 1 : 2);
    const list = [...rows.values()].sort((a, b) => {
      if (rank(a) !== rank(b)) return rank(a) - rank(b);
      const at = (a.publish && a.publish.published_at) || (a.experiment && a.experiment.started_at) || "";
      const bt = (b.publish && b.publish.published_at) || (b.experiment && b.experiment.started_at) || "";
      return String(bt).localeCompare(String(at));
    });

    const body = $("runs-body");
    $("runs-count").textContent = `${list.length} 条`;
    $("nav-runs").textContent = list.length;

    if (!list.length) {
      body.innerHTML = `<tr><td colspan="6" class="muted">还没有运行记录；发布成功后会在这里登记实验</td></tr>`;
      renderLessons(overview.lessons || []);
      return;
    }

    body.innerHTML = list.map((row) => {
      const item = row.experiment || {};
      const publish = row.publish || {};
      const title = titles.get(row.productId) || item.title || row.productId;
      const angle = angles.get(row.variantKey)
        || String(row.variantKey).replace(`${row.productId}-`, "")
        || "—";
      const when = publish.published_at
        ? String(publish.published_at).replace("T", " ").slice(5, 16)
        : item.started_at
          ? `开始 ${esc(item.started_at)}`
          : "";

      let status = { label: "—", tone: "" };
      if (row.observing) status = { label: "观察中", tone: "brand" };
      else if (row.experiment) status = { label: "已判定", tone: "" };
      else if (publish.status) status = { label: statusLabels[publish.status] || publish.status, tone: publish.status === "published" ? "ok" : publish.status === "preview" ? "" : "danger" };

      let metric = "—";
      if (item.metric_label) {
        metric = `${item.metric_label} +${Math.round(Number(item.min_lift || 0) * 100)}%`;
      } else if (publish.price_change_pct !== undefined && publish.price_change_pct !== null) {
        metric = `改价 ${(Number(publish.price_change_pct) * 100).toFixed(1)}%`;
      }

      const compare = item.baseline !== undefined && item.baseline !== null
        ? `${pct(item.baseline)} <span class="muted">→</span> ${item.current === null || item.current === undefined ? `<span class="muted">待积累</span>` : pct(item.current)}`
        : "—";

      let exposure = `<span class="muted">—</span>`;
      if (item.sample) {
        const impressions = Number(item.sample.impressions || 0);
        const required = Number(item.required_impressions || 0);
        const progress = required
          ? Math.min(1, impressions / required)
          : item.window_days
            ? Math.min(1, (item.window_days - (item.days_left || 0)) / item.window_days)
            : 0;
        exposure = `
          <div class="mini-bar ${progress >= 1 ? "ok" : ""}" role="img" aria-label="曝光进度 ${Math.round(progress * 100)}%"><i data-width="${Math.round(progress * 100)}"></i></div>
          <div class="cell-note">${num(impressions)} 曝光${required ? ` / ${num(required)}` : ""}</div>`;
      }

      let conclusion = `<span class="muted">未登记实验</span>`;
      if (row.observing) {
        conclusion = item.overdue
          ? `<span class="badge warn">已到期 · 待判定</span>`
          : `<span class="badge">还有 ${esc(item.days_left ?? "—")} 天</span>`;
      } else if (item.verdict) {
        const tone = item.verdict === "supported" ? "ok" : item.verdict === "rejected" ? "danger" : "warn";
        const label = item.verdict_cn || verdictLabels[item.verdict] || item.verdict;
        const p = item.p_value === undefined || item.p_value === null ? "" : `p=${Number(item.p_value).toFixed(3)}`;
        conclusion = `<span class="badge ${tone}">${esc(label)}</span>${p ? `<div class="cell-note">${esc(p)}</div>` : ""}`;
      }

      return `<tr>
        <td class="t-title-cell">
          <b>${esc(title)}</b>
          <span>${esc(angle)}${when ? ` · ${when}` : ""}</span>
        </td>
        <td><span class="badge ${status.tone}">${esc(status.label)}</span></td>
        <td>${esc(metric)}</td>
        <td class="num">${compare}</td>
        <td>${exposure}</td>
        <td>${conclusion}</td>
      </tr>`;
    }).join("");

    applyWidths($("runs-body"));

    const summary = board.summary || {};
    const tally = $("experiment-summary");
    if (tally) {
      tally.innerHTML = [
        ["观察中", summary.observing || 0, ""],
        ["成立", summary.supported || 0, "ok"],
        ["不成立", summary.rejected || 0, "danger"],
        ["不确定", summary.inconclusive || 0, "warn"],
        ["样本不足", summary.insufficient || 0, "muted"],
      ].map(([label, value, tone]) => `
        <span class="tally-item ${tone}"><strong>${esc(value)}</strong>${esc(label)}</span>`).join("");
    }

    renderLessons(overview.lessons || []);
  }

  function renderLessons(lessons) {
    const list = $("lessons-list");
    if (!list) return;
    if (!lessons.length) {
      list.innerHTML = `<li class="muted">经验库为空：实验判定成立或不成立后才会沉淀</li>`;
      return;
    }
    list.innerHTML = lessons.map((lesson) => `
      <li>
        <span class="badge">${esc(lesson.scope)}</span>
        ${esc(lesson.statement)}
        <span class="muted">证据 ${esc(lesson.evidence_count)} 条 · 置信 ${esc(lesson.confidence)}</span>
      </li>`).join("");
  }

  function renderMemory(memory) {
    const body = $("memory-body");
    if (!body) return;
    const health = memory.health || {};
    const decisions = memory.decisions || [];
    const lessons = memory.lessons || [];
    const snapshots = memory.snapshots || [];
    const hasProfile = Object.keys(memory.profile || {}).length > 0;
    const summary = $("memory-summary");
    if (summary) {
      summary.innerHTML = `
        <span class="chip">店铺画像 <b>${hasProfile ? 1 : 0}</b></span>
        <span class="chip">决策 <b>${decisions.length}</b></span>
        <span class="chip">经验 <b>${lessons.length}</b>${lessons.length ? `<span class="hint">${esc(lessons[0].statement).slice(0, 24)}…</span>` : ""}</span>
        <span class="chip">快照 <b>${snapshots.length}</b>${snapshots.length ? `<span class="hint">最近 ${esc(String(snapshots[0].saved_at || "").replace("T", " ").slice(5, 16))}</span>` : ""}</span>`;
    }
    const tabs = document.querySelectorAll("[data-memory-tab]");
    tabs.forEach((node) => {
      const active = node.dataset.memoryTab === state.memoryTab;
      node.classList.toggle("active", active);
      node.setAttribute("aria-selected", active ? "true" : "false");
    });
    const head = `<div class="memory-head">
      <span class="muted">${esc(health.root || "")}</span>
      ${health.bad_lines ? `<span class="badge danger">损坏行 ${esc(health.bad_lines)}</span>` : ""}
    </div>`;

    if (state.memoryTab === "decisions") {
      const decisions = memory.decisions || [];
      body.innerHTML = head + (decisions.length
        ? `<ol class="decision-list">${decisions.map((item) => `
            <li>
              <div class="decision-head">
                <strong>${esc(item.run_id || item.decision_id || "")}</strong>
                <span class="muted">${esc(item.created_at || "")}</span>
              </div>
              <div class="decision-body">扫描 ${esc(item.scanned ?? "—")} 个，选 ${esc(item.selected_count ?? "—")} 个${item.quota && item.quota.guarded ? "（名额被裁剪）" : ""}</div>
              ${item.reason ? `<div class="muted">${esc(item.reason)}</div>` : ""}
              ${item.store_health && item.store_health.summary ? `<div class="cell-note">${esc(item.store_health.summary)}</div>` : ""}
            </li>`).join("")}</ol>`
        : `<div class="empty">还没有决策记录</div>`);
      return;
    }

    if (state.memoryTab === "lessons") {
      const lessons = memory.lessons || [];
      body.innerHTML = head + (lessons.length
        ? `<ul class="lesson-cards">${lessons.map((lesson) => `
            <li>
              <div class="lesson-head"><span class="badge">${esc(lesson.scope)}</span><span class="muted">置信 ${esc(lesson.confidence)} · ${esc(lesson.direction || "")}</span></div>
              <p>${esc(lesson.statement)}</p>
              <div class="cell-note">证据 ${esc(lesson.evidence_count)} 条${lesson.evidence_refs ? ` · ${esc((lesson.evidence_refs || []).join(", "))}` : ""}</div>
            </li>`).join("")}</ul>`
        : `<div class="empty">经验库为空</div>`);
      return;
    }

    if (state.memoryTab === "snapshots") {
      const snapshots = memory.snapshots || [];
      const replay = state.replay;
      body.innerHTML = head + (snapshots.length
        ? `<div class="table-wrap">
            <table>
              <caption class="sr-only">记忆快照</caption>
              <thead><tr><th scope="col">run_id</th><th scope="col">保存时间</th><th scope="col">内容</th><th scope="col">操作</th></tr></thead>
              <tbody>${snapshots.map((item) => `
                <tr>
                  <td><code>${esc(item.run_id)}</code></td>
                  <td>${esc(item.saved_at || "—")}</td>
                  <td class="muted">实验 ${esc(item.experiments)} · 决策 ${esc(item.decisions)} · 经验 ${esc(item.lessons)}</td>
                  <td><button type="button" class="ghost" data-replay="${esc(item.run_id)}">回放</button></td>
                </tr>`).join("")}</tbody>
            </table>
          </div>`
        : `<div class="empty">还没有记忆快照；主管每轮会保存一份</div>`)
        + (replay ? renderReplay(replay) : "");
      body.querySelectorAll("[data-replay]").forEach((node) => {
        node.addEventListener("click", () => replaySnapshot(node.dataset.replay));
      });
      return;
    }

    const profile = memory.profile || {};
    const keys = Object.keys(profile).filter((key) => key !== "store_health");
    body.innerHTML = head + (keys.length
      ? `<dl class="profile-grid">${keys.map((key) => `
          <div><dt>${esc(profileLabels[key] || key)}</dt><dd>${esc(fmtValue(profile[key]))}</dd></div>`).join("")}</dl>
         ${profile.store_health && profile.store_health.summary ? `<p class="health-summary">${esc(profile.store_health.summary)}</p>` : ""}`
      : `<div class="empty">还没有店铺画像；主管第一轮后会写入</div>`);
  }

  function renderReplay(replay) {
    const profile = replay.store_profile || {};
    const decisions = replay.decisions || [];
    const lessons = replay.lessons || [];
    const experiments = replay.experiments || [];
    return `<div class="replay">
      <div class="replay-head">
        <strong>回放 ${esc(replay.run_id)}</strong>
        <span class="muted">${esc(replay.saved_at || "")}</span>
        <span class="badge">实验 ${experiments.length}</span>
        <span class="badge">决策 ${decisions.length}</span>
        <span class="badge">经验 ${lessons.length}</span>
      </div>
      <p class="muted">这是该轮主管调用时实际拿到的记忆原文。</p>
      <details class="evidence"><summary>店铺画像</summary><pre>${esc(JSON.stringify(profile, null, 2))}</pre></details>
      <details class="evidence"><summary>决策 ${decisions.length} 条</summary><pre>${esc(JSON.stringify(decisions, null, 2))}</pre></details>
      <details class="evidence"><summary>实验 ${experiments.length} 条</summary><pre>${esc(JSON.stringify(experiments, null, 2))}</pre></details>
      <details class="evidence"><summary>经验 ${lessons.length} 条</summary><pre>${esc(JSON.stringify(lessons, null, 2))}</pre></details>
    </div>`;
  }

  async function replaySnapshot(runId) {
    try {
      const result = await api(`/api/tenants/${encodeURIComponent(state.tenantId)}/memory/${encodeURIComponent(runId)}`);
      state.replay = result;
      renderMemory((state.overview && state.overview.memory) || {});
      toast("已加载记忆快照");
    } catch (error) {
      toast(`回放失败：${error.message}`, true);
    }
  }

  async function loadUsers() {
    const data = await api("/api/users");
    renderUsers(data.users || []);
  }

  function renderUsers(users) {
    const body = $("users-body");
    if (!users.length) {
      body.innerHTML = `<tr><td colspan="5" class="muted">暂无用户</td></tr>`;
      return;
    }
    body.innerHTML = users.map((user) => `
      <tr>
        <td>${esc(user.username)}${user.display_name && user.display_name !== user.username ? `<br><span class="muted">${esc(user.display_name)}</span>` : ""}</td>
        <td><span class="badge">${esc(user.role)}</span></td>
        <td>${esc((user.tenants || []).join(", "))}</td>
        <td><span class="badge ${user.disabled ? "danger" : "ok"}">${user.disabled ? "已停用" : "启用"}</span></td>
        <td>
          <div class="row-actions">
            <button data-user-action="toggle" data-username="${esc(user.username)}" data-disabled="${user.disabled}">${user.disabled ? "启用" : "停用"}</button>
            <button data-user-action="role" data-username="${esc(user.username)}" data-role="${esc(user.role)}">改角色</button>
            <button data-user-action="tenants" data-username="${esc(user.username)}" data-tenants="${esc((user.tenants || []).join(","))}">改租户</button>
            <button data-user-action="reset" data-username="${esc(user.username)}">重置密码</button>
          </div>
        </td>
      </tr>`).join("");
    body.querySelectorAll("[data-user-action]").forEach((button) => {
      button.addEventListener("click", () => handleUserAction(button));
    });
  }

  async function handleUserAction(button) {
    const username = button.dataset.username;
    const action = button.dataset.userAction;
    try {
      if (action === "toggle") {
        await api(`/api/users/${encodeURIComponent(username)}`, "PATCH", {
          disabled: button.dataset.disabled !== "true",
        });
      } else if (action === "role") {
        const role = window.prompt("角色：admin / approver / operator", button.dataset.role);
        if (!role) return;
        await api(`/api/users/${encodeURIComponent(username)}`, "PATCH", { role });
      } else if (action === "tenants") {
        const tenants = window.prompt("租户，逗号分隔", button.dataset.tenants || "globaltrade");
        if (!tenants) return;
        await api(`/api/users/${encodeURIComponent(username)}`, "PATCH", {
          tenants: tenants.split(",").map((item) => item.trim()).filter(Boolean),
        });
      } else if (action === "reset") {
        const password = window.prompt("新密码（至少 8 位）");
        if (!password) return;
        await api(`/api/users/${encodeURIComponent(username)}/password`, "POST", { password });
      }
      toast("用户已更新");
      await loadUsers();
      await loadAudit();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function loadAudit() {
    const data = await api("/api/audit?limit=100");
    renderAudit(data.logs || []);
  }

  function renderAudit(logs) {
    state.auditLogs = logs;
    const body = $("audit-body");
    if (!logs.length) {
      body.innerHTML = `<tr><td colspan="6" class="muted">暂无审计记录</td></tr>`;
      $("audit-count").textContent = "暂无记录";
      $("audit-more").hidden = true;
      return;
    }
    body.innerHTML = logs.map((item, index) => {
      const tone = item.status === "ok" ? "" : "danger";
      return `<tr class="${index >= 5 ? "audit-more" : ""}">
        <td>${esc((item.ts || "").replace("T", " ").replace("+00:00", "Z"))}</td>
        <td>${esc(item.actor)}</td>
        <td>${esc(item.action)}</td>
        <td>${esc(item.tenant || "—")}</td>
        <td>${esc(item.target || "—")}</td>
        <td><span class="badge ${tone}">${esc(item.status)}</span></td>
      </tr>`;
    }).join("");
    $("audit-count").textContent = `最近 5 条 / 共 ${logs.length}${logs.length >= 100 ? "+" : ""} 条`;
    $("audit-more").hidden = logs.length <= 5;
    setAuditExpanded(state.auditOpen);
  }

  // 收起只留 5 行；展开在面板内滚动，页面高度不变。
  function setAuditExpanded(open) {
    state.auditOpen = Boolean(open);
    const panel = $("audit-panel");
    if (!panel) return;
    panel.classList.toggle("expanded", state.auditOpen);
    $("audit-toggle").setAttribute("aria-expanded", state.auditOpen ? "true" : "false");
    const more = $("audit-more");
    more.setAttribute("aria-expanded", state.auditOpen ? "true" : "false");
    const total = state.auditLogs.length;
    if (total <= 5) {
      more.hidden = true;
      $("audit-foot-note").textContent = `共 ${total} 条，已全部显示`;
      return;
    }
    more.hidden = false;
    more.textContent = state.auditOpen ? "收起" : `展开全部 ${total} 条`;
    $("audit-foot-note").textContent = state.auditOpen
      ? "已展开：面板内滚动查看，页面高度不变"
      : "收起状态只显示最近 5 条；展开后在面板内滚动";
  }

  async function createUser(event) {
    event.preventDefault();
    try {
      const tenants = $("new-tenants").value.split(",").map((item) => item.trim()).filter(Boolean);
      await api("/api/users", "POST", {
        username: $("new-username").value.trim(),
        password: $("new-password").value,
        role: $("new-role").value,
        tenants,
      });
      $("user-form").reset();
      $("new-tenants").value = "globaltrade";
      toast("用户已创建");
      await loadUsers();
      await loadAudit();
    } catch (error) {
      toast(error.message, true);
    }
  }

  const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
  let passwordReturnFocus = null;

  function closePasswordModal() {
    $("password-modal").classList.add("hidden");
    if (passwordReturnFocus && typeof passwordReturnFocus.focus === "function") {
      passwordReturnFocus.focus();
    }
    passwordReturnFocus = null;
  }

  function openPasswordModal() {
    $("password-form").reset();
    $("password-error").textContent = "";
    passwordReturnFocus = document.activeElement;
    $("password-modal").classList.remove("hidden");
    window.setTimeout(() => $("old-password").focus(), 50);
  }

  // 弹窗内 Tab 循环 + Esc 关闭，避免焦点跑到背景内容
  document.addEventListener("keydown", (event) => {
    if ($("password-modal").classList.contains("hidden")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePasswordModal();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = Array.from($("password-modal").querySelectorAll(FOCUSABLE))
      .filter((node) => !node.disabled && node.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  async function changePassword(event) {
    event.preventDefault();
    $("password-error").textContent = "";
    const next = $("new-password-self").value;
    if (next !== $("confirm-password").value) {
      $("password-error").textContent = "两次新密码不一致";
      return;
    }
    try {
      await api("/api/me/password", "POST", {
        old_password: $("old-password").value,
        new_password: next,
      });
      closePasswordModal();
      toast("密码已修改，请重新登录");
      window.setTimeout(showLogin, 800);
    } catch (error) {
      $("password-error").textContent = error.message;
    }
  }

  $("login-form").addEventListener("submit", login);
  $("logout-button").addEventListener("click", logout);
  $("password-button").addEventListener("click", openPasswordModal);
  $("cancel-password").addEventListener("click", closePasswordModal);
  $("password-form").addEventListener("submit", changePassword);
  $("user-form").addEventListener("submit", createUser);
  $("refresh-audit").addEventListener("click", () => loadAudit().catch((error) => toast(error.message, true)));
  $("audit-toggle").addEventListener("click", () => setAuditExpanded(!state.auditOpen));
  $("audit-more").addEventListener("click", () => setAuditExpanded(!state.auditOpen));
  initRail();
  document.querySelectorAll("[data-memory-tab]").forEach((node) => {
    node.addEventListener("click", () => {
      state.memoryTab = node.dataset.memoryTab;
      renderMemory((state.overview && state.overview.memory) || {});
    });
  });
  $("export-audit").addEventListener("click", () => {
    window.location.href = "/api/audit/export?limit=5000";
  });
  $("tenant-select").addEventListener("change", async (event) => {
    state.tenantId = event.target.value;
    try {
      await loadOverview();
    } catch (error) {
      toast(error.message, true);
    }
  });

  boot();
})();
