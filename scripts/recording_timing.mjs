export function automaticFinishDelayMs(_outputPath, elapsedMs = 0, maximumSeconds = 88) {
  // The workflow itself determines the recording length. Once every safe
  // feature/result control has been exercised, keep the final result visible
  // for two seconds without padding short recordings to a fixed total length.
  const maximumMs = Math.max(0, Number(maximumSeconds) || 0) * 1000;
  const availableMs = Math.max(0, maximumMs - Math.max(0, Number(elapsedMs) || 0) - 6000);
  return Math.min(2000, availableMs);
}

export function humanClickPauseMs(sequence, phase = "before") {
  const index = Math.max(0, Number(sequence) || 0);
  if (phase === "after") return 820 + ((index * 211 + 97) % 520);
  return 560 + ((index * 173 + 61) % 420);
}

export const DESTRUCTIVE_CONTROL_PATTERN = String.raw`删|移除|清空|清除|丢弃|销毁|重置|取消|关闭|退出|注销|下线|停止|终止|撤销|驳回|remove|delete|\bdel\b|trash|discard|erase|destroy|clear|reset|cancel|close|logout|stop|terminate|revoke|reject|^\s*[×✕✖✗❌]\s*$`;

export const GENERIC_CONTROL_SELECTOR =
  "button:visible:not([disabled]), [role=button]:visible:not([aria-disabled=true]), "
  + "[role=tab]:visible:not([aria-disabled=true]), input[type=submit]:visible:not([disabled]), "
  + "input[type=checkbox]:visible:not([disabled]), input[type=radio]:visible:not([disabled]), "
  + "summary:visible";

export function isDestructiveFeatureControl(label) {
  return new RegExp(DESTRUCTIVE_CONTROL_PATTERN, "i").test(String(label || ""));
}

export function isSafeFeatureControl(label) {
  const text = String(label || "").replace(/\s+/g, " ").trim();
  if (!text) return false;
  return !isDestructiveFeatureControl(text);
}

export function recordingControlKey(label) {
  return String(label || "")
    .replace(/\s+/g, " ")
    // Counters in labels such as “添加站点（4/80）” change after every
    // click. They describe state, not a new control, so exclude them from
    // the traversal identity.
    .replace(/[（(]\s*\d+\s*(?:[\/／]\s*\d+)?\s*[)）]/g, "")
    .replace(/\b\d+\s*[\/／]\s*\d+\b/g, "")
    .trim()
    .toLowerCase();
}

export function recordingControlIdentity(label, explicitIdentity, occurrence) {
  // React may replace an arrow button after every reorder. Its DOM identity
  // changes, while the same on-screen control slot remains.
  return recordingControlKey(`${label}|${explicitIdentity || `occurrence:${occurrence}`}`);
}

export function repeatedRecordingControlFamily(label) {
  const key = recordingControlKey(label);
  if (/第\s*\d+\s*行第\s*\d+\s*列/.test(key)) {
    return key.replace(/\d+/g, "#");
  }
  if (/提交第\s*\d+\s*轮/.test(key)) {
    return key.replace(/\d+/g, "#");
  }
  if (/添加|增加|启停区间|\badd\b|append|^[+＋]\s*\S/i.test(key)) {
    return key.replace(/\d+/g, "#");
  }
  return "";
}

export function shouldPrepareInputsAfterControl(label) {
  // Reordering arrows change the current result, not the form's input set.
  // Re-scanning every input after each arrow can consume the recording limit.
  return !/^[↑↓←→⇧⇩]$/.test(String(label || "").trim());
}

export function recordingTraversalComplete(passes, failures) {
  return failures.length === 0 && passes.every((pass) => pass.complete);
}

export function finalizeInteractionEvidence(interactionMode, demonstration, metrics = {}, requestCount = 0) {
  if (demonstration?.required) return demonstration;
  const clicks = Number(metrics?.clicks || 0);
  const requests = Number(requestCount || 0);
  if (interactionMode === "manual") {
    return {
      required: false,
      ok: true,
      interactionMode: "manual",
      clicks,
      requests,
      review: "human",
    };
  }
  return {
    required: true,
    ok: clicks > 0 && requests > 0,
    clicks,
    requests,
    error: "没有检测到真实功能点击和成功接口请求",
  };
}

export function isGenericSampleLabel(label) {
  return /载入|示例|样例|模板|预置|demo|sample|example/i.test(String(label || ""));
}

export function isNewlyRevealedControl(label, availableBefore) {
  return !isGenericSampleLabel(label)
    && !availableBefore.has(recordingControlKey(label));
}

export function isLikelyValidSampleLabel(label) {
  const text = String(label || "");
  return isGenericSampleLabel(text)
    && !/无解|非法|错误|失败|不可行|invalid|infeasible|error|failure/i.test(text);
}

export function isFileUploadSubmitLabel(label) {
  return /开始交付|开始上传|上传|提交|发布|保存|确认/i.test(String(label || ""));
}

export function isGenericActionLabel(label) {
  return /计算|运行|分析|审计|裁决|核验|校验|验证|比较|评估|检查|生成|提交|开始|启动|执行|求解|solve|compute|run|inspect|check|verify|compare|analy/i.test(String(label || ""));
}

export function isPrimarySubmitLabel(label) {
  const text = String(label || "").trim().replace(/^[^\p{L}\p{N}]+/gu, "");
  return /^(提交|开始|启动|运行|执行|计算|求解|分析|审计|裁决|综合|submit|run|solve)/i.test(text);
}

export function isResultNavigationLabel(label) {
  return /下一|上一步|前一步|后一步|首步|末步|第一步|最后一步|第\s*\d+\s*阶|#\s*\d+|候选阶梯|播放|暂停|详情|明细|结果|见证|轨迹|时间线|切换|previous|next|first|last|play|pause|details?|results?|witness|trace|timeline|tab/i.test(String(label || ""));
}

export function shouldTraverseChoiceControl(type, label, choiceCount) {
  if (type !== "checkbox" && type !== "radio") return true;
  // A large group of choices is form data, not a set of result controls.
  // Leave the prepared valid draft intact instead of toggling every option.
  return choiceCount <= 12 || isGenericActionLabel(label) || isResultNavigationLabel(label);
}

export function browserWorkflowEvidence(result, requestCount = 0) {
  return { required: true, workflow: "browser-ui", requests: requestCount,
    uploaded: [], failedControls: [], resultRestored: false, ...result };
}

export function isWithdrawnResultText(body) {
  const text = String(body || "").replace(/\s+/g, " ");
  return /裁决已撤下|旧裁决已撤下|结果已撤下|结果已清除|输入已修改[^。]{0,80}重新提交|(?:result|decision|verdict)\s+(?:has been\s+)?(?:cleared|withdrawn|invalidated)|input\s+(?:has\s+)?changed[^.]{0,80}(?:submit|run)\s+again/i.test(text);
}

export function matchesRecordingLabel(matcher, label) {
  if (typeof matcher === "function") return Boolean(matcher(label));
  if (matcher instanceof RegExp) return matcher.test(String(label || ""));
  return false;
}
