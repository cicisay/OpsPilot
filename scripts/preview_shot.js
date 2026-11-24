// 预览稿截图：静态 HTML → 无头 Edge → 整页 PNG。
// 不依赖后端，直接 file:// 打开，避免截图时被服务状态干扰。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const htmlPath = path.resolve(process.argv[2] || "preview/layout-v2.html");
const outPath = path.resolve(process.argv[3] || "preview/layout-v2.png");
const DEBUG_PORT = 9231;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(url, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) return true;
    } catch (_) {}
    await sleep(200);
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
    const res = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.text || "evaluate failed");
    return res.result.value;
  }
}

(async () => {
  const edge = spawn(
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--force-device-scale-factor=2",
      "--no-first-run",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${path.join(process.env.TEMP || "/tmp", "edge-preview")}`,
      "--window-size=1440,1000",
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  try {
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
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 2,
      mobile: false,
    });

    await cdp.send("Page.navigate", { url: `file:///${htmlPath.replace(/\\/g, "/")}` });
    await sleep(1200);

    const size = await cdp.evaluate(`({
      w: document.documentElement.clientWidth,
      h: document.documentElement.scrollHeight,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth
    })`);
    console.log("page:", JSON.stringify(size));

    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: size.w,
      height: size.h,
      deviceScaleFactor: 2,
      mobile: false,
    });
    await sleep(500);

    const { data } = await cdp.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
    });
    fs.writeFileSync(outPath, Buffer.from(data, "base64"));
    console.log("saved", outPath, (fs.statSync(outPath).size / 1024).toFixed(0) + "KB");

    // 审计区特写：按元素包围盒裁切，四周留 16px 呼吸位
    const box = await cdp.evaluate(`(() => {
      const el = document.querySelector(".audit");
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
    })()`);
    if (box) {
      const pad = 16;
      const clip = {
        x: Math.max(0, box.x - pad),
        y: Math.max(0, box.y - pad),
        width: box.w + pad * 2,
        height: box.h + pad * 2,
        scale: 2,
      };
      const shot = await cdp.send("Page.captureScreenshot", {
        format: "png",
        clip,
        captureBeyondViewport: true,
      });
      const detailPath = outPath.replace(/\.png$/, "-audit.png");
      fs.writeFileSync(detailPath, Buffer.from(shot.data, "base64"));
      console.log("saved", detailPath, (fs.statSync(detailPath).size / 1024).toFixed(0) + "KB");
    }
    ws.close();
  } finally {
    edge.kill();
  }
})().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
