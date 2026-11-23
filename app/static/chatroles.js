// Роли чатов на странице потока: поиск по названию, «только выбранные»,
// подсветка строк с ролью и счётчик.
(function () {
  const form = document.querySelector(".chatroles");
  if (!form) return;
  const find = form.querySelector(".chatroles__find");
  const only = form.querySelector(".chatroles__picked");
  const channels = form.querySelector(".chatroles__channels");
  const count = form.querySelector(".chatroles__count");
  const rows = [...form.querySelectorAll("tbody tr")];

  function update() {
    const q = find.value.trim().toLowerCase();
    let picked = 0, shown = 0;
    for (const r of rows) {
      const on = !!r.querySelector("select").value;
      r.classList.toggle("is-on", on);
      if (on) picked++;
      // каналы прячем, пока их не попросили; выбранный канал виден всегда
      const show = (!q || r.dataset.title.includes(q)) && (!only.checked || on)
                && (on || channels.checked || r.dataset.channel !== "1");
      r.hidden = !show;
      if (show) shown++;
    }
    // нашлась тема — показываем и её группу, чтобы было понятно, чья она
    for (const r of rows) {
      if (r.hidden || !r.dataset.parent) continue;
      const g = form.querySelector(`tr[data-group="${r.dataset.parent}"]`);
      if (g && g.hidden) { g.hidden = false; shown++; }
    }
    count.textContent = `выбрано ${picked}` + (shown < rows.length ? ` · показано ${shown} из ${rows.length}` : "");
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
})();
