// Вкладки карточки участника. Без скрипта видны все разделы подряд —
// скрипт только прячет лишние. Открытая вкладка берётся из ?tab=, из #якоря
// (в том числе старых: #celi-potok, #razbory — ищем, в какой вкладке он лежит)
// или первая; выбор пишется в адрес, чтобы «Назад» и обновление не сбрасывали её.
(function () {
  const nav = document.querySelector("[data-tabs]");
  if (!nav) return;
  const tabs = [...nav.querySelectorAll("[data-tab]")];
  const panels = [...document.querySelectorAll("[data-panel]")];
  const ids = new Set(tabs.map(t => t.dataset.tab));

  function pick() {
    const q = new URLSearchParams(location.search).get("tab");
    if (q && ids.has(q)) return q;
    const h = location.hash.slice(1);
    if (h) {
      const el = document.getElementById(h);
      const panel = el && el.closest("[data-panel]");
      if (panel && ids.has(panel.dataset.panel)) return panel.dataset.panel;
    }
    return tabs[0].dataset.tab;
  }

  function show(name, push) {
    for (const t of tabs) {
      const on = t.dataset.tab === name;
      t.classList.toggle("is-on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const p of panels) p.hidden = p.dataset.panel !== name;
    if (push) {
      const url = new URL(location.href);
      url.searchParams.set("tab", name);
      url.hash = "";
      history.replaceState(null, "", url);
    }
  }

  nav.addEventListener("click", e => {
    const t = e.target.closest("[data-tab]");
    if (!t) return;
    e.preventDefault();
    show(t.dataset.tab, true);
  });
  // панели без своей вкладки (например, разборы для роли без доступа) не показываем
  for (const p of panels) if (!ids.has(p.dataset.panel)) p.remove();
  show(pick(), false);
  window.addEventListener("hashchange", () => show(pick(), false));
})();
