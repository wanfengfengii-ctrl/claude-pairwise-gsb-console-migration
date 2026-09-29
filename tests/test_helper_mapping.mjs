import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../chrome-solo-qa-gsb-helper/background.js", import.meta.url), "utf8");
const context = vm.createContext({
  chrome: {
    runtime: { onMessage: { addListener() {} } },
    tabs: { query: async () => [] },
    scripting: { executeScript: async () => [] },
  },
  Headers,
  URL,
  fetch,
  crypto,
  btoa,
  setTimeout,
});
vm.runInContext(source, context);

function buildData(schema, bundle, uploaded) {
  context.schema = schema;
  context.bundle = bundle;
  context.uploaded = uploaded;
  return vm.runInContext("buildData(schema, bundle, uploaded)", context);
}

test("maps the current SOLO-QA recording labels to local A/B videos", () => {
  const schema = {
    fields: [
      { field_key: "a_runtime_recording", label: "A-运行录屏", is_required: true },
      { field_key: "b_runtime_recording", label: "B-运行录屏", is_required: true },
    ],
  };
  const result = buildData(schema, { values: {} }, {
    a_video: "https://files.example/a.mp4",
    b_video: "https://files.example/b.mp4",
  });
  assert.equal(result.a_runtime_recording, "https://files.example/a.mp4");
  assert.equal(result.b_runtime_recording, "https://files.example/b.mp4");
});

test("keeps exact field-key mapping for existing SOLO-QA fields", () => {
  const schema = { fields: [{ field_key: "user_prompt", label: "User Prompt", is_required: true }] };
  const result = buildData(schema, { values: { user_prompt: "实现复杂工作流" } }, {});
  assert.equal(result.user_prompt, "实现复杂工作流");
});

test("maps the new A/B model-name fields to each Arm's actual model", () => {
  const schema = { fields: [
    { field_key: "model_used_for_a", label: "A-模型名称", is_required: true },
    { field_key: "model_used_for_b", label: "B-模型名称", is_required: true },
  ] };
  const result = buildData(schema, { values: {
    a_model_name: "auto_model/urm",
    b_model_name: "ark/urm-03",
  } }, {});
  assert.equal(result.model_used_for_a, "auto_model/urm");
  assert.equal(result.model_used_for_b, "ark/urm-03");
});

test("maps both delivery scores and descriptions without replacing the GSB reason", () => {
  const schema = { fields: [
    { field_key: "a_score_delivery", label: "A-交付完整性", is_required: true },
    { field_key: "a_desc_delivery", label: "A-交付完整性描述", is_required: true },
    { field_key: "b_score_delivery", label: "B-交付完整性", is_required: true },
    { field_key: "b_desc_delivery", label: "B-交付完整性描述", is_required: true },
    { field_key: "gsb_reason", label: "GSB 理由", is_required: true },
  ] };
  const result = buildData(schema, { values: {
    a_score_delivery: 4, a_desc_delivery: "A 的完整性证据",
    b_score_delivery: 3, b_desc_delivery: "B 的完整性证据",
    gsb_reason: "A/B 对比分析",
  } }, {});
  assert.equal(result.a_score_delivery, 4);
  assert.equal(result.b_score_delivery, 3);
  assert.equal(result.a_desc_delivery, "A 的完整性证据");
  assert.equal(result.b_desc_delivery, "B 的完整性证据");
  assert.equal(result.gsb_reason, "A/B 对比分析");
});

test("keeps sending validity when the form schema omits it but the validator requires it", () => {
  const schema = { fields: [{ field_key: "user_prompt", label: "User Prompt", is_required: true }] };
  const result = buildData(schema, {
    values: { user_prompt: "实现复杂工作流", validity: "有效" },
  }, {});
  assert.equal(result.validity, "有效");
});

test("reports a missing local value for a required platform field", () => {
  const schema = { fields: [{ field_key: "new_required", label: "全新必填项", is_required: true }] };
  assert.throws(
    () => buildData(schema, { values: {} }, {}),
    /SOLO-QA 必填字段缺少可提交的本地值：全新必填项/,
  );
});

test("limits a status sync to the requested Pair when a row button is used", () => {
  context.syncItems = [
    { pair_id: "pair-1111111111111111", remote_id: "51" },
    { pair_id: "pair-2222222222222222", remote_id: "52" },
  ];
  context.syncPayload = { pair_ids: ["pair-2222222222222222"] };
  const result = vm.runInContext("selectSyncItems(syncItems, syncPayload)", context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), [
    { pair_id: "pair-2222222222222222", remote_id: "52" },
  ]);
});

test("default sync only polls submissions still awaiting a local outcome", () => {
  context.syncItems = [
    { pair_id: "pair-1111111111111111", remote_id: "51", status: "qc_pending" },
    { pair_id: "pair-2222222222222222", remote_id: "52", status: "qc_passed" },
    { pair_id: "pair-3333333333333333", remote_id: "53", status: "needs_fix" },
    { pair_id: "pair-4444444444444444", remote_id: "54", status: "failed" },
    { pair_id: "pair-5555555555555555", remote_id: "55", status: "discarded" },
  ];
  context.syncPayload = {};
  const result = vm.runInContext("selectSyncItems(syncItems, syncPayload)", context);
  assert.deepEqual(Array.from(result, (item) => item.pair_id), [
    "pair-1111111111111111", "pair-4444444444444444",
  ]);
});

test("explicit row sync can still verify a settled submission", () => {
  context.syncItems = [
    { pair_id: "pair-1111111111111111", remote_id: "51", status: "qc_passed" },
  ];
  context.syncPayload = { pair_ids: ["pair-1111111111111111"] };
  const result = vm.runInContext("selectSyncItems(syncItems, syncPayload)", context);
  assert.equal(result.length, 1);
});

test("uses the current SOLO-QA GSB API routes", () => {
  assert.match(source, /remoteJson\("\/gsb\/form-schema"\)/);
  assert.match(source, /remoteJson\(`\/gsb\/submissions\/\$\{encodeURIComponent\(remoteId\)\}`\)/);
});

test("falls back to the submissions list when the detail endpoint is unavailable", async () => {
  vm.runInContext(`
    remoteJson = async (path) => {
      if (path.startsWith("/gsb/submissions?")) return { items: [{
        id: "1542", status: "PENDING_FIX",
        data: { a_session_id: "a-session", b_session_id: "b-session", user_prompt: "prompt" }
      }] };
      throw new Error("数据不存在或无权访问");
    };
    fallbackBundle = { values: {
      a_session_id: "a-session", b_session_id: "b-session", user_prompt: "prompt"
    } };
  `, context);
  const result = await vm.runInContext('loadRemoteDetail(fallbackBundle, "1542")', context);
  assert.equal(result.id, "1542");
  assert.equal(result.status, "PENDING_FIX");
});

test("does not fetch detail when list results already contain matching identifiers", async () => {
  vm.runInContext(`
    lookupPaths = [];
    remoteJson = async (path) => {
      lookupPaths.push(path);
      if (path.includes("/1542")) throw new Error("unexpected detail request");
      return { items: [{ id: "1542", status: "SUBMITTED", data: {
        a_session_id: "a-session", b_session_id: "b-session"
      } }] };
    };
    lookupBundle = { values: { a_session_id: "a-session", b_session_id: "b-session" } };
  `, context);
  const found = await vm.runInContext("findRemote(lookupBundle)", context);
  assert.equal(found.id, "1542");
  assert.equal(vm.runInContext("lookupPaths.length", context), 2);
});

test("uploads the four attachments sequentially", async () => {
  vm.runInContext(`
    activeUploads = 0; maximumUploads = 0; uploadCount = 0;
    uploadFile = async (meta) => {
      activeUploads += 1; maximumUploads = Math.max(maximumUploads, activeUploads);
      await new Promise((resolve) => setTimeout(resolve, 5));
      activeUploads -= 1; uploadCount += 1;
      return { url: meta.name };
    };
    uploadTestBundle = { files: Object.fromEntries(
      ["a_trace_file", "a_video", "b_trace_file", "b_video"].map((key) => [key, { name: key, size: 1 }])
    ) };
  `, context);
  const result = await vm.runInContext("uploadBundle(uploadTestBundle, {})", context);
  assert.equal(vm.runInContext("maximumUploads", context), 1);
  assert.equal(vm.runInContext("uploadCount", context), 4);
  assert.equal(result.a_video, "a_video");
  assert.equal(result.b_video, "b_video");
});

test("reuses all four existing attachments only when their names match the local files", () => {
  const keys = ["a_trace_file", "a_video", "b_trace_file", "b_video"];
  context.reuseSchema = { fields: keys.map((key) => ({ field_key: key })) };
  context.reuseBundle = { files: Object.fromEntries(keys.map((key) => [key, {
    name: `${key}.${key.endsWith("video") ? "mp4" : "jsonl"}`, sha256: "abc",
  }])) };
  context.reuseRaw = { data: Object.fromEntries(keys.map((key) => [key,
    key.endsWith("video")
      ? `https://files.myqcloud.com/trace/random_${key}.mp4`
      : [{ url: `https://files.myqcloud.com/trace/random_${key}.jsonl`, sha256: "abc" }],
  ])) };
  const reused = vm.runInContext("existingRemoteAttachments(reuseSchema, reuseBundle, reuseRaw)", context);
  assert.equal(reused.a_trace_file[0].url, context.reuseRaw.data.a_trace_file[0].url);
  assert.equal(reused.b_video, context.reuseRaw.data.b_video);
  context.reuseRaw.data.b_video = "https://files.myqcloud.com/trace/random_other.mp4";
  assert.equal(vm.runInContext("existingRemoteAttachments(reuseSchema, reuseBundle, reuseRaw)", context), null);
  context.reuseRaw.data.b_video = "https://files.myqcloud.com/trace/random_b_video.mp4";
  context.reuseRaw.data.a_trace_file[0].sha256 = "different";
  assert.equal(vm.runInContext("existingRemoteAttachments(reuseSchema, reuseBundle, reuseRaw)", context), null);
});

test("a text-only repair updates the existing record without uploading again", async () => {
  vm.runInContext(`
    repairBundle = { pair_id: "pair-1111111111111111", ready: true, payload_sha256: "new-text",
      solo_qa: { remote_id: "8607", remote_status: "PENDING_FIX" }, values: {}, files: reuseBundle.files };
    repairWrites = []; repairPut = null;
    loadBundle = async () => repairBundle;
    loadRemoteDetail = async () => ({ id: "8607", status: "PENDING_FIX", raw: reuseRaw });
    recordState = async (_bundle, values) => { repairWrites.push(values); };
    uploadBundle = async () => { throw new Error("attachments must not be uploaded"); };
    remoteJson = async (path, options) => {
      if (path === "/gsb/form-schema") return reuseSchema;
      repairPut = { path, data: JSON.parse(options.body).data };
      return { id: "8607", status: "SUBMITTED" };
    };
  `, context);
  context.reuseRaw.data.a_trace_file[0].sha256 = "abc";
  const result = await vm.runInContext('repairOne("pair-1111111111111111")', context);
  assert.equal(result.outcome, "repaired");
  assert.equal(vm.runInContext("repairPut.path", context), "/gsb/submissions/8607");
  assert.equal(vm.runInContext("repairPut.data.a_video", context), context.reuseRaw.data.a_video);
  assert.equal(vm.runInContext("repairWrites.at(-1).status", context), "qc_pending");
});

test("repair checks required text before uploading attachments", async () => {
  vm.runInContext(`
    missingBundle = { pair_id: "pair-1111111111111111", ready: true,
      solo_qa: { remote_id: "8607", remote_status: "PENDING_FIX" }, values: {}, files: {} };
    missingUploadCalls = 0;
    loadBundle = async () => missingBundle;
    loadRemoteDetail = async () => ({ id: "8607", status: "PENDING_FIX", raw: {} });
    recordState = async () => ({});
    uploadBundle = async () => { missingUploadCalls += 1; return {}; };
    remoteJson = async (path) => {
      if (path === "/gsb/form-schema") return { fields: [
        { field_key: "a_score_delivery", label: "A-交付完整性", is_required: true },
      ] };
      throw new Error("repair must not reach the remote PUT");
    };
  `, context);
  await assert.rejects(
    vm.runInContext('repairOne("pair-1111111111111111")', context),
    /必填字段缺少可提交的本地值：A-交付完整性/,
  );
  assert.equal(vm.runInContext("missingUploadCalls", context), 0);
});

test("resumes only a proven pre-create interruption and blocks ambiguous POST retries", async () => {
  const stored = new Map([["solo-qa-submit:pair-1111111111111111", {
    payload_sha256: "payload", phase: "precreate", detail: null,
  }]]);
  context.chrome.storage = { local: {
    get: async (key) => ({ [key]: stored.get(key) }),
    set: async (values) => { for (const [key, value] of Object.entries(values)) stored.set(key, value); },
    remove: async (key) => { stored.delete(key); },
  } };
  vm.runInContext(`
    testBundle = { pair_id: "pair-1111111111111111", payload_sha256: "payload",
      ready: true, solo_qa: { status: "submitting" }, values: {}, files: {} };
    stateWrites = []; createCalls = 0;
    loadBundle = async () => testBundle;
    recordState = async (_bundle, values) => { stateWrites.push(values); };
    findRemote = async () => null;
    buildData = () => ({});
    uploadBundle = async () => ({});
    remoteJson = async (path) => {
      if (path === "/gsb/form-schema") return { fields: [] };
      createCalls += 1;
      const error = new Error("gateway timeout"); error.remoteStatus = 504; throw error;
    };
  `, context);
  await assert.rejects(
    vm.runInContext('submitOneUnlocked("pair-1111111111111111")', context),
    /结果待确认/,
  );
  assert.equal(vm.runInContext("createCalls", context), 1);
  assert.equal(stored.get("solo-qa-submit:pair-1111111111111111").phase, "creating");
  assert.equal(vm.runInContext("stateWrites.length", context), 0);
  await assert.rejects(
    vm.runInContext('submitOneUnlocked("pair-1111111111111111")', context),
    /已拦截重复提交/,
  );
  vm.runInContext('testBundle.solo_qa.status = "failed"', context);
  await assert.rejects(
    vm.runInContext('submitOneUnlocked("pair-1111111111111111")', context),
    /已拦截重复提交/,
  );
  assert.equal(vm.runInContext("createCalls", context), 1);
});

test("coalesces concurrent submit requests for the same Pair", async () => {
  vm.runInContext(`
    submitCallCount = 0;
    submitOneUnlocked = async (pairId) => {
      submitCallCount += 1;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return { pair_id: pairId, outcome: "submitted", remote_id: "474" };
    };
  `, context);
  const result = await vm.runInContext(`Promise.all([
    submitOne("pair-1111111111111111"), submitOne("pair-1111111111111111")
  ])`, context);
  assert.equal(vm.runInContext("submitCallCount", context), 1);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), [
    { pair_id: "pair-1111111111111111", outcome: "submitted", remote_id: "474" },
    { pair_id: "pair-1111111111111111", outcome: "submitted", remote_id: "474" },
  ]);
});
