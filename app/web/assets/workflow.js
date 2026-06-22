document.addEventListener("DOMContentLoaded", () => {
  const identity = ReviewUI.getIdentity();
  const elements = {
    projectId: document.getElementById("current-project"),
    bidderId: document.getElementById("current-bidder"),
    taskId: document.getElementById("current-task"),
    log: document.getElementById("activity-log"),
  };

  function renderIdentity() {
    const current = ReviewUI.getIdentity();
    elements.projectId.textContent = current.projectId || "未创建";
    elements.bidderId.textContent = current.bidderId || "未创建";
    elements.taskId.textContent = current.taskId || "未创建";
    document.getElementById("bidder-step").classList.toggle("locked", !current.projectId);
    document.getElementById("task-step").classList.toggle("locked", !current.bidderId);
    if (current.projectId) setStatus("project", "已就绪", "success");
    if (current.bidderId) setStatus("bidder", "已就绪", "success");
    if (current.taskId) setStatus("task", "已创建", "success");
  }

  function setStatus(prefix, text, style) {
    const badge = document.getElementById(`${prefix}-status`);
    badge.textContent = text;
    badge.className = `status-badge ${style}`;
  }

  function addLog(title, detail, type = "normal") {
    const empty = elements.log.querySelector(".log-empty");
    if (empty) empty.remove();
    const item = document.createElement("div");
    item.className = `log-item ${type === "error" ? "error" : ""}`;
    const strong = document.createElement("strong");
    strong.textContent = title;
    const span = document.createElement("span");
    span.textContent = `${new Date().toLocaleTimeString()} · ${detail}`;
    item.append(strong, span);
    elements.log.prepend(item);
  }

  function bindFileSummary(inputId, summaryId) {
    const input = document.getElementById(inputId);
    input.addEventListener("change", () => {
      const files = [...input.files];
      const bytes = files.reduce((sum, file) => sum + file.size, 0);
      const size = bytes > 1024 * 1024
        ? `${(bytes / 1024 / 1024).toFixed(1)} MiB`
        : `${(bytes / 1024).toFixed(1)} KiB`;
      document.getElementById(summaryId).textContent = files.length
        ? `已选择 ${files.length} 个文件，共 ${size}`
        : "尚未选择文件";
    });
  }

  async function runUpload({ prefix, inputId, buttonId, url, success }) {
    const input = document.getElementById(inputId);
    const button = document.getElementById(buttonId);
    if (!input.files.length) {
      ReviewUI.toast("请先选择文件夹", "error");
      return;
    }
    const started = performance.now();
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "处理中，请勿关闭页面…";
    setStatus(prefix, "处理中", "running");
    addLog(`${original}开始`, `${input.files.length} 个文件`);
    try {
      const target = typeof url === "function" ? url(ReviewUI.getIdentity()) : url;
      const data = await ReviewUI.request(target, {
        method: "POST",
        body: ReviewUI.buildFolderForm(input.files),
      });
      success(data);
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      setStatus(prefix, data.status || "成功", "success");
      addLog(`${original}完成`, `耗时 ${seconds}s`);
      ReviewUI.toast("操作完成");
      renderIdentity();
    } catch (error) {
      setStatus(prefix, "失败", "failed");
      addLog(`${original}失败`, error.message, "error");
      ReviewUI.toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  bindFileSummary("project-files", "project-summary");
  bindFileSummary("bidder-files", "bidder-summary");
  bindFileSummary("task-files", "task-summary");

  document.getElementById("create-project").addEventListener("click", () => {
    runUpload({
      prefix: "project",
      inputId: "project-files",
      buttonId: "create-project",
      url: "/api/projects",
      success: (data) => {
        ReviewUI.clearIdentity();
        ReviewUI.setIdentity({ projectId: data.project_id });
        addLog("Project ID", data.project_id);
      },
    });
  });

  document.getElementById("create-bidder").addEventListener("click", () => {
    runUpload({
      prefix: "bidder",
      inputId: "bidder-files",
      buttonId: "create-bidder",
      url: ({ projectId }) => `/api/projects/${encodeURIComponent(projectId)}/bidders`,
      success: (data) => {
        ReviewUI.setIdentity({ bidderId: data.bidder_id });
        addLog("Bidder ID", data.bidder_id);
      },
    });
  });

  document.getElementById("create-task").addEventListener("click", () => {
    runUpload({
      prefix: "task",
      inputId: "task-files",
      buttonId: "create-task",
      url: ({ projectId, bidderId }) =>
        `/api/projects/${encodeURIComponent(projectId)}/bidders/${encodeURIComponent(bidderId)}/audit-tasks`,
      success: (data) => {
        ReviewUI.setIdentity({ taskId: data.task_id });
        addLog("Task ID", data.task_id);
        window.setTimeout(() => {
          window.location.href = `/web/monitor.html?task_id=${encodeURIComponent(data.task_id)}`;
        }, 800);
      },
    });
  });

  document.getElementById("reset-workflow").addEventListener("click", () => {
    if (!window.confirm("只清除浏览器保存的 ID，不删除后端数据。确定继续？")) return;
    ReviewUI.clearIdentity();
    renderIdentity();
    addLog("本地记录已清空", "后端 SQLite 和文件未删除");
  });
  document.getElementById("clear-log").addEventListener("click", () => {
    elements.log.innerHTML = '<div class="log-empty">操作日志已清空。</div>';
  });

  renderIdentity();
  if (identity.projectId || identity.bidderId || identity.taskId) {
    addLog("已恢复浏览器记录", "可以从上次完成的位置继续操作");
  }
});
