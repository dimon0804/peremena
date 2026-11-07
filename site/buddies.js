/* Пиксельные персонажи в 3D. Каждый непрозрачный пиксель спрайта — воксель толщиной DEPTH: лицевая и задняя
   сторона рисуются самим спрайтом через аффинное преобразование (пиксели остаются чёткими), а боковые грани —
   четырёхугольниками по краю силуэта. Без библиотек: CSP сайта пускает только свои скрипты.
   Разметка: <button class="buddy" data-buddy="teacher" data-say="реплика|реплика"><span class="buddy-say"></span><canvas></canvas></button> */
(() => {
  "use strict";

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DEPTH = 7;         // толщина фигурки в пикселях спрайта
  const PITCH = -0.17;     // взгляд чуть сверху: видно макушку и плечи
  const LIGHT = norm([-0.45, -0.75, 0.5]); // свет сверху слева, в координатах взгляда
  const models = new Map();

  function norm(v) { const l = Math.hypot(...v); return v.map((c) => c / l); }

  function load(name) {
    if (!models.has(name)) {
      models.set(name, new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(build(img));
        img.onerror = reject;
        img.src = `/img/chars/${name}.png`;
      }));
    }
    return models.get(name);
  }

  // Края силуэта: для каждого пикселя — стороны, за которыми пусто. Только их и видно сбоку.
  function build(img) {
    const w = img.naturalWidth, h = img.naturalHeight;
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, w, h).data;
    const on = (i, j) => i >= 0 && j >= 0 && i < w && j < h && d[(j * w + i) * 4 + 3] > 127;
    const rgbAt = (i, j) => { const k = (j * w + i) * 4; return [d[k], d[k + 1], d[k + 2]]; };
    const dark = (c) => c[0] + c[1] + c[2] < 150;
    // край силуэта — чёрный контур, и бока из него вышли бы чёрными (на тёмном фоне фигурка «плоская»).
    // Бок красим цветом того, что за контуром: одеждой, волосами, кожей.
    const sideColor = (i, j, di, dj) => {
      for (let n = 1; n <= 3; n++) {
        const x = i + di * n, y = j + dj * n;
        if (!on(x, y)) break;
        const c = rgbAt(x, y);
        if (!dark(c)) return c;
      }
      return rgbAt(i, j);
    };
    const edges = [];
    for (let j = 0; j < h; j++) {
      for (let i = 0; i < w; i++) {
        if (!on(i, j)) continue;
        if (!on(i - 1, j)) edges.push({ i, j, side: 0, rgb: sideColor(i, j, 1, 0) });
        if (!on(i + 1, j)) edges.push({ i, j, side: 1, rgb: sideColor(i, j, -1, 0) });
        if (!on(i, j - 1)) edges.push({ i, j, side: 2, rgb: sideColor(i, j, 0, 1) });
        if (!on(i, j + 1)) edges.push({ i, j, side: 3, rgb: sideColor(i, j, 0, -1) });
      }
    }
    // спина: тот же силуэт в тени — при развороте видно, что фигурка объёмная, а не открытка
    const back = document.createElement("canvas");
    back.width = w; back.height = h;
    const b = back.getContext("2d");
    b.drawImage(img, 0, 0);
    b.globalCompositeOperation = "source-atop";
    b.fillStyle = "rgba(20, 8, 40, .62)";
    b.fillRect(0, 0, w, h);
    return { w, h, img, back, edges };
  }

  const NORMALS = [[-1, 0, 0], [1, 0, 0], [0, -1, 0], [0, 1, 0]];

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
      load(el.dataset.buddy).then((m) => { this.model = m; this.resize(); }).catch(() => el.remove());
      el.addEventListener("click", () => this.poke());
      if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
        el.addEventListener("pointerenter", () => this.speak(false));
        el.addEventListener("pointerleave", () => this.hush(600));
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

    // клик: реплика, прыжок и оборот вокруг себя
    poke() {
      this.speak(this.el.classList.contains("is-talking"));
      this.hush(3600);
      if (reduce) return;
      const now = performance.now();
      this.hop = { t0: now, dur: 560 };
      this.spin = { t0: now, dur: 900, from: this.yaw, to: this.yaw + Math.PI * 2 * (Math.random() < 0.5 ? 1 : -1) };
      wake();
    }

    step(now, dt, pointer) {
      if (this.spin) {
        const k = Math.min(1, (now - this.spin.t0) / this.spin.dur);
        this.yaw = this.spin.from + (this.spin.to - this.spin.from) * (1 - Math.pow(1 - k, 3));
        if (k >= 1) { this.yaw = this.target; this.vel = 0; this.spin = null; }
      } else {
        let target = this.base + Math.sin(now / 2000 + this.phase) * 0.22;
        if (pointer) {
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
      g.imageSmoothingEnabled = false;

      const s = Math.min((H * 0.86) / m.h, (W * 0.94) / (m.w + DEPTH));
      const ox = W / 2, oy = H - s * 2.8;
      const hopK = this.hop ? Math.min(1, (now - this.hop.t0) / this.hop.dur) : 0;
      const lift = (reduce ? 0 : Math.sin(now / 520 + this.phase) * 0.35) + (this.hop ? -4 * hopK * (1 - hopK) * 9 : 0);

      // тень на полу сжимается, когда персонаж в воздухе
      const shrink = 1 - Math.min(1, -Math.min(0, lift) / 14) * 0.45;
      g.fillStyle = "rgba(12, 0, 23, .32)";
      g.beginPath();
      g.ellipse(ox, oy + s * 0.6, (m.w / 2) * s * 0.92 * shrink, s * 1.9 * shrink, 0, 0, Math.PI * 2);
      g.fill();

      const ca = Math.cos(this.yaw), sa = Math.sin(this.yaw), cp = Math.cos(PITCH), sp = Math.sin(PITCH);
      const half = DEPTH / 2;
      const P = (x, y, z) => {
        const X = x - m.w / 2, Y = y - m.h + lift, Z = z;
        const x1 = X * ca + Z * sa, z1 = -X * sa + Z * ca;
        const y2 = Y * cp - z1 * sp, z2 = Y * sp + z1 * cp;
        return [ox + x1 * s, oy + y2 * s, z2];
      };
      const R = (n) => {
        const x1 = n[0] * ca + n[2] * sa, z1 = -n[0] * sa + n[2] * ca;
        return [x1, n[1] * cp - z1 * sp, n[1] * sp + z1 * cp];
      };

      // боковые грани: только повёрнутые к нам, от дальних к ближним
      const shade = NORMALS.map((n) => {
        const r = R(n);
        return { vis: r[2] > 0.001, k: 0.42 + 0.4 * Math.max(0, r[0] * LIGHT[0] + r[1] * LIGHT[1] + r[2] * LIGHT[2]) };
      });
      const quads = [];
      for (const e of m.edges) {
        const sh = shade[e.side];
        if (!sh.vis) continue;
        const { i, j } = e;
        let a, b;
        if (e.side === 0) { a = [i, j]; b = [i, j + 1]; }
        else if (e.side === 1) { a = [i + 1, j]; b = [i + 1, j + 1]; }
        else if (e.side === 2) { a = [i, j]; b = [i + 1, j]; }
        else { a = [i, j + 1]; b = [i + 1, j + 1]; }
        const p1 = P(a[0], a[1], half), p2 = P(b[0], b[1], half), p3 = P(b[0], b[1], -half), p4 = P(a[0], a[1], -half);
        quads.push({ z: (p1[2] + p3[2]) / 2, pts: [p1, p2, p3, p4], c: e.rgb, k: sh.k });
      }
      quads.sort((q1, q2) => q1.z - q2.z);
      g.lineWidth = 1;
      g.lineJoin = "round";
      for (const q of quads) {
        const col = `rgb(${q.c[0] * q.k | 0},${q.c[1] * q.k | 0},${q.c[2] * q.k | 0})`;
        g.fillStyle = col; g.strokeStyle = col;
        g.beginPath();
        g.moveTo(q.pts[0][0], q.pts[0][1]);
        for (let n = 1; n < 4; n++) g.lineTo(q.pts[n][0], q.pts[n][1]);
        g.closePath(); g.fill(); g.stroke();
      }

      // ближняя плоскость — последней: у плиты она всегда впереди своих боковых граней
      const front = ca * cp > 0;
      const z = front ? half : -half, o = P(0, 0, z);
      g.setTransform(ca * s, sa * sp * s, 0, cp * s, o[0], o[1]);
      g.drawImage(front ? m.img : m.back, 0, 0);
      g.setTransform(1, 0, 0, 1, 0, 0);
    }
  }

  // ---------------------------------------------------------------- общий цикл
  const buddies = [...document.querySelectorAll(".buddy")].map((el) => new Buddy(el));
  if (!buddies.length) return;

  let pointer = null, raf = 0, last = 0;
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  if (finePointer && !reduce) {
    window.addEventListener("pointermove", (e) => { pointer = { x: e.clientX, y: e.clientY }; }, { passive: true });
    document.documentElement.addEventListener("pointerleave", () => { pointer = null; });
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - (last || now)) / 1000);
    last = now;
    let any = false;
    for (const b of buddies) {
      if (!b.visible) continue;
      any = true;
      // за курсором следят только те, рядом с кем он: иначе вся страница вертит головами
      const near = pointer && Math.abs(pointer.y - b.el.getBoundingClientRect().top) < 420 ? pointer : null;
      b.step(now, dt, near);
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
  buddies.forEach((b) => io.observe(b.el));
  document.addEventListener("visibilitychange", wake);

  const ro = new ResizeObserver((entries) => {
    for (const en of entries) buddies.find((x) => x.canvas === en.target)?.resize();
  });
  buddies.forEach((b) => ro.observe(b.canvas));

  // кто-то из группы иногда сам что-то говорит — пока группа на экране и посетитель её не трогает
  const crew = buddies.filter((b) => b.el.closest(".squad"));
  if (crew.length && !reduce) {
    let turn = 0;
    setInterval(() => {
      if (document.hidden || !crew[0].visible || crew.some((b) => b.el.matches(":hover"))) return;
      const b = crew[turn++ % crew.length];
      b.speak(true);
      b.hush(3200);
    }, 6500);
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
