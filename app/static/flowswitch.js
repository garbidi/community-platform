// «Обзор» с несколькими потоками: крючок на леске, им цепляют точку-поток.
// Отпустили крючок у точки — слева набегает волна, смывает обзор, а пока
// вода расходится, проступает обзор выбранного потока. Обзор подгружается
// фрагментом (?partial=1), адрес меняется (?flow=…), «Назад» работает.
// Анимации выключаются в «Настройках» — тогда смена мгновенная.
(function () {
  const hook = document.querySelector(".flowhook");
  const pane = document.getElementById("flowpane");
  if (!hook || !pane) return;
  const flows = JSON.parse(hook.dataset.flows || "[]");
  const stops = [...hook.querySelectorAll(".flowhook__stop")];
  const grip = hook.querySelector(".flowhook__hook");
  const n = flows.length;
  let index = +hook.dataset.index || 0;
  // без ?flow= в адресе — поток по умолчанию; с ним страница и открылась,
  // если пришли без параметра (иначе «Назад» уводил бы на первую точку)
  const home = new URLSearchParams(location.search).has("flow")
    ? (JSON.parse(hook.dataset.default || "null") ?? index) : index;
  let busy = false;

  const pct = i => (n > 1 ? (i / (n - 1)) * 100 : 0);
  const motion = () => window.vershinaMotion ? window.vershinaMotion() : true;

  function mark(i) {
    stops.forEach((s, k) => s.classList.toggle("is-on", k === i));
    hook.setAttribute("aria-valuenow", i);
    hook.setAttribute("aria-valuetext", flows[i].name);
  }

  function place(p, smooth) {
    grip.style.transition = smooth
      ? "left 420ms cubic-bezier(0.16, 1, 0.3, 1), transform 520ms cubic-bezier(0.34, 1.56, 0.64, 1), color 160ms"
      : "color 160ms";
    grip.style.left = p + "%";
  }

  // ---------------------------------------------------------- перетаскивание
  let drag = null;
  function ratioAt(x) {
    const r = hook.getBoundingClientRect();
    return Math.max(0, Math.min(1, (x - r.left) / r.width));
  }
  function nearest(ratio) { return Math.round(ratio * (n - 1)); }

  grip.addEventListener("pointerdown", e => {
    if (busy) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, v: 0, t: performance.now() };
    grip.classList.add("is-drag");
  });
  grip.addEventListener("pointermove", e => {
    if (!drag) return;
    const now = performance.now();
    drag.v = (e.clientX - drag.x) / Math.max(1, now - drag.t);
    drag.x = e.clientX; drag.t = now;
    const ratio = ratioAt(e.clientX);
    place(ratio * 100, false);
    // крючок раскачивается на леске против движения
    grip.style.setProperty("--swing", Math.max(-28, Math.min(28, -drag.v * 18)) + "deg");
    mark(nearest(ratio));
  });
  function release(e) {
    if (!drag) return;
    drag = null;
    grip.classList.remove("is-drag");
    grip.style.setProperty("--swing", "0deg");
    const i = nearest(ratioAt(e.clientX));
    place(pct(i), true);
    choose(i, true);
  }
  grip.addEventListener("pointerup", release);
  grip.addEventListener("pointercancel", release);

  stops.forEach((s, i) => s.addEventListener("click", () => {
    if (busy) return;
    place(pct(i), true);
    mark(i);
    choose(i, true);
  }));

  hook.addEventListener("keydown", e => {
    if (busy) return;
    let i = index;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") i = Math.min(n - 1, index + 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") i = Math.max(0, index - 1);
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = n - 1;
    else return;
    e.preventDefault();
    place(pct(i), true); mark(i); choose(i, true);
  });

  window.addEventListener("popstate", () => {
    const name = new URLSearchParams(location.search).get("flow");
    const i = flows.findIndex(f => f.name === name);
    const target = i >= 0 ? i : home;
    place(pct(target), true); mark(target); choose(target, false);
  });

  // ---------------------------------------------------------- смена потока
  async function load(name) {
    const r = await fetch("/?partial=1&flow=" + encodeURIComponent(name),
                          { credentials: "same-origin", headers: { "X-Partial": "1" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.text();
  }

  function swap(html) {
    if (window.vershinaDeepStop) window.vershinaDeepStop();
    pane.innerHTML = html;
    pane.dataset.flow = flows[index].name;
    if (window.vershinaDeep) window.vershinaDeepStop = window.vershinaDeep(pane.querySelector(".deep__canvas"));
  }

  async function choose(i, push) {
    if (i === index && pane.dataset.flow === flows[i].name) return;
    index = i;
    const name = flows[i].name;
    if (push) {
      const url = new URL(location.href);
      url.searchParams.set("flow", name);
      history.pushState(null, "", url);
    }
    busy = true;
    const html = load(name);
    try {
      if (motion()) await wave(html);
      else swap(await html);
    } catch (e) {
      location.href = "/?flow=" + encodeURIComponent(name);
    } finally {
      busy = false;
    }
  }

  // ---------------------------------------------------------- волна
  function wave(htmlPromise) {
    return new Promise((resolve, reject) => {
      const box = pane.getBoundingClientRect();
      const left = Math.max(0, box.left - 24);
      const W = window.innerWidth - left, H = window.innerHeight;
      const cv = document.createElement("canvas");
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      cv.className = "wave";
      cv.style.left = left + "px";
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + "px"; cv.style.height = H + "px";
      document.body.appendChild(cv);
      const ctx = cv.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      let html = null, failed = null;
      htmlPromise.then(h => { html = h; }, e => { failed = e; });

      const RUN = 820, CALM = 1300;
      const spray = [], bubbles = [];
      let phase = "run", t0 = performance.now(), swapped = false, tCalm = 0;
      const ease = x => x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;

      // гребень: x гребня на высоте y
      function crestAt(y, cx, t) {
        return cx + Math.sin(y * 0.011 + t * 0.004) * 26 + Math.sin(y * 0.031 - t * 0.006) * 9
             + Math.max(0, 1 - y / (H * 0.35)) * 40;   // верх волны забегает вперёд — «заворачивается»
      }

      function water(alpha, cx, t, foam) {
        // тело воды: от левого края до гребня
        ctx.beginPath();
        ctx.moveTo(-10, -10);
        for (let y = -10; y <= H + 10; y += 12) ctx.lineTo(crestAt(y, cx, t), y);
        ctx.lineTo(-10, H + 10);
        ctx.closePath();
        const g = ctx.createLinearGradient(0, 0, 0, H);
        g.addColorStop(0, `rgba(48,66,150,${alpha})`);
        g.addColorStop(0.55, `rgba(24,32,96,${alpha})`);
        g.addColorStop(1, `rgba(10,14,48,${alpha})`);
        ctx.fillStyle = g;
        ctx.fill();
        // блики в толще
        ctx.save();
        ctx.clip();
        ctx.globalCompositeOperation = "lighter";
        for (let k = 0; k < 7; k++) {
          const yy = (k / 7) * H + Math.sin(t * 0.002 + k) * 20;
          ctx.strokeStyle = `rgba(170,195,255,${0.06 * alpha})`;
          ctx.lineWidth = 18;
          ctx.beginPath();
          for (let x = -10; x <= W; x += 20) ctx.lineTo(x, yy + Math.sin(x * 0.01 + t * 0.003 + k) * 14);
          ctx.stroke();
        }
        ctx.restore();
        if (!foam) return;
        // пена по гребню
        ctx.beginPath();
        for (let y = -10; y <= H + 10; y += 6) {
          const x = crestAt(y, cx, t);
          y === -10 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.strokeStyle = `rgba(245,240,225,${0.85 * foam})`;
        ctx.lineWidth = 5;
        ctx.stroke();
        ctx.strokeStyle = `rgba(245,240,225,${0.35 * foam})`;
        ctx.lineWidth = 14;
        ctx.stroke();
      }

      function frame(now) {
        const t = now - t0;
        ctx.clearRect(0, 0, W, H);

        if (phase === "run") {
          const p = Math.min(1, t / RUN);
          const cx = -W * 0.3 + ease(p) * W * 1.45;
          // то, что уже под водой, смыто: показываем только правее гребня
          pane.style.clipPath = `inset(0 0 0 ${Math.max(0, cx - (box.left - left) - 40)}px)`;
          water(0.96, cx, now, 1);
          // брызги впереди гребня
          for (let k = 0; k < 4; k++) {
            const y = Math.random() * H;
            spray.push({ x: crestAt(y, cx, now), y, vx: 2 + Math.random() * 5, vy: -1 - Math.random() * 3,
                         r: 1 + Math.random() * 2.5, life: 1 });
          }
          if (p >= 1) {
            if (failed) { cleanup(); reject(failed); return; }
            if (html !== null) { phase = "calm"; tCalm = now; }
            // ещё грузится — вода стоит и колышется, пока не придёт ответ
          }
        } else {
          if (!swapped) {
            swap(html); swapped = true;
            pane.style.clipPath = "";
            pane.classList.add("is-surfacing");
            for (let k = 0; k < 90; k++) bubbles.push({ x: Math.random() * W, y: H * (0.3 + Math.random() * 0.8),
              r: 1 + Math.random() * 4, v: 0.6 + Math.random() * 1.8, ph: Math.random() * 6 });
          }
          const q = Math.min(1, (now - tCalm) / CALM);
          const a = 0.96 * Math.pow(1 - q, 1.6);
          water(a, W * 2, now, 0);
          // вода расходится кругами
          ctx.save();
          ctx.globalCompositeOperation = "lighter";
          for (let k = 0; k < 4; k++) {
            const rr = (q * 1.2 + k * 0.22) * Math.max(W, H) * 0.6;
            ctx.strokeStyle = `rgba(200,215,255,${0.18 * (1 - q)})`;
            ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(W * 0.35, H * 0.4, rr, 0, 7); ctx.stroke();
          }
          ctx.restore();
          for (const b of bubbles) {
            b.y -= b.v; b.x += Math.sin((b.y + b.ph * 40) * 0.04) * 0.6;
            ctx.strokeStyle = `rgba(230,236,255,${0.55 * (1 - q)})`;
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, 7); ctx.stroke();
          }
          if (q >= 1) { cleanup(); resolve(); return; }
        }

        for (const s of spray) { s.x += s.vx; s.y += s.vy; s.vy += 0.12; s.life -= 0.03; }
        for (let k = spray.length - 1; k >= 0; k--) if (spray[k].life <= 0) spray.splice(k, 1);
        for (const s of spray) {
          ctx.fillStyle = `rgba(245,240,225,${0.8 * s.life})`;
          ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
        }
        requestAnimationFrame(frame);
      }

      function cleanup() {
        cv.remove();
        pane.style.clipPath = "";
        setTimeout(() => pane.classList.remove("is-surfacing"), 50);
      }
      requestAnimationFrame(frame);
    });
  }
})();
