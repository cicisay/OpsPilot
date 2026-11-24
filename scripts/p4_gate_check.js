// P4 验收：在含 block / 未审版本的租户里断言界面确实把这两类版本锁住。
const { spawn } = require("child_process");
const path = require("path");

const PORT = 8793;
const DEBUG_PORT = 9225;
const BASE = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(url, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return true;
    } catch (_) { /* retry */ }
    await sleep(300);
  }
  throw new Error(`timeout: ${url}`);
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
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(`${method} timeout`)); }
      }, 20000);
    });
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
    return result.result.value;
  }
}

const snapshot = (selectors) => `(() => {
  const out = {};
  for (const [key, sel] of Object.entries(${JSON.stringify({})} || ${JSON.stringify(selectors)})) {
    out[key] = document.querySelector(sel) ? true : false;
  }
  return out;
})()`;

(async () => {
  const server = spawn("python",
    ["run.py", "console", "serve", "--demo", "--port", String(PORT),
      "--db", path.join(process.env.TEMP || "/tmp", "p4console.db")],
    { cwd: process.cwd(), env: { ...process.env, PYTHONPATH: "src" }, stdio: "ignore" });
  const edge = spawn("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    ["--headless=new", "--disable-gpu", "--no-first-run",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${path.join(process.env.TEMP || "/tmp", "edge-p4block")}`,
      "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });

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
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await sleep(1200);
    await cdp.evaluate(`fetch("/api/login", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({username:"admin", password:"p4-admin-pass"})}).then(r => r.status)`);
    await cdp.send("Page.reload", { ignoreCache: true });
    await sleep(2500);

    // 切到 blocktest 租户
    await cdp.evaluate(`(() => {
      const select = document.getElementById("tenant-select");
      select.value = "blocktest";
      select.dispatchEvent(new Event("change"));
      return true;
    })()`);
    await sleep(2000);

    const result = await cdp.evaluate(`(async () => {
      const read = () => {
        const approve = document.getElementById("approve-button");
        const cells = Array.from(document.querySelectorAll(".matrix-cell")).map((node) => ({
          label: node.querySelector(".matrix-label").textContent.trim(),
          value: node.querySelector(".matrix-value").textContent.trim(),
          tone: node.className.replace("matrix-cell", "").trim(),
        }));
        return {
          variant: document.querySelector(".variant-tabs [aria-pressed='true']").textContent.trim(),
          approvable: approve ? !approve.disabled : null,
          approveDisabledAttr: approve ? approve.disabled : null,
          unchecked: Boolean(document.querySelector(".matrix.unchecked")),
          cells,
          override: document.querySelector(".matrix-note") ? document.querySelector(".matrix-note").textContent.trim() : "",
          findings: document.querySelectorAll(".findings li").length,
          hint: document.querySelector(".detail-actions .hint") ? document.querySelector(".detail-actions .hint").textContent.trim() : "",
        };
      };
      const pick = (index) => {
        const tabs = document.querySelectorAll(".variant-tabs [data-variant]");
        tabs[index].click();
        return new Promise((resolve) => setTimeout(() => resolve(read()), 250));
      };
      document.querySelector("#proposals .proposal").click();
      await new Promise((resolve) => setTimeout(resolve, 500));
      const first = await pick(0);
      const second = await pick(1);
      const third = await pick(2);
      return { first, second, third };
    })()`);

    const expect = (label, actual, wanted) => {
      const ok = JSON.stringify(actual) === JSON.stringify(wanted);
      console.log(`${ok ? "PASS" : "FAIL"} ${label}: ${JSON.stringify(actual)}${ok ? "" : ` (want ${JSON.stringify(wanted)})`}`);
      return ok;
    };

    let allOk = true;
    allOk &= expect("v1 未审/通过 → 可签发", result.first.approvable, true);
    allOk &= expect("v2 block → 不可签发", result.second.approvable, false);
    allOk &= expect("v2 知识产权格为否决", result.second.cells.find((c) => c.label === "知识产权").value, "否决");
    allOk &= expect("v2 有代码覆盖提示", result.second.override.includes("代码覆盖"), true);
    allOk &= expect("v2 显示 findings", result.second.findings > 0, true);
    allOk &= expect("v3 未审 → 不可签发", result.third.approvable, false);
    allOk &= expect("v3 显示未审空态", result.third.unchecked, true);

    console.log(allOk ? "ALL OK" : "SOME CHECKS FAILED");

    // 回到 v2 截一张，留作界面证据
    await cdp.evaluate(`(() => {
      const tabs = document.querySelectorAll(".variant-tabs [data-variant]");
      tabs[1].click();
      document.getElementById("detail-heading").scrollIntoView({ block: "start" });
      return true;
    })()`);
    await sleep(600);
    const { data } = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    require("fs").writeFileSync(
      path.join(process.env.TEMP || "/tmp", "p4shots", "p4-block.png"),
      Buffer.from(data, "base64")
    );
    console.log("saved p4-block.png");

    process.exitCode = allOk ? 0 : 1;
    ws.close();
  } finally {
    edge.kill();
    server.kill();
  }
})().catch((error) => {
  console.error("FAILED:", error.message);
  process.exit(1);
});
