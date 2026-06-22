document.addEventListener("DOMContentLoaded", async () => {
  const health = await ReviewUI.checkHealth();
  const version = document.getElementById("service-version");
  const description = document.getElementById("service-description");
  if (health) {
    version.textContent = `v${health.version}`;
    description.textContent = `${health.app} 后端进程运行正常。`;
  } else {
    version.textContent = "服务离线";
    description.textContent = "请先启动 uvicorn 后端服务。";
  }
});
