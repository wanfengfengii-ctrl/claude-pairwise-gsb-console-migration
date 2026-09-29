(() => {
  "use strict";
  const PAGE_SOURCE = "pairwise-gsb-console";
  const HELPER_SOURCE = "solo-qa-gsb-helper";
  const VERSION = chrome.runtime.getManifest().version;
  const ALLOWED = new Set(["PAIRWISE_GSB_SUBMIT", "PAIRWISE_GSB_REPAIR", "PAIRWISE_GSB_SYNC"]);

  function post(type, requestId, payload = {}) {
    window.postMessage({ source: HELPER_SOURCE, type, requestId: requestId || "", payload }, window.location.origin);
  }
  function announce() { post("PAIRWISE_GSB_BRIDGE_READY", "", { version: VERSION }); }

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "PAIRWISE_GSB_PROGRESS" && typeof message.requestId === "string") {
      post("PAIRWISE_GSB_BRIDGE_PROGRESS", message.requestId, message.progress || {});
    }
  });

  window.addEventListener("message", async (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const message = event.data;
    if (!message || message.source !== PAGE_SOURCE) return;
    if (message.type === "PAIRWISE_GSB_BRIDGE_PING") return announce();
    if (!ALLOWED.has(message.type) || typeof message.requestId !== "string") return;
    try {
      const response = await chrome.runtime.sendMessage({ type: message.type, requestId: message.requestId, payload: message.payload || {} });
      if (!response?.ok) throw new Error(response?.error || "提交助手没有返回结果");
      post("PAIRWISE_GSB_BRIDGE_RESULT", message.requestId, response.data || {});
    } catch (error) {
      post("PAIRWISE_GSB_BRIDGE_ERROR", message.requestId, {
        error: error instanceof Error ? error.message : String(error),
      });
    }
  });
  announce();
})();
