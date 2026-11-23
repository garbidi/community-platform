// «Доступы»: копирование готового сообщения с доступом.
(function () {
  document.addEventListener("click", async e => {
    const b = e.target.closest(".copybtn");
    if (!b) return;
    const text = b.previousElementSibling.textContent;
    try {
      await navigator.clipboard.writeText(text);
      b.textContent = "Скопировано ✓";
    } catch (err) {
      const r = document.createRange();
      r.selectNodeContents(b.previousElementSibling);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      b.textContent = "Выделено — Ctrl+C";
    }
    setTimeout(() => { b.textContent = "Скопировать"; }, 2500);
  });
})();
