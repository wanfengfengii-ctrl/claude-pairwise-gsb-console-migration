"use strict";

const LOCAL_ORIGIN = "http://127.0.0.1:8865";
const LOCAL_API = `${LOCAL_ORIGIN}/api/solo-qa`;
const SOLO_ORIGIN = "https://solo2.jzxhnh.com";
const PAIR_RE = /^pair-[a-f0-9]{16}$/;
const RETRYABLE = new Set([0, 408, 425, 429, 500, 502, 503, 504]);
const RETRY_DELAYS = [500, 1500, 3500];
const LOOKUP_RETRIES = 2;
const GET_TIMEOUT_MS = 25000;
const WRITE_TIMEOUT_MS = 120000;
const UPLOAD_TIMEOUT_MS = 300000;
const STATUS_MAP = {
  SUBMITTED: "qc_pending",
  QC_PASSED: "qc_passed",
  PENDING_FIX: "needs_fix",
  DISCARDED: "discarded",
};
const activeSubmissions = new Map();

function errorMessage(body, fallback) {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object") {
    const messages = [];
    const add = (value, field = "") => {
      if (Array.isArray(value)) return value.forEach((item) => add(item, field));
      if (value && typeof value === "object") {
        return add(value.message || value.msg || value.detail || value.error, value.field || field);
      }
      const text = String(value || "").trim();
      if (text) messages.push(field ? `${field}：${text}` : text);
    };
    if (Array.isArray(body.errors)) body.errors.forEach((item) => add(item));
    else if (body.errors && typeof body.errors === "object") {
      Object.entries(body.errors).forEach(([field, value]) => add(value, field));
    }
    add(body.detail); add(body.error); add(body.message);
    if (messages.length) return [...new Set(messages)].join("；");
  }
  return fallback;
}

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { credentials: "omit", cache: "no-store", ...options });
  } catch (error) {
    throw new Error(`本地 Pairwise 系统连接失败：${error instanceof Error ? error.message : String(error)}`);
  }
  const text = await response.text();
  let body = {};
  if (text) { try { body = JSON.parse(text); } catch { body = text; } }
  if (!response.ok) throw new Error(errorMessage(body, `本地接口请求失败 (${response.status})`));
  return body;
}
function localJson(path, options = {}) { return requestJson(`${LOCAL_API}${path}`, options); }
function jsonOptions(body, method = "POST") {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}
function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function soloTab() {
  const tabs = await chrome.tabs.query({ url: `${SOLO_ORIGIN}/*` });
  const tab = tabs.find((item) => item.active && Number.isInteger(item.id))
    || tabs.find((item) => Number.isInteger(item.id));
  if (!tab) throw new Error("请先在 Chrome 打开并登录 SOLO-QA，再保持该页面打开");
  return tab;
}

function bytesToBase64(bytes) {
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 32 * 1024) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32 * 1024)));
  }
  return btoa(chunks.join(""));
}

async function requestRemoteOnce(path, options = {}, pageBody = null) {
  const tab = await soloTab();
  const method = String(options.method || "GET").toUpperCase();
  const headers = {};
  new Headers(options.headers || {}).forEach((value, key) => { headers[key] = value; });
  const body = pageBody || (typeof options.body === "string"
    ? { kind: "text", value: options.body } : { kind: "none" });
  const timeoutMs = path === "/submissions/upload" ? UPLOAD_TIMEOUT_MS
    : method === "GET" ? GET_TIMEOUT_MS : WRITE_TIMEOUT_MS;
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN",
      func: async (request) => {
        const cookieValue = (name) => {
          const prefix = `${name}=`;
          const entry = document.cookie.split("; ").find((item) => item.startsWith(prefix));
          return entry ? decodeURIComponent(entry.slice(prefix.length)) : "";
        };
        const requestHeaders = { ...request.headers };
        if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
          const csrf = cookieValue("solo_qa_csrf");
          if (!csrf) return { ok: false, status: 403, body: { detail: "SOLO-QA 页面缺少 CSRF 凭据，请刷新登录页面" } };
          requestHeaders["X-CSRF-Token"] = csrf;
        }
        let requestBody;
        if (request.body.kind === "text") requestBody = request.body.value;
        if (request.body.kind === "file") {
          const binary = atob(request.body.base64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
          const form = new FormData();
          form.append("file", new Blob([bytes], { type: request.body.contentType }), request.body.filename);
          if (request.body.uploadKind) form.append("kind", request.body.uploadKind);
          requestBody = form;
          delete requestHeaders["content-type"];
          delete requestHeaders["Content-Type"];
        }
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), request.timeoutMs);
        let response;
        let responseBody = {};
        try {
          response = await fetch(`/api/v1${request.path}`, {
            method: request.method, headers: requestHeaders, body: requestBody,
            credentials: "include", cache: "no-store", signal: controller.signal,
          });
          const text = await response.text();
          if (text) { try { responseBody = JSON.parse(text); } catch { responseBody = text; } }
        } catch (error) {
          return { ok: false, status: 0, body: { detail: controller.signal.aborted
            ? (request.path === "/submissions/upload"
              ? `附件上传超过 ${Math.round(request.timeoutMs / 1000)} 秒；远端可能留下未关联附件，但本次 GSB 提交或返修尚未写入，可稍后单条重试`
              : `SOLO-QA 请求超过 ${Math.round(request.timeoutMs / 1000)} 秒，结果可能已在远端生效，请先同步确认`)
            : `SOLO-QA 连接失败：${error instanceof Error ? error.message : String(error)}` } };
        } finally {
          clearTimeout(timeout);
        }
        return { ok: response.ok, status: response.status, body: responseBody };
      },
      args: [{ path, method, headers, body, timeoutMs }],
    });
  } catch (error) {
    const failure = new Error(`无法调用已登录的 SOLO-QA 页面：${error instanceof Error ? error.message : String(error)}`);
    failure.remoteStatus = 0; throw failure;
  }
  const result = injected?.[0]?.result;
  if (!result || typeof result.status !== "number") {
    const failure = new Error("SOLO-QA 页面没有返回有效结果，请刷新后重试"); failure.remoteStatus = 0; throw failure;
  }
  if (!result.ok) {
    const failure = new Error(errorMessage(result.body, `SOLO-QA 请求失败 (${result.status || "网络错误"})`));
    failure.remoteStatus = result.status; throw failure;
  }
  return result.body;
}

async function remote(path, options = {}, pageBody = null, attempts = 1) {
  let last;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try { return await requestRemoteOnce(path, options, pageBody); }
    catch (error) {
      last = error;
      if (attempt >= attempts || !RETRYABLE.has(Number(error?.remoteStatus))) throw error;
      await wait(RETRY_DELAYS[Math.min(attempt - 1, RETRY_DELAYS.length - 1)]);
    }
  }
  throw last;
}
function remoteJson(path, options = {}) {
  return remote(path, options, null, String(options.method || "GET").toUpperCase() === "GET" ? LOOKUP_RETRIES : 1);
}

async function sha256Hex(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function uploadFile(meta, uploadKind = "") {
  const response = await fetch(`${LOCAL_ORIGIN}${meta.url}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`读取本地文件失败 (${response.status})：${meta.name}`);
  const blob = await response.blob();
  if (blob.size !== Number(meta.size)) throw new Error(`本地文件大小已变化：${meta.name}`);
  if (await sha256Hex(blob) !== meta.sha256) throw new Error(`本地文件摘要已变化：${meta.name}`);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  return remote("/submissions/upload", { method: "POST" }, {
    kind: "file", base64: bytesToBase64(bytes), filename: meta.name,
    contentType: meta.content_type || blob.type || "application/octet-stream", uploadKind,
  }, 1);
}

function normalizeLabel(value) {
  return String(value || "")
    .replace(/[＊*：:\s_\-—–/\\（）()【】\[\]]/g, "")
    .toLowerCase();
}
const FIELD_SOURCE_BY_LABEL = new Map([
  ["a模型名称", "a_model_name"],
  ["b模型名称", "b_model_name"],
  ["a运行录屏", "a_video"],
  ["a运行录像", "a_video"],
  ["a录屏", "a_video"],
  ["a录像", "a_video"],
  ["b运行录屏", "b_video"],
  ["b运行录像", "b_video"],
  ["b录屏", "b_video"],
  ["b录像", "b_video"],
]);
function fieldSourceKeys(field) {
  const schemaKey = String(field.field_key || "");
  const keys = [schemaKey];
  for (const label of [field.label, field.name, field.title]) {
    const alias = FIELD_SOURCE_BY_LABEL.get(normalizeLabel(label));
    if (alias && !keys.includes(alias)) keys.push(alias);
  }
  return keys;
}
function choices(field) {
  return [field.options, field.choices, field.validation?.options].find(Array.isArray) || [];
}
function normalizeChoice(field, value) {
  const options = choices(field);
  if (!options.length) return value;
  const wanted = normalizeLabel(value);
  const match = options.find((option) => {
    const optionValue = typeof option === "object" ? option.value : option;
    const optionLabel = typeof option === "object" ? option.label : option;
    return normalizeLabel(optionValue) === wanted || normalizeLabel(optionLabel) === wanted;
  });
  if (!match) {
    const allowed = options.map((option) => typeof option === "object" ? option.label ?? option.value : option);
    throw new Error(`${field.label || field.field_key}的值“${value}”不被本期表单接受；当前可选：${allowed.join("、")}`);
  }
  return typeof match === "object" ? match.value : match;
}
function buildData(schema, bundle, uploaded) {
  const result = {}, missing = [];
  for (const field of schema.fields || []) {
    if (field.is_enabled === false) continue;
    const key = String(field.field_key || "");
    if (!key) continue;
    const sourceKeys = fieldSourceKeys(field);
    const uploadKey = sourceKeys.find((candidate) => Object.prototype.hasOwnProperty.call(uploaded, candidate));
    const valueKey = sourceKeys.find((candidate) => Object.prototype.hasOwnProperty.call(bundle.values || {}, candidate));
    if (uploadKey) result[key] = uploaded[uploadKey];
    else if (valueKey) result[key] = normalizeChoice(field, bundle.values[valueKey]);
    else if (field.is_required) missing.push(field.label || key);
  }
  // The current SOLO-QA form schema no longer exposes `validity`, while the
  // submission validator still requires it. Keep sending the local value until
  // the platform schema and validator are consistent again.
  if (!Object.prototype.hasOwnProperty.call(result, "validity")
      && Object.prototype.hasOwnProperty.call(bundle.values || {}, "validity")) {
    result.validity = bundle.values.validity;
  }
  if (missing.length) throw new Error(`SOLO-QA 必填字段缺少可提交的本地值：${missing.join("、")}`);
  return result;
}

function pendingUploads(bundle) {
  const pending = {};
  for (const key of ["a_trace_file", "b_trace_file"]) {
    if (bundle.files?.[key]) pending[key] = [{}];
  }
  for (const key of ["a_video", "b_video"]) {
    if (bundle.files?.[key]) pending[key] = "pending";
  }
  return pending;
}

async function loadBundle(pairId) {
  if (!PAIR_RE.test(pairId)) throw new Error("Pair ID 格式不正确");
  return localJson(`/pairs/${pairId}/payload`);
}
async function recordState(bundle, values) {
  return localJson("/state", jsonOptions({ pair_id: bundle.pair_id, payload_sha256: bundle.payload_sha256, ...values }));
}
function detailSources(item) {
  return [item, item?.data, item?.values, item?.form_data, item?.payload].filter((value) => value && typeof value === "object");
}
function firstValue(item, keys) {
  for (const source of detailSources(item)) for (const key of keys) if (source[key] != null && String(source[key]).trim()) return String(source[key]);
  return "";
}
function compactRemote(item) {
  return {
    id: String(item?.id || ""), status: String(item?.status || "SUBMITTED"),
    a_session_id: firstValue(item, ["a_session_id", "A-SessionID"]),
    b_session_id: firstValue(item, ["b_session_id", "B-SessionID"]),
    a_prompt_id: firstValue(item, ["a_prompt_id", "A-PromptID"]),
    b_prompt_id: firstValue(item, ["b_prompt_id", "B-PromptID"]),
    user_prompt: firstValue(item, ["user_prompt", "User Prompt"]),
    qc_summary: String(item?.qc_summary || item?.message || "").slice(0, 2000),
    submitted_at: String(item?.submitted_at || item?.created_at || "").slice(0, 128),
    updated_at: String(item?.updated_at || item?.qc_finished_at || "").slice(0, 128),
  };
}
async function findRemote(bundle, progress = () => {}) {
  const values = bundle.values || {};
  const prompt = String(values.user_prompt || "").trim();
  const queries = [...new Set([
    values.a_session_id, values.b_session_id, values.a_prompt_id, values.b_prompt_id,
    prompt.slice(0, 48),
  ].map((value) => String(value || "").trim()).filter(Boolean))];
  const seen = new Set();
  // Search in small waves: the remote endpoint is slow, but every query must
  // still be checked before a new submission can be created safely.
  for (let offset = 0; offset < queries.length; offset += 2) {
    progress(`查重 ${Math.min(offset + 2, queries.length)}/${queries.length}`);
    const responses = await Promise.all(queries.slice(offset, offset + 2).map((query) =>
      remoteJson(`/gsb/submissions?page=1&page_size=50&keyword=${encodeURIComponent(query)}`)));
    for (const response of responses) {
      const items = Array.isArray(response.items) ? response.items : [];
      for (const item of items) {
        const id = String(item?.id || "");
        if (!id || seen.has(id)) continue;
        seen.add(id);
        let detail = compactRemote(item);
        const sessionsPossible = Boolean(values.a_session_id && values.b_session_id);
        const promptsPossible = Boolean(values.a_prompt_id && values.b_prompt_id);
        if ((sessionsPossible && (!detail.a_session_id || !detail.b_session_id))
            || (promptsPossible && (!detail.a_prompt_id || !detail.b_prompt_id))) {
          try { detail = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(id)}`)); }
          catch { /* The platform can list a submission while denying its detail endpoint. */ }
        }
        const sameSessions = sessionsPossible
          && detail.a_session_id === values.a_session_id && detail.b_session_id === values.b_session_id;
        const samePrompts = promptsPossible
          && detail.a_prompt_id === values.a_prompt_id && detail.b_prompt_id === values.b_prompt_id;
        if (sameSessions || samePrompts) return detail;
      }
    }
  }
  return null;
}
async function loadRemoteDetail(bundle, remoteId) {
  try {
    const raw = await remoteJson(`/gsb/submissions/${encodeURIComponent(remoteId)}`);
    return { ...compactRemote(raw), raw };
  }
  catch (detailError) {
    const recovered = await findRemote(bundle);
    if (recovered && String(recovered.id) === String(remoteId)) return recovered;
    throw detailError;
  }
}
function stateForRemote(status) { return STATUS_MAP[String(status || "")] || "qc_pending"; }
function stateValues(detail, bundle = null) {
  const values = {
    status: stateForRemote(detail.status), remote_id: detail.id,
    remote_url: detail.id ? `${SOLO_ORIGIN}/app/gsb/submissions/${detail.id}` : "",
    remote_status: detail.status, qc_summary: detail.qc_summary,
    submitted_at: detail.submitted_at, remote_updated_at: detail.updated_at,
    error: "",
  };
  if (bundle?.payload_sha256) values.payload_sha256 = bundle.payload_sha256;
  return values;
}
function markerKey(pairId) { return `solo-qa-submit:${pairId}`; }
async function getMarker(pairId) {
  return (await chrome.storage.local.get(markerKey(pairId)))[markerKey(pairId)] || null;
}
async function setMarker(bundle, phase, detail = null) {
  await chrome.storage.local.set({ [markerKey(bundle.pair_id)]: {
    payload_sha256: bundle.payload_sha256, phase, detail,
  } });
}
async function clearMarker(pairId) { await chrome.storage.local.remove(markerKey(pairId)); }
function uncertainCreate(error) {
  const status = Number(error?.remoteStatus);
  return !status || RETRYABLE.has(status) || status >= 500;
}

async function uploadBundle(bundle, schema, progress = () => {}) {
  const traceLimit = Number(schema.attachmentMaxMb ?? schema.attachment_max_mb ?? 27) * 1024 * 1024;
  const videoLimit = Number(schema.videoMaxMb ?? schema.video_max_mb ?? 500) * 1024 * 1024;
  for (const key of ["a_trace_file", "b_trace_file"]) if (Number(bundle.files[key]?.size || 0) > traceLimit) throw new Error(`${key} 超过平台轨迹上限`);
  for (const key of ["a_video", "b_video"]) if (Number(bundle.files[key]?.size || 0) > videoLimit) throw new Error(`${key} 超过平台录像上限`);
  const entries = [
    ["a_trace_file", "A 轨迹", ""], ["a_video", "A 录像", "video"],
    ["b_trace_file", "B 轨迹", ""], ["b_video", "B 录像", "video"],
  ];
  const uploaded = {};
  // Upload sequentially. The four injected page requests used to overlap,
  // competing for the same gateway and making a failed batch hard to locate.
  for (const [index, [key, label, kind]] of entries.entries()) {
    progress(`上传 ${label}（${index + 1}/4）`);
    try { uploaded[key] = await uploadFile(bundle.files[key], kind); }
    catch (error) {
      throw new Error(`${label}上传失败（本次尚未写入 GSB 记录）：${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return {
    a_trace_file: [uploaded.a_trace_file], a_video: uploaded.a_video.url || "",
    b_trace_file: [uploaded.b_trace_file], b_video: uploaded.b_video.url || "",
  };
}

function existingRemoteAttachments(schema, bundle, raw) {
  if (!raw || typeof raw !== "object") return null;
  const keys = ["a_trace_file", "a_video", "b_trace_file", "b_video"];
  const result = {};
  for (const key of keys) {
    const field = (schema.fields || []).find((candidate) => candidate.is_enabled !== false
      && fieldSourceKeys(candidate).includes(key));
    if (!field) return null;
    const fieldKey = String(field.field_key || "");
    const source = detailSources(raw).find((item) => Object.prototype.hasOwnProperty.call(item, fieldKey));
    const value = source?.[fieldKey];
    const trace = key.endsWith("trace_file");
    const attachment = trace ? (Array.isArray(value) && value.length === 1 ? value[0] : null) : value;
    const url = typeof attachment === "string" ? attachment : attachment?.url;
    const name = String(bundle.files?.[key]?.name || "");
    if (!name || typeof url !== "string") return null;
    let parsed, path;
    try { parsed = new URL(url); path = decodeURIComponent(parsed.pathname); } catch { return null; }
    if (parsed.protocol !== "https:" || !parsed.hostname.endsWith(".myqcloud.com")
        || !(path.endsWith(`_${name}`) || path.endsWith(`/${name}`))) return null;
    if (trace && (!attachment || typeof attachment !== "object" || Array.isArray(attachment))) return null;
    const remoteHash = attachment && typeof attachment === "object"
      ? String(attachment.sha256 || attachment.sha256_hex || "").toLowerCase() : "";
    if (remoteHash && remoteHash !== String(bundle.files[key].sha256 || "").toLowerCase()) return null;
    result[key] = trace ? [attachment] : url;
  }
  return result;
}

async function submitOneUnlocked(pairId, progress = () => {}) {
  progress("读取本地提交资料");
  const bundle = await loadBundle(pairId);
  if (!bundle.ready) throw new Error(`提交前检查未通过：${(bundle.issues || []).join("；")}`);
  if (bundle.solo_qa?.remote_id) {
    await clearMarker(pairId).catch(() => {});
    return { pair_id: pairId, outcome: "skipped", remote_id: String(bundle.solo_qa.remote_id), reason: "该 Pair 已绑定远端记录，只能同步状态或提交返修" };
  }
  const marker = await getMarker(pairId);
  if (marker?.phase === "created" && marker.detail?.id) {
    if (marker.payload_sha256 !== bundle.payload_sha256) {
      throw new Error("远端已创建记录，但本地提交资料随后改变；请先同步远端状态，禁止直接重交");
    }
    await recordState(bundle, stateValues(marker.detail, bundle));
    await clearMarker(pairId).catch(() => {});
    return { pair_id: pairId, outcome: "recovered", remote_id: marker.detail.id };
  }
  if (marker?.phase === "creating") {
    const recovered = await findRemote(bundle, progress);
    if (recovered) {
      await recordState(bundle, stateValues(recovered, bundle));
      await clearMarker(pairId).catch(() => {});
      return { pair_id: pairId, outcome: "recovered", remote_id: recovered.id };
    }
    throw new Error("该 Pair 上一次创建请求的结果仍不确定，已拦截重复提交；请先在 SOLO-QA 核对并同步状态");
  }
  if (bundle.solo_qa?.status === "submitting") {
    const recovered = await findRemote(bundle, progress);
    if (recovered) {
      await recordState(bundle, stateValues(recovered, bundle));
      await clearMarker(pairId).catch(() => {});
      return { pair_id: pairId, outcome: "recovered", remote_id: recovered.id };
    }
    if (marker?.payload_sha256 !== bundle.payload_sha256 || marker.phase !== "precreate") {
      throw new Error("该 Pair 上一次创建请求的结果仍不确定，已拦截重复提交；请先在 SOLO-QA 核对并同步状态");
    }
    progress("上次中断在创建前，安全续传");
  } else {
    const recoveredBefore = await findRemote(bundle, progress);
    if (recoveredBefore) {
      await recordState(bundle, stateValues(recoveredBefore, bundle));
      await clearMarker(pairId).catch(() => {});
      return { pair_id: pairId, outcome: "recovered", remote_id: recoveredBefore.id };
    }
  }
  // Persist the last safe phase *before* the local submitting flag. If the
  // worker goes away here, a later attempt knows no create POST was sent.
  await setMarker(bundle, "precreate");
  if (bundle.solo_qa?.status !== "submitting") {
    try { await recordState(bundle, { status: "submitting", error: "" }); }
    catch (error) {
      // Another helper instance may have acquired the local submitting slot.
      // Never let our pre-create marker certify that instance's POST as safe.
      await clearMarker(pairId).catch(() => {});
      throw error;
    }
  }
  let createStarted = false;
  try {
    progress("读取远端表单");
    const schema = await remoteJson("/gsb/form-schema");
    buildData(schema, bundle, pendingUploads(bundle));
    const uploaded = await uploadBundle(bundle, schema, progress);
    const data = buildData(schema, bundle, uploaded);
    progress("创建远端 GSB 记录");
    await setMarker(bundle, "creating");
    createStarted = true;
    const created = await remoteJson("/gsb/submissions", jsonOptions({ data, schema_fingerprint: schema.fingerprint || "" }));
    const detail = compactRemote({ ...created, status: created.status || "SUBMITTED" });
    if (!detail.id) throw new Error("SOLO-QA 已响应，但没有返回提交 ID");
    await setMarker(bundle, "created", detail);
    await recordState(bundle, stateValues(detail, bundle));
    await clearMarker(pairId).catch(() => {});
    return { pair_id: pairId, outcome: "submitted", remote_id: detail.id, status: detail.status };
  } catch (error) {
    let recovered = null;
    if (createStarted) {
      progress("核对远端是否已创建");
      try { recovered = await findRemote(bundle, progress); } catch { recovered = null; }
    }
    if (recovered) {
      await recordState(bundle, stateValues(recovered, bundle));
      await clearMarker(pairId).catch(() => {});
      return { pair_id: pairId, outcome: "recovered", remote_id: recovered.id };
    }
    const message = error instanceof Error ? error.message : String(error);
    if (createStarted && uncertainCreate(error)) {
      throw new Error(`远端创建结果待确认，已拦截重试以免重复提交：${message}`);
    }
    await recordState(bundle, { status: "failed", error: message });
    await clearMarker(pairId);
    throw new Error(message);
  }
}

async function submitOne(pairId, progress = () => {}) {
  if (activeSubmissions.has(pairId)) return activeSubmissions.get(pairId);
  const pending = submitOneUnlocked(pairId, progress);
  activeSubmissions.set(pairId, pending);
  try { return await pending; }
  finally { activeSubmissions.delete(pairId); }
}

async function repairOne(pairId, progress = () => {}) {
  progress("读取返修资料");
  const bundle = await loadBundle(pairId);
  if (!bundle.ready) throw new Error(`返修前检查未通过：${(bundle.issues || []).join("；")}`);
  const remoteId = String(bundle.solo_qa?.remote_id || "");
  if (!remoteId || bundle.solo_qa?.remote_status !== "PENDING_FIX") throw new Error("只有远端待返修的 Pair 可以返修，请先同步质检状态");
  const before = await loadRemoteDetail(bundle, remoteId);
  if (before.status !== "PENDING_FIX") throw new Error("远端记录已不是待返修状态，请先同步");
  await recordState(bundle, { status: "submitting", remote_id: remoteId, remote_status: before.status, error: "" });
  try {
    const schema = await remoteJson("/gsb/form-schema");
    buildData(schema, bundle, pendingUploads(bundle));
    const existing = existingRemoteAttachments(schema, bundle, before.raw);
    if (existing) progress("复用已核对的远端附件");
    const uploaded = existing || await uploadBundle(bundle, schema, progress);
    const data = buildData(schema, bundle, uploaded);
    progress("提交返修");
    const updated = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(remoteId)}`, jsonOptions({
      data, schema_fingerprint: schema.fingerprint || "", comment: "",
    }, "PUT")));
    const detail = updated.id && updated.status ? updated : await loadRemoteDetail(bundle, remoteId);
    await recordState(bundle, stateValues(detail, bundle));
    return { pair_id: pairId, outcome: "repaired", remote_id: remoteId, status: detail.status };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await recordState(bundle, { status: "failed", remote_id: remoteId, remote_status: "PENDING_FIX", error: message });
    throw new Error(message);
  }
}

async function batch(payload, action, report = () => {}) {
  const ids = Array.isArray(payload?.pair_ids) ? [...new Set(payload.pair_ids.map(String))] : [];
  if (!ids.length || ids.length > 100 || ids.some((id) => !PAIR_RE.test(id))) throw new Error("请选择有效的 Pair");
  const results = [];
  for (const [index, pairId] of ids.entries()) {
    const progress = (phase) => report({ pair_id: pairId, index: index + 1, total: ids.length, phase });
    progress("开始处理");
    try { results.push(await action(pairId, progress)); }
    catch (error) { results.push({ pair_id: pairId, outcome: "failed", error: error instanceof Error ? error.message : String(error) }); }
    progress(results[results.length - 1].outcome === "failed" ? "处理失败，继续下一条" : "处理完成");
  }
  return { results, failed: results.filter((item) => item.outcome === "failed").length };
}

function selectSyncItems(items, payload = {}) {
  const requested = Array.isArray(payload?.pair_ids)
    ? [...new Set(payload.pair_ids.map(String).filter(Boolean))] : [];
  // The default sync is a follow-up poll, not a full historical rescan.
  // Explicit row/selection sync remains available for manual verification.
  if (!requested.length) return items.filter((item) => item.remote_id
    && ["submitting", "submitted", "qc_pending", "failed"].includes(String(item.status || "")));
  if (requested.length > 100 || requested.some((id) => !PAIR_RE.test(id))) throw new Error("请选择有效的 Pair");
  const wanted = new Set(requested);
  return items.filter((item) => wanted.has(String(item.pair_id || "")));
}

async function syncRemote(payload = {}, report = () => {}) {
  const local = await localJson("/submissions");
  const results = [];
  const selected = selectSyncItems(local.items || [], payload);
  for (const [index, item] of selected.entries()) {
    report({ pair_id: item.pair_id, index: index + 1, total: selected.length, phase: "查询质检状态" });
    try {
      const bundle = await loadBundle(item.pair_id);
      const detail = await loadRemoteDetail(bundle, item.remote_id);
      await localJson("/state", jsonOptions({ pair_id: item.pair_id, payload_sha256: item.payload_sha256 || "", ...stateValues(detail) }));
      results.push({ pair_id: item.pair_id, outcome: "synced", status: detail.status });
    } catch (error) {
      results.push({ pair_id: item.pair_id, outcome: "failed", error: error instanceof Error ? error.message : String(error) });
    }
  }
  return { results, failed: results.filter((item) => item.outcome === "failed").length };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  let origin = "";
  try { origin = new URL(sender.url || "").origin; } catch { origin = ""; }
  if (origin !== LOCAL_ORIGIN) { sendResponse({ ok: false, error: "只接受本地 Pairwise 系统发起的请求" }); return false; }
  const report = (progress) => {
    if (!Number.isInteger(sender.tab?.id)) return;
    chrome.tabs.sendMessage(sender.tab.id, {
      type: "PAIRWISE_GSB_PROGRESS", requestId: message.requestId, progress,
    }).catch(() => {});
  };
  const action = message?.type === "PAIRWISE_GSB_SUBMIT"
    ? () => batch(message.payload || {}, submitOne, report)
    : message?.type === "PAIRWISE_GSB_REPAIR"
      ? () => batch(message.payload || {}, repairOne, report)
      : message?.type === "PAIRWISE_GSB_SYNC" ? () => syncRemote(message.payload || {}, report) : null;
  if (!action) { sendResponse({ ok: false, error: "未知的提交助手操作" }); return false; }
  action().then((data) => sendResponse({ ok: true, data })).catch((error) => sendResponse({
    ok: false, error: error instanceof Error ? error.message : String(error),
  }));
  return true;
});
