const ReviewUI = (() => {
  const storageKeys = {
    project: "dw.project_id",
    bidder: "dw.bidder_id",
    task: "dw.task_id",
  };

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const error = payload?.error || {};
      const detail = error.details ? `\n${JSON.stringify(error.details)}` : "";
      throw new Error(`${error.message || `请求失败 (${response.status})`}${detail}`);
    }
    return payload;
  }

  async function checkHealth() {
    const pill = document.getElementById("health-pill");
    const text = document.getElementById("health-text");
    if (!pill || !text) return null;
    try {
      const data = await request("/health");
      pill.classList.remove("offline");
      pill.classList.add("online");
      text.textContent = "服务在线";
      return data;
    } catch {
      pill.classList.remove("online");
      pill.classList.add("offline");
      text.textContent = "服务离线";
      return null;
    }
  }

  function toast(message, type = "success") {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className = "toast-container";
      document.body.appendChild(container);
    }
    const item = document.createElement("div");
    item.className = `toast ${type === "error" ? "error" : ""}`;
    item.textContent = message;
    container.appendChild(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function getIdentity() {
    return {
      projectId: localStorage.getItem(storageKeys.project) || "",
      bidderId: localStorage.getItem(storageKeys.bidder) || "",
      taskId: localStorage.getItem(storageKeys.task) || "",
    };
  }

  function setIdentity(values) {
    if (values.projectId) localStorage.setItem(storageKeys.project, values.projectId);
    if (values.bidderId) localStorage.setItem(storageKeys.bidder, values.bidderId);
    if (values.taskId) localStorage.setItem(storageKeys.task, values.taskId);
  }

  function clearIdentity() {
    Object.values(storageKeys).forEach((key) => localStorage.removeItem(key));
  }

  function buildFolderForm(files) {
    const form = new FormData();
    [...files].forEach((file) => {
      form.append("files", file, file.name);
      form.append("relative_paths", file.webkitRelativePath || file.name);
    });
    return form;
  }

  function formatStage(stage) {
    const names = {
      saving_files: "保存文件",
      parsing_bid_documents: "解析投标文件",
      summarizing_bid_documents: "摘要与相关性判断",
      building_bid_index: "构建投标索引",
      auditing: "智能审核",
      writing_outputs: "生成报告",
    };
    return names[stage] || stage || "等待执行";
  }

  document.addEventListener("DOMContentLoaded", checkHealth);

  return {
    request,
    checkHealth,
    toast,
    getIdentity,
    setIdentity,
    clearIdentity,
    buildFolderForm,
    formatStage,
  };
})();
