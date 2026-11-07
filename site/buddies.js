/* Пиксельные персонажи в 3D. Каждый непрозрачный пиксель спрайта — воксель толщиной DEPTH: лицевая и задняя
   сторона рисуются самим спрайтом через аффинное преобразование (пиксели остаются чёткими), а боковые грани —
   четырёхугольниками по краю силуэта. Фигурка разбита на тело и две ноги, чтобы ходить шагом.
   Без библиотек: CSP сайта пускает только свои скрипты.
   Разметка: <button class="buddy" data-buddy="teacher" data-say="реплика|реплика"><span class="buddy-say"></span><canvas></canvas></button>
   data-walk — ходит по дорожке внутри .squad; data-turn — поворот в покое; data-say-now — реплика видна сразу. */
(() => {
  "use strict";

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  const DEPTH = 7;         // толщина фигурки в пикселях спрайта
  const PITCH = -0.17;     // взгляд чуть сверху: видно макушку и плечи
  const LIGHT = norm([-0.45, -0.75, 0.5]); // свет сверху слева, в координатах взгляда
  // где начинаются ноги (доля высоты спрайта): у учительницы длинная юбка — шагают только туфли
  const LEGS = { teacher: 0.93, chemist: 0.8, student: 0.75, coder: 0.76 };
  const NORMALS = [[-1, 0, 0], [1, 0, 0], [0, -1, 0], [0, 1, 0]];
  // спрайт заранее увеличен в UP раз «по пикселю», а рисуется со сглаживанием: пиксели остаются чёткими,
  // а края при повороте ровные, без ряби и лесенки
  const UP = 4;

  // соседние грани бока одного цвета склеиваем в полосу: меньше швов и пёстрой «нарезки» на боках
  function mergeEdges(edges) {
    const near = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) < 48;
    const key = (e) => (e.side < 2 ? `${e.side}:${e.i}` : `${e.side}:${e.j}`);
    const groups = new Map();
    for (const e of edges) { const k = key(e); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(e); }
    const out = [];
    for (const list of groups.values()) {
      const vertical = list[0].side < 2;
      list.sort((a, b) => (vertical ? a.j - b.j : a.i - b.i));
      let run = null;
      for (const e of list) {
        const pos = vertical ? e.j : e.i;
        if (run && pos === run.end && near(run.rgb, e.rgb)) {
          run.sum[0] += e.rgb[0]; run.sum[1] += e.rgb[1]; run.sum[2] += e.rgb[2]; run.n++; run.end++;
          continue;
        }
        if (run) out.push(run);
        run = { side: e.side, i: e.i, j: e.j, end: pos + 1, rgb: e.rgb, sum: [...e.rgb], n: 1 };
      }
      if (run) out.push(run);
    }
    for (const r of out) { r.rgb = r.sum.map((v) => Math.round(v / r.n)); r.len = r.end - (r.side < 2 ? r.j : r.i); delete r.sum; }
    return out;
  }

  function upscale(src, w, h) {
    const c = document.createElement("canvas");
    c.width = w * UP; c.height = h * UP;
    const g = c.getContext("2d");
    g.imageSmoothingEnabled = false;
    g.drawImage(src, 0, 0, w * UP, h * UP);
    return c;
  }
  const models = new Map();

  function norm(v) { const l = Math.hypot(...v); return v.map((c) => c / l); }

  function load(name) {
    if (!models.has(name)) {
      models.set(name, new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(build(img, LEGS[name] || 0.8));
        img.onerror = reject;
        img.src = `/img/chars/${name}.png`;
      }));
    }
    return models.get(name);
  }

  function build(img, legsFrom) {
    const w = img.naturalWidth, h = img.naturalHeight;
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, w, h).data;
    const on = (i, j) => i >= 0 && j >= 0 && i < w && j < h && d[(j * w + i) * 4 + 3] > 127;
    const rgbAt = (i, j) => { const k = (j * w + i) * 4; return [d[k], d[k + 1], d[k + 2]]; };
    const dark = (col) => col[0] + col[1] + col[2] < 150;
    // край силуэта — чёрный контур, и бока из него вышли бы чёрными (на тёмном фоне фигурка «плоская»).
    // Бок красим цветом того, что за контуром: одеждой, волосами, кожей.
    // самый светлый из ближайших внутренних пикселей: тёмные волосы и куртка за контуром не дают чёрных полос
    const sideColor = (i, j, di, dj) => {
      let best = null, lum = -1;
      for (let n = 1; n <= 5; n++) {
        const x = i + di * n, y = j + dj * n;
        if (!on(x, y)) break;
        const col = rgbAt(x, y);
        if (!dark(col)) return col;
        const l = col[0] + col[1] + col[2];
        if (l > lum) { lum = l; best = col; }
      }
      return best ? best.map((v) => Math.min(255, v + 18)) : rgbAt(i, j);
    };
    // расстояние до края силуэта: у края пиксели чуть темнее, к середине светлее — фигурка читается округлой
    const dist = new Uint8Array(w * h).fill(255);
    const queue = [];
    for (let j = 0; j < h; j++) {
      for (let i = 0; i < w; i++) {
        if (!on(i, j)) continue;
        if (!on(i - 1, j) || !on(i + 1, j) || !on(i, j - 1) || !on(i, j + 1)) { dist[j * w + i] = 1; queue.push(j * w + i); }
      }
    }
    for (let q = 0; q < queue.length; q++) {
      const v = queue[q], i = v % w, j = (v - i) / w, dv = dist[v];
      for (const [x, y] of [[i - 1, j], [i + 1, j], [i, j - 1], [i, j + 1]]) {
        if (on(x, y) && dist[y * w + x] > dv + 1) { dist[y * w + x] = dv + 1; queue.push(y * w + x); }
      }
    }
    const ROUND = [1, 0.8, 0.88, 0.94, 0.98];   // множитель яркости по расстоянию до края: 1-й пиксель — контур
    const lit = (j) => 1.06 - 0.1 * (j / h);     // свет сверху: голова чуть светлее ботинок

    // ноги делим по середине между ступнями (средний x нижнего ряда)
    const legTop = Math.round(h * legsFrom);
    let sx = 0, n = 0;
    for (let i = 0; i < w; i++) if (on(i, h - 1)) { sx += i; n++; }
    const split = n ? sx / n : w / 2;
    const partOf = (i, j) => (j < legTop ? 0 : i < split ? 1 : 2); // 0 — тело, 1 — левая нога, 2 — правая

    const parts = [0, 1, 2].map(() => {
      const pc = document.createElement("canvas"); pc.width = w; pc.height = h;
      return { canvas: pc, ctx: pc.getContext("2d"), data: null, edges: [] };
    });
    parts.forEach((p) => { p.data = p.ctx.createImageData(w, h); });
    for (let j = 0; j < h; j++) {
      for (let i = 0; i < w; i++) {
        if (!on(i, j)) continue;
        const p = parts[partOf(i, j)], k = (j * w + i) * 4;
        const r = (ROUND[dist[j * w + i]] ?? 1.02) * lit(j);
        p.data.data[k] = Math.min(255, d[k] * r); p.data.data[k + 1] = Math.min(255, d[k + 1] * r);
        p.data.data[k + 2] = Math.min(255, d[k + 2] * r); p.data.data[k + 3] = d[k + 3];
        if (!on(i - 1, j)) p.edges.push({ i, j, side: 0, rgb: sideColor(i, j, 1, 0) });
        if (!on(i + 1, j)) p.edges.push({ i, j, side: 1, rgb: sideColor(i, j, -1, 0) });
        if (!on(i, j - 1)) p.edges.push({ i, j, side: 2, rgb: sideColor(i, j, 0, 1) });
        if (!on(i, j + 1)) p.edges.push({ i, j, side: 3, rgb: sideColor(i, j, 0, -1) });
      }
    }
    for (const p of parts) {
      p.ctx.putImageData(p.data, 0, 0);
      // спина: тот же силуэт в тени — при развороте видно, что фигурка объёмная, а не открытка
      const back = document.createElement("canvas"); back.width = w; back.height = h;
      const b = back.getContext("2d");
      b.drawImage(p.canvas, 0, 0);
      b.globalCompositeOperation = "source-atop";
      b.fillStyle = "rgba(20, 8, 40, .62)";
      b.fillRect(0, 0, w, h);
      p.big = upscale(p.canvas, w, h);
      p.bigBack = upscale(back, w, h);
      p.edges = mergeEdges(p.edges);
      p.data = null;
    }
    // порядок рисования плоскостей: сначала ноги, потом тело — тело перекрывает приподнятую ногу на стыке
    return { w, h, parts: [parts[1], parts[2], parts[0]] };
  }

  class Buddy {
    constructor(el) {
      this.el = el;
      this.canvas = el.querySelector("canvas");
      this.ctx = this.canvas.getContext("2d");
      this.say = el.querySelector(".buddy-say");
      this.lines = (el.dataset.say || "").split("|").map((s) => s.trim()).filter(Boolean);
      this.line = 0;
      this.base = Number(el.dataset.turn || 0.32);        // поворот в покое, у каждого свой
      this.phase = Math.random() * Math.PI * 2;
      this.yaw = this.base; this.vel = 0; this.target = this.base;
      this.spin = null; this.hop = null; this.visible = false;
      this.stride = 0; this.walking = false;               // шаг: фаза и идёт ли сейчас
      this.walker = el.dataset.walk !== undefined ? { x: 0, dir: Math.random() < 0.5 ? 1 : -1, until: 0, rest: true } : null;
      load(el.dataset.buddy).then((m) => { this.model = m; this.resize(); }).catch(() => el.remove());
      el.addEventListener("click", () => this.poke());
      if (finePointer) {
        el.addEventListener("pointerenter", () => { this.hover = true; this.speak(false); });
        el.addEventListener("pointerleave", () => { this.hover = false; this.hush(600); });
      }
    }

    resize() {
      const r = this.canvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.cw = Math.max(1, Math.round(r.width * dpr));
      this.ch = Math.max(1, Math.round(r.height * dpr));
      this.canvas.width = this.cw; this.canvas.height = this.ch;
      this.draw(performance.now());
    }

    speak(next = true) {
      if (!this.say || !this.lines.length) return;
      if (next) this.line = (this.line + 1) % this.lines.length;
      this.say.textContent = this.lines[this.line];
      this.el.classList.add("is-talking");
      clearTimeout(this.hushTimer);
    }

    hush(delay) {
      clearTimeout(this.hushTimer);
      this.hushTimer = setTimeout(() => this.el.classList.remove("is-talking"), delay);
    }

    // клик: реплика, прыжок и оборот вокруг себя; кто шёл — останавливается поговорить
    poke() {
      this.speak(this.el.classList.contains("is-talking"));
      this.hush(3600);
      if (this.walker) { this.walker.rest = true; this.walker.until = performance.now() + 4200; }
      if (reduce) return;
      const now = performance.now();
      this.hop = { t0: now, dur: 560 };
      this.spin = { t0: now, dur: 900, from: this.yaw, to: this.yaw + Math.PI * 2 * (Math.random() < 0.5 ? 1 : -1) };
      wake();
    }

    // прогулка по своей полосе коридора: идёт, останавливается, иногда что-то говорит; у края полосы
    // сначала встаёт и только потом поворачивает обратно — не бьётся о стену и не проходит сквозь соседа
    walk(now, dt, lane) {
      const wk = this.walker;
      if (!lane || reduce) { this.walking = false; return; }
      if (!wk.placed) {
        wk.x = (lane.min + lane.max) / 2;
        wk.dir = lane.k % 2 ? -1 : 1;
        wk.rest = true; wk.until = now + 1200 + lane.k * 1100;   // на старте сначала стоят, потом по очереди трогаются
        wk.placed = true;
        this.el.classList.add("is-placed");
      }
      wk.x = Math.min(lane.max, Math.max(lane.min, wk.x));
      if (this.hover || this.spin) { this.walking = false; this.place(); return; }
      if (now > wk.until) {
        if (wk.rest) {
          wk.rest = false; wk.face = null;
          wk.until = now + 3000 + Math.random() * 4000;
          if (wk.turn) { wk.dir = -wk.dir; wk.turn = false; }
        } else {
          wk.rest = true; wk.until = now + 1400 + Math.random() * 2000;
          if (Math.random() < 0.4) { this.speak(true); this.hush(2400); }
        }
      }
      this.walking = !wk.rest && lane.max - lane.min > 4;
      if (this.walking) {
        const speed = Math.min(58, Math.max(30, this.el.offsetWidth * 0.42)); // px/с: спокойный шаг
        wk.x += wk.dir * speed * dt;
        if ((wk.dir < 0 && wk.x <= lane.min) || (wk.dir > 0 && wk.x >= lane.max)) {
          wk.x = Math.min(lane.max, Math.max(lane.min, wk.x));
          wk.rest = true; wk.turn = true; wk.until = now + 500 + Math.random() * 900;
          this.walking = false;
        } else {
          this.stride += (speed * dt) / Math.max(14, speed * 0.45);  // чуть больше двух шагов в секунду
        }
      }
      this.place();
    }

    place() { if (this.walker) this.el.style.transform = `translate3d(${this.walker.x.toFixed(1)}px, 0, 0)`; }

    step(now, dt, pointer, track) {
      if (this.walker) this.walk(now, dt, track);
      if (this.spin) {
        const k = Math.min(1, (now - this.spin.t0) / this.spin.dur);
        this.yaw = this.spin.from + (this.spin.to - this.spin.from) * (1 - Math.pow(1 - k, 3));
        if (k >= 1) { this.yaw = this.target; this.vel = 0; this.spin = null; }
      } else {
        let target = this.base + Math.sin(now / 2000 + this.phase) * 0.22;
        if (this.walking) target = this.walker.dir * 0.62;   // идёт вполоборота туда, куда шагает
        else if (this.walker && this.walker.face && performance.now() < this.walker.until) target = this.walker.face;
        else if (pointer) {
          const r = this.el.getBoundingClientRect();
          const dx = (pointer.x - (r.left + r.width / 2)) / Math.max(320, window.innerWidth / 3);
          target = Math.max(-0.75, Math.min(0.75, dx)) * 0.9;
        }
        this.target = target;
        // пружина с лёгким недодемпфированием: фигурка поворачивается мягко и чуть «доворачивает»
        const acc = (target - this.yaw) * 34 - this.vel * 9.5;
        this.vel += acc * dt;
        this.yaw += this.vel * dt;
      }
      if (this.hop && now - this.hop.t0 > this.hop.dur) this.hop = null;
      this.draw(now);
    }

    draw(now) {
      const m = this.model;
      if (!m || !this.cw) return;
      const g = this.ctx, W = this.cw, H = this.ch;
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.clearRect(0, 0, W, H);
      g.imageSmoothingEnabled = true;
      g.imageSmoothingQuality = "high";

      const s = Math.min((H * 0.86) / m.h, (W * 0.94) / (m.w + DEPTH));
      const ox = W / 2, oy = H - s * 2.8;
      const hopK = this.hop ? Math.min(1, (now - this.hop.t0) / this.hop.dur) : 0;
      // шаг: ноги поднимаются по очереди, корпус подпрыгивает в такт
      let legL = 0, legR = 0, bob = 0;
      if (this.walking) {
        const f = this.stride % 2, lift = Math.sin((f % 1) * Math.PI);
        if (f < 1) legL = -1.6 * lift; else legR = -1.6 * lift;
        bob = -0.9 * lift;
      }
      const idle = reduce || this.walking ? 0 : Math.sin(now / 520 + this.phase) * 0.35;
      const lift = idle + bob + (this.hop ? -4 * hopK * (1 - hopK) * 9 : 0);

      // тень на полу сжимается, когда персонаж в воздухе
      const shrink = 1 - Math.min(1, -Math.min(0, lift) / 14) * 0.45;
      g.fillStyle = "rgba(12, 0, 23, .32)";
      g.beginPath();
      g.ellipse(ox, oy + s * 0.6, (m.w / 2) * s * 0.92 * shrink, s * 1.9 * shrink, 0, 0, Math.PI * 2);
      g.fill();

      const ca = Math.cos(this.yaw), sa = Math.sin(this.yaw), cp = Math.cos(PITCH), sp = Math.sin(PITCH);
      const half = DEPTH / 2;
      const P = (x, y, z, dy) => {
        const X = x - m.w / 2, Y = y - m.h + dy, Z = z;
        const x1 = X * ca + Z * sa, z1 = -X * sa + Z * ca;
        const y2 = Y * cp - z1 * sp, z2 = Y * sp + z1 * cp;
        return [ox + x1 * s, oy + y2 * s, z2];
      };
      const R = (nv) => {
        const x1 = nv[0] * ca + nv[2] * sa, z1 = -nv[0] * sa + nv[2] * ca;
        return [x1, nv[1] * cp - z1 * sp, nv[1] * sp + z1 * cp];
      };

      // боковые грани: только повёрнутые к нам, от дальних к ближним
      const shade = NORMALS.map((nv) => {
        const r = R(nv);
        return { vis: r[2] > 0.001, k: 0.42 + 0.4 * Math.max(0, r[0] * LIGHT[0] + r[1] * LIGHT[1] + r[2] * LIGHT[2]) };
      });
      const offs = [lift + legL, lift + legR, lift];  // в порядке m.parts: левая нога, правая, тело
      const quads = [];
      m.parts.forEach((part, pi) => {
        const dy = offs[pi];
        for (const e of part.edges) {
          const sh = shade[e.side];
          if (!sh.vis) continue;
          const { i, j, len } = e;
          let a, b;
          if (e.side === 0) { a = [i, j]; b = [i, j + len]; }
          else if (e.side === 1) { a = [i + 1, j]; b = [i + 1, j + len]; }
          else if (e.side === 2) { a = [i, j]; b = [i + len, j]; }
          else { a = [i, j + 1]; b = [i + len, j + 1]; }
          const p1 = P(a[0], a[1], half, dy), p2 = P(b[0], b[1], half, dy), p3 = P(b[0], b[1], -half, dy), p4 = P(a[0], a[1], -half, dy);
          quads.push({ z: (p1[2] + p3[2]) / 2, pts: [p1, p2, p3, p4], c: e.rgb, k: sh.k, front: ca * cp > 0 });
        }
      });
      quads.sort((q1, q2) => q1.z - q2.z);
      g.lineWidth = 1;
      g.lineJoin = "round";
      for (const q of quads) {
        // бок плавно темнеет от лица к спине — толщина читается формой, а не ступенькой
        const near = `rgb(${q.c[0] * q.k | 0},${q.c[1] * q.k | 0},${q.c[2] * q.k | 0})`;
        const far = `rgb(${q.c[0] * q.k * 0.55 | 0},${q.c[1] * q.k * 0.55 | 0},${q.c[2] * q.k * 0.6 | 0})`;
        const fr = q.front ? q.pts[0] : q.pts[3], bk = q.front ? q.pts[3] : q.pts[0];
        const grad = g.createLinearGradient(fr[0], fr[1], bk[0], bk[1]);
        grad.addColorStop(0, near); grad.addColorStop(1, far);
        g.fillStyle = grad; g.strokeStyle = grad;
        g.beginPath();
        g.moveTo(q.pts[0][0], q.pts[0][1]);
        for (let k = 1; k < 4; k++) g.lineTo(q.pts[k][0], q.pts[k][1]);
        g.closePath(); g.fill(); g.stroke();
      }

      // ближняя плоскость — последней: у плиты она всегда впереди своих боковых граней
      const front = ca * cp > 0, z = front ? half : -half;
      m.parts.forEach((part, pi) => {
        const o = P(0, 0, z, offs[pi]);
        g.setTransform((ca * s) / UP, (sa * sp * s) / UP, 0, (cp * s) / UP, o[0], o[1]);
        g.drawImage(front ? part.big : part.bigBack, 0, 0);
      });
      g.setTransform(1, 0, 0, 1, 0, 0);
    }
  }

  // ---------------------------------------------------------------- общий цикл
  const buddies = [...document.querySelectorAll(".buddy")].map((el) => new Buddy(el));
  if (!buddies.length) return;

  // полосы для гуляющих: коридор между стоящими по краям делится поровну, у каждого гуляющего своя
  const lanes = new Map();
  function measureTracks() {
    lanes.clear();
    for (const sq of document.querySelectorAll(".squad")) {
      const still = [...sq.querySelectorAll(".buddy:not([data-walk])")];
      const walkers = [...sq.querySelectorAll(".buddy[data-walk]")].filter((el) => el.offsetWidth > 0);
      if (!walkers.length) continue;
      const ww = walkers[0].offsetWidth, gapPx = 10;
      const left = still.length ? still[0].offsetLeft + still[0].offsetWidth + gapPx : 0;
      const right = still.length > 1 ? still[still.length - 1].offsetLeft - gapPx : sq.clientWidth;
      const seg = Math.max(0, right - left) / walkers.length;
      walkers.forEach((el, k) => {
        const min = left + k * seg + (k ? 6 : 0), max = left + (k + 1) * seg - ww - (k < walkers.length - 1 ? 6 : 0);
        lanes.set(el, { k, min, max: Math.max(min, max) });
      });
    }
  }
  measureTracks();
  window.addEventListener("resize", measureTracks, { passive: true });

  let pointer = null, raf = 0, last = 0;
  if (finePointer && !reduce) {
    window.addEventListener("pointermove", (e) => { pointer = { x: e.clientX, y: e.clientY }; }, { passive: true });
    document.documentElement.addEventListener("pointerleave", () => { pointer = null; });
  }

  // соседи сошлись на границе полос лицом к лицу — останавливаются, здороваются и расходятся обратно
  const GREET = { student: "Здравствуйте!", chemist: "Привет! Домашку видел?", teacher: "Добрый день!", coder: "Привет!" };
  function meetings(now) {
    const ws = buddies.filter((b) => b.walker && b.walking && lanes.has(b.el));
    for (const a of ws) {
      for (const c of ws) {
        const la = lanes.get(a.el), lc = lanes.get(c.el);
        if (lc.k !== la.k + 1 || a.el.parentElement !== c.el.parentElement) continue;
        const A = a.walker, C = c.walker, near = a.el.offsetWidth * 0.35;
        if (A.dir !== 1 || C.dir !== -1 || A.x < la.max - near || C.x > lc.min + near) continue;
        for (const [w, to] of [[A, 1], [C, -1]]) { w.rest = true; w.turn = true; w.until = now + 2800; w.face = to * 0.95; }
        a.walking = c.walking = false;
        const first = Math.random() < 0.5 ? a : c, second = first === a ? c : a;
        const hi = (b) => { if (!b.say) return; b.say.textContent = GREET[b.el.dataset.buddy] || "Привет!"; b.el.classList.add("is-talking"); b.hush(1500); };
        hi(first);
        setTimeout(() => hi(second), 1000);
      }
    }
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - (last || now)) / 1000);
    last = now;
    meetings(now);
    let any = false;
    for (const b of buddies) {
      if (!b.visible) continue;
      any = true;
      // за курсором следят только те, рядом с кем он: иначе вся страница вертит головами
      const near = pointer && Math.abs(pointer.y - b.el.getBoundingClientRect().top) < 420 ? pointer : null;
      b.step(now, dt, near, b.walker ? lanes.get(b.el) : null);
    }
    raf = any && !document.hidden ? requestAnimationFrame(frame) : 0;
    if (!raf) last = 0;
  }
  function wake() { if (!raf && !reduce && !document.hidden) raf = requestAnimationFrame(frame); }

  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      const b = buddies.find((x) => x.el === en.target);
      if (b) { b.visible = en.isIntersecting; if (b.visible && reduce) b.draw(performance.now()); }
    }
    wake();
  }, { rootMargin: "80px" });
  // гуляющих наблюдаем по их коридору: сами они ездят по нему и могут на миг выйти из рамки
  buddies.forEach((b) => io.observe(b.el));
  document.addEventListener("visibilitychange", wake);

  const ro = new ResizeObserver((entries) => {
    for (const en of entries) buddies.find((x) => x.canvas === en.target)?.resize();
    measureTracks();
  });
  buddies.forEach((b) => ro.observe(b.canvas));

  // при «меньше движения» гуляющие просто стоят посередине своих полос
  if (reduce) {
    for (const [el, l] of lanes) {
      el.style.transform = `translate3d(${((l.min + l.max) / 2).toFixed(1)}px, 0, 0)`;
      el.classList.add("is-placed");
    }
  }

  // стоящие в коридоре иногда сами что-то говорят — пока группа на экране и посетитель её не трогает
  const crew = buddies.filter((b) => b.el.closest(".squad") && !b.walker);
  if (crew.length && !reduce) {
    let turn = 0;
    setInterval(() => {
      if (document.hidden || !crew[0].visible || buddies.some((b) => b.hover || b.el.classList.contains("is-talking"))) return;  // по одному, не хором
      const b = crew[turn++ % crew.length];
      b.speak(true);
      b.hush(3200);
    }, 7000);
  }

  // реплика, которая видна сразу (404, вход)
  buddies.filter((b) => b.el.dataset.sayNow !== undefined).forEach((b) => { b.line = 0; b.speak(false); });
})();

/* Портреты команды: плитка наклоняется за курсором, как карточка героя в игре. Только мышь и без «меньше движения». */
(() => {
  "use strict";
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (const li of document.querySelectorAll(".team li")) {
    const ava = li.querySelector(".ava"), img = ava && ava.querySelector("img");
    if (!img) continue;
    li.addEventListener("pointermove", (e) => {
      const r = ava.getBoundingClientRect();
      const px = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) - 0.5;
      const py = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)) - 0.5;
      ava.style.transform = `perspective(700px) rotateX(${(-py * 12).toFixed(2)}deg) rotateY(${(px * 14).toFixed(2)}deg) translateY(-6px)`;
      img.style.transform = `translate(${(px * -8).toFixed(1)}px, ${(py * -6).toFixed(1)}px) scale(1.06)`;
    });
    li.addEventListener("pointerleave", () => { ava.style.transform = ""; img.style.transform = ""; });
  }
})();
