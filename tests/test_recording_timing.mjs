import assert from "node:assert/strict";
import test from "node:test";

import {
  automaticFinishDelayMs,
  browserWorkflowEvidence,
  GENERIC_CONTROL_SELECTOR,
  isDestructiveFeatureControl,
  finalizeInteractionEvidence,
  humanClickPauseMs,
  isFileUploadSubmitLabel,
  isGenericActionLabel,
  isResultNavigationLabel,
  isGenericSampleLabel,
  isNewlyRevealedControl,
  isLikelyValidSampleLabel,
  isPrimarySubmitLabel,
  isWithdrawnResultText,
  isSafeFeatureControl,
  matchesRecordingLabel,
  recordingControlKey,
  recordingControlIdentity,
  repeatedRecordingControlFamily,
  shouldTraverseChoiceControl,
  shouldPrepareInputsAfterControl,
  recordingTraversalComplete,
} from "../scripts/recording_timing.mjs";

test("a failed control cannot become a complete traversal", () => {
  assert.equal(recordingTraversalComplete([{ complete: true }], [{ label: "结果", error: "click failed" }]), false);
  assert.equal(recordingTraversalComplete([{ complete: false }], []), false);
  assert.equal(recordingTraversalComplete([{ complete: true }, { complete: true }], []), true);
});

test("generic traversal includes mode radio controls", () => {
  assert.match(GENERIC_CONTROL_SELECTOR, /input\[type=radio\]:visible/);
  assert.match(GENERIC_CONTROL_SELECTOR, /summary:visible/);
});

test("large input grids are not mistaken for feature and result controls", () => {
  assert.equal(shouldTraverseChoiceControl("checkbox", "左二（力臂 -2）", 24), false);
  assert.equal(shouldTraverseChoiceControl("radio", "普通模式", 3), true);
  assert.equal(shouldTraverseChoiceControl("checkbox", "显示结果详情", 24), true);
  assert.equal(shouldTraverseChoiceControl("button", "提交裁决", 24), true);
});

test("a specialized browser workflow returns the same completed evidence it emits", () => {
  const evidence = browserWorkflowEvidence({ ok: true, clicks: ["建立槽位"],
    controlsComplete: true, finalResultVisible: true }, 0);
  assert.equal(evidence.required, true);
  assert.equal(evidence.workflow, "browser-ui");
  assert.equal(evidence.ok, true);
  assert.equal(evidence.requests, 0);
});

test("a sample traverses newly revealed controls without consuming the global action", () => {
  const availableBefore = new Set([recordingControlKey("校验并比较")]);
  assert.equal(isNewlyRevealedControl("校验并比较", availableBefore), false);
  assert.equal(isNewlyRevealedControl("示例：缓慢漂移", availableBefore), false);
  assert.equal(isNewlyRevealedControl("发起连续漂移复核 drift-run", availableBefore), true);
});

test("automatic recordings hold the completed result for two seconds without fixed padding", () => {
  assert.equal(automaticFinishDelayMs("recording", 9000, 88), 2000);
  assert.equal(automaticFinishDelayMs("long-workflow", 41000, 50), 2000);
  assert.equal(automaticFinishDelayMs("near-deadline", 83000, 88), 0);
});

test("feature traversal skips destructive controls", () => {
  assert.equal(isSafeFeatureControl("运行分析"), true);
  assert.equal(isSafeFeatureControl("结果详情"), true);
  assert.equal(isSafeFeatureControl("删除记录"), false);
  assert.equal(isSafeFeatureControl("删"), false);
  assert.equal(isSafeFeatureControl("清除结果"), false);
  assert.equal(isSafeFeatureControl("Trash item"), false);
  assert.equal(isSafeFeatureControl("Clear all"), false);
  assert.equal(isSafeFeatureControl("×"), false);
  assert.equal(isSafeFeatureControl(" ✕ "), false);
  assert.equal(isDestructiveFeatureControl("× 删除候选"), true);
  assert.equal(isDestructiveFeatureControl("删除载波"), true);
  assert.equal(isDestructiveFeatureControl("结果详情"), false);
});

test("automatic clicks use varied human-paced pauses", () => {
  const before = Array.from({ length: 8 }, (_, index) => humanClickPauseMs(index, "before"));
  const after = Array.from({ length: 8 }, (_, index) => humanClickPauseMs(index, "after"));
  assert.ok(before.every((value) => value >= 560 && value < 980));
  assert.ok(after.every((value) => value >= 820 && value < 1340));
  assert.ok(new Set(before).size > 4);
  assert.ok(new Set(after).size > 4);
});

test("manual recording is saved for human review without automatic request detection", () => {
  assert.deepEqual(
    finalizeInteractionEvidence("manual", { required: false, ok: true }, { clicks: 0 }, 0),
    {
      required: false,
      ok: true,
      interactionMode: "manual",
      clicks: 0,
      requests: 0,
      review: "human",
    },
  );
});

test("automatic recording still requires both a click and a successful request", () => {
  assert.equal(finalizeInteractionEvidence("auto", { required: false }, { clicks: 1 }, 0).ok, false);
  assert.equal(finalizeInteractionEvidence("auto", { required: false }, { clicks: 1 }, 1).ok, true);
});

test("generic recorder recognizes Chinese sample and primary action labels", () => {
  assert.equal(isGenericSampleLabel("示例：等价改版"), true);
  assert.equal(isLikelyValidSampleLabel("已合法示例"), true);
  assert.equal(isLikelyValidSampleLabel("需修复示例"), true);
  assert.equal(isLikelyValidSampleLabel("无解示例"), false);
  assert.equal(isLikelyValidSampleLabel("非法输入示例"), false);
  assert.equal(isPrimarySubmitLabel("▶ 运行审计"), true);
  assert.equal(isPrimarySubmitLabel("运行审计"), true);
  assert.equal(isPrimarySubmitLabel("导出 JSON"), false);
  assert.equal(isGenericActionLabel("校验并比较"), true);
  assert.equal(isGenericActionLabel("启动单砖失效审计"), true);
  assert.equal(isGenericActionLabel("发起审计"), true);
  assert.equal(isGenericActionLabel("进行裁决"), true);
  assert.equal(isGenericActionLabel("导入"), false);
  assert.equal(isGenericActionLabel("清空"), false);
  assert.equal(isResultNavigationLabel("下一步"), true);
  assert.equal(isResultNavigationLabel("播放轨迹"), true);
  assert.equal(isResultNavigationLabel("τ 枚举明细（0/41 有效）"), true);
  assert.equal(isResultNavigationLabel("第二份见证"), true);
  assert.equal(isResultNavigationLabel("第 2 阶 Σw=8 · 3 片"), true);
  assert.equal(isResultNavigationLabel("#3 权重 110 · 2 片"), true);
  assert.equal(isGenericActionLabel("on 生成候选阶梯（2–5 级）"), true);
  assert.equal(isResultNavigationLabel("新增规则"), false);
  assert.equal(matchesRecordingLabel(/示例|sample/i, "载入示例"), true);
  assert.equal(matchesRecordingLabel((label) => label === "运行", "运行"), true);
  assert.equal(matchesRecordingLabel(/示例/i, "删除"), false);
});

test("local import controls are not mistaken for upload submission", () => {
  assert.equal(isFileUploadSubmitLabel("导入文件（.txt / .csv）"), false);
  assert.equal(isFileUploadSubmitLabel("开始上传"), true);
  assert.equal(isFileUploadSubmitLabel("提交文件"), true);
});

test("dynamic control counters do not create a new recording action", () => {
  assert.equal(recordingControlKey("+ 添加站点（4/80）"), "+ 添加站点");
  assert.equal(recordingControlKey("+ 添加站点（5/80）"), "+ 添加站点");
  assert.equal(recordingControlKey("Add station (7/80)"), "add station");
  assert.equal(recordingControlKey("运行方案 2"), "运行方案 2");
});

test("redrawn identical controls retain stable slot identities", () => {
  assert.equal(recordingControlIdentity("↑", "", 2), recordingControlIdentity("↑", "", 2));
  assert.notEqual(recordingControlIdentity("↑", "", 2), recordingControlIdentity("↑", "", 3));
  assert.equal(recordingControlIdentity("结果详情", "result-tab", 1),
    recordingControlIdentity("结果详情", "result-tab", 4));
});

test("repeated form actions share a bounded demonstration family", () => {
  assert.equal(repeatedRecordingControlFamily("提交第 1 轮（覆盖全部 3 件）"),
    repeatedRecordingControlFamily("提交第 8 轮（覆盖全部 3 件）"));
  assert.equal(repeatedRecordingControlFamily("第1行第1列，待定 待定"),
    repeatedRecordingControlFamily("第8行第7列，待定 待定"));
  assert.equal(repeatedRecordingControlFamily("＋ 启停区间"), "＋ 启停区间");
  assert.equal(repeatedRecordingControlFamily("+ 校准珠"), "+ 校准珠");
  assert.equal(repeatedRecordingControlFamily("+ 颗粒候选"), "+ 颗粒候选");
  assert.equal(repeatedRecordingControlFamily("结果详情"), "");
});

test("reordering arrows do not trigger a full input rescan", () => {
  assert.equal(shouldPrepareInputsAfterControl("↑"), false);
  assert.equal(shouldPrepareInputsAfterControl("↓"), false);
  assert.equal(shouldPrepareInputsAfterControl("结果详情"), true);
});

test("a withdrawn final result cannot be accepted as a completed recording", () => {
  assert.equal(isWithdrawnResultText("裁决已撤下。提交修改后的输入以获得新裁决。"), true);
  assert.equal(isWithdrawnResultText("输入已修改 — 旧裁决已撤下，请重新提交"), true);
  assert.equal(isWithdrawnResultText("Input changed; submit again. Result has been invalidated."), true);
  assert.equal(isWithdrawnResultText("重建裁决已完成，轨迹与结果详情可见"), false);
});
