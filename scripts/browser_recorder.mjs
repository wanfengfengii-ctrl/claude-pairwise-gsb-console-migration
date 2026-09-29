import { chromium } from "playwright";
import ffmpegPath from "ffmpeg-static";
import { spawn } from "node:child_process";
import { copyFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import {
  automaticFinishDelayMs, browserWorkflowEvidence, DESTRUCTIVE_CONTROL_PATTERN, GENERIC_CONTROL_SELECTOR, finalizeInteractionEvidence, humanClickPauseMs, isGenericActionLabel,
  isFileUploadSubmitLabel, isGenericSampleLabel, isLikelyValidSampleLabel,
  isNewlyRevealedControl, isPrimarySubmitLabel,
  isResultNavigationLabel, isWithdrawnResultText,
  isSafeFeatureControl, matchesRecordingLabel,
  recordingControlKey, recordingControlIdentity, recordingTraversalComplete,
  repeatedRecordingControlFamily, shouldTraverseChoiceControl,
  shouldPrepareInputsAfterControl,
} from "./recording_timing.mjs";
import { rankOpenApiOperations, sampleValue } from "./recording_openapi.mjs";

const [url, outputPath, profileDir, maximumRaw = "88", stopFile = `${outputPath}.stop`, interactionMode = "auto", apiDemoRaw = "", uploadFixture = "", arm = ""] = process.argv.slice(2);
if (!url || !outputPath || !profileDir) {
  console.error("usage: browser_recorder.mjs URL OUTPUT PROFILE [MAX_SECONDS]");
  process.exit(2);
}
let apiDemo = null;
if (apiDemoRaw) {
  try {
    const parsed = JSON.parse(apiDemoRaw);
    if ((parsed?.path && parsed?.method) || (Array.isArray(parsed?.steps) && parsed.steps.length)) apiDemo = parsed;
  } catch (error) {
    console.error(`invalid API demo descriptor: ${error?.message || error}`);
    process.exit(2);
  }
}

const maximum = Math.max(5, Math.min(88, Number(maximumRaw) || 88));
let context;
let recordedVideo;
let stopping = false;
let finishing = false;
let demonstrationPromise = Promise.resolve({ required: false, ok: true });
let monitorRequests = false;
const successfulRequests = [];
const failedPageAssets = [];
const recordingStartedAt = Date.now();
let visibleRecordingStartedAt = recordingStartedAt;
let clickSequence = 0;

async function saveVideo(sourcePath, targetPath) {
  if (!targetPath.toLowerCase().endsWith(".mp4")) {
    await copyFile(sourcePath, targetPath);
    return;
  }
  await new Promise((resolve, reject) => {
    const command = spawn(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-y", "-i", sourcePath,
      "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "medium",
      "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", targetPath,
    ]);
    let error = "";
    command.stderr.on("data", (chunk) => { error += chunk.toString(); });
    command.on("error", reject);
    command.on("close", (code) => code === 0 ? resolve() : reject(new Error(`MP4 转换失败：${error.slice(-2000)}`)));
  });
}

function fallbackBodyForPath(path) {
  if (path === "/plan") {
    return {
      targets: [
        { id: 1, duration: 3, value: 5, windows: [{ open: 0, close: 10 }] },
        { id: 2, duration: 3, value: 8, windows: [{ open: 0, close: 10 }] },
      ],
      slew: { from_night_start: [0, 0], between_targets: [[0, 1], [1, 0]] },
    };
  }
  if (path === "/api/v1/plan") {
    return {
      targets: [
        { id: 1, exposure: 5, science_value: 9, windows: [[0, 6]] },
        { id: 2, exposure: 2, science_value: 6, windows: [[0, 3]] },
        { id: 3, exposure: 2, science_value: 6, windows: [[2, 5]] },
      ],
      slews: {
        night_start: [0, 0, 0],
        1: [0, 10, 10], 2: [10, 0, 0], 3: [10, 10, 0],
      },
    };
  }
  if (path === "/phase") {
    return {
      markers: ["m1", "m2"],
      father: { genotypes: ["AC", "TT"] },
      mother: { genotypes: ["GG", "AC"] },
      children: [{ name: "k", genotypes: ["AG", "TC"] }],
    };
  }
  if (path === "/api/v1/phase") {
    return {
      markers: ["rs01", "rs02", "rs03"],
      father: ["0/1", "0/1", "0/0"],
      mother: ["0/0", "0/1", "0/0"],
      children: [{ id: "proband", genotypes: ["0/0", "0/1", "0/0"] }],
    };
  }
  if (/linearizability\/check\/?$/i.test(path)) {
    return {
      initial_value: 0,
      operations: [
        { id: "w", type: "write", value: 1, invoke: 0, respond: 2 },
        { id: "r", type: "read", value: 1, invoke: 3, respond: 4 },
      ],
    };
  }
  if (path === "/analyze") {
    return {
      endpoints: [
        { grain_left: 1, grain_right: 2 },
        { grain_left: 2, grain_right: 1 },
      ],
      candidates: [{ endpoint_a: 0, endpoint_b: 1, cost: 3 }],
    };
  }
  if (/\/stitch\/?$/i.test(path)) {
    return {
      endpoints: [
        { id: "e0", left_grain: "g1", right_grain: "g2" },
        { id: "e1", left_grain: "g2", right_grain: "g1" },
      ],
      candidates: [{ a: "e0", b: "e1", cost: 3 }],
    };
  }
  if (path === "/api/plans") {
    return {
      switches: ["s1", "s2"], ingresses: ["s1"],
      old_next: { s1: "s2", s2: "DELIVER" },
      new_next: { s1: "DELIVER", s2: "DELIVER" },
      idempotency_key: `recording-plan-${Date.now()}`,
    };
  }
  if (path === "/plans") {
    return {
      idempotency_key: `recording-plan-${Date.now()}`,
      topology: {
        switches: [
          { id: "s1", old_next: "s2", new_next: "DELIVER" },
          { id: "s2", old_next: "DELIVER", new_next: "DELIVER" },
        ],
        ingresses: ["s1"],
      },
    };
  }
  if (/align/i.test(path)) {
    return { planned: [{ code: "A", at_ms: 0 }], actual: [{ code: "A", at_ms: 0 }] };
  }
  if (/turnpike/i.test(path)) {
    return { L: 10, n: 5, distances: [2, 4, 7, 10, 2, 5, 8, 3, 6, 3] };
  }
  return {};
}

async function demonstrateBareJsonApiWorkflow(page) {
  const rootPayload = await page.evaluate(() => {
    const raw = document.body?.innerText || "";
    try { return JSON.parse(raw); } catch { return null; }
  });
  if (!rootPayload || typeof rootPayload !== "object") return { required: false, ok: true };
  let declarations = rootPayload.endpoints && typeof rootPayload.endpoints === "object"
    ? Object.entries(rootPayload.endpoints)
    : [];
  if (!declarations.length && /health/i.test(new URL(page.url()).pathname)) {
    declarations = [["health", `GET ${new URL(page.url()).pathname}`]];
    const discovered = await page.evaluate(async (paths) => {
      const found = [];
      for (const path of paths) {
        try {
          const response = await fetch(path, {
            method: "POST", headers: { "content-type": "application/json" }, body: "{}",
          });
          if (![404, 405, 501].includes(response.status) && response.status < 500) found.push(path);
        } catch {}
      }
      return found;
    }, [
      "/plan", "/api/v1/plan", "/phase", "/api/v1/phase",
      "/api/v1/linearizability/check", "/api/plans", "/plans", "/analyze", "/api/analyze",
    ]);
    if (discovered[0]) declarations.push(["执行业务功能", `POST ${discovered[0]}`]);
  }
  const operations = declarations.map(([name, declaration]) => {
    const text = String(declaration || "").trim();
    const keyText = String(name || "").trim();
    const match = text.match(/^(GET|POST|PUT|PATCH|DELETE)\s+(\/\S*)/i)
      || keyText.match(/^(GET|POST|PUT|PATCH|DELETE)\s+(\/\S*)/i);
    const path = match ? match[2] : (text.startsWith("/") ? text : "");
    const method = match ? match[1].toUpperCase() : (/health/i.test(name) ? "GET" : "POST");
    return { name, method, path, body: method === "GET" ? null : fallbackBodyForPath(path) };
  }).filter((item) => item.path && ["GET", "POST", "PUT", "PATCH"].includes(item.method));
  if (!operations.length) return { required: false, ok: true };

  await page.evaluate((items) => {
    document.body.innerHTML = "";
    Object.assign(document.body.style, {
      margin: "0", background: "#f4f7f5", color: "#17211b",
      font: "16px/1.5 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif",
    });
    const main = document.createElement("main");
    Object.assign(main.style, { width: "1040px", margin: "0 auto", padding: "34px 0 60px" });
    main.innerHTML = `<header style="margin-bottom:22px"><p style="margin:0;color:#47725b">纯后端服务 · 真实接口操作</p>
      <h1 style="margin:5px 0 4px;font-size:30px">${String(document.title || "API 功能验收")}</h1>
      <p style="margin:0;color:#607066">依次点击接口并展示实际 HTTP 响应。</p></header>`;
    for (const item of items) {
      const card = document.createElement("section");
      Object.assign(card.style, {
        margin: "14px 0", padding: "18px 20px", border: "1px solid #cbd8d0",
        borderRadius: "14px", background: "white", boxShadow: "0 8px 24px rgba(31,63,45,.07)",
      });
      card.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;gap:18px">
        <div><strong style="font-size:18px">${item.name}</strong>
        <div style="margin-top:5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#365745">${item.method} ${item.path}</div></div>
        <button type="button" style="border:0;border-radius:10px;background:#176b47;color:white;padding:11px 18px;font-size:16px;cursor:pointer">发送真实请求</button></div>
        ${item.body === null ? "" : `<pre style="margin:14px 0 0;padding:12px;background:#f5f7f6;border-radius:9px;white-space:pre-wrap">${JSON.stringify(item.body, null, 2)}</pre>`}
        <pre data-result style="display:none;margin:14px 0 0;padding:12px;max-height:180px;overflow:auto;background:#0d1711;color:#d9eee1;border-radius:9px;white-space:pre-wrap">等待请求</pre>`;
      const result = card.querySelector("[data-result]");
      card.querySelector("button").addEventListener("click", async () => {
        result.style.display = "block";
        result.textContent = "请求中…";
        try {
          const response = await fetch(item.path, {
            method: item.method,
            headers: item.body === null ? undefined : { "content-type": "application/json" },
            body: item.body === null ? undefined : JSON.stringify(item.body),
          });
          const text = await response.text();
          card.dataset.status = String(response.status);
          result.textContent = `HTTP ${response.status}\n${text.slice(0, 2400)}`;
        } catch (error) {
          card.dataset.status = "0";
          result.textContent = String(error);
        }
      });
      main.appendChild(card);
    }
    document.body.appendChild(main);
  }, operations);

  const cards = page.locator("main section");
  const results = [];
  for (let index = 0; index < await cards.count(); index += 1) {
    const card = cards.nth(index);
    await moveAndClick(page, card.locator("button"));
    await page.waitForFunction((position) => Boolean(document.querySelectorAll("main section")[position]?.dataset.status), index);
    const status = Number(await card.getAttribute("data-status") || 0);
    results.push({ ...operations[index], status, ok: status >= 200 && status < 300 });
    await humanScrollIntoView(page, card.locator("[data-result]"));
    await page.waitForTimeout(700);
  }
  const business = results.filter((item) => !/health/i.test(item.name));
  const ok = (business.length ? business : results).some((item) => item.ok);
  const summary = {
    required: true, ok, workflow: "bare-json-api-operations",
    featureCount: results.length, clicks: results.length,
    requests: results.filter((item) => item.ok).length,
    operations: results.map(({ method, path, status, ok }) => ({ method, path, status, ok })),
    error: "没有成功完成业务接口请求",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...summary })}\n`);
  return summary;
}

async function humanScrollIntoView(page, locator) {
  const target = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const top = window.scrollY + rect.top;
    const centered = top - Math.max(36, (window.innerHeight - Math.min(rect.height, window.innerHeight * 0.7)) / 2);
    return Math.max(0, Math.min(document.documentElement.scrollHeight - window.innerHeight, centered));
  });
  const current = await page.evaluate(() => window.scrollY);
  const distance = target - current;
  if (Math.abs(distance) < 12) return;
  const steps = Math.max(7, Math.min(13, Math.ceil(Math.abs(distance) / 85)));
  for (let index = 0; index < steps; index += 1) {
    await page.mouse.wheel(0, distance / steps);
    await page.waitForTimeout(105 + ((index * 37) % 55));
  }
  await page.waitForTimeout(420);
}

async function moveAndClick(page, locator) {
  await humanScrollIntoView(page, locator);
  const box = await locator.boundingBox();
  if (!box) throw new Error("目标控件不可见");
  const sequence = clickSequence;
  clickSequence += 1;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
    steps: 26 + (sequence % 9),
  });
  await page.waitForTimeout(humanClickPauseMs(sequence, "before"));
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(humanClickPauseMs(sequence, "after"));
}

function emitBrowserWorkflow(result) {
  const evidence = browserWorkflowEvidence(result, successfulRequests.length);
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...evidence })}\n`);
  return evidence;
}

async function demonstrateStainRestorationWorkflow(page) {
  const clicks = [];
  const filled = [];
  const newGrid = page.locator("#new-grid");
  if (await newGrid.count()) {
    await moveAndClick(page, newGrid);
    clicks.push("新建网格");
  }
  await page.locator('input[data-row="0"]').fill("1");
  await page.locator('input[data-col="0"]').fill("1");
  filled.push("首行潮湿数", "首列潮湿数");
  const cell = page.locator('[data-r="0"][data-c="0"]').first();
  await moveAndClick(page, cell);
  clicks.push("标注首格潮湿");
  const solve = page.locator("#solve-btn, #solve").first();
  await moveAndClick(page, solve);
  clicks.push("复原");
  const result = await page.locator("#result-panel:not([hidden]), #result").first();
  await result.getByText("唯一结论", { exact: false }).first().waitFor({ state: "visible", timeout: 20000 });
  await humanScrollIntoView(page, result);
  const finalResultVisible = await result.isVisible()
    && /唯一结论/.test(await result.innerText());
  return emitBrowserWorkflow({ ok: finalResultVisible, clicks, filled,
    featureCount: clicks.length + filled.length, resultControlCount: 0,
    controlsComplete: true, finalResultVisible, visibleChange: true,
    primaryFeatureVerified: finalResultVisible, error: finalResultVisible ? "" : "复原结论不可见" });
}

async function demonstrateDesalinationWorkflow(page) {
  const clicks = [];
  const filled = [];
  const demo = page.locator("#load-demo");
  if (await demo.count()) {
    await moveAndClick(page, demo);
    clicks.push("载入示例");
    await moveAndClick(page, page.getByRole("button", { name: "建立方案", exact: true }));
    clicks.push("建立方案");
    await page.locator("form.round-form").first().waitFor({ state: "visible", timeout: 10000 });
    for (let round = 0; round < 3; round += 1) {
      const form = page.locator("form.round-form").first();
      const values = form.locator('input[data-field="value"]');
      const dates = form.locator('input[data-field="ts"]');
      const count = await values.count();
      for (let index = 0; index < count; index += 1) {
        await values.nth(index).fill(String(400 - round * 100 - index));
        await dates.nth(index).fill(`2030-01-01T${String(10 + round).padStart(2, "0")}:${String(index).padStart(2, "0")}`);
      }
      filled.push(`第 ${round + 1} 轮完整读数`);
      const submit = form.locator('button[type="submit"]');
      await moveAndClick(page, submit);
      clicks.push(`提交第 ${round + 1} 轮读数`);
      if (round < 2) await page.locator("form.round-form").first()
        .getByRole("button", { name: new RegExp(`提交第\\s*${round + 2}\\s*轮`) })
        .waitFor({ state: "visible", timeout: 10000 });
    }
  } else {
    await page.locator('input[placeholder="如：三号浸泡槽"]').fill("录像演示槽");
    const names = page.locator('input[placeholder="器物名称"]');
    for (let index = 0; index < await names.count(); index += 1) {
      await names.nth(index).fill(`器物${index + 1}`);
    }
    filled.push("槽位与器物初始归属");
    await moveAndClick(page, page.getByRole("button", { name: "建立槽位", exact: true }));
    clicks.push("建立槽位");
    for (let round = 0; round < 3; round += 1) {
      const values = page.locator('.submit-form input[type="number"]');
      const count = await values.count();
      for (let index = 0; index < count; index += 1) {
        await values.nth(index).fill(String(40 - round * 10 - index));
      }
      filled.push(`第 ${round + 1} 轮完整读数`);
      await moveAndClick(page, page.getByRole("button", {
        name: new RegExp(`提交第\\s*${round + 1}\\s*轮`),
      }).first());
      clicks.push(`提交第 ${round + 1} 轮读数`);
    }
  }
  await page.getByText(/整槽合格|全部在泡器物已共同达标/).first()
    .waitFor({ state: "visible", timeout: 10000 });
  const finalResultVisible = await page.getByText(/整槽合格|全部在泡器物已共同达标/).first().isVisible();
  return emitBrowserWorkflow({ ok: finalResultVisible, clicks, filled,
    featureCount: clicks.length + filled.length, resultControlCount: 0,
    controlsComplete: true, finalResultVisible, visibleChange: true,
    primaryFeatureVerified: finalResultVisible,
    error: finalResultVisible ? "" : "共同连续达标的资格结论不可见" });
}

async function demonstrateLightRecoveryWorkflow(page) {
  const clicks = [];
  const filled = [];
  const add = async (selector, label) => {
    await moveAndClick(page, page.locator(selector).first());
    clicks.push(label);
  };
  await add("#btn-add-case", "添加展柜");
  await add('[data-act="add-lamp"]', "添加灯");
  await add('[data-act="add-row"]', "添加启停区间");
  const fields = await page.locator("#cases input[data-field]").evaluateAll((elements) =>
    elements.map((element) => ({
      ci: element.dataset.ci, li: element.dataset.li,
      ri: element.dataset.ri, field: element.dataset.field,
    })));
  const values = {
    winLen: "2", winLimit: "100", totalLimit: "100",
    recovery: "0.5", residLimit: "100", recoveryRate: "0.5", burdenLimit: "100",
    on: "0", off: "1", iuv: "1",
  };
  for (const item of fields) {
    if (!(item.field in values) || item.field === "recoveryRate" || item.field === "burdenLimit") continue;
    let value = values[item.field];
    if (item.field === "on" && item.ri === "1") value = "2";
    if (item.field === "off" && item.ri === "1") value = "3";
    const parts = [`[data-ci="${item.ci}"]`];
    if (item.li !== undefined) parts.push(`[data-li="${item.li}"]`);
    if (item.ri !== undefined) parts.push(`[data-ri="${item.ri}"]`);
    parts.push(`[data-field="${item.field}"]`);
    await page.locator(`#cases input${parts.join("")}`).fill(value);
  }
  filled.push("展柜限额、各灯启停与照度");
  await add("#btn-review", "发起剂量复核");
  await page.locator("#verdict").getByText(/剂量复核通过|复核通过/)
    .first().waitFor({ state: "visible", timeout: 10000 });
  const recoveryFields = page.locator('#cases input[data-field="recoveryRate"], #cases input[data-field="burdenLimit"]');
  for (let index = 0; index < await recoveryFields.count(); index += 1) {
    const field = recoveryFields.nth(index);
    await field.fill(await field.getAttribute("data-field") === "recoveryRate" ? "0.5" : "100");
  }
  if (await recoveryFields.count()) filled.push("逐柜恢复系数与允许负担");
  await add("#btn-recovery", "发起恢复复核");
  const result = page.locator("#recovery-verdict");
  await result.getByText(/恢复复核通过|恢复复核不通过/).first()
    .waitFor({ state: "visible", timeout: 10000 });
  await humanScrollIntoView(page, result);
  const finalResultVisible = await result.isVisible()
    && /恢复复核通过|恢复复核不通过/.test(await result.innerText());
  return emitBrowserWorkflow({ ok: finalResultVisible, clicks, filled,
    featureCount: clicks.length + filled.length, resultControlCount: 0,
    controlsComplete: true, finalResultVisible, visibleChange: true,
    primaryFeatureVerified: finalResultVisible,
    error: finalResultVisible ? "" : "恢复复核的逐柜结果不可见" });
}

async function selectSwaggerOperations(page, preferredDemo = null) {
  const spec = await page.evaluate(async () => {
    const response = await fetch("/openapi.json");
    if (!response.ok) throw new Error(`OpenAPI ${response.status}`);
    return response.json();
  });
  const candidates = rankOpenApiOperations(spec);
  if (!candidates.length) throw new Error("没有可演示的业务接口");
  const preferredSteps = Array.isArray(preferredDemo?.steps)
    ? preferredDemo.steps : (preferredDemo?.path ? [preferredDemo] : []);
  return candidates.map((selected) => {
    const preferred = preferredSteps.find((step) =>
      String(step?.path || "") === selected.path
      && String(step?.method || "").toLowerCase() === selected.method);
    return {
      path: selected.path,
      method: selected.method,
      preferred: Boolean(preferred),
      hasPathParameters: /{[^}]+}/.test(selected.path),
      body: preferred
        ? (preferred.body ?? null)
        : selected.content
        ? (selected.content.example ?? selected.content.examples?.default?.value
          ?? sampleValue(selected.content.schema, spec))
        : (["post", "put", "patch"].includes(selected.method) ? fallbackBodyForPath(selected.path) : null),
      declaredBody: Boolean(selected.content),
    };
  }).sort((left, right) => Number(right.preferred) - Number(left.preferred));
}

async function findSwaggerBlock(page, selected) {
  const blocks = page.locator(".opblock");
  for (let index = 0; index < await blocks.count(); index += 1) {
    const block = blocks.nth(index);
    const path = (await block.locator(".opblock-summary-path").first().innerText()).trim();
    const method = (await block.locator(".opblock-summary-method").first().innerText()).trim().toLowerCase();
    if (path === selected.path && method === selected.method) return block;
  }
  throw new Error(`Swagger 中未找到 ${selected.method.toUpperCase()} ${selected.path}`);
}

async function demonstrateSwaggerWorkflow(page, preferredDemo = null) {
  if (!new URL(page.url()).pathname.startsWith("/docs")) return { required: false, ok: true };
  await page.waitForTimeout(900);
  const candidates = await selectSwaggerOperations(page, preferredDemo);
  const expanded = [];
  for (const selected of candidates.slice(0, 6)) {
    try {
      const block = await findSwaggerBlock(page, selected);
      if (!(await block.getAttribute("class") || "").includes("is-open")) {
        await moveAndClick(page, block.locator(".opblock-summary").first());
        await page.waitForTimeout(240);
      }
      expanded.push(`${selected.method.toUpperCase()} ${selected.path}`);
    } catch {}
  }
  const results = [];
  const executable = candidates.filter((selected) => !selected.hasPathParameters).slice(0, 3);
  // When the API exposes multiple real operations, B demonstrates the same
  // eligible operations in another order. A single operation stays unchanged.
  if (interactionMode === "auto" && arm === "B" && executable.length > 1
      && !executable.some((selected) => selected.preferred)) {
    executable.push(executable.shift());
  }
  for (const selected of (executable.length ? executable : candidates.slice(0, 1))) {
    try {
      results.push(await demonstrateSwaggerOperation(page, selected));
    } catch (error) {
      results.push({ required: true, ok: false, method: selected.method, path: selected.path,
        error: error?.message || String(error) });
    }
    // A backend-only Swagger demo needs one successful business call. Later
    // endpoints are optional and can consume the entire 90-second cap after
    // the required operation has already succeeded.
    if (results.at(-1)?.ok) break;
    await page.waitForTimeout(320);
  }
  const successful = results.filter((result) => result.ok);
  return {
    required: true,
    ok: successful.length > 0,
    operations: results.map(({ method, path, status, ok }) => ({ method, path, status, ok })),
    expanded,
    clicks: expanded.length + results.length,
    featureCount: expanded.length,
    requests: successful.length,
    workflow: "swagger-business-operations",
    error: successful.length ? "" : "没有成功的业务接口请求",
  };
}

async function demonstrateSwaggerOperation(page, selected) {
  const block = await findSwaggerBlock(page, selected);
  if (!(await block.getAttribute("class") || "").includes("is-open")) {
    await moveAndClick(page, block.locator(".opblock-summary").first());
  }
  await page.waitForTimeout(650);
  if (!selected.declaredBody && selected.body !== null) {
    return demonstrateDirectApiWorkflow(page, selected);
  }
  await moveAndClick(page, block.locator("button.try-out__btn").first());
  await page.waitForTimeout(650);
  if (selected.body !== null) {
    const textarea = block.locator("textarea").first();
    await moveAndClick(page, textarea);
    await textarea.fill(JSON.stringify(selected.body, null, 2));
    await page.waitForTimeout(450);
  }
  await moveAndClick(page, block.locator("button.execute").first());
  const responses = block.locator(".live-responses-table").first();
  await responses.waitFor({ state: "visible", timeout: 15000 });
  await humanScrollIntoView(page, responses);
  const statusTexts = await responses.locator(".response-col_status").allTextContents();
  const status = Number(statusTexts.map((text) => text.match(/\d{3}/)?.[0]).find(Boolean) || 0);
  await page.waitForTimeout(850);
  const result = { required: true, ok: status >= 200 && status < 300, status, method: selected.method, path: selected.path };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function installDirectApiPanel(page, selected) {
  await page.evaluate((descriptor) => {
    const steps = Array.isArray(descriptor.steps) ? descriptor.steps : [descriptor];
    document.getElementById("pairwise-direct-api-demo")?.remove();
    const panel = document.createElement("section");
    panel.id = "pairwise-direct-api-demo";
    Object.assign(panel.style, {
      margin: "24px auto", padding: "20px", maxWidth: "980px", border: "2px solid #2563eb",
      borderRadius: "12px", background: "#eff6ff", color: "#172554", font: "16px/1.5 system-ui",
    });
    const title = document.createElement("h2");
    title.style.margin = "0 0 12px";
    title.textContent = steps.length > 1 ? "真实接口流程演示" : "真实接口演示";
    const request = document.createElement("pre");
    request.style.whiteSpace = "pre-wrap";
    request.textContent = steps.map((step, index) => {
      const bodyText = step.body === null || step.body === undefined
        ? "" : `\n${JSON.stringify(step.body, null, 2)}`;
      return `${index + 1}. ${step.method.toUpperCase()} ${step.path}${bodyText}`;
    }).join("\n\n");
    const button = document.createElement("button");
    button.type = "button";
    button.style.cssText = "font-size:18px;padding:10px 18px;cursor:pointer";
    button.textContent = steps.length > 1 ? "执行真实接口流程" : "发送真实请求";
    const result = document.createElement("pre");
    result.dataset.result = "";
    result.style.cssText = "min-height:70px;white-space:pre-wrap";
    result.textContent = "等待点击";
    panel.append(title, request, button, result);
    button.addEventListener("click", async () => {
      result.textContent = "请求中…";
      try {
        const lines = [];
        let finalStatus = 200;
        for (const step of steps) {
          const response = await fetch(step.path, {
            method: step.method.toUpperCase(),
            headers: step.headers || { "content-type": "application/json" },
            body: step.body === null || step.body === undefined
              ? undefined : (typeof step.body === "string" ? step.body : JSON.stringify(step.body)),
          });
          const text = await response.text();
          lines.push(`${step.method.toUpperCase()} ${step.path} → HTTP ${response.status}\n${text.slice(0, 500)}`);
          if (response.status < 200 || response.status >= 300) {
            finalStatus = response.status;
            break;
          }
        }
        panel.dataset.status = String(finalStatus);
        result.textContent = lines.join("\n\n");
      } catch (error) {
        panel.dataset.status = "0";
        result.textContent = String(error);
      }
    });
    (document.querySelector(".swagger-ui") || document.body).prepend(panel);
  }, selected);
  const panel = page.locator("#pairwise-direct-api-demo");
  await panel.waitFor({ state: "visible", timeout: 10000 });
  // Begin from the top of the delivered API surface before moving to its
  // action.  Centering a long request panel here made the first video frame
  // look as if recording had started halfway through the project.
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(1000);
  return panel;
}

async function demonstrateDirectApiWorkflow(page, selected) {
  const panel = await installDirectApiPanel(page, selected);
  await moveAndClick(page, panel.locator("button"));
  await page.waitForFunction(() => Boolean(document.querySelector("#pairwise-direct-api-demo")?.dataset.status));
  const status = Number(await panel.getAttribute("data-status") || 0);
  const steps = Array.isArray(selected.steps) ? selected.steps : [selected];
  const resultOutput = panel.locator("[data-result]");
  await humanScrollIntoView(page, resultOutput);
  await resultOutput.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  const result = { required: true, ok: status >= 200 && status < 300, status,
    method: steps[steps.length - 1].method, path: steps[steps.length - 1].path,
    stepCount: steps.length, compatibilityRequest: true };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateColdRoomAlarmWorkflow(page) {
  const eventId = `recording-${Date.now()}`;
  await page.evaluate((id) => {
    document.getElementById("pairwise-device-demo")?.remove();
    const panel = document.createElement("section");
    panel.id = "pairwise-device-demo";
    Object.assign(panel.style, {
      position: "fixed", right: "28px", bottom: "28px", zIndex: "2147483000",
      width: "300px", padding: "18px", border: "2px solid #ef4444", borderRadius: "14px",
      background: "#fff7ed", color: "#431407", boxShadow: "0 18px 50px rgba(0,0,0,.25)",
      font: "16px/1.45 system-ui",
    });
    panel.innerHTML = `<strong>设备网关真实回调</strong>
      <p style="margin:8px 0">点击后上报一次冷库门强制开启告警。</p>
      <button type="button" style="font-size:16px;padding:10px 16px;cursor:pointer">模拟设备告警</button>
      <div data-result style="margin-top:8px">等待点击</div>`;
    const result = panel.querySelector("[data-result]");
    panel.querySelector("button").addEventListener("click", async () => {
      result.textContent = "正在上报…";
      try {
        const response = await fetch("/api/events", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({
            event_id: id, door_id: "recording-door", kind: "FORCED_OPEN",
            occurred_at: new Date().toISOString(),
          }),
        });
        panel.dataset.status = String(response.status);
        result.textContent = response.ok ? `上报成功 · HTTP ${response.status}` : `上报失败 · HTTP ${response.status}`;
      } catch (error) {
        panel.dataset.status = "0";
        result.textContent = String(error);
      }
    });
    document.body.appendChild(panel);
  }, eventId);
  const panel = page.locator("#pairwise-device-demo");
  await moveAndClick(page, panel.locator("button"));
  await page.waitForFunction(() => Boolean(document.querySelector("#pairwise-device-demo")?.dataset.status));
  const status = Number(await panel.getAttribute("data-status") || 0);
  if (status >= 200 && status < 300) {
    await page.getByText(eventId, { exact: false }).first().waitFor({ state: "visible", timeout: 15000 });
  }
  await page.waitForTimeout(850);
  const result = { required: true, ok: status >= 200 && status < 300, status,
    method: "post", path: "/api/events", workflow: "device-gateway-callback" };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function prepareGenericInputs(page) {
  const controls = page.locator(
    'input:visible:not([disabled]):not([readonly]), textarea:visible:not([disabled]):not([readonly])',
  );
  const filled = [];
  for (let index = 0; index < await controls.count(); index += 1) {
    const control = controls.nth(index);
    const type = String(await control.getAttribute("type") || "text").toLowerCase();
    if (["button", "submit", "reset", "checkbox", "radio", "file", "hidden", "color", "range"].includes(type)) continue;
    if (String(await control.inputValue().catch(() => "")).trim()) continue;
    const hint = [
      await control.getAttribute("name"), await control.getAttribute("placeholder"),
      await control.getAttribute("aria-label"), await control.getAttribute("data-testid"),
    ].filter(Boolean).join(" ").toLowerCase();
    let value = "录像演示";
    if (type === "email") value = "demo@example.com";
    else if (type === "url") value = "https://example.com";
    else if (type === "tel") value = "13800000000";
    else if (type === "number") value = String(Math.max(1, Number(await control.getAttribute("min")) || 1));
    else if (type === "date") value = "2030-01-01";
    else if (type === "time") value = "10:00";
    else if (type === "datetime-local") value = "2030-01-01T10:00";
    else if (/scene|场次|项目|project/.test(hint)) value = "DEMO-001";
    else if (/name|名称|姓名|标题|title/.test(hint)) value = "演示记录";
    else if (/code|编号|标识|\bid\b/.test(hint)) value = `demo-${Date.now()}-${index + 1}`;
    else if (/search|搜索|查询/.test(hint)) value = "演示";
    try {
      await control.fill(value);
      filled.push(hint || `${type}-${index + 1}`);
    } catch {}
  }
  if (filled.length) await page.waitForTimeout(700);
  return filled;
}

async function prepareGenericFileInputs(page) {
  if (!uploadFixture || !existsSync(uploadFixture)) return [];
  const controls = page.locator('input[type="file"]:visible:not([disabled])');
  const uploaded = [];
  for (let index = 0; index < await controls.count(); index += 1) {
    const control = controls.nth(index);
    const accept = String(await control.getAttribute("accept") || "").toLowerCase();
    if (accept && /json/.test(accept) && !uploadFixture.toLowerCase().endsWith(".json")) continue;
    await control.scrollIntoViewIfNeeded();
    const box = await control.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 24 });
      await page.waitForTimeout(700);
    }
    await control.setInputFiles(uploadFixture);
    uploaded.push({ input: index + 1, file: uploadFixture.split("/").pop() });
    await page.waitForTimeout(5000);
  }
  return uploaded;
}

async function demonstrateFileUploadWorkflow(page, fileInput) {
  const beforeRequests = successfulRequests.length;
  const payload = Buffer.from(`pairwise browser recording ${Date.now()}\n`.repeat(64));
  const clicks = [];

  const pointAt = async (locator) => {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    if (!box) throw new Error("文件控件不可见");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 24 });
    await page.waitForTimeout(320);
  };
  const chooseFile = async (name) => {
    await pointAt(fileInput);
    await fileInput.setInputFiles({ name, mimeType: "application/octet-stream", buffer: payload });
    await page.waitForTimeout(500);
  };
  const clickButton = async (pattern) => {
    const buttons = page.locator("button:visible:not([disabled])");
    for (let index = 0; index < await buttons.count(); index += 1) {
      const button = buttons.nth(index);
      const label = (await button.innerText().catch(() => "")).trim();
      if (!matchesRecordingLabel(pattern, label)) continue;
      await moveAndClick(page, button);
      clicks.push(label || `button-${index + 1}`);
      return true;
    }
    return false;
  };

  await chooseFile("recording-demo.bin");
  if (!await clickButton(isFileUploadSubmitLabel)) {
    throw new Error("选择文件后没有可用的提交按钮");
  }
  await page.locator('[data-test="download-area"]').waitFor({ state: "visible", timeout: 20000 });
  await page.waitForTimeout(650);

  const requestCount = successfulRequests.length - beforeRequests;
  const result = {
    required: true,
    ok: clicks.length > 0 && requestCount > 0,
    clicks,
    filled: ["file"],
    requests: requestCount,
    visibleChange: true,
    workflow: "browser-file-upload",
    error: "文件已选择，但没有完成真实上传请求",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateCryoPackageWorkflow(page) {
  const beforeRequests = successfulRequests.length;
  const session = `REC${Date.now()}`;
  const clicks = [];
  const payload = Buffer.alloc(65537, 0x43);
  const isSingleAction = await page.getByRole("button", { name: "传输并封存", exact: true }).count() > 0;
  const sessionInput = page.locator('input:not([type="file"])').first();
  const fileInput = page.locator('input[type="file"]:visible:not([disabled])').first();
  await sessionInput.fill(session);
  await fileInput.setInputFiles({
    name: "recording-demo.bin", mimeType: "application/octet-stream", buffer: payload,
  });
  const clickNamed = async (name) => {
    const button = page.getByRole("button", { name, exact: true });
    await button.waitFor({ state: "visible", timeout: 15000 });
    await button.waitFor({ state: "attached", timeout: 15000 });
    await moveAndClick(page, button);
    clicks.push(name);
  };
  const primaryLabel = isSingleAction ? "传输并封存" : "传输缺失分块";
  await page.getByRole("button", { name: primaryLabel, exact: true }).waitFor({ state: "visible", timeout: 15000 });
  await page.waitForFunction((label) => {
    const button = [...document.querySelectorAll("button")]
      .find((element) => element.textContent?.trim() === label);
    return button && !button.disabled;
  }, primaryLabel, { timeout: 15000 });
  if (isSingleAction) {
    await clickNamed(primaryLabel);
    await page.getByText("封存成功", { exact: false }).first().waitFor({ state: "visible", timeout: 20000 });
    await clickNamed("查询/恢复服务器进度");
    await clickNamed("仅请求封存");
  } else {
    await clickNamed(primaryLabel);
    await page.getByText("全块到齐，可封存", { exact: false }).waitFor({ state: "visible", timeout: 20000 });
    await clickNamed("重发所有块（断线恢复）");
    await page.getByText("全块到齐，可封存", { exact: false }).waitFor({ state: "visible", timeout: 20000 });
    await clickNamed("刷新服务端状态");
    await clickNamed("封存（seal）");
  }
  await page.getByText("封存成功", { exact: false }).first().waitFor({ state: "visible", timeout: 20000 });
  const finalResultVisible = /封存成功|已封存/.test(await page.locator("body").innerText());
  const ok = finalResultVisible && successfulRequests.length > beforeRequests
    && clicks.length === (isSingleAction ? 3 : 4);
  const result = {
    required: true,
    ok,
    clicks,
    uploaded: [{ input: 1, file: "recording-demo.bin" }],
    filled: ["会话号", "采集包文件"],
    requests: successfulRequests.length - beforeRequests,
    visibleChange: true,
    controlsComplete: true,
    resultControlCount: 0,
    featureCount: clicks.length + 1,
    finalResultVisible,
    workflow: "cryo-package-seal",
    error: ok ? "" : "采集包未完成传输、恢复与封存",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateLaminateStripWorkflow(page) {
  const clicks = [];
  const filled = [];
  const click = async (locator, label) => {
    await moveAndClick(page, locator);
    clicks.push(label);
  };
  for (const label of ["需修复示例", "无解示例", "非法输入示例", "已合法示例"]) {
    await click(page.getByRole("button", { name: label, exact: true }), label);
  }
  await click(page.getByRole("button", { name: /添加层/ }), "添加层");
  await click(page.getByRole("button", { name: "已合法示例", exact: true }), "已合法示例");
  await click(page.getByRole("checkbox"), "开启条带模式");
  await page.getByText("条带模式：开", { exact: true }).waitFor({ state: "visible", timeout: 5000 });
  await click(page.getByRole("button", { name: /添加条带/ }), "添加条带");
  const secondStart = page.getByLabel("第 2 条条带起始层");
  await secondStart.selectOption("2");
  filled.push("第 2 条条带起始层：第 3 层");
  await click(page.getByRole("button", { name: /启动综合/ }), "启动带条带约束的综合");
  await page.getByRole("heading", { name: "④ 工艺条带落位证据" })
    .waitFor({ state: "visible", timeout: 15000 });
  const evidence = page.getByRole("region", { name: "条带落位证据" });
  const count = await evidence.locator("details.strip-evidence").count();
  if (count !== 2) throw new Error(`条带结果不完整：应显示两条，实际 ${count} 条`);
  for (const summary of await page.locator("details.check > summary, details.strip-evidence > summary").all()) {
    const label = (await summary.innerText()).replace(/\s+/g, " ").trim();
    await click(summary, label);
  }
  const copy = page.getByRole("button", { name: "复制结果", exact: true });
  if (await copy.count()) await click(copy, "复制结果");
  await humanScrollIntoView(page, evidence);
  const finalResultVisible = await page.getByRole("heading", { name: "④ 工艺条带落位证据" }).isVisible()
    && await evidence.getByText("2 条条带全部完整落位", { exact: false }).isVisible();
  const result = {
    required: true, ok: finalResultVisible, clicks, filled,
    uploaded: [], requests: successfulRequests.length,
    featureCount: clicks.length + filled.length,
    resultControlCount: count, controlsComplete: true,
    finalResultVisible, visibleChange: true, resultRestored: false,
    failedControls: [], workflow: "laminate-strip-ui",
    error: finalResultVisible ? "" : "带条带约束的综合结果不可见",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateGenericWorkflow(page) {
  await page.waitForTimeout(900);
  const before = await page.locator("body").innerText();
  if (before.includes("纸本文物水渍分布复原")) {
    return demonstrateStainRestorationWorkflow(page);
  }
  if (before.includes("金属文物脱盐换液控制台") || before.includes("金属文物脱盐期间")) {
    return demonstrateDesalinationWorkflow(page);
  }
  if (before.includes("纸张恢复参数") || before.includes("光损恢复系数")) {
    return demonstrateLightRecoveryWorkflow(page);
  }
  if (before.includes("复合材料铺层序列修复工作台") && before.includes("工艺条带（返工保留）")) {
    return demonstrateLaminateStripWorkflow(page);
  }
  const workflowResults = [];
  if (/冷库门告警中控/.test(before)) {
    const result = await demonstrateColdRoomAlarmWorkflow(page);
    workflowResults.push(result);
  }
  const cryoPackage = /冷冻电镜采集包.{0,12}封存台/.test(before);
  if (cryoPackage) {
    return await demonstrateCryoPackageWorkflow(page);
  }
  const fileInput = page.locator('input[type="file"]:visible:not([disabled])').first();
  const hasFileInput = Boolean(await fileInput.count());
  const hasProjectFixture = Boolean(uploadFixture && existsSync(uploadFixture));
  const uploadActions = page.locator(
    "button:visible:not([disabled]), [role=button]:visible:not([aria-disabled=true]), "
    + "input[type=submit]:visible:not([disabled])",
  );
  let hasUploadSubmit = false;
  for (let index = 0; index < await uploadActions.count(); index += 1) {
    const control = uploadActions.nth(index);
    const label = [
      await control.innerText().catch(() => ""), await control.getAttribute("value"),
      await control.getAttribute("aria-label"), await control.getAttribute("title"),
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
    if (isFileUploadSubmitLabel(label)) {
      hasUploadSubmit = true;
      break;
    }
  }
  // A file input may be a local import helper for an otherwise normal app.
  // Only enter the specialized upload workflow when the page also exposes a
  // real upload/submit action; otherwise continue through its feature buttons.
  if (hasFileInput && !hasProjectFixture && hasUploadSubmit) {
    const result = await demonstrateFileUploadWorkflow(page, fileInput);
    workflowResults.push(result);
  }
  let uploaded = [];
  if (hasFileInput && hasProjectFixture) {
    uploaded = await prepareGenericFileInputs(page);
  }
  const clicked = workflowResults.flatMap((result) => Array.isArray(result.clicks) ? result.clicks : []);
  const previouslyClickedLabels = new Set(clicked.map(recordingControlKey));
  const seenControls = new Set();
  const repeatedControlCounts = new Map();
  const failedControls = [];
  const maximumAutomaticControls = 32;
  const clickMatching = async (matcher, alternateOrder = false, afterClick = null, prepareInputs = true) => {
    let added = 0;
    let complete = false;
    while (clicked.length < maximumAutomaticControls
      && Date.now() - recordingStartedAt < maximum * 1000 - 12000) {
      const controls = page.locator(GENERIC_CONTROL_SELECTOR);
      let target = null;
      let targetLabel = "";
      let targetKey = "";
      // One DOM snapshot avoids several browser round-trips per control on
      // every traversal pass. The page can have dozens of controls, and that
      // overhead alone can consume the 90-second recording budget.
      const controlSnapshot = await controls.evaluateAll((elements) => elements.map((element) => {
        const label = [
          element.innerText || "", element.getAttribute("value"),
          element.getAttribute("aria-label"), element.getAttribute("title"),
          element.matches('input[type="checkbox"], input[type="radio"]')
            ? element.closest("label")?.innerText || "" : "",
        ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
        const identity = [
          element.getAttribute("data-testid"), element.getAttribute("id"),
          element.getAttribute("name"),
        ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
        return { label, identity, type: element.getAttribute("type") || "" };
      }));
      const controlCount = controlSnapshot.length;
      const choiceCount = controlSnapshot.filter(({ type }) => type === "checkbox" || type === "radio").length;
      const availableBefore = new Set(controlSnapshot.map(({ label, identity }) =>
        recordingControlKey([label, identity].filter(Boolean).join(" "))));
      const labelOccurrences = new Map();
      for (let offset = 0; offset < controlCount; offset += 1) {
        const index = alternateOrder && controlCount > 1 ? controlCount - 1 - offset : offset;
        const control = controls.nth(index);
        const { label, identity, type } = controlSnapshot[index];
        const visibleLabel = label || identity;
        const matchText = [label, identity].filter(Boolean).join(" ");
        const labelKey = recordingControlKey(visibleLabel);
        const occurrence = (labelOccurrences.get(labelKey) || 0) + 1;
        labelOccurrences.set(labelKey, occurrence);
        const key = recordingControlIdentity(visibleLabel, identity, occurrence);
        const repeatedFamily = repeatedRecordingControlFamily(visibleLabel);
        if (!key || !isSafeFeatureControl(matchText)
          || !shouldTraverseChoiceControl(type, matchText, choiceCount) || seenControls.has(key)
          || (repeatedFamily && (repeatedControlCounts.get(repeatedFamily) || 0) >= 3)
          || (!hasProjectFixture && /导入文件|选择文件|browse files?/i.test(matchText))
          || previouslyClickedLabels.has(labelKey)
          || !matchesRecordingLabel(matcher, matchText)) continue;
        target = control;
        targetLabel = visibleLabel;
        targetKey = key;
        break;
      }
      if (!target) {
        complete = true;
        break;
      }
      try {
        await moveAndClick(page, target);
        seenControls.add(targetKey);
        const repeatedFamily = repeatedRecordingControlFamily(targetLabel);
        if (repeatedFamily) {
          repeatedControlCounts.set(repeatedFamily, (repeatedControlCounts.get(repeatedFamily) || 0) + 1);
        }
        clicked.push(targetLabel);
        added += 1;
        await page.waitForTimeout(280);
        // Samples replace the whole form. Filling after every sample wastes
        // the recording budget and may alter the sample before the main action.
        if (prepareInputs && !isGenericSampleLabel(targetLabel)
          && shouldPrepareInputsAfterControl(targetLabel)) {
          await prepareGenericInputs(page);
        }
        if (afterClick) await afterClick(availableBefore);
      } catch (error) {
        failedControls.push({ key: targetKey, label: targetLabel, error: String(error?.message || error).slice(0, 400) });
        seenControls.add(targetKey);
      }
    }
    return { added, complete };
  };
  const sampleRevealedTraversals = [];
  const sampleTraversal = await clickMatching(isGenericSampleLabel, false, async (availableBefore) => {
    // A sample can reveal an enabled next action or expandable result which
    // disappears when the next sample replaces the form state. Do not run a
    // global action which was already available before choosing the sample.
    sampleRevealedTraversals.push(await clickMatching(
      (label) => isNewlyRevealedControl(label, availableBefore), false, null, false,
    ));
  });
  // Traversing every example can leave the form on an intentionally invalid
  // example. Restore a valid one before exercising the primary action.
  if (sampleTraversal.added) {
    const samples = page.locator('button:visible:not([disabled]), [role=button]:visible:not([aria-disabled=true])');
    const sampleLabels = await samples.evaluateAll((elements) => elements.map((element) => [
      element.innerText || "", element.getAttribute("aria-label"), element.getAttribute("title"),
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim()));
    const preferredIndex = sampleLabels.findIndex((label) => isLikelyValidSampleLabel(label)
      && /已合法|合法|有效|可行|valid|feasible/i.test(label));
    const fallbackIndex = sampleLabels.findIndex(isLikelyValidSampleLabel);
    const sampleIndex = preferredIndex >= 0 ? preferredIndex : fallbackIndex;
    if (sampleIndex >= 0) {
      await moveAndClick(page, samples.nth(sampleIndex));
      clicked.push(sampleLabels[sampleIndex]);
    }
  }
  const filled = await prepareGenericInputs(page);
  const actionTraversal = await clickMatching(isGenericActionLabel);
  let actionCount = actionTraversal.added;
  let fallbackTraversal = { added: 0, complete: true };
  // Some small applications expose only a create/add/search control. Use one
  // of those as a fallback. Control identities are de-duplicated so changing
  // counters cannot cause an unbounded series of identical clicks.
  if (!actionCount) {
    fallbackTraversal = await clickMatching(
      /领取|保存|创建|新增|添加|发送|确认|更新|修订|查询|搜索|演示|测试|save|create|add|send|confirm|update|search|test/i,
    );
    actionCount += fallbackTraversal.added;
  }
  if (actionCount) {
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
  }
  // Primary actions often reveal replay controls below the fold. Exercise the
  // newly rendered result/trace controls before closing the recording.
  const alternateResultOrder = interactionMode === "auto" && arm === "B";
  const resultTraversal = await clickMatching(isResultNavigationLabel, alternateResultOrder);
  // Labels vary widely across generated projects. Finish with a catch-all pass
  // over every remaining safe control so a valid button is not skipped merely
  // because its wording is absent from the action/navigation dictionaries.
  // The same identity/deadline guards above still prevent repeated or endless
  // clicks, and destructive controls remain excluded.
  const remainingTraversal = await clickMatching(/.+/i, alternateResultOrder);
  let alternateResultTraversal = { added: 0, complete: true };
  // A result option may reveal only one tab for the default sample. When the
  // page offers an ambiguity sample, exercise it with the enabled option so
  // the newly rendered second/third result tabs are also traversed.
  const option = page.locator('input[type=checkbox]:visible:not([disabled])').first();
  const ambiguitySample = page.locator('button:visible:not([disabled])').filter({
    hasText: /歧义|多解|ambiguous|multiple solutions/i,
  }).first();
  if (await option.count() && await ambiguitySample.count()
      && Date.now() - recordingStartedAt < maximum * 1000 - 18000) {
    try {
      if (!await option.isChecked()) {
        await moveAndClick(page, option);
        clicked.push("启用多结果选项");
      }
      const sampleLabel = (await ambiguitySample.innerText()).replace(/\s+/g, " ").trim();
      if (isSafeFeatureControl(sampleLabel)) {
        await moveAndClick(page, ambiguitySample);
        clicked.push(sampleLabel);
        const submit = page.locator('button:visible:not([disabled])').filter({
          hasText: /提交|运行|计算|求解|分析|裁决|生成|submit|run|solve/i,
        }).first();
        if (!await submit.count()) throw new Error("多解样例缺少可用主操作");
        const submitLabel = (await submit.innerText()).replace(/\s+/g, " ").trim();
        await moveAndClick(page, submit);
        clicked.push(submitLabel);
        await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
        await page.waitForTimeout(350);
        alternateResultTraversal = await clickMatching(isResultNavigationLabel, alternateResultOrder);
      }
    } catch (error) {
      failedControls.push({ key: "alternate-result-sample", label: "多解结果标签",
        error: String(error?.message || error).slice(0, 400) });
    }
  }
  let restoredResultTraversal = { added: 0, complete: true };
  // A safe "add" or edit control can withdraw a previously visible result.
  // Reopen the app's initial, known-valid state and submit once more so the
  // recording ends on an actual result, not a stale-input warning.
  const hasVisibleResult = async () => {
    const body = await page.locator("body").innerText();
    const emptyResult = await page.locator(
      ".result-panel .empty-state:visible, [data-testid='result-empty']:visible, [data-test='result-empty']:visible",
    ).count() > 0;
    return !isWithdrawnResultText(body) && !emptyResult;
  };
  let finalResultVisible = await hasVisibleResult();
  let resultRestored = false;
  if (!finalResultVisible) {
    if (Date.now() - recordingStartedAt < maximum * 1000 - 9000) {
      try {
        const submitCurrent = async () => {
          const actions = page.locator(
            "button:visible:not([disabled]), [role=button]:visible:not([aria-disabled=true]), "
            + "input[type=submit]:visible:not([disabled])",
          );
          for (let index = 0; index < await actions.count(); index += 1) {
            const control = actions.nth(index);
            const label = [
              await control.innerText().catch(() => ""), await control.getAttribute("value"),
              await control.getAttribute("aria-label"),
            ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
            const sample = await control.evaluate((element) => Boolean(element.closest(".samples")));
            if (sample || !isSafeFeatureControl(label) || !isPrimarySubmitLabel(label)) continue;
            await moveAndClick(page, control);
            clicked.push(label);
            await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
            await page.waitForTimeout(350);
            return await hasVisibleResult();
          }
          return false;
        };
        // Submit the already selected sample first. Reloading would throw
        // away its ambiguity/ladder state and leave the new result tabs unseen.
        finalResultVisible = await submitCurrent();
        if (!finalResultVisible) {
          await page.reload({ waitUntil: "domcontentloaded", timeout: 8000 });
          finalResultVisible = await submitCurrent();
        }
        resultRestored = finalResultVisible;
        if (finalResultVisible) {
          restoredResultTraversal = await clickMatching(isResultNavigationLabel, alternateResultOrder);
        }
      } catch (error) {
        failedControls.push({ key: "restore-final-result", label: "重新显示最终结果", error: String(error?.message || error).slice(0, 400) });
      }
    }
  }
  const resultControlCount = clicked.filter(isResultNavigationLabel).length;
  const controlsComplete = recordingTraversalComplete(
    [sampleTraversal, ...sampleRevealedTraversals, actionTraversal, fallbackTraversal, resultTraversal,
      remainingTraversal, alternateResultTraversal, restoredResultTraversal], failedControls,
  );
  // End on the actual output instead of leaving the recording at the first
  // toolbar button. A short downward review makes below-the-fold results
  // visible and keeps the complete automatic recording comfortably under 90s.
  await page.waitForTimeout(700);
  const bottom = await page.evaluate(() => Math.max(0, document.documentElement.scrollHeight - window.innerHeight));
  const current = await page.evaluate(() => window.scrollY);
  const distance = bottom - current;
  if (distance > 12) {
    const steps = Math.max(5, Math.min(12, Math.ceil(distance / 120)));
    for (let index = 0; index < steps; index += 1) {
      await page.mouse.wheel(0, distance / steps);
      await page.waitForTimeout(90 + ((index * 29) % 45));
    }
  }
  await page.waitForTimeout(1000);
  const after = await page.locator("body").innerText();
  finalResultVisible = finalResultVisible && await hasVisibleResult();
  const visibleChange = before !== after;
  const childOk = workflowResults.length === 0 || workflowResults.some((result) => result.ok);
  const ok = controlsComplete && finalResultVisible && childOk && (clicked.length > 0 || uploaded.length > 0)
    && (visibleChange || successfulRequests.length > 0) && failedPageAssets.length === 0;
  const result = {
    required: true,
    ok,
    clicks: clicked,
    uploaded,
    featureCount: clicked.length + uploaded.length,
    resultControlCount,
    filled,
    requests: successfulRequests.length,
    visibleChange,
    failedPageAssets,
    controlsComplete,
    finalResultVisible,
    resultRestored,
    failedControls,
    workflow: "browser-ui",
    error: ok ? "" : !controlsComplete
      ? "安全功能和结果控件未遍历完成" : !finalResultVisible
      ? "最终结果不可见或已被输入变更撤下" : failedPageAssets.length
      ? "页面功能资源加载失败" : "没有完成可见的真实功能操作",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateFailureEvidence(page) {
  await page.waitForTimeout(1800);
  const sections = page.locator("section");
  for (let index = 0; index < await sections.count(); index += 1) {
    const section = sections.nth(index);
    await section.scrollIntoViewIfNeeded();
    const output = section.locator("pre");
    if (await output.count()) {
      await output.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    }
    const box = await section.boundingBox();
    if (box) {
      await page.mouse.move(Math.min(1180, box.x + 80), Math.min(650, box.y + 45), { steps: 24 });
    }
    await page.waitForTimeout(1400);
  }
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await page.waitForTimeout(1800);
  return { required: true, ok: true, interactionMode: "failure", evidence: "docker-validation-output" };
}

async function finish(reason) {
  if (finishing) return;
  finishing = true;
  try {
    let demonstration = await Promise.race([
      demonstrationPromise,
      new Promise((resolve) => setTimeout(() => resolve({ required: true, ok: false, error: "真实功能演示尚未完成" }), 6000)),
    ]);
    const pages = context?.pages() || [];
    if (interactionMode === "manual" && pages[0]) {
      const observed = await pages[0].evaluate(() =>
        window.__pairwiseManualSnapshot?.() || { clicks: [], observed: [], activated: [] });
      const body = await pages[0].locator("body").innerText();
      const emptyResult = await pages[0].locator(
        ".result-panel .empty-state:visible, [data-testid='result-empty']:visible, [data-test='result-empty']:visible",
      ).count() > 0;
      const clicks = observed.clicks || [];
      const activated = new Set(observed.activated || []);
      const controlsComplete = (observed.observed || []).length > 0
        && observed.observed.every((key) => activated.has(key));
      const finalResultVisible = !isWithdrawnResultText(body) && !emptyResult;
      demonstration = {
        required: true, ok: finalResultVisible && clicks.length > 0,
        interactionMode: "manual", workflow: "browser-ui", clicks,
        featureCount: clicks.length,
        resultControlCount: clicks.filter(isResultNavigationLabel).length,
        controlsComplete, finalResultVisible,
        requests: successfulRequests.length,
        error: !clicks.length ? "手动录像没有可核验的操作"
          : !finalResultVisible ? "手动录像结束时最终结果不可见" : "",
      };
      process.stdout.write(`${JSON.stringify({ event: "interaction", ...demonstration })}\n`);
    } else if (!demonstration.required && pages[0]) {
      const metrics = await pages[0].evaluate(() => window.__pairwiseRecordingMetrics || { clicks: 0 });
      demonstration = finalizeInteractionEvidence(
        interactionMode, demonstration, metrics, successfulRequests.length,
      );
    }
    stopping = true;
    // Keep the handle captured when the page was created.  If a user closes
    // the manual Chrome window before pressing the console's stop button,
    // context.pages() is empty even though Playwright has a valid WebM file.
    const video = recordedVideo || pages[0]?.video();
    await context?.close();
    if (video) await saveVideo(await video.path(), outputPath);
    if (interactionMode === "auto" && reason === "maximum_duration") {
      throw new Error("90 秒内未完成自动录像的业务操作与安全控件遍历");
    }
    if (demonstration.required && !demonstration.ok) {
      throw new Error(demonstration.error || `真实接口请求失败：HTTP ${demonstration.status || "未知"}`);
    }
    process.stdout.write(`${JSON.stringify({ event: "finished", reason, demonstration })}\n`);
    process.exit(0);
  } catch (error) {
    console.error(error?.stack || String(error));
    process.exit(1);
  }
}

async function finishAfterAutomaticWorkflow(page) {
  await demonstrationPromise;
  if (finishing) return;
  const elapsedMs = Date.now() - visibleRecordingStartedAt;
  const delayMs = automaticFinishDelayMs(outputPath, elapsedMs, maximum);
  process.stdout.write(`${JSON.stringify({
    event: "automatic_timing",
    workflowSeconds: Math.round(elapsedMs / 100) / 10,
    plannedSeconds: Math.round((elapsedMs + delayMs) / 100) / 10,
  })}\n`);
  // The hard deadline may close the page while this final hold is pending.
  // A plain timer avoids an unhandled Playwright rejection in that race.
  if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
  await finish("automatic_workflow_complete");
}

try {
  context = await chromium.launchPersistentContext(profileDir, {
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: false,
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: `${profileDir}/videos`, size: { width: 1280, height: 720 } },
    args: ["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"],
  });
  await context.addInitScript(({ manualEvidence, destructiveControlPattern }) => {
    window.__pairwiseRecordingMetrics = { clicks: 0 };
    if (manualEvidence) {
      const controls = "button,[role=button],[role=tab],input[type=submit],input[type=checkbox]";
      const unsafe = new RegExp(destructiveControlPattern, "i");
      const identities = new WeakMap();
      const state = { clicks: [], observed: new Set(), activated: new Set() };
      let sequence = 0;
      let pendingScan = false;
      const labelOf = (element) => [element.innerText, element.getAttribute("value"),
        element.getAttribute("aria-label"), element.getAttribute("title"),
        element.matches('input[type="checkbox"]') ? element.closest("label")?.innerText : "",
      ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim()
        || element.id || element.getAttribute("name") || "";
      const keyOf = (element) => {
        if (!identities.has(element)) identities.set(element, String(++sequence));
        return identities.get(element);
      };
      const blockUnsafeActivation = (event) => {
        const element = event.target?.closest?.(controls);
        if (!element || !unsafe.test(labelOf(element))) return;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
      };
      // Manual sessions are still subject to the no-destructive-click rule.
      // Block pointer and keyboard activation before application handlers run.
      document.addEventListener("pointerdown", blockUnsafeActivation, true);
      document.addEventListener("click", blockUnsafeActivation, true);
      document.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") blockUnsafeActivation(event);
      }, true);
      const scan = () => {
        pendingScan = false;
        document.querySelectorAll(controls).forEach((element) => {
          const label = labelOf(element);
          if (!label || unsafe.test(label) || element.disabled
              || element.getAttribute("aria-disabled") === "true"
              || element.getClientRects().length === 0) return;
          state.observed.add(keyOf(element));
        });
      };
      const scheduleScan = () => {
        if (pendingScan) return;
        pendingScan = true;
        requestAnimationFrame(scan);
      };
      const startScan = () => {
        scan();
        new MutationObserver(scheduleScan).observe(document.documentElement,
          { childList: true, subtree: true, attributes: true,
            attributeFilter: ["disabled", "hidden", "aria-disabled", "style", "class"] });
      };
      document.documentElement ? startScan()
        : document.addEventListener("DOMContentLoaded", startScan, { once: true });
      document.addEventListener("pointerdown", (event) => {
        const element = event.target?.closest?.(controls);
        if (!element) return;
        scan();
        const label = labelOf(element);
        if (!label) return;
        state.clicks.push(label);
        if (!unsafe.test(label)) state.activated.add(keyOf(element));
        scheduleScan();
      }, true);
      window.__pairwiseManualSnapshot = () => {
        scan();
        return { clicks: state.clicks, observed: [...state.observed],
          activated: [...state.activated] };
      };
    }
    const ensureCursor = () => {
      const existing = document.getElementById("pairwise-recording-cursor");
      if (existing) return existing;
      const cursor = document.createElement("div");
      cursor.id = "pairwise-recording-cursor";
      cursor.innerHTML = '<svg viewBox="0 0 28 36" width="28" height="36" aria-hidden="true"><path d="M2 2L2 28L9 21L14 33L20 30L15 19L25 19Z" fill="#111827" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>';
      Object.assign(cursor.style, {
        position: "fixed", left: "0", top: "0", width: "28px", height: "36px",
        transform: "translate3d(0,0,0)", pointerEvents: "none", zIndex: "2147483647",
        opacity: "0", filter: "drop-shadow(0 1px 2px rgba(0,0,0,.7))",
        transition: "opacity .08s linear", willChange: "transform,opacity",
      });
      document.documentElement.appendChild(cursor);
      return cursor;
    };
    let cursorFrame = 0;
    let cursorX = 0;
    let cursorY = 0;
    const hideCursor = () => {
      const cursor = document.getElementById("pairwise-recording-cursor");
      if (cursor) cursor.style.opacity = "0";
    };
    const updateCursor = (event) => {
      cursorX = event.clientX;
      cursorY = event.clientY;
      if (cursorFrame) return;
      cursorFrame = requestAnimationFrame(() => {
        cursorFrame = 0;
        const cursor = ensureCursor();
        cursor.style.transform = `translate3d(${cursorX}px,${cursorY}px,0)`;
        cursor.style.opacity = "1";
      });
    };
    const hideOpenApiLink = () => {
      document.querySelectorAll('.swagger-ui a[href*="openapi.json"]').forEach((link) => {
        const wrapper = link.parentElement;
        link.remove();
        if (wrapper && !wrapper.textContent.trim()) wrapper.style.display = "none";
      });
    };
    const observer = new MutationObserver(hideOpenApiLink);
    const observeDocument = () => {
      observer.observe(document.documentElement, { childList: true, subtree: true });
      hideOpenApiLink();
      ensureCursor();
    };
    document.documentElement ? observeDocument() : document.addEventListener("DOMContentLoaded", observeDocument, { once: true });
    // addInitScript runs in the top page and child frames. Each document owns
    // one cursor that appears only while the real pointer is inside it, so an
    // iframe or navigation cannot leave a frozen cursor behind. requestAnimationFrame
    // coalesces high-frequency events and avoids slowing down heavy pages.
    document.addEventListener("pointermove", updateCursor, true);
    document.addEventListener("mousemove", updateCursor, true);
    document.addEventListener("pointerleave", hideCursor, true);
    document.addEventListener("mouseleave", hideCursor, true);
    window.addEventListener("blur", hideCursor, true);
    window.addEventListener("pagehide", hideCursor, true);
    document.addEventListener("pointerdown", (event) => {
      updateCursor(event);
      window.__pairwiseRecordingMetrics.clicks += 1;
      const dot = document.createElement("div");
      Object.assign(dot.style, {
        position: "fixed", left: `${event.clientX - 15}px`, top: `${event.clientY - 15}px`,
        width: "30px", height: "30px", border: "3px solid #ef4444", borderRadius: "50%",
        background: "rgba(239,68,68,.16)", pointerEvents: "none", zIndex: "2147483647",
        transition: "transform .45s ease-out, opacity .45s ease-out",
      });
      document.documentElement.appendChild(dot);
      requestAnimationFrame(() => { dot.style.transform = "scale(1.8)"; dot.style.opacity = "0"; });
      setTimeout(() => dot.remove(), 520);
    }, true);
  }, {
    manualEvidence: interactionMode === "manual",
    destructiveControlPattern: DESTRUCTIVE_CONTROL_PATTERN,
  });
  const pages = context.pages();
  const page = pages[0] || await context.newPage();
  recordedVideo = page.video();
  page.on("close", () => {
    if (!finishing) void finish("page_closed");
  });
  page.on("response", (response) => {
    if (response.status() >= 400 && ["script", "stylesheet"].includes(response.request().resourceType())) {
      try {
        if (new URL(response.url()).origin === new URL(page.url()).origin) {
          failedPageAssets.push({ url: response.url(), status: response.status() });
        }
      } catch {}
    }
    if (!monitorRequests || response.status() < 200 || response.status() >= 300) return;
    const request = response.request();
    if (!["xhr", "fetch"].includes(request.resourceType())) return;
    try {
      if (new URL(response.url()).origin === new URL(page.url()).origin) {
        successfulRequests.push({ method: request.method(), url: response.url(), status: response.status() });
      }
    } catch {}
  });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.bringToFront();
  visibleRecordingStartedAt = Date.now();
  monitorRequests = true;
  process.stdout.write(`${JSON.stringify({ event: "ready", url: page.url() })}\n`);
  demonstrationPromise = interactionMode === "failure"
    ? demonstrateFailureEvidence(page)
    : interactionMode === "manual"
    ? (apiDemo
      ? installDirectApiPanel(page, apiDemo)
        .then(() => ({ required: false, ok: true, interactionMode, apiDemo: true }))
        .catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
      : Promise.resolve({ required: false, ok: true, interactionMode }))
    : apiDemo?.force_direct
    ? demonstrateDirectApiWorkflow(page, apiDemo).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
    : new URL(page.url()).pathname.startsWith("/docs")
    ? demonstrateSwaggerWorkflow(page, apiDemo).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
    : apiDemo
    ? demonstrateDirectApiWorkflow(page, apiDemo).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
    : String(await page.evaluate(() => document.contentType || "")).toLowerCase().includes("json")
    ? demonstrateBareJsonApiWorkflow(page).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
    : demonstrateGenericWorkflow(page).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }));
  if (interactionMode === "failure") {
    void demonstrationPromise.then(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      await finish("failure_evidence_complete");
    });
  } else if (interactionMode === "auto") {
    void finishAfterAutomaticWorkflow(page);
  }
  // Reserve time for the final evidence check, browser close, and MP4
  // conversion so the saved file remains below the 90-second delivery limit.
  const elapsedBeforeDeadline = Date.now() - recordingStartedAt;
  const deadlineDelay = Math.max(1000, maximum * 1000 - elapsedBeforeDeadline - 8000);
  setTimeout(() => finish("maximum_duration"), deadlineDelay);
  setInterval(() => { if (existsSync(stopFile)) finish("manual_stop"); }, 250);
  process.on("SIGINT", () => finish("manual_stop"));
  process.on("SIGTERM", () => finish("terminated"));
} catch (error) {
  console.error(error?.stack || String(error));
  try { await context?.close(); } catch {}
  process.exit(1);
}
