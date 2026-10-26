/* Сайт «Перемены»: живой счётчик на главной и вход через бота. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // «1 класс», «2 класса», «5 классов» — счётчик на главной показывает живые цифры, падеж должен совпадать
  const plural = (n, one, few, many) => {
    const a = Math.abs(Math.round(n)) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  };
  const LABELS = {
    saved_h: ["час рутины бот уже снял с учителей", "часа рутины бот уже снял с учителей", "часов рутины бот уже снял с учителей"],
    saved_m: ["минуту рутины бот уже снял с учителей", "минуты рутины бот уже снял с учителей", "минут рутины бот уже снял с учителей"],
    classes: ["класс подключён", "класса подключено", "классов подключено"],
    polls: ["сбор проведён без ручного подсчёта", "сбора проведено без ручного подсчёта", "сборов проведено без ручного подсчёта"],
    answers: ["ответ родителей собран", "ответа родителей собрано", "ответов родителей собрано"],
  };

  function countUp(el, value) {
    const fmt = (n) => Math.round(n).toLocaleString("ru-RU");
    if (reduceMotion || value < 10) { el.textContent = fmt(value); return; }
    const t0 = performance.now(), dur = 1200;
    const tick = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      el.textContent = fmt(value * (1 - Math.pow(1 - k, 3)));
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------ главная
  async function home() {
    try {
      const r = await fetch("/api/public/stats");
      if (r.ok) {
        const s = await r.json();
        if (s.bot_link) $$("[data-bot-link]").forEach((a) => (a.href = s.bot_link));
        // счётчик показываем, когда есть чем гордиться — пустые нули на главной не нужны
        if (s.classes > 0 || s.minutes_saved > 0) {
          const live = $("#live");
          live.hidden = false;
          // пока сэкономлено меньше двух часов, честнее показывать минуты, чем «0 часов»
          const inHours = s.minutes_saved >= 120;
          const vals = { saved: inHours ? s.minutes_saved / 60 : s.minutes_saved, classes: s.classes, polls: s.polls, answers: s.answers };
          $$("[data-stat]", live).forEach((el) => {
            const key = el.dataset.stat === "saved" ? (inHours ? "saved_h" : "saved_m") : el.dataset.stat;
            el.nextElementSibling.textContent = plural(vals[el.dataset.stat], ...LABELS[key]);
          });
          const io = new IntersectionObserver((es) => {
            if (es.some((e) => e.isIntersecting)) {
              $$("[data-stat]", live).forEach((el) => countUp(el, vals[el.dataset.stat] || 0));
              io.disconnect();
            }
          });
          io.observe(live);
        }
      }
    } catch (_) { /* главная работает и без статистики */ }
    // уже вошли — кнопка ведёт прямо в кабинет
    try {
      const me = await (await fetch("/api/auth/session", { credentials: "same-origin" })).json();
      if (me.logged_in) $$("[data-cabinet]").forEach((a) => { a.href = "/app/"; if (a.textContent.trim() === "Войти") a.textContent = "Кабинет"; });
    } catch (_) { /* не вошли */ }
  }

  // ------------------------------------------------------------ вход
  async function login() {
    const qr = $("#qr"), link = $("#login-link"), status = $("#status"), again = $("#again");
    let code = null, timer = null, deadline = 0;

    const setStatus = (text, cls = "") => { status.textContent = text; status.className = "status " + cls; };

    async function start() {
      clearInterval(timer);
      again.hidden = true;
      setStatus("Готовлю ссылку…");
      try {
        const r = await fetch("/api/auth/start", { method: "POST", headers: { "X-Peremena": "1" } });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Вход временно недоступен");
        code = d.code;
        $("#pin").textContent = d.pin;
        if (d.bot_username) $("#bot-name").href = `https://max.ru/${d.bot_username}`;
        qr.innerHTML = d.qr_svg;
        link.href = d.link;
        link.hidden = false;
        deadline = Date.now() + d.expires_in * 1000;
        setStatus("Ждём подтверждения в боте…");
        timer = setInterval(poll, 2000);
      } catch (e) {
        setStatus(e.message, "err");
        again.hidden = false;
      }
    }

    async function poll() {
      if (Date.now() > deadline) {
        clearInterval(timer);
        setStatus("Ссылка устарела.", "err");
        again.hidden = false;
        return;
      }
      try {
        const r = await fetch(`/api/auth/status?code=${encodeURIComponent(code)}`, { credentials: "same-origin" });
        const d = await r.json();
        if (d.status === "ok") {
          clearInterval(timer);
          setStatus("✓ Вход подтверждён, открываю кабинет…", "ok");
          location.href = "/app/";
        } else if (d.status === "rejected") {
          clearInterval(timer);
          setStatus("Вход отклонён в боте.", "err");
          again.hidden = false;
        } else if (d.status === "expired") {
          clearInterval(timer);
          setStatus("Ссылка устарела.", "err");
          again.hidden = false;
        }
      } catch (_) { /* сеть моргнула — попробуем на следующем шаге */ }
    }

    again.addEventListener("click", start);
    // уже вошли — сразу в кабинет
    try {
      const me = await (await fetch("/api/auth/session", { credentials: "same-origin" })).json();
      if (me.logged_in) { location.href = "/app/"; return; }
    } catch (_) { /* продолжаем вход */ }
    start();
  }

  if (document.body.dataset.page === "login") login(); else home();
})();
