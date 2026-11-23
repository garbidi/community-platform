// Календарь потока на «Обзоре»: стрелки листают месяцы, клик по дню
// подгружает справа его план (/day?flow=…&d=…). Обзор потока меняется
// крючком целиком (flowswitch.js), поэтому события ловим на документе.
(function () {
  let pending = null;

  function step(month, dir) {
    const next = dir > 0 ? month.nextElementSibling : month.previousElementSibling;
    if (!next || !next.classList.contains("cal__month")) return;
    month.hidden = true;
    next.hidden = false;
    const btn = next.querySelector(`.cal__nav[data-step="${dir}"]:not([disabled])`)
             || next.querySelector(".cal__nav:not([disabled])");
    if (btn) btn.focus();
  }

  async function pick(day) {
    const planner = day.closest(".planner");
    const panel = planner.querySelector(".dayplan");
    planner.querySelectorAll(".cal__day.is-picked").forEach(d => d.classList.remove("is-picked"));
    day.classList.add("is-picked");
    const url = "/day?flow=" + encodeURIComponent(planner.dataset.flow) + "&d=" + day.dataset.date;
    const mine = pending = url;
    panel.classList.add("is-loading");
    try {
      const r = await fetch(url, { credentials: "same-origin" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const html = await r.text();
      if (mine !== pending) return;            // уже кликнули другой день
      panel.innerHTML = html;
      panel.scrollTop = 0;
      // на узком экране панель под календарём — подвести к ней
      if (window.innerWidth < 900) panel.scrollIntoView({ block: "start", behavior: "smooth" });
    } catch (e) {
      if (mine === pending) panel.innerHTML = '<p class="dayplan__none">Не получилось загрузить день — обновите страницу.</p>';
    } finally {
      if (mine === pending) panel.classList.remove("is-loading");
    }
  }

  document.addEventListener("click", e => {
    const nav = e.target.closest(".cal__nav");
    if (nav) { step(nav.closest(".cal__month"), +nav.dataset.step); return; }
    const day = e.target.closest("button.cal__day");
    if (day) pick(day);
  });

  // стрелками по дням внутри месяца
  document.addEventListener("keydown", e => {
    const day = e.target.closest && e.target.closest("button.cal__day");
    if (!day) return;
    const shift = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (!shift) return;
    const cells = [...day.closest(".cal__grid").querySelectorAll(".cal__day:not(.cal__wd)")];
    let i = cells.indexOf(day) + shift;
    while (cells[i] && cells[i].tagName !== "BUTTON") i += Math.sign(shift);
    if (cells[i]) { e.preventDefault(); cells[i].focus(); }
  });
})();
