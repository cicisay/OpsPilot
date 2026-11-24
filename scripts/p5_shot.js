// P5 前端改版验证：临时库起服务 → 登录 → 整页桌面/移动截图 + 折叠断言。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const OUT_DIR = path.resolve(process.argv[2] || "preview/p5");
const DB = process.argv[3] || path.join(process.env.TEMP || "/tmp", "opspilot-preview.db");
const PORT = 8794;
const DEBUG_PORT = 9232;
const BASE = `http://127.0.0.1:${PORT}`;
const PASSWORD = "preview-pass-2026";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT_DIR, { recursive: true });

async function waitFor(url, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) return true;
    } catch (_) {}
    await sleep(250);
  }
  throw new Error(`timeout waiting for ${url}`);
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out`));
        }
      }, 20000);
    });
  }
  async evaluate(expression) {
    const res = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.text || "evaluate failed");
    return res.result.value;
  }
}

async function fullShot(cdp, name, width) {
  await cdp.send("Emulation.setDeviceMetricsOverride", { width, height: 1000, deviceScaleFactor: 2, mobile: width < 600 });
  await sleep(500);
  const size = await cdp.evaluate(`({ w: document.documentElement.clientWidth, h: document.documentElement.scrollHeight })`);
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: size.w, height: size.h, deviceScaleFactor: 2, mobile: width < 600 });
  await sleep(400);
  const { data } = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const file = path.join(OUT_DIR, name);
  fs.writeFileSync(file, Buffer.from(data, "base64"));
  console.log("saved", file, (fs.statSync(file).size / 1024).toFixed(0) + "KB", JSON.stringify(size));
}

(async () => {
  const server = spawn("python", ["run.py", "console", "serve", "--port", String(PORT), "--db", DB], {
    cwd: process.cwd(),
    env: { ...process.env, PYTHONPATH: "src" },
    stdio: "ignore",
  });
  const edge = spawn(
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    [
      "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${path.join(process.env.TEMP || "/tmp", "edge-p5shot")}`,
      "--window-size=1440,1000", "about:blank",
    ],
    { stdio: "ignore" }
  );

  try {
    await waitFor(`${BASE}/`);
    await waitFor(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
    const targets = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    const page = targets.find((t) => t.type === "page");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    const cdp = new CDP(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 2, mobile: false });
    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await sleep(1200);

    const login = await cdp.evaluate(`(async () => {
      const res = await fetch("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "admin", password: ${JSON.stringify(PASSWORD)} }),
      });
      return res.status + " " + (await res.text()).slice(0, 60);
    })()`);
    console.log("login:", login);
    await cdp.send("Page.reload", { ignoreCache: true });
    await sleep(2500);

    const probe = await cdp.evaluate(`(() => {
      const visibleRows = (sel) => Array.from(document.querySelectorAll(sel))
        .filter((tr) => getComputedStyle(tr).display !== "none").length;
      const auditPanel = document.getElementById("audit-panel");
      return {
        app: !document.getElementById("app-view").classList.contains("hidden"),
        kpis: document.querySelectorAll("#stats .kpi").length,
        mini: document.querySelectorAll("#stats-mini > span").length,
        healthCards: document.querySelectorAll("#health-body .cat").length,
        tasks: document.querySelectorAll("#tasks-body .task").length,
        proposals: document.querySelectorAll("#proposals .proposal").length,
        detail: (document.getElementById("detail").textContent || "").trim().slice(0, 30),
        runs: document.querySelectorAll("#runs-body tr").length,
        auditTotal: document.querySelectorAll("#audit-body tr").length,
        auditVisible: visibleRows("#audit-body tr"),
        auditCount: document.getElementById("audit-count").textContent,
        auditExpanded: auditPanel.classList.contains("expanded"),
        auditHeight: Math.round(auditPanel.getBoundingClientRect().height),
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      };
    })()`);
    console.log("probe:", JSON.stringify(probe));

    await fullShot(cdp, "p5-desktop.png", 1440);

    // 交互回归：切换方案 → 切换版本，右栏标题与价格必须跟着变
    const interact = await cdp.evaluate(`(() => {
      const items = document.querySelectorAll("#proposals .proposal");
      items[1].click();
      const title = (document.querySelector("#detail h3") || {}).textContent || "";
      const rows = document.querySelectorAll("#detail .vrow");
      if (rows[1]) rows[1].click();
      const price = (document.querySelector("#detail .price-box b") || {}).textContent || "";
      const active = document.querySelector("#detail .vrow.active b");
      items[0].click();
      return { title: title.slice(0, 40), variants: rows.length, price, active: active ? active.textContent : "" };
    })()`);
    console.log("interact:", JSON.stringify(interact));
    await sleep(300);

    // 展开审计：行数应全部可见，面板高度受 420 限制
    await cdp.evaluate(`document.getElementById("audit-toggle").click(); true`);
    await sleep(500);
    const expanded = await cdp.evaluate(`(() => {
      const panel = document.getElementById("audit-panel");
      const visible = Array.from(document.querySelectorAll("#audit-body tr"))
        .filter((tr) => getComputedStyle(tr).display !== "none").length;
      return {
        expanded: panel.classList.contains("expanded"),
        aria: document.getElementById("audit-toggle").getAttribute("aria-expanded"),
        visible,
        panelHeight: Math.round(panel.getBoundingClientRect().height),
        label: document.getElementById("audit-more").textContent,
      };
    })()`);
    console.log("expanded:", JSON.stringify(expanded));
    await cdp.evaluate(`document.getElementById("audit-more").click(); true`);
    await sleep(300);

    await fullShot(cdp, "p5-mobile.png", 390);
    const mobile = await cdp.evaluate(`({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      railHeight: Math.round(document.querySelector(".rail").getBoundingClientRect().height),
      auditVisible: Array.from(document.querySelectorAll("#audit-body tr")).filter((tr) => getComputedStyle(tr).display !== "none").length,
    })`);
    console.log("mobile:", JSON.stringify(mobile));

    ws.close();
  } finally {
    edge.kill();
    server.kill();
  }
})().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
