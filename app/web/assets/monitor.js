document.addEventListener("DOMContentLoaded", () => {
  const stages = [
    ["saving_files", "保存文件"],
    ["parsing_bid_documents", "文档解析"],
    ["summarizing_bid_documents", "摘要路由"],
    ["building_bid_index", "构建索引"],
    ["auditing", "智能审核"],
    ["writing_outputs", "结果输出"],
  ];
  const terminal = new Set(["completed", "completed_with_report_error", "failed"]);
  let eventSource = null;
  let currentPayload = null;

  const input = document.getElementById("task-id-input");
  const queryTask = new URLSearchParams(window.location.search).get("task_id");
  input.value = queryTask || ReviewUI.getIdentity().taskId || "";

  function renderStages(current, status) {
    const index = stages.findIndex(([key]) => key === current);
    const completed = status === "completed" || status === "completed_with_report_error";
    document.getElementById("stage-list").innerHTML = stages.map(([key, label], itemIndex) => {
      const state = completed || itemIndex < index ? "done" : itemIndex === index ? "current" : "";
      return `
        <div class="stage ${state}">
          <span class="stage-number">${itemIndex + 1}</span>
          <strong>${label}</strong>
        </div>
      `;
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

  function enumText(value) {
    const labels = {
      compliant: "合规",
      non_compliant: "不合规",
      risk: "存在风险",
      manual_review: "人工复核",
      not_applicable: "不适用",
      matched: "匹配",
      not_matched: "不匹配",
      uncertain: "不确定",
      fatal: "致命",
      major: "重大",
      minor: "一般",
      info: "提示",
      unknown: "未知",
      found: "已找到响应",
      not_found: "未找到响应",
      unreadable: "无法读取",
      supported: "资料支持",
      partially_supported: "部分支持",
      unsupported: "不支持",
      contradicted: "存在矛盾",
      ocr_required: "需要 OCR",
      unverifiable: "无法核验",
      weakly_related: "弱相关",
      record_project_match: "记录项目匹配",
      record_finding: "记录审核发现",
      record_file_result: "记录文件结果",
      project_match: "项目适用性判断",
      rule_audit: "规则审核",
      format: "形式与完整性",
      qualification: "资格与合规",
      commercial: "商务响应",
      technical: "技术响应",
      performance_capacity: "业绩与履约能力",
      consistency_risk: "一致性与风险",
      record_batch: "记录批量审核结果",
    };
    return labels[value] || value || "—";
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
        <td>${item.is_applicable ? "适用" : item.project_match.status === "uncertain" ? "待人工确认" : "不适用"}</td>
        <td><span class="conclusion ${item.overall_conclusion}">${escapeHtml(enumText(item.overall_conclusion))}</span></td>
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

  function actorLabel(actor) {
    return {
      pipeline: "流程",
      main_agent: "主 Agent",
      evidence_agent: "资料核验 Agent",
      tool: "工具",
    }[actor] || actor || "系统";
  }

  function toolText(name) {
    return {
      search_bid_response: "检索投标响应",
      retrieve_rule_source: "读取招标规则原文",
      request_evidence_review: "委派资料核验",
      search_evidence: "检索支撑资料",
      retrieve_evidence_source: "读取支撑资料原文",
    }[name] || name || "未知工具";
  }

  function renderEventDetail(detail) {
    if (!detail || !Object.keys(detail).length) return "";
    const preferred = [
      ["file_name", "文件"],
      ["rule_name", "规则"],
      ["statement", "待核验声明"],
      ["action", "动作"],
      ["result", "审核结论"],
      ["status", "核验状态"],
      ["reason", "原因"],
      ["summary", "摘要"],
      ["result_count", "命中数量"],
      ["file_result_count", "文件结果数"],
    ];
    const enumFields = new Set(["action", "result", "status", "work_kind"]);
    const rows = preferred
      .filter(([key]) => detail[key] !== undefined && detail[key] !== null)
      .map(([key, label]) => `
        <div><dt>${label}</dt><dd>${escapeHtml(
          enumFields.has(key) ? enumText(detail[key]) : detail[key]
        )}</dd></div>
      `)
      .join("");
    const tools = Array.isArray(detail.tools)
      ? `<div><dt>计划工具</dt><dd>${detail.tools
          .map((item) => escapeHtml(toolText(item.name)))
          .join("、")}</dd></div>`
      : "";
    const topResults = Array.isArray(detail.top_results) && detail.top_results.length
      ? `
        <details class="trace-results">
          <summary>查看前 ${detail.top_results.length} 条检索命中</summary>
          <pre>${escapeHtml(JSON.stringify(detail.top_results, null, 2))}</pre>
        </details>
      `
      : "";
    return `<dl class="trace-detail">${rows}${tools}</dl>${topResults}`;
  }

  function renderTrace(payload) {
    const events = payload.events || [];
    document.getElementById("trace-count").textContent = `${events.length} 条事件`;
    const container = document.getElementById("agent-trace");
    if (!events.length) {
      container.innerHTML =
        '<div class="trace-empty">该任务没有轨迹数据；旧任务需要重新执行审核</div>';
      return;
    }
    container.innerHTML = [...events].reverse().map((event) => {
      const time = event.created_at
        ? new Date(event.created_at).toLocaleTimeString("zh-CN", { hour12: false })
        : "—";
      return `
        <article class="trace-event actor-${escapeHtml(event.actor)}">
          <div class="trace-marker"></div>
          <div class="trace-event-body">
            <div class="trace-event-head">
              <span class="trace-actor">${escapeHtml(actorLabel(event.actor))}</span>
              <time>${escapeHtml(time)}</time>
            </div>
            <h3>${escapeHtml(event.title)}</h3>
            ${renderEventDetail(event.detail)}
          </div>
        </article>
      `;
    }).join("");
  }

  function showDetail(item) {
    document.getElementById("dialog-title").textContent = item.file.file_name;
    const findings = item.findings.length
      ? item.findings.map((finding) => `
          <article class="finding-detail">
            <h3>${escapeHtml(finding.title)}</h3>
            <p><strong>结论：</strong>${escapeHtml(enumText(finding.result))} · ${escapeHtml(enumText(finding.severity))}</p>
            <p>${escapeHtml(finding.description)}</p>
            <p><strong>规则来源：</strong>${escapeHtml(finding.rule_source.source_path)} / ${escapeHtml(finding.rule_source.section || "文件级")}</p>
            ${finding.suggestion ? `<p><strong>建议：</strong>${escapeHtml(finding.suggestion)}</p>` : ""}
          </article>
        `).join("")
      : "<p>该文件没有普通规则 Finding。</p>";
    document.getElementById("dialog-body").innerHTML = `
      <p><strong>项目匹配：</strong>${escapeHtml(enumText(item.project_match.status))} · ${escapeHtml(item.project_match.reason)}</p>
      <p><strong>总体结论：</strong>${escapeHtml(enumText(item.overall_conclusion))}</p>
      ${item.document_summary?.summary ? `<p><strong>文件摘要：</strong>${escapeHtml(item.document_summary.summary)}</p>` : ""}
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
    renderTrace(payload);
    renderResults(payload);
    renderDownloads(payload);

    const indicator = document.getElementById("stream-indicator");
    indicator.textContent = terminal.has(payload.status) ? "任务已结束" : "实时接收事件";
  }

  function closeStream() {
    if (eventSource) eventSource.close();
    eventSource = null;
  }

  function connectStream(taskId) {
    closeStream();
    const indicator = document.getElementById("stream-indicator");
    indicator.textContent = "正在连接";
    eventSource = new EventSource(
      `/api/audit-tasks/${encodeURIComponent(taskId)}/stream`
    );
    eventSource.addEventListener("task-update", (event) => {
      const payload = JSON.parse(event.data);
      render(payload);
      indicator.textContent = terminal.has(payload.status)
        ? "任务已结束"
        : "实时接收事件";
    });
    eventSource.addEventListener("stream-end", () => {
      indicator.textContent = "任务已结束";
      closeStream();
    });
    eventSource.onerror = () => {
      if (!eventSource) return;
      indicator.textContent = "连接中断，正在重连";
    };
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
      if (!terminal.has(payload.status)) {
        connectStream(taskId);
      } else {
        closeStream();
      }
    } catch (error) {
      closeStream();
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
  window.addEventListener("beforeunload", closeStream);

  if (input.value) loadTask(false);
});
