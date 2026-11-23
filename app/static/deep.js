// Шапка обзора: толща воды, в которой каждый участник потока — светящаяся
// частица. В «Вершине» погружение — это рост: чем больше выполнена декларация,
// тем глубже частица. У поверхности — холодный дневной свет, который гаснет
// к середине толщи; на дне — тёплое свечение, россыпь жемчуга и золота
// и сундук с горкой монет и бриллиантами. Частицы теплеют с глубиной, закрывшие декларацию
// лежат на дне с золотым ореолом. Без библиотек; при выключенных анимациях
// рисуется один неподвижный кадр.
(function () {
// Запуск на конкретном холсте; возвращает функцию остановки — при смене
// потока в «Обзоре» старая толща гасится, новая запускается (flowswitch.js).
function init(canvas) {
  if (!canvas) return () => {};
  const ctx = canvas.getContext("2d");
  let alive = true;
  let dots = [];
  try { dots = JSON.parse(canvas.dataset.dots || "[]"); } catch (e) { dots = []; }
  const still = !window.vershinaMotion();

  // Оттенки команд — из бренда: крем, коралл, голубой, янтарь, сирень
  const TEAM = [[255, 236, 205], [255, 150, 110], [150, 190, 255], [255, 205, 120], [200, 170, 255]];
  const GOLD = [255, 200, 110];
  let W = 0, H = 0, dpr = 1;
  let parts = [], bubbles = [], motes = [], gems = [], chest = null;

  function rnd(a, b) { return a + Math.random() * (b - a); }
  function mix(a, b, k) { return a.map((v, i) => Math.round(v + (b[i] - v) * k)); }
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

  // Линия дна: пологие дюны
  function floorY(x) {
    // на широком экране дно ниже: слева над ним подпись
    const base = W < 640 ? 0.86 : 0.9;
    return H * base + Math.sin(x * 0.006 + 1.3) * H * 0.03 + Math.sin(x * 0.017) * H * 0.01;
  }

  function layout() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Частицы — справа, слева текст; на узком экране текст сверху, частицы ниже
    const left = W < 640 ? W * 0.06 : W * 0.42;
    parts = dots.map((d) => {
      const p = Math.max(0, Math.min(1, d.p));
      const x = rnd(left, W * 0.96);
      const top = W < 640 ? H * 0.5 : H * 0.14;
      const bottom = floorY(x) - 7;
      // глубина — выполнение: 0% у поверхности, 100% — на дне
      const y = top + p * (bottom - top);
      return {
        x, y0: y, y, p, team: d.t % TEAM.length,
        r: rnd(2.2, 3.4) + p * 0.8, ph: rnd(0, Math.PI * 2), sp: rnd(0.25, 0.6),
        drift: p >= 0.999 ? 0 : rnd(-0.07, 0.07), full: p >= 0.999,
        col: mix(TEAM[d.t % TEAM.length], GOLD, Math.pow(p, 1.4) * 0.75),
        spark: rnd(0, 1000), who: d, minx: left,
      };
    });
    motes = Array.from({ length: Math.round(W * H / 9000) }, () => ({
      x: rnd(0, W), y: rnd(0, H * 0.85), r: rnd(0.4, 1.1), a: rnd(0.06, 0.2), sp: rnd(0.03, 0.12),
    }));
    // Сокровища на дне: жемчуг и золотые искры, мерцают вразнобой
    // сокровища — правее подписи, чтобы не мельтешили под текстом
    const gx = W < 640 ? W * 0.02 : W * 0.4;
    gems = Array.from({ length: Math.max(14, Math.round((W - gx) / 40)) }, () => {
      const x = rnd(gx, W * 0.98);
      const pearl = Math.random() < 0.35;
      return { x, y: floorY(x) + rnd(2, H * 0.06), r: pearl ? rnd(1.6, 2.6) : rnd(0.9, 1.6),
               pearl, ph: rnd(0, Math.PI * 2), sp: rnd(0.6, 1.6) };
    });
    // Ларец — справа от текста, на склоне дюны
    const cx = W < 640 ? W * 0.78 : W * 0.7;
    const cs = Math.max(15, Math.min(32, W / 48));
    chest = { x: cx, y: floorY(cx) + 2, s: cs, ...treasure(cs) };
    bubbles = [];
  }

  // Сундук с горкой монет и бриллиантами: координаты — относительно сундука,
  // считаются один раз, чтобы при перерисовке ничего не прыгало
  function treasure(s) {
    const heap = [];
    for (let i = 0; i < 38; i++) {
      const u = rnd(-1, 1);
      const top = Math.sqrt(Math.max(0, 1 - u * u));           // горка — полуэллипс
      heap.push({ dx: u * s * 1.15, dy: -rnd(0.05, 0.95) * top * s * 0.7, tilt: rnd(-0.5, 0.5),
                  tone: rnd(0, 1) });
    }
    heap.sort((a, b) => a.dy - b.dy);                           // дальние — раньше
    const spill = Array.from({ length: 5 }, () => ({ dx: rnd(-1.0, 1.0) * s, dy: rnd(0.05, 0.45) * s,
                                                     tilt: rnd(-0.9, 0.9), tone: rnd(0, 1) }));
    const piles = [{ dx: s * 1.9, n: 5 }, { dx: -s * 1.8, n: 3 }, { dx: s * 2.5, n: 2 }];
    const loose = Array.from({ length: 9 }, () => ({ dx: rnd(-3.2, 3.4) * s, dy: rnd(-0.05, 0.25) * s,
                                                     tilt: rnd(-0.4, 0.4), tone: rnd(0, 1) }));
    const GEM = [[235, 250, 255], [150, 220, 255], [255, 170, 210], [190, 255, 220]];
    const gemsIn = [
      { dx: 0.05 * s, dy: -0.72 * s, d: 0.55 * s, c: GEM[0] },
      { dx: -0.6 * s, dy: -0.38 * s, d: 0.38 * s, c: GEM[1] },
      { dx: 0.62 * s, dy: -0.42 * s, d: 0.36 * s, c: GEM[2] },
      { dx: -2.4 * s, dy: 0.12 * s, d: 0.34 * s, c: GEM[3] },
      { dx: 3.0 * s, dy: 0.1 * s, d: 0.3 * s, c: GEM[1] },
    ].map(g => ({ ...g, ph: rnd(0, 7), sp: rnd(0.7, 1.4) }));
    return { heap, spill, piles, loose, gemsList: gemsIn };
  }

  function coin(x, y, r, tilt, tone, a = 1) {
    // монета — плоский золотой эллипс с ребром и бликом
    const base = tone > 0.5 ? [255, 206, 92] : [240, 180, 70];
    ctx.save();
    ctx.translate(x, y); ctx.rotate(tilt);
    ctx.fillStyle = `rgba(150,96,30,${a})`;
    ctx.beginPath(); ctx.ellipse(0, r * 0.18, r, r * 0.42, 0, 0, 7); ctx.fill();
    ctx.fillStyle = `rgba(${base[0]},${base[1]},${base[2]},${a})`;
    ctx.beginPath(); ctx.ellipse(0, 0, r, r * 0.42, 0, 0, 7); ctx.fill();
    ctx.strokeStyle = `rgba(255,235,170,${0.7 * a})`; ctx.lineWidth = 0.7;
    ctx.beginPath(); ctx.ellipse(0, 0, r * 0.68, r * 0.28, 0, 0, 7); ctx.stroke();
    ctx.restore();
  }

  function diamond(x, y, d, c, glint) {
    // бриллиант: площадка, корона, павильон — грани светлее и темнее
    const w = d / 2, crown = d * 0.3, pav = d * 0.62, table = d * 0.26;
    const col = (k, a = 1) => `rgba(${Math.min(255, c[0] * k)},${Math.min(255, c[1] * k)},${Math.min(255, c[2] * k)},${a})`;
    ctx.save();
    ctx.translate(x, y);
    // павильон
    ctx.fillStyle = col(0.75);
    ctx.beginPath(); ctx.moveTo(-w, 0); ctx.lineTo(w, 0); ctx.lineTo(0, pav); ctx.closePath(); ctx.fill();
    ctx.fillStyle = col(1.0, 0.9);
    ctx.beginPath(); ctx.moveTo(-w * 0.35, 0); ctx.lineTo(w * 0.35, 0); ctx.lineTo(0, pav); ctx.closePath(); ctx.fill();
    // корона
    ctx.fillStyle = col(1.05);
    ctx.beginPath(); ctx.moveTo(-w, 0); ctx.lineTo(-table, -crown); ctx.lineTo(table, -crown); ctx.lineTo(w, 0); ctx.closePath(); ctx.fill();
    ctx.fillStyle = col(1.25, 0.95);
    ctx.beginPath(); ctx.moveTo(-table, -crown); ctx.lineTo(table, -crown); ctx.lineTo(w * 0.3, 0); ctx.lineTo(-w * 0.3, 0); ctx.closePath(); ctx.fill();
    // рёбра граней
    ctx.strokeStyle = "rgba(255,255,255,0.55)"; ctx.lineWidth = 0.6;
    ctx.beginPath();
    ctx.moveTo(-w, 0); ctx.lineTo(w, 0);
    ctx.moveTo(-table, -crown); ctx.lineTo(-w * 0.3, 0); ctx.lineTo(0, pav); ctx.lineTo(w * 0.3, 0); ctx.lineTo(table, -crown);
    ctx.stroke();
    ctx.restore();
    if (glint > 0) {
      ctx.save(); ctx.globalCompositeOperation = "lighter";
      const g = ctx.createRadialGradient(x, y - crown * 0.4, 0, x, y - crown * 0.4, d * 1.4);
      g.addColorStop(0, col(1.3, 0.5 * glint)); g.addColorStop(1, col(1, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y - crown * 0.4, d * 1.4, 0, 7); ctx.fill();
      star(x + w * 0.3, y - crown, d * 1.1 * glint, `rgba(255,255,255,${0.9 * glint})`);
      ctx.restore();
    }
  }

  function drawChest(t) {
    const { x, y, s, heap, spill, piles, loose, gemsList } = chest;
    const w = s * 1.35, h = s * 0.95;                 // короб шире, чем выше
    const top = y - h;
    const tw = g => still ? 0.6 : Math.max(0, Math.sin(t * 0.0013 * g.sp + g.ph) * 1.4 - 0.4);

    // крышка откинута назад и наклонена: видна её освещённая изнанка
    ctx.save();
    ctx.translate(x - w * 0.98, top - s * 0.05);
    ctx.rotate(-0.16);
    const lw = w * 2.0, lh = s * 0.95;
    const inner = ctx.createLinearGradient(0, -lh, 0, 0);
    inner.addColorStop(0, "rgb(70,44,34)");
    inner.addColorStop(1, "rgb(150,98,52)");          // снизу её подсвечивает золото
    ctx.fillStyle = inner;
    ctx.beginPath();
    ctx.moveTo(0, 0); ctx.lineTo(s * 0.12, -lh); ctx.lineTo(lw - s * 0.05, -lh * 1.08); ctx.lineTo(lw, 0);
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "rgba(255,205,120,0.85)"; ctx.lineWidth = 1.4; ctx.stroke();
    ctx.strokeStyle = "rgba(255,205,120,0.45)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(s * 0.08, -lh * 0.5); ctx.lineTo(lw - s * 0.02, -lh * 0.54); ctx.stroke();
    ctx.restore();

    // горка золота — над кромкой короба, переваливается через край
    const mound = ctx.createRadialGradient(x, top, 0, x, top, w);
    mound.addColorStop(0, "rgb(255,214,120)");
    mound.addColorStop(1, "rgb(196,130,46)");
    ctx.fillStyle = mound;
    ctx.beginPath(); ctx.ellipse(x, top, w * 0.92, s * 0.62, 0, Math.PI, 0); ctx.fill();
    for (const c of heap) coin(x + c.dx, top + c.dy, s * 0.17, c.tilt, c.tone);
    for (const g of gemsList.slice(0, 3)) diamond(x + g.dx, top + g.dy + s * 0.3, g.d, g.c, tw(g));

    // короб: дерево, планка, кованые полосы, замок
    const wood = ctx.createLinearGradient(0, top, 0, y);
    wood.addColorStop(0, "rgb(92,58,40)");
    wood.addColorStop(1, "rgb(52,32,26)");
    ctx.fillStyle = wood;
    ctx.beginPath(); ctx.roundRect(x - w, top, w * 2, h, s * 0.08); ctx.fill();
    ctx.strokeStyle = "rgba(30,18,14,0.8)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x - w, top + h * 0.5); ctx.lineTo(x + w, top + h * 0.5); ctx.stroke();
    ctx.fillStyle = "rgb(214,160,72)";
    ctx.fillRect(x - w, top, w * 2, s * 0.12);                        // окованная кромка
    for (const k of [-0.78, 0.78]) ctx.fillRect(x + k * w - s * 0.08, top, s * 0.16, h);
    ctx.fillStyle = "rgb(236,186,90)";
    ctx.beginPath(); ctx.roundRect(x - s * 0.2, top + h * 0.22, s * 0.4, s * 0.44, s * 0.06); ctx.fill();
    ctx.fillStyle = "rgb(40,24,18)";
    ctx.beginPath(); ctx.arc(x, top + h * 0.38, s * 0.06, 0, 7); ctx.fill();
    ctx.fillRect(x - s * 0.025, top + h * 0.38, s * 0.05, s * 0.12);
    ctx.strokeStyle = "rgba(255,215,140,0.55)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect(x - w, top, w * 2, h, s * 0.08); ctx.stroke();

    // монеты, скатившиеся через край, стопки и россыпь на песке
    for (const c of spill) coin(x + c.dx, top + c.dy, s * 0.16, c.tilt, c.tone);
    for (const p of piles) {
      const px = x + p.dx, py = floorY(x + p.dx) + s * 0.08;
      for (let i = 0; i < p.n; i++) coin(px + (i % 2 ? 1 : -1) * s * 0.03, py - i * s * 0.12, s * 0.2, 0, i % 2);
    }
    for (const c of loose) coin(x + c.dx, floorY(x + c.dx) + c.dy, s * 0.15, c.tilt, c.tone, 0.95);
    for (const g of gemsList.slice(3)) diamond(x + g.dx, floorY(x + g.dx) - g.d * 0.15 + g.dy, g.d, g.c, tw(g));
  }

  function water(t) {
    // Холодная толща сверху, глубокая середина, тёплый отсвет у дна
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "rgb(48,66,142)");
    g.addColorStop(0.42, "rgb(24,30,86)");
    g.addColorStop(0.72, "rgb(18,20,62)");
    g.addColorStop(1, "rgb(40,30,52)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    // Лучи сверху — гаснут к середине толщи
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < 5; i++) {
      const cx = W * (0.15 + i * 0.2) + Math.sin(t * 0.00012 + i * 1.7) * W * 0.05;
      const spread = W * (0.05 + 0.02 * Math.sin(t * 0.0002 + i));
      const lg = ctx.createLinearGradient(0, 0, 0, H * 0.55);
      lg.addColorStop(0, "rgba(190,210,255,0.09)");
      lg.addColorStop(1, "rgba(190,210,255,0)");
      ctx.fillStyle = lg;
      ctx.beginPath();
      ctx.moveTo(cx - spread * 0.4, 0); ctx.lineTo(cx + spread * 0.4, 0);
      ctx.lineTo(cx + spread * 1.8, H * 0.55); ctx.lineTo(cx - spread * 1.8, H * 0.55);
      ctx.closePath(); ctx.fill();
    }

    // Тёплое свечение со дна — медленно «дышит»
    const breath = 0.85 + 0.15 * Math.sin(t * 0.0007);
    const glow = ctx.createRadialGradient(W * 0.62, H * 1.05, 0, W * 0.62, H * 1.05, Math.max(W, H) * 0.75);
    glow.addColorStop(0, `rgba(255,190,110,${0.30 * breath})`);
    glow.addColorStop(0.35, `rgba(230,140,90,${0.12 * breath})`);
    glow.addColorStop(1, "rgba(200,120,90,0)");
    ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);
    ctx.restore();

    // Поверхность: две волнистые линии
    for (let k = 0; k < 2; k++) {
      ctx.beginPath();
      for (let x = 0; x <= W; x += 8) {
        const y = 10 + k * 7 + Math.sin(x * 0.012 + t * 0.0009 * (k ? -1 : 1)) * 3
                + Math.sin(x * 0.031 + t * 0.0013) * 1.5;
        x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.strokeStyle = k ? "rgba(255,236,205,0.10)" : "rgba(255,236,205,0.22)";
      ctx.lineWidth = 1; ctx.stroke();
    }
  }

  function seabed(t) {
    // Песок: тёмный силуэт дюн с тёплой кромкой
    ctx.beginPath();
    ctx.moveTo(0, H);
    for (let x = 0; x <= W; x += 6) ctx.lineTo(x, floorY(x));
    ctx.lineTo(W, H); ctx.closePath();
    const sand = ctx.createLinearGradient(0, H * 0.8, 0, H);
    sand.addColorStop(0, "rgb(52,38,58)");
    sand.addColorStop(1, "rgb(22,16,32)");
    ctx.fillStyle = sand; ctx.fill();
    ctx.beginPath();
    for (let x = 0; x <= W; x += 6) x ? ctx.lineTo(x, floorY(x)) : ctx.moveTo(x, floorY(x));
    ctx.strokeStyle = "rgba(255,200,130,0.25)"; ctx.lineWidth = 1; ctx.stroke();

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    // Свечение над сундуком — от горки золота
    if (chest) {
      const { x, y, s } = chest;
      const pulse = 0.8 + 0.2 * Math.sin(t * 0.0011);
      const beam = ctx.createRadialGradient(x, y - s * 1.2, 0, x, y - s * 1.2, s * 6);
      beam.addColorStop(0, `rgba(255,215,140,${0.55 * pulse})`);
      beam.addColorStop(1, "rgba(255,180,100,0)");
      ctx.fillStyle = beam;
      ctx.beginPath(); ctx.arc(x, y - s * 1.2, s * 6, 0, 7); ctx.fill();
    }
    // Россыпь: жемчуг — мягкий белый, золото — искры-звёздочки
    for (const g of gems) {
      const tw = still ? 0.7 : Math.max(0, Math.sin(t * 0.0012 * g.sp + g.ph));
      if (g.pearl) {
        const halo = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, g.r * 4);
        halo.addColorStop(0, `rgba(255,245,230,${0.35 + 0.3 * tw})`);
        halo.addColorStop(1, "rgba(255,245,230,0)");
        ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(g.x, g.y, g.r * 4, 0, 7); ctx.fill();
        ctx.fillStyle = "rgba(255,248,236,0.9)";
        ctx.beginPath(); ctx.arc(g.x, g.y, g.r, 0, 7); ctx.fill();
      } else {
        ctx.fillStyle = `rgba(255,205,110,${0.35 + 0.55 * tw})`;
        ctx.beginPath(); ctx.arc(g.x, g.y, g.r, 0, 7); ctx.fill();
        if (tw > 0.85) star(g.x, g.y, 6 * tw, `rgba(255,225,160,${(tw - 0.85) * 4})`);
      }
    }
    ctx.restore();

    if (chest) drawChest(t);
  }

  function star(x, y, r, color) {
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x - r, y); ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r); ctx.lineTo(x, y + r);
    ctx.stroke();
  }

  function frame(t) {
    water(t);
    seabed(t);
    ctx.save();
    ctx.globalCompositeOperation = "lighter";

    // Взвесь в толще
    for (const m of motes) {
      if (!still) { m.y -= m.sp; if (m.y < 0) { m.y = H * 0.85; m.x = rnd(0, W); } }
      ctx.fillStyle = `rgba(200,215,255,${m.a})`;
      ctx.beginPath(); ctx.arc(m.x, m.y, m.r, 0, 7); ctx.fill();
    }

    // Пузырьки поднимаются со дна — от ларца и россыпи
    if (!still && Math.random() < 0.07) {
      const src = Math.random() < 0.4 && chest ? { x: chest.x + rnd(-chest.s, chest.s), y: chest.y - chest.s }
                                                 : gems[(Math.random() * gems.length) | 0];
      if (src) bubbles.push({ x: src.x, y: src.y, r: rnd(0.8, 1.9), life: 1 });
    }
    bubbles = bubbles.filter(b => b.life > 0 && b.y > 12);
    for (const b of bubbles) {
      b.y -= 0.5; b.x += Math.sin(b.y * 0.05) * 0.2; b.life -= 0.0035;
      ctx.strokeStyle = `rgba(230,225,255,${0.35 * b.life})`;
      ctx.lineWidth = 0.8;
      ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, 7); ctx.stroke();
    }

    // Участники: чем глубже, тем теплее; на дне — с золотым ореолом
    for (const p of parts) {
      if (!still && !p.full && p !== picked) {     // выбранная замирает рядом со своей карточкой
        p.y = p.y0 + Math.sin(t * 0.001 * p.sp + p.ph) * 4 * (1 - p.p * 0.6);
        p.x += p.drift;
        // не заплывать под текст слева
        if (p.x < p.minx || p.x > W * 0.97) { p.drift *= -1; p.x = Math.max(p.minx, Math.min(W * 0.97, p.x)); }
      }
      const c = p.col;
      const on = p === hover || p === picked;
      const halo = p.r * (p.full ? 8 : 5) * (on ? 1.6 : 1);
      const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, halo);
      glow.addColorStop(0, rgba(c, p.full ? 0.7 : 0.5));
      glow.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(p.x, p.y, halo, 0, 7); ctx.fill();
      ctx.fillStyle = rgba(c, 0.95);
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r * (on ? 1.5 : 1), 0, 7); ctx.fill();
      if (on) {
        ctx.strokeStyle = "rgba(255,240,215,0.85)"; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 3.2, 0, 7); ctx.stroke();
      }
      if (p.full && !still) {
        // изредка вспыхивает звёздочкой — «нашёл своё»
        const k = Math.sin(t * 0.0009 + p.spark);
        if (k > 0.92) star(p.x, p.y, p.r * 4 * (k - 0.92) * 12, `rgba(255,235,180,${(k - 0.92) * 9})`);
      }
    }
    ctx.restore();

    if (!still && visible && alive) requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------- кто это
  // Холст лежит под текстом, поэтому события ловим на всей шапке и сами
  // ищем ближайшую частицу под курсором.
  const box = canvas.parentElement;
  let hover = null, picked = null;
  const pop = document.createElement("div");
  pop.className = "deep__pop"; pop.hidden = true;
  pop.setAttribute("role", "dialog");
  box.appendChild(pop);

  function hit(e) {
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    let best = null, bd = 18 * 18;            // промахнуться на пару пикселей можно
    for (const p of parts) {
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d < bd) { bd = d; best = p; }
    }
    return best;
  }
  function esc(s) { return String(s ?? "").replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch])); }
  function show(p) {
    picked = p;
    const w = p.who, pct = Math.round((w.p || 0) * 100);
    pop.innerHTML = `<b>${esc(w.n || "Участник")}</b>
      <span class="deep__pop-sub">${esc(w.team || "")}${w.team ? " · " : ""}выполнено ${pct}%${pct >= 100 ? " — на дне 🔥" : ""}</span>
      ${w.c != null ? `<a href="/participants/${encodeURIComponent(w.c)}">Открыть карточку →</a>` : ""}`;
    pop.hidden = false;
    // рядом с точкой, но не за краем шапки
    const bw = box.clientWidth, pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = p.x + 14, top = p.y - ph - 10;
    if (left + pw > bw - 8) left = p.x - pw - 14;
    if (top < 8) top = p.y + 14;
    pop.style.left = Math.max(8, left) + "px";
    pop.style.top = top + "px";
    if (still) frame(0);
  }
  function hide() { picked = null; pop.hidden = true; if (still) frame(0); }

  box.addEventListener("mousemove", e => {
    if (e.target.closest("a, button, .deep__pop")) return;
    const p = hit(e);
    if (p !== hover) { hover = p; box.style.cursor = p ? "pointer" : ""; if (still) frame(0); }
  });
  box.addEventListener("mouseleave", () => { hover = null; box.style.cursor = ""; });
  // клик где угодно на странице: по точке — показать её, мимо карточки — закрыть
  const onClick = e => {
    if (e.target.closest(".deep__pop")) return;
    const p = box.contains(e.target) && !e.target.closest("a, button") ? hit(e) : null;
    if (p) show(p);
    else if (picked) hide();
  };
  document.addEventListener("click", onClick);
  const onKey = e => { if (e.key === "Escape" && picked) hide(); };
  document.addEventListener("keydown", onKey);

  let visible = true;
  const io = new IntersectionObserver(([e]) => {
    const was = visible; visible = e.isIntersecting;
    if (visible && !was && !still && alive) requestAnimationFrame(frame);
  });
  io.observe(canvas);

  let timer;
  const onResize = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { if (!alive) return; hide(); layout(); if (still) frame(0); }, 150);
  };
  window.addEventListener("resize", onResize);
  layout();
  requestAnimationFrame(frame);
  return () => {
    alive = false; io.disconnect(); window.removeEventListener("resize", onResize);
    document.removeEventListener("keydown", onKey); document.removeEventListener("click", onClick); pop.remove();
  };
}

// Анимации: выключаются в «Настройках» (data-motion="off" на <body>);
// без явного выбора — как в системе («уменьшить движение»).
window.vershinaMotion = function () {
  const m = document.body.dataset.motion;
  if (m === "off") return false;
  if (m === "on") return true;
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
};
window.vershinaDeep = init;
window.vershinaDeepStop = init(document.querySelector(".deep__canvas"));
})();
