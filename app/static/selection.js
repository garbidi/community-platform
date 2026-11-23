// Отбор в основной ММ: доска «не решено → думает → идёт дальше / не идёт».
// Данные — в #pick-data; решение уходит POST /selection/<поток>/<человек>, на экране — сразу.
(function () {
  "use strict";
  var root = document.getElementById("pick");
  if (!root) return;
  var flow = root.dataset.flow;
  var people = JSON.parse(document.getElementById("pick-data").textContent);
  var byId = {};
  people.forEach(function (p) { byId[p.id] = p; });
  var LANES = ["open", "maybe", "yes", "no"];
  var TITLES = {};
  document.querySelectorAll(".lane").forEach(function (s) {
    TITLES[s.dataset.lane] = s.querySelector("h2").textContent;
  });
  var q = document.getElementById("pick-q"), city = document.getElementById("pick-city"),
      mentor = document.getElementById("pick-mentor"), shown = document.getElementById("pick-shown");
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function initials(name) {
    return name.split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w[0]; }).join("").toUpperCase();
  }
  function ava(p, cls) {
    return p.photo
      ? '<img class="' + cls + '" src="/avatar/' + p.card + '" alt="" loading="lazy" width="44" height="44">'
      : '<span class="' + cls + ' ' + cls + '--none" aria-hidden="true">' + esc(initials(p.name)) + "</span>";
  }
  // в таблице Лилии в колонке «Ментор» где имя («Камила»), где номер группы («10»)
  function mentorLabel(m) { return /^\d+$/.test(m) ? "группа " + esc(m) : "ментор " + esc(m); }

  function matches(p) {
    var t = (q.value || "").trim().toLowerCase();
    if (city && city.value && p.city !== city.value) return false;
    if (mentor && mentor.value && p.mentor !== mentor.value) return false;
    if (!t) return true;
    return [p.name, p.city, p.niche, p.inviter, p.mentor, p.username].join(" ").toLowerCase().indexOf(t) >= 0;
  }

  function card(p) {
    var meta = [];
    if (p.inviter) meta.push("от " + esc(p.inviter));
    if (p.mentor) meta.push(mentorLabel(p.mentor));
    var flags = "";
    if (p.review) flags += '<span class="chip">разбор</span>';
    if (/^да/i.test(p.knows)) flags += '<span class="chip">знает про ММ</span>';
    return '<li class="pcard" draggable="true" tabindex="0" data-id="' + p.id + '" aria-label="' + esc(p.name) + '">' +
      ava(p, "pcard__ava") +
      '<div class="pcard__body"><b class="pcard__name">' + esc(p.name) + "</b>" +
      '<span class="pcard__sub">' + esc([p.city, p.niche].filter(Boolean).join(" · ") || "—") + "</span>" +
      (meta.length ? '<span class="pcard__meta">' + meta.join(" · ") + "</span>" : "") +
      (flags ? '<span class="pcard__flags">' + flags + "</span>" : "") + "</div></li>";
  }

  function order(lane, a, b) {
    if (lane !== "open" && a.at !== b.at) return a.at < b.at ? 1 : -1;     // свежие решения сверху
    return a.name.localeCompare(b.name, "ru");
  }

  function render() {
    var n = 0;
    LANES.forEach(function (lane) {
      var list = root.querySelector('[data-list="' + lane + '"]');
      var items = people.filter(function (p) { return p.lane === lane && matches(p); })
        .sort(function (a, b) { return order(lane, a, b); });
      n += items.length;
      list.innerHTML = items.map(card).join("");
      list.parentNode.classList.toggle("lane--empty", !items.length);
    });
    var filtered = (q.value || (city && city.value) || (mentor && mentor.value));
    shown.textContent = filtered ? "показано " + n + " из " + people.length : "";
    counts();
  }

  function counts() {
    var c = { open: 0, maybe: 0, yes: 0, no: 0 };
    people.forEach(function (p) { c[p.lane]++; });
    LANES.forEach(function (lane) {
      document.querySelectorAll('[data-count="' + lane + '"]').forEach(function (el) { el.textContent = c[lane]; });
      var seg = document.querySelector('[data-seg="' + lane + '"]');
      if (seg) seg.style.flexGrow = c[lane];
    });
  }

  var status = document.createElement("p");
  status.className = "pick-status";
  status.setAttribute("role", "status");
  root.parentNode.insertBefore(status, root);
  function say(text, bad) {
    status.textContent = text;
    status.classList.toggle("pick-status--bad", !!bad);
  }

  function move(id, lane) {
    var p = byId[id];
    if (!p || p.lane === lane) return;
    var was = p.lane, wasAt = p.at;
    p.lane = lane;
    p.at = new Date().toISOString();
    render();
    flash(id);
    if (current && current.id === id) fillSeg(p);
    var body = new FormData();
    body.append("lane", lane);
    fetch("/flows/" + flow + "/selection/" + id, { method: "POST", body: body, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.error || "ошибка"); return j; }); })
      .then(function () { say(p.name + " → " + TITLES[lane]); })
      .catch(function (e) {
        p.lane = was; p.at = wasAt; render();
        if (current && current.id === id) fillSeg(p);
        say("Не сохранилось: " + e.message + ". Попробуйте ещё раз.", true);
      });
  }

  function flash(id) {
    if (reduce) return;
    var el = root.querySelector('.pcard[data-id="' + id + '"]');
    if (el) { el.classList.add("pcard--landed"); setTimeout(function () { el.classList.remove("pcard--landed"); }, 600); }
  }

  // ---- перетаскивание
  var dragId = null;
  root.addEventListener("dragstart", function (e) {
    var el = e.target.closest(".pcard");
    if (!el) return;
    dragId = +el.dataset.id;
    el.classList.add("pcard--drag");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(dragId));
  });
  root.addEventListener("dragend", function (e) {
    var el = e.target.closest(".pcard");
    if (el) el.classList.remove("pcard--drag");
    root.querySelectorAll(".lane--over").forEach(function (s) { s.classList.remove("lane--over"); });
    dragId = null;
  });
  root.querySelectorAll(".lane").forEach(function (sec) {
    sec.addEventListener("dragover", function (e) {
      if (dragId == null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      sec.classList.add("lane--over");
    });
    sec.addEventListener("dragleave", function (e) {
      if (!sec.contains(e.relatedTarget)) sec.classList.remove("lane--over");
    });
    sec.addEventListener("drop", function (e) {
      e.preventDefault();
      sec.classList.remove("lane--over");
      if (dragId != null) move(dragId, sec.dataset.lane);
    });
  });

  // ---- карточка: открыть, клавиши 1–4
  root.addEventListener("click", function (e) {
    var el = e.target.closest(".pcard");
    if (el) open(+el.dataset.id);
  });
  root.addEventListener("keydown", function (e) {
    var el = e.target.closest(".pcard");
    if (!el) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(+el.dataset.id); }
    var k = "1234".indexOf(e.key);
    if (k >= 0) {
      e.preventDefault();
      var id = +el.dataset.id;
      move(id, LANES[k]);
      var again = root.querySelector('.pcard[data-id="' + id + '"]');
      if (again) again.focus();
    }
  });

  // ---- панель справа
  var sheet = document.getElementById("sheet"), scrim = document.getElementById("sheet-scrim");
  var current = null, opener = null;
  var FACTS = [
    ["inviter", "Кто вовлёк"], ["mentor", "Ментор / группа 4 дней"], ["revenue", "Доход"], ["profit", "Чистая прибыль"],
    ["knows", "Знает про основной ММ"], ["motives", "Истинные мотивы"], ["point_a", "Точка А и боли"],
    ["point_b", "Точка Б"], ["expect", "Ожидания от ММ"], ["mentor_note", "Комментарий ментора"],
    ["comment", "Комментарий"], ["risk", "Риск"], ["review", "Разбор"]
  ];
  function fillSeg(p) {
    sheet.querySelectorAll('input[name="lane"]').forEach(function (r) { r.checked = r.value === p.lane; });
    var by = document.getElementById("sheet-by");
    by.textContent = p.lane !== "open" && p.by ? "Решение: " + p.by + (p.at ? ", " + p.at.slice(8, 10) + "." + p.at.slice(5, 7) : "") : "";
  }
  function open(id) {
    var p = byId[id];
    if (!p) return;
    current = p;
    opener = document.activeElement;
    var img = document.getElementById("sheet-ava");
    if (p.photo) { img.src = "/avatar/" + p.card; img.hidden = false; } else { img.hidden = true; img.removeAttribute("src"); }
    document.getElementById("sheet-name").textContent = p.name;
    document.getElementById("sheet-sub").textContent = [p.city, p.niche].filter(Boolean).join(" · ");
    var c = [];
    if (p.username) c.push('<a href="https://t.me/' + esc(p.username.replace(/^@/, "")) + '" target="_blank" rel="noopener">@' + esc(p.username.replace(/^@/, "")) + "</a>");
    if (p.phone) {
      var tel = (p.phone.length === 10 ? "7" : "") + p.phone;
      c.push('<a href="tel:+' + esc(tel) + '">+' + esc(tel) + "</a>");
    }
    document.getElementById("sheet-contact").innerHTML = c.join(" · ");
    var html = "";
    FACTS.forEach(function (f) {
      if (p[f[0]]) html += "<dt>" + f[1] + "</dt><dd>" + esc(p[f[0]]).replace(/\n/g, "<br>") + "</dd>";
    });
    if (p.zoom && /^https?:/.test(p.zoom)) html += '<dt>Запись зума</dt><dd><a href="' + esc(p.zoom) + '" target="_blank" rel="noopener">открыть</a></dd>';
    document.getElementById("sheet-facts").innerHTML = html || "<dd class=\"tag\">В таблице пока ничего не заполнено.</dd>";
    document.getElementById("sheet-open").href = "/participants/" + p.card;
    fillSeg(p);
    sheet.classList.add("sheet--open");
    sheet.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    sheet.focus();
  }
  function close() {
    sheet.classList.remove("sheet--open");
    sheet.setAttribute("aria-hidden", "true");
    scrim.hidden = true;
    current = null;
    if (opener && document.contains(opener)) opener.focus();
  }
  sheet.addEventListener("change", function (e) {
    if (e.target.name === "lane" && current) move(current.id, e.target.value);
  });
  sheet.querySelector("[data-close]").addEventListener("click", close);
  scrim.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && current) close();
  });

  [q, city, mentor].forEach(function (el) { if (el) el.addEventListener("input", render); });
  render();
})();
