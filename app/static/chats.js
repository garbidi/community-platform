// «Посты» как телеграм: ответ на сообщение, выбранные картинки, обновление
// переписки без перезагрузки (только чтение чата), прокрутка к последним.
(function () {
  const box = document.getElementById("tgMsgs");
  if (!box) return;
  const feed = document.getElementById("tgFeed");
  const toBottom = () => { box.scrollTop = box.scrollHeight; };
  // держимся внизу, пока человек сам не прокрутил вверх: картинки
  // догружаются позже и иначе сдвигают переписку к старым сообщениям
  let stick = true;
  toBottom();
  box.addEventListener("load", e => {
    if (e.target.tagName === "IMG" && stick) toBottom();
  }, true);

  // ответ на конкретное сообщение
  const replyTo = document.getElementById("tgReplyTo");
  const replying = document.getElementById("tgReplying");
  document.addEventListener("click", e => {
    const b = e.target.closest(".tgmsg__replybtn");
    if (b) {
      replyTo.value = b.dataset.reply;
      document.getElementById("tgReplyWho").textContent = b.dataset.who;
      replying.hidden = false;
      document.getElementById("tgText").focus();
    }
  });
  const cancel = document.getElementById("tgReplyCancel");
  if (cancel) cancel.addEventListener("click", () => { replyTo.value = ""; replying.hidden = true; });

  // выбранные картинки — превью до сохранения
  const input = document.getElementById("tgFileInput");
  const files = document.getElementById("tgFiles");
  if (input) input.addEventListener("change", () => {
    files.innerHTML = "";
    [...input.files].slice(0, 10).forEach(f => {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f); img.alt = f.name;
      files.appendChild(img);
    });
  });

  // «✨ Сгенерировать»: текст в поле — пожелание, ответ Claude встаёт на его место
  const gen = document.getElementById("tgGen");
  if (gen) gen.addEventListener("click", async () => {
    const text = document.getElementById("tgText");
    const body = new FormData();
    body.append("flow", gen.dataset.flow);
    body.append("kind", document.getElementById("tgGenKind").value);
    body.append("hint", text.value);
    const label = gen.textContent;
    gen.disabled = true; gen.textContent = "Claude пишет… до минуты";
    try {
      const r = await fetch("/posts/generate", { method: "POST", body, credentials: "same-origin" });
      const j = await r.json().catch(() => ({ error: "Сервер ответил не так — попробуйте ещё раз." }));
      if (j.text) { text.value = j.text; text.rows = Math.min(16, j.text.split("\n").length + 1); text.focus(); }
      else alert(j.error || "Не получилось.");
    } catch (e) { alert("Сеть моргнула — попробуйте ещё раз."); }
    finally { gen.disabled = false; gen.textContent = label; }
  });

  // обновление переписки: сначала из базы, раз в минуту — дочитать из телеграма
  async function load(refresh) {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    try {
      const r = await fetch(`/posts/messages?chat=${box.dataset.chat}&topic=${box.dataset.topic}&refresh=${refresh ? 1 : 0}`,
                            { credentials: "same-origin" });
      if (!r.ok) return;
      const tmp = document.createElement("div");
      tmp.innerHTML = await r.text();
      // свежая порция заменяет только хвост: подгруженную историю оставляем
      const firstNew = tmp.querySelector(".tgmsg[data-id]");
      if (firstNew) {
        const id = +firstNew.dataset.id;
        const old = [...feed.querySelectorAll(".tgmsg[data-id]")].filter(m => +m.dataset.id < id);
        if (old.length) {
          const keep = document.createElement("div");
          let n = feed.firstChild;
          while (n && !(n.dataset && n.dataset.id && +n.dataset.id >= id)) { const x = n.nextSibling; keep.appendChild(n); n = x; }
          feed.innerHTML = keep.innerHTML + tmp.innerHTML;
        } else feed.innerHTML = tmp.innerHTML;
      } else if (!feed.querySelector(".tgmsg")) feed.innerHTML = tmp.innerHTML;
      sameDays();
      if (atBottom) toBottom();
    } catch (e) { /* сеть моргнула — попробуем в следующий раз */ }
  }
  // прокрутили к верху — подгружаем более ранние сообщения
  let busy = false, done = false;
  const more = document.createElement("div");
  more.className = "tgmore"; more.hidden = true; more.textContent = "Загружаем более ранние сообщения…";
  box.prepend(more);
  function sameDays() {      // на стыке порций дата могла повториться
    let prev = null;
    feed.querySelectorAll(".tgday").forEach(d => {
      if (prev && d.textContent === prev.textContent) d.remove(); else prev = d;
    });
  }
  async function older() {
    const first = feed.querySelector(".tgmsg[data-id]");
    if (busy || done || !first) return;
    busy = true; more.hidden = false;
    try {
      const r = await fetch(`/posts/older?chat=${box.dataset.chat}&topic=${box.dataset.topic}&before=${first.dataset.id}`,
                            { credentials: "same-origin" });
      const html = r.ok ? (await r.text()).trim() : "";
      if (!html) { done = true; more.textContent = "Это начало переписки"; return; }
      const h = box.scrollHeight;
      feed.insertAdjacentHTML("afterbegin", html);
      sameDays();
      box.scrollTop += box.scrollHeight - h;     // остаёмся на том же месте
    } catch (e) { /* сеть моргнула — попробуем при следующей прокрутке */ }
    finally { busy = false; if (!done) more.hidden = true; }
  }
  box.addEventListener("scroll", () => {
    stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (box.scrollTop < 150) older();
  });

  const btn = document.getElementById("tgRefresh");
  if (btn) btn.addEventListener("click", async () => { btn.disabled = true; await load(true); btn.disabled = false; });
  load(true);
  setInterval(() => { if (!document.hidden) load(true); }, 60000);
})();
