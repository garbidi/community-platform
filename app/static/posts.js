// «Посты»: подтверждение отправки и удаления, счётчик знаков,
// автообновление, пока Claude переписывает пост.
(function () {
  document.addEventListener("click", e => {
    const b = e.target.closest("button[data-confirm]");
    if (b && !confirm(b.dataset.confirm)) e.preventDefault();
  });
  document.querySelectorAll(".post__form").forEach(form => {
    const ta = form.querySelector("textarea");
    const out = form.querySelector(".post__count");
    const upd = () => {
      const n = ta.value.length;
      out.textContent = `${n} / 4000`;
      out.classList.toggle("is-over", n > 3800);
    };
    ta.addEventListener("input", upd);
    upd();
  });
  const list = document.querySelector(".postlist");
  if (list && list.dataset.writing === "1") setTimeout(() => location.reload(), 4000);
})();
