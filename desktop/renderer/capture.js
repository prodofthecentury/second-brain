const titleEl = document.getElementById("qcTitle");
const bodyEl = document.getElementById("qcBody");

async function save() {
  const body = bodyEl.value.trim();
  if (!body) return;
  const title = titleEl.value.trim() || body.split(/\r?\n/)[0].slice(0, 60) || "Quick Note";
  const res = await window.secondBrain.request("POST", "/v1/notes", {
    title,
    body,
    tags: ["quick-capture"],
  });
  if (res.ok) {
    titleEl.value = "";
    bodyEl.value = "";
    await window.secondBrain.notifyMainRefresh();
    await window.secondBrain.closeCapture();
  }
}

document.getElementById("qcSave").addEventListener("click", save);
document.getElementById("qcCancel").addEventListener("click", () => window.secondBrain.closeCapture());
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") window.secondBrain.closeCapture();
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") save();
});
