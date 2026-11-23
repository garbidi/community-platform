// Поиск по расшифровке встречи: прячет реплики без совпадения, считает найденное.
(function () {
  const find = document.querySelector(".tr-find");
  if (!find) return;
  const rows = [...document.querySelectorAll(".transcript li")];
  const count = document.querySelector(".tr-count");
  find.addEventListener("input", () => {
    const q = find.value.trim().toLowerCase();
    let n = 0;
    for (const r of rows) {
      const hit = !q || r.textContent.toLowerCase().includes(q);
      r.hidden = !hit;
      if (hit) n++;
    }
    count.textContent = q ? `найдено реплик: ${n}` : "";
  });
})();

// разбор «кто есть кто» идёт в фоне — обновить страницу, когда закончится
if (document.querySelector("[data-running]")) setTimeout(() => location.reload(), 5000);
