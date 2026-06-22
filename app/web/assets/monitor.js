document.addEventListener("DOMContentLoaded", () => {
  const stages = [
    ["saving_files", "保存文件"],
    ["parsing_bid_documents", "文档解析"],
    ["building_bid_index", "构建索引"],
    ["auditing", "智能审核"],
    ["writing_outputs", "结果输出"],
  ];
  const terminal = new Set(["completed", "completed_with_report_error", "failed"]);
  let timer = null;
  let currentPayload = null;

  const input = document.getElementById("task-id-input");
  const queryTask = new URLSearchParams(window.location.search).get("task_id");
  input.value = queryTask || ReviewUI.getIdentity().taskId || "";

  function renderStages(current, status) {
    const index = stages.findIndex(([key]) => key === current);
    const completed = status === "completed" || status === "completed_with_report_error";
    document.getElementById("stage-list").innerHTML = stages.map(([key, label], itemIndex) => {
      const state = completed || itemIndex < index ? "done" : itemIndex === index ? "current" : "";
      return `<div class="stage ${state}"><strong>${label}</strong><span>${key}</span></div>`;
    }).join("");
  }

  function statusText(status) {
    const labels = {
      running: "运行中",
      completed: "已完成",
      completed_with_report_error: "报告异常",
      failed: "失败",
    };
    return labels[status] || status || "—";
  }

  function renderResults(payload) {
    const results = payload.result?.file_results || [];
    const body = document.getElementById("result-body");
    if (!results.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-cell">任务尚未生成审核结果</td></tr>';
      return;
    }
    body.innerHTML = results.map((item, index) => `
      <tr>
        <td>${escapeHtml(item.file.file_name)}</td>
        <td>${item.is_applicable ? "适用" : "不适用"}</td>
        <td><span class="conclusion ${item.overall_conclusion}">${escapeHtml(item.overall_conclusion)}</span></td>
        <td>${item.findings.length}</td>
        <td>${item.evidence_results.length}</td>
        <td><button class="detail-button" data-index="${index}">查看</button></td>
      </tr>
    `).join("");
    body.querySelectorAll(".detail-button").forEach((button) => {
      button.addEventListener("click", () => showDetail(results[Number(button.dataset.index)]));
    });
  }

  function renderDownloads(payload) {
    const area = document.getElementById("download-actions");
    const outputs = payload.outputs;
    if (!outputs) {
      area.innerHTML = "";
      return;
    }
    const links = [];
    if (outputs.json) {
      links.push(`<a class="download-link" href="/api/audit-tasks/${encodeURIComponent(payload.task_id)}/outputs/json">下载 JSON</a>`);
    }
    (outputs.word || []).forEach((_, index) => {
      links.push(`<a class="download-link" href="/api/audit-tasks/${encodeURIComponent(payload.task_id)}/outputs/word/${index}">Word ${index + 1}</a>`);
    });
    area.innerHTML = links.join("");
  }

  function showDetail(item) {
    document.getElementById("dialog-title").textContent = item.file.file_name;
    const findings = item.findings.length
      ? item.findings.map((finding) => `
          <article class="finding-detail">
            <h3>${escapeHtml(finding.title)}</h3>
            <p><strong>结论：</strong>${escapeHtml(finding.result)} · ${escapeHtml(finding.severity)}</p>
            <p>${escapeHtml(finding.description)}</p>
            <p><strong>规则来源：</strong>${escapeHtml(finding.rule_source.source_path)} / ${escapeHtml(finding.rule_source.section || "文件级")}</p>
            ${finding.suggestion ? `<p><strong>建议：</strong>${escapeHtml(finding.suggestion)}</p>` : ""}
          </article>
        `).join("")
      : "<p>该文件没有普通规则 Finding。</p>";
    document.getElementById("dialog-body").innerHTML = `
      <p><strong>项目匹配：</strong>${escapeHtml(item.project_match.status)} · ${escapeHtml(item.project_match.reason)}</p>
      <p><strong>总体结论：</strong>${escapeHtml(item.overall_conclusion)}</p>
      ${findings}
    `;
    document.getElementById("detail-dialog").showModal();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function render(payload) {
    currentPayload = payload;
    document.getElementById("observer-empty").classList.add("hidden");
    document.getElementById("observer-content").classList.remove("hidden");
    document.getElementById("overview-status").textContent = statusText(payload.status);
    document.getElementById("overview-stage").textContent = ReviewUI.formatStage(payload.progress?.stage);
    document.getElementById("detail-task").textContent = payload.task_id;
    document.getElementById("detail-project").textContent = payload.project_id;
    document.getElementById("detail-bidder").textContent = payload.bidder_id;

    const results = payload.result?.file_results || [];
    const findings = results.flatMap((item) => item.findings || []);
    document.getElementById("overview-files").textContent = results.length || "—";
    document.getElementById("overview-risks").textContent =
      findings.filter((item) => ["risk", "non_compliant"].includes(item.result)).length || "—";
    document.getElementById("raw-json").textContent = JSON.stringify(payload, null, 2);

    const errorBox = document.getElementById("error-box");
    if (payload.error) {
      errorBox.classList.remove("hidden");
      errorBox.textContent = `${payload.error.code || "error"}：${payload.error.message || "未知错误"}`;
    } else {
      errorBox.classList.add("hidden");
    }
    renderStages(payload.progress?.stage, payload.status);
    renderResults(payload);
    renderDownloads(payload);

    const poll = document.getElementById("poll-indicator");
    if (terminal.has(payload.status)) {
      poll.textContent = "已停止轮询";
      if (timer) window.clearInterval(timer);
      timer = null;
    } else {
      poll.textContent = "每 2 秒自动轮询";
    }
  }

  async function loadTask(showError = true) {
    const taskId = input.value.trim();
    if (!taskId) {
      if (showError) ReviewUI.toast("请输入 Task ID", "error");
      return;
    }
    try {
      const payload = await ReviewUI.request(`/api/audit-tasks/${encodeURIComponent(taskId)}`);
      ReviewUI.setIdentity({ taskId });
      render(payload);
      if (!terminal.has(payload.status) && !timer) {
        timer = window.setInterval(() => loadTask(false), 2000);
      }
    } catch (error) {
      if (timer) window.clearInterval(timer);
      timer = null;
      if (showError) ReviewUI.toast(error.message, "error");
    }
  }

  document.getElementById("load-task").addEventListener("click", () => loadTask());
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadTask();
  });
  document.getElementById("dialog-close").addEventListener("click", () => {
    document.getElementById("detail-dialog").close();
  });
  document.getElementById("copy-json").addEventListener("click", async () => {
    await navigator.clipboard.writeText(JSON.stringify(currentPayload || {}, null, 2));
    ReviewUI.toast("JSON 已复制");
  });

  if (input.value) loadTask(false);
});
