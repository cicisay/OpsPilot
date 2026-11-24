// P4 截图验证：起服务 → 起无头 Edge → CDP 登录 → 截桌面/移动两张图 → 收尾。
// 单进程内完成，避免后台子进程被 shell 回收。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const OUT_DIR = process.argv[2] || ".";
const PORT = 8792;
const DEBUG_PORT = 9224;
const BASE = `http://127.0.0.1:${PORT}`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(url, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return true;
    } catch (_) {
      /* keep waiting */
    }
    await sleep(300);
  }
  throw new Error(`timeout waiting for ${url}`);
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
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
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text || "evaluate failed");
    }
    return result.result.value;
  }
}

async function shoot(cdp, name, width, height) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width < 600,
  });
  await sleep(600);
  const { data } = await cdp.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  const file = path.join(OUT_DIR, name);
  fs.writeFileSync(file, Buffer.from(data, "base64"));
  console.log("saved", file);
}

(async () => {
  const server = spawn(
    "python",
    ["run.py", "console", "serve", "--demo", "--port", String(PORT), "--db",
      path.join(process.env.TEMP || "/tmp", "p4console.db")],
    { cwd: process.cwd(), env: { ...process.env, PYTHONPATH: "src" }, stdio: "ignore" }
  );
  const edge = spawn(
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${path.join(process.env.TEMP || "/tmp", "edge-p4shot")}`,
      "--window-size=1440,1000",
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  try {
    await waitFor(`${BASE}/`);
    await waitFor(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
    const targets = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    const page = targets.find((item) => item.type === "page");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    const cdp = new CDP(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });

    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await sleep(1500);
    const login = await cdp.evaluate(`(async () => {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "admin", password: "p4-admin-pass" }),
      });
      return response.status + " " + (await response.text()).slice(0, 80);
    })()`);
    console.log("login:", login);

    await cdp.send("Page.reload", { ignoreCache: true });
    await sleep(3000);

    const probe = await cdp.evaluate(`(() => {
      const visible = (id) => {
        const node = document.getElementById(id);
        if (!node) return "missing";
        const rect = node.getBoundingClientRect();
        return Math.round(rect.height) + "x" + Math.round(rect.width);
      };
      return {
        appVisible: !document.getElementById("app-view").classList.contains("hidden"),
        stats: document.querySelectorAll("#stats .stat").length,
        healthCards: document.querySelectorAll("#health-body .health-card").length,
        tasks: document.querySelectorAll("#tasks-body .task-card").length,
        proposals: document.querySelectorAll("#proposals .proposal").length,
        observingRows: document.querySelectorAll("#experiments-observing tr").length,
        judgedRows: document.querySelectorAll("#experiments-body tr").length,
        memoryHeight: visible("memory-body"),
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      };
    })()`);
    console.log("probe:", JSON.stringify(probe));

    await shoot(cdp, "p4-desktop.png", 1440, 1000);

    // 选中第一个方案，展开合规矩阵
    await cdp.evaluate(`(() => {
      const node = document.querySelector("#proposals .proposal");
      if (node) node.click();
      return true;
    })()`);
    await sleep(800);
    await shoot(cdp, "p4-detail.png", 1440, 1200);

    // 记忆面板 → 快照 tab
    await cdp.evaluate(`(() => {
      const tab = document.querySelector('[data-memory-tab="snapshots"]');
      if (tab) tab.click();
      const replay = document.querySelector("[data-replay]");
      if (replay) replay.click();
      return true;
    })()`);
    await sleep(1200);
    await cdp.evaluate(`document.getElementById("memory-heading").scrollIntoView({block:"start"}); true`);
    await sleep(400);
    await shoot(cdp, "p4-memory.png", 1440, 1000);

    await shoot(cdp, "p4-mobile.png", 390, 900);
    const mobile = await cdp.evaluate(`({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      bodyOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    })`);
    console.log("mobile:", JSON.stringify(mobile));
    ws.close();
  } finally {
    edge.kill();
    server.kill();
  }
})().catch((error) => {
  console.error("FAILED:", error.message);
  process.exit(1);
});
