/* «Перемена» — мини-приложение MAX. Без сборки: чистый JS, работает в WebView MAX и в браузере (dev). */
(() => {
  "use strict";

  // max-web-app.js подгружается только внутри MAX (см. index.html) — в браузере кабинет не ждёт чужой CDN
  let WA = window.WebApp || null;
  const params = new URLSearchParams(location.search);
  const DEV_USER = params.get("dev_user");
  const DEV = !!DEV_USER || params.has("dev");
  const $view = document.getElementById("view");
  const $tabbar = document.getElementById("tabbar");
  const $toast = document.getElementById("toast");
  const $modal = document.getElementById("modal");
  const $modalBody = document.getElementById("modal-body");

  const S = { me: null, templates: null, stack: [], route: null };

  // ------------------------------------------------------------------ утилиты
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  };
  const REDUCED = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rub = (n) => `${Number(n || 0).toLocaleString("ru-RU")} ₽`;
  const mins = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const toMins = (s) => { const [h, m] = (s || "0:0").split(":").map(Number); return h * 60 + (m || 0); };
  const fmtMinutes = (t) => { t = Math.round(t); const h = Math.floor(t / 60), m = t % 60; return h ? (m ? `${h} ч ${m} мин` : `${h} ч`) : `${m} мин`; };

  // Внутри MAX есть «мост» (BackButton, вибрация); в браузере его вызовы сыплют ошибками в консоль
  let IN_MAX = !!WA?.initData;

  // ждём max-web-app.js, если index.html решил, что мы внутри MAX; не загрузился за 4 с — работаем как сайт
  function maxReady() {
    const s = window.__maxScript;
    if (WA || !s) return Promise.resolve();
    return new Promise((ok) => {
      const done = () => { WA = window.WebApp || null; IN_MAX = !!WA?.initData; ok(); };
      s.addEventListener("load", done, { once: true });
      s.addEventListener("error", done, { once: true });
      setTimeout(done, 4000);
    });
  }

  function haptic(kind) {
    if (!IN_MAX) return;
    try { WA?.HapticFeedback?.notificationOccurred(kind); } catch (_) { /* не на всех платформах */ }
  }

  let toastTimer;
  function toast(msg, isErr) {
    $toast.textContent = msg;
    $toast.classList.toggle("err", !!isErr);
    $toast.setAttribute("role", isErr ? "alert" : "status");
    $toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => ($toast.hidden = true), isErr ? 4500 : 2800);
  }

  // демо: что в настоящем боте ушло бы в личку или в чат, здесь честно называем «как было бы»
  const isDemo = () => !!S.me?.demo;
  const say = (real, demo) => (isDemo() ? demo : real);

  // ошибки проверки FastAPI/pydantic приходят по-английски массивом — переводим на человеческий русский
  const FIELD = { text: "Текст", question: "Вопрос", answer: "Ответ", name: "Название", amount: "Сумма", child_name: "Имя ребёнка",
    code: "Код", title: "Подпись", tags: "Теги", options: "Варианты", comment: "Комментарий", chat_ids: "Классы", keywords: "Ключевые слова",
    urgent_words: "Слова-маркеры", work_start: "Начало дня", work_end: "Конец дня", subject: "Предмет" };
  function humanError(detail) {
    if (typeof detail === "string") return detail;
    if (!Array.isArray(detail) || !detail.length) return null;
    const d = detail[0], field = FIELD[d.loc?.[d.loc.length - 1]], c = d.ctx || {};
    const where = field ? `«${field}»: ` : "";
    switch (d.type) {
      case "string_too_short": return `${where}слишком коротко — нужно хотя бы ${c.min_length} ${plural(c.min_length, "символ", "символа", "символов")}`;
      case "string_too_long": return `${where}слишком длинно — не больше ${c.max_length} ${plural(c.max_length, "символа", "символов", "символов")}`;
      case "too_short": return field ? `Выберите хотя бы одно: ${field.toLowerCase()}` : "Не хватает данных";
      case "missing": return field ? `Заполните поле «${field}»` : "Заполните все поля";
      case "greater_than_equal": case "greater_than": return `${where}число слишком маленькое`;
      case "less_than_equal": case "less_than": return `${where}число слишком большое`;
      case "int_parsing": case "int_type": case "float_parsing": return `${where}нужно число`;
      default: return field ? `Проверьте поле «${field}»` : "Проверьте, всё ли заполнено";
    }
  }

  async function api(path, { method = "GET", body, form } = {}) {
    // X-Peremena — защита веб-кабинета от CSRF: чужой сайт не может добавить заголовок к запросу
    const headers = { "X-Peremena": "1" };
    if (WA?.initData) headers["X-Max-Init-Data"] = WA.initData;
    let url = path;
    if (!WA?.initData && DEV_USER) url += (url.includes("?") ? "&" : "?") + "dev_user=" + encodeURIComponent(DEV_USER);
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let res;
    try {
      res = await fetch(url, { method, headers, body: payload });
    } catch (_) {
      // «Failed to fetch»: запрос не дошёл до сервера — сервис остановлен или нет сети
      throw new Error("Нет связи с «Переменой». Проверьте интернет и попробуйте ещё раз.");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* пустой ответ */ }
    if (!res.ok) {
      const msg = humanError(data?.detail)
        || (res.status === 429 ? "Слишком часто. Подождите немного и попробуйте снова."
          : res.status >= 500 ? "На сервере что-то пошло не так. Попробуйте ещё раз через минуту."
            : res.status === 404 ? "Не нашлось — возможно, это уже удалили." : "Не получилось. Попробуйте ещё раз.");
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function render(html) {
    // в браузере нет системной кнопки «Назад» мессенджера — рисуем свою
    const back = !IN_MAX && S.stack.length ? '<button class="back" data-act="goBack"><svg class="i" aria-hidden="true"><use href="#i-back"/></svg>Назад</button>' : "";
    $view.innerHTML = back + html;
    window.scrollTo(0, 0);
  }
  function loading() {
    $view.innerHTML = '<div class="skeleton" aria-busy="true" aria-label="Загружаю"><i></i><i></i><i></i></div>';
  }
  function fail(e) {
    render(`<div class="empty"><div class="empty-ico warn" aria-hidden="true">!</div><b>Не получилось загрузить</b>${esc(e.message || e)}
      <div><button class="btn outline" data-act="retry" style="margin-top:16px">Попробовать ещё раз</button></div></div>`);
  }

  // ------------------------------------------------------------------ навигация
  const PARENT_TABS = [["my", "list", "Сборы"], ["board", "board", "Доска"], ["ask", "ask", "Спросить"], ["shelf", "folder", "Материалы"]];

  // вкладки собираются из ролей: классный руководитель, предметник, администратор, родитель
  function tabsFor(me) {
    const t = [];
    if (me.is_teacher) t.push(["polls", "list", "Сборы"]);
    if (me.is_teacher || me.is_subject_teacher) t.push(["board", "board", "Доска"]);
    if (me.is_teacher) t.push(["faq", "book", "Справочник"]);
    if (me.is_teacher || me.is_subject_teacher) t.push(["shelf", "folder", "Полка"]);
    // классному руководителю «Школа» открывается из «Ещё» — внизу не больше пяти вкладок, иначе подписи не помещаются на телефоне
    if (!me.is_teacher && (me.is_admin || me.is_subject_teacher)) t.push(["school", "school", "Школа"]);
    if (me.is_teacher) t.push(["more", "more", "Ещё"]);
    return t.length ? t : PARENT_TABS;
  }
  // нет ни одной роли и ни одного класса — человек только что открыл бота: сначала объясняем, что делать
  const isNewcomer = (me) => !me.is_teacher && !me.is_subject_teacher && !me.is_admin && !me.member_chats.length && !me.demo;

  function drawTabs(active) {
    if (isNewcomer(S.me)) { $tabbar.hidden = true; document.body.classList.add("no-tabs"); return; }
    document.body.classList.remove("no-tabs");
    const tabs = tabsFor(S.me);
    $tabbar.hidden = false;
    $tabbar.innerHTML = tabs.map(([k, ico, label]) =>
      `<button type="button" data-tab="${k}" class="${k === active ? "on" : ""}"${k === active ? ' aria-current="page"' : ""}><svg class="ico" aria-hidden="true"><use href="#i-${ico}"/></svg><span>${label}</span></button>`).join("");
  }
  $tabbar.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) { S.stack = []; go(b.dataset.tab, {}, false); }
  });

  const TAB_OF = { pollDetail: "polls", newPoll: "polls", campaign: "school", newCampaign: "school" };

  function go(name, args = {}, push = true) {
    if (push && S.route) S.stack.push(S.route);
    S.route = { name, args };
    const tab = TAB_OF[name] || name;
    drawTabs(tab);
    syncBack();
    (VIEWS[name] || VIEWS[tabsFor(S.me)[0][0]])(args);
  }
  function back() {
    const prev = S.stack.pop();
    if (!prev) return;
    S.route = prev;
    drawTabs(TAB_OF[prev.name] || prev.name);
    syncBack();
    VIEWS[prev.name](prev.args);
  }
  function syncBack() {
    if (!IN_MAX) return;
    try {
      if (S.stack.length) WA?.BackButton?.show(); else WA?.BackButton?.hide();
    } catch (_) { /* старые версии MAX */ }
  }
  function bindMaxBack() {
    if (!IN_MAX) return;
    try { WA?.BackButton?.onClick(back); } catch (_) { /* старые версии MAX */ }
  }

  // сбор денег: галочка «сдал» у участника — учитель подтверждает получение
  $view.addEventListener("change", async (e) => {
    const box = e.target.closest("[data-paid]");
    if (!box || S.route?.name !== "pollDetail") return;
    try {
      await api(`/api/polls/${S.route.args.id}/paid`, { method: "POST", body: { target_id: Number(box.dataset.paid), paid: box.checked } });
      haptic("success");
      VIEWS.pollDetail(S.route.args);
    } catch (err) { toast(err.message, true); box.checked = !box.checked; }
  });

  // делегирование кликов по data-act
  const ACTIONS = {};
  $view.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const fn = ACTIONS[el.dataset.act];
    if (fn) { e.preventDefault(); fn(el, e); }
  });
  // поиск и проверка — настоящие формы: Enter и кнопка «Найти» на клавиатуре телефона работают сами
  $view.addEventListener("submit", (e) => {
    const f = e.target.closest("form[data-form]");
    if (!f) return;
    e.preventDefault();
    ACTIONS[f.dataset.form]?.(f.querySelector("[type=submit]"), e);
  });

  // ------------------------------------------------------------------ модальное окно
  function modal(html, onSubmit) {
    $modalBody.innerHTML = html;
    $modalBody.querySelector(".modal-title")?.setAttribute("id", "modal-title");
    $modalBody.onsubmit = async (e) => {
      const action = e.submitter?.value;
      if (action === "cancel") return;
      e.preventDefault();
      try {
        const keep = await onSubmit(new FormData($modalBody), action);
        if (keep !== true) $modal.close();
      } catch (err) { toast(err.message, true); }
    };
    if (!$modal.open) $modal.showModal();
  }

  // ================================================================== УЧИТЕЛЬ
  const deadlineChip = (p) => p.closed ? '<span class="chip done">закрыт</span>'
    : p.deadline ? `<span class="chip accent">до ${esc(p.deadline_text)}</span>` : '<span class="chip">без срока</span>';

  const pollCard = (p) => {
    const pct = p.total ? Math.round((100 * p.answered) / p.total) : 0;
    const status = !p.total ? "в списке пока никого"
      : p.missing.length ? `Ответили <b>${p.answered}</b> из ${p.total}` : '<span class="ok">Ответили все</span>';
    return `<button type="button" class="card tap poll-card" data-act="openPoll" data-id="${p.id}">
      <span class="card-head"><span class="card-title">${esc(p.title)}</span>${deadlineChip(p)}</span>
      <span class="muted small">${esc(p.chat_title)}</span>
      <span class="row spread poll-nums"><span class="big sm">${p.answered}<small>/${p.total}</small></span>
      <span class="muted small poll-meta">${p.money ? `<span class="money-line">${rub(p.money.collected)} из ${rub(p.money.expected)}</span>` : ""}<span>${status}</span></span></span>
      <span class="bar" role="img" aria-label="Ответили ${p.answered} из ${p.total}"><i style="width:${pct}%"></i></span>
    </button>`;
  };

  // одна кнопка главного действия раздела: на телефоне — плавающая внизу, на широком экране — в шапке раздела
  // «Метрики пилота» из анкеты проекта: считаются по данным самого бота, цель отмечена на полосе
  function pilotCard(pl) {
    if (!pl || typeof pl !== "object") return "";
    const num = (v) => (typeof v === "number" && isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : null);
    const row = (label, pct, goal, note) => {
      if (pct === null) return "";
      const g = num(goal);
      const reached = g !== null && pct >= g;
      return `<div class="pilot-row">
        <div class="row spread"><span>${label}</span><b class="${reached ? "ok" : ""}">${pct}%</b></div>
        <span class="bar pilot-bar" role="img" aria-label="${label}: ${pct}%${g !== null ? `, цель ${g}%` : ""}"><i style="width:${pct}%"></i>${g !== null ? `<s style="left:${g}%"></s>` : ""}</span>
        <div class="muted small">${g !== null ? `Цель пилота не меньше ${g}%. ${reached ? "Цель достигнута." : "Пока ниже цели."}` : ""}${note ? ` ${note}` : ""}</div>
      </div>`;
    };
    const asked = (pl.answered_by_bot || 0) + (pl.forwarded || 0);
    const rows = row("Ответили до срока", num(pl.on_time_pct), pl.on_time_goal,
      pl.polls_closed ? `По ${pl.polls_closed} ${plural(pl.polls_closed, "закрытому сбору", "закрытым сборам", "закрытым сборам")}.` : "")
      + row("Вопросы, закрытые без вас", num(pl.self_pct), pl.self_goal,
        asked ? `Бот ответил сам на ${pl.answered_by_bot || 0} из ${asked}.` : "");
    if (!rows) return "";
    return `<div class="card pilot"><div class="card-title">Метрики пилота</div>
      <p class="muted small">Считаются по тому, что видит сам бот: ответам на сборы и вопросам родителей. Опросов и ручного подсчёта здесь нет.</p>${rows}</div>`;
  }

  const pageHead = (title, action, tag = "h1") => `<div class="page-head ${tag}"><${tag}>${title}</${tag}>${action || ""}</div>`;

  // кнопка-сегмент («Согласие», «Пятница 18:00»): состояние озвучивается экранным чтецам через aria-pressed
  const segBtn = (act, key, label, on, attr = "k") =>
    `<button type="button" data-act="${act}" data-${attr}="${key}" class="${on ? "on" : ""}" aria-pressed="${on}">${label}</button>`;

  // подсказка шаблона «Например: «…» Сумму спрошу следующим шагом.» → только сам пример, без кавычек и шагов бота
  const example = (hint) => { const m = /«([^»]+)»/.exec(hint || ""); return m ? m[1] : (hint || "").replace(/^Например:\s*/, ""); };

  // ошибка у конкретного поля: подсвечиваем его, ставим фокус и говорим, что не так
  function invalid(id, msg) {
    const el = document.getElementById(id);
    if (el) {
      el.setAttribute("aria-invalid", "true");
      el.focus();
      el.addEventListener("input", () => el.removeAttribute("aria-invalid"), { once: true });
    }
    toast(msg, true);
  }

  const noClassesHtml = () => `<div class="empty"><div class="empty-ico" aria-hidden="true"><svg><use href="#i-school"/></svg></div><b>Пока нет ни одного класса</b>
    Добавьте бота «Перемена» в чат класса в MAX и назначьте его администратором чата — тогда он увидит родителей, и сюда можно будет создавать сборы.</div>`;

  const VIEWS = {
    async polls() {
      loading();
      try {
        const [items, att] = await Promise.all([api("/api/polls"), api("/api/attendance")]);
        const open = items.filter((p) => !p.closed), closed = items.filter((p) => p.closed);
        const absent = att.items.length
          ? `<div class="card absent"><div class="card-head"><div class="card-title">Сегодня не придут: ${att.items.length}</div><span class="muted small">${esc(att.day_text)}</span></div>
              <ul class="list">${att.items.map((a) => `<li><span class="name">${esc(a.who)}</span><span class="muted">${esc(a.reason_text)}${a.comment ? " · " + esc(a.comment) : ""}</span></li>`).join("")}</ul></div>`
          : `<div class="card absent quiet-note small muted">Сегодня все на месте: никто из родителей не отметил, что ребёнок не придёт.</div>`;
        render(`${pageHead("Сборы", '<button type="button" class="btn head-action" data-act="newPoll"><svg class="i" aria-hidden="true"><use href="#i-plus"/></svg>Новый сбор</button>')}
          ${absent}
          ${items.length ? "" : `<div class="empty"><div class="empty-ico" aria-hidden="true"><svg><use href="#i-list"/></svg></div><b>Сборов пока нет</b>Согласия, справки, деньги на экскурсию: бот пришлёт каждому родителю кнопки в личку и сам посчитает, кто ответил.
            <div><button type="button" class="btn" data-act="newPoll" style="margin-top:16px">Создать первый сбор</button></div></div>`}
          ${open.map(pollCard).join("")}
          ${closed.length ? `<h2 class="section-title">Завершённые</h2>${closed.map(pollCard).join("")}` : ""}`);
      } catch (e) { fail(e); }
    },

    async pollDetail({ id }) {
      loading();
      try {
        const p = await api(`/api/polls/${id}`);
        const pct = p.total ? Math.round((100 * p.answered) / p.total) : 0;
        const byOpt = p.options.map((o, i) => {
          const names = p.targets.filter((t) => t.answer === i).map((t) => t.name);
          return `<li><span>${esc(o)}</span><b>${names.length}</b></li>`;
        }).join("");
        const answeredLine = p.answered
          ? `<div class="ok small">Ответили ${p.answered} из ${p.total}</div>`
          : `<div class="muted small">Пока никто не ответил${p.closed ? "" : " — бот напомнит сам за сутки и за 2 часа до срока"}</div>`;
        render(`
          <div class="card">
            <div class="card-head"><h1 class="card-title lg">${esc(p.title)}</h1>${deadlineChip(p)}</div>
            <div class="muted small">${esc(p.chat_title)}</div>
            <div class="big" style="margin-top:18px">${p.answered}<small>/${p.total}</small></div>
            <div class="bar" role="img" aria-label="Ответили ${p.answered} из ${p.total}"><i style="width:${pct}%"></i></div>
            ${answeredLine}
            ${p.missing.length ? `<h2>Не ответили · ${p.missing.length}</h2><div class="names">${p.missing.map((n) => `<span>${esc(n)}</span>`).join("")}</div>`
              : p.total ? '<h2 class="ok">Ответили все</h2>' : '<p class="muted small">В списке класса пока никого. Назначьте бота администратором чата класса — тогда он увидит родителей и разошлёт им сбор.</p>'}
            ${p.closed
              // у закрытого сбора «Напомнить» не нужно, а список — нужен: кто что ответил, для отчёта
              ? '<div class="btns one"><button type="button" class="btn outline" data-act="toggleList" aria-controls="fullList" aria-expanded="false">Кто что ответил</button></div>'
              : `<div class="btns">
              <button type="button" class="btn" data-act="remind" data-id="${p.id}" ${p.missing.length ? "" : "disabled"}>Напомнить${p.missing.length ? " " + p.missing.length : ""}</button>
              <button type="button" class="btn outline" data-act="toggleList" aria-controls="fullList" aria-expanded="false">Кто что ответил</button></div>`}
          </div>
          ${p.money ? `<div class="card money">
            <div class="card-title">Собрано ${rub(p.money.collected)} из ${rub(p.money.expected)}</div>
            <div class="muted small">По ${rub(p.money.amount)} с каждого</div>
            <div class="bar money-bar"><i style="width:${p.money.expected ? Math.round(100 * p.money.collected / p.money.expected) : 0}%"></i></div>
            <p class="muted small">Подтверждено ${p.money.paid} из ${p.money.participants}. Отметьте, кто сдал, — родитель получит подтверждение.</p>
            <ul class="list">${p.targets.filter((t) => t.answer !== 2).map((t) => `<li class="paid-li"><label class="paid-row"><input type="checkbox" data-paid="${t.id}" ${t.paid ? "checked" : ""}><span class="name">${esc(t.name)}</span></label><span class="muted small">${t.answer === 0 ? "перевёл" : t.answer === 1 ? "сдаст наличными" : t.answer == null ? "не ответил" : esc(p.options[t.answer])}</span></li>`).join("")}</ul>
          </div>` : ""}
          <div class="card"><div class="card-title">Ответы</div><ul class="list">${byOpt}</ul></div>
          <div class="card" id="fullList" hidden>
            <div class="card-title">Кто что ответил</div>
            <ul class="list">${p.targets.map((t) => `<li><span class="name">${esc(t.name)}</span><span class="${t.answer == null ? "muted" : ""}">${t.answer == null ? (t.delivered ? "не ответил" : "ещё не открывал бота") : esc(p.options[t.answer])}</span></li>`).join("")}</ul>
          </div>
          <div class="card"><div class="card-title">Текст сбора</div><div class="bubble" style="margin-top:8px">${esc(p.text)}</div></div>
          <div class="row actions-row">
            <button type="button" class="btn ghost" data-act="export" data-url="${esc(p.export_url)}" data-id="${p.id}"><svg class="i" aria-hidden="true"><use href="#i-download"/></svg>Скачать таблицу</button>
            <button type="button" class="btn ghost" data-act="print" data-kind="sheet" data-id="${p.id}">Ведомость для подписи</button>
            <button type="button" class="btn ghost" data-act="print" data-kind="${p.missing.length ? "blanks_missing" : "blanks"}" data-id="${p.id}">Бланки с QR${p.missing.length ? " для " + p.missing.length : ""}</button>
            ${p.closed ? `<button type="button" class="btn danger" data-act="deletePoll" data-id="${p.id}"><svg class="i" aria-hidden="true"><use href="#i-trash"/></svg>Удалить сбор</button>`
              : `<button type="button" class="btn danger" data-act="closePoll" data-id="${p.id}">Закрыть сбор</button>`}
          </div>`);
      } catch (e) { fail(e); }
    },

    async newPoll() {
      loading();
      try {
        S.templates = S.templates || await api("/api/templates");
        const chats = S.me.chats;
        if (!chats.length) { render(noClassesHtml()); return; }
        const draft = { template: "consent", deadline: "friday" };
        const draw = () => {
          const tpl = S.templates.find((t) => t.key === draft.template);
          render(`<h1>Новый сбор</h1>
            <label class="field"><span>Класс</span><select id="f-chat">${chats.map((c) => `<option value="${c.chat_id}">${esc(c.title || c.chat_id)}</option>`).join("")}</select></label>
            <fieldset class="field"><legend>Что собираем</legend>
              <div class="seg">${S.templates.map((t) => segBtn("tpl", t.key, esc(t.label), t.key === draft.template, "key")).join("")}</div></fieldset>
            <label class="field"><span>Текст сообщения <em class="hint">первая строка станет заголовком</em></span>
              <textarea id="f-text" maxlength="3000" placeholder="${esc(example(tpl.hint))}">${esc(draft.text || "")}</textarea></label>
            ${draft.template === "choice"
              ? `<label class="field"><span>Варианты ответа <em class="hint">каждый с новой строки, от 2 до 10</em></span><textarea id="f-opts" style="min-height:90px" placeholder="Вторник, 18:00&#10;Четверг, 18:30">${esc(draft.opts || "")}</textarea></label>`
              : `<p class="muted small buttons-preview">Родители увидят кнопки: ${tpl.options.map((o) => `<span class="chip">${esc(o)}</span>`).join(" ")}</p>`}
            ${draft.template === "money" ? `<label class="field"><span>Сумма с каждого, ₽</span><input type="number" id="f-amount" min="1" max="100000" inputmode="numeric" value="${esc(draft.amount || "")}" placeholder="300"></label>` : ""}
            <fieldset class="field"><legend>Срок ответа</legend>
              <div class="seg">${[["today", "Сегодня 18:00"], ["tomorrow", "Завтра 18:00"], ["friday", "Пятница 18:00"], ["week", "Через неделю"], ["custom", "Своя дата"], ["", "Без срока"]]
                .map(([k, l]) => segBtn("dl", k, l, k === draft.deadline, "key")).join("")}</div>
              ${draft.deadline === "custom" ? `<input type="datetime-local" id="f-dt" aria-label="Дата и время срока" style="margin-top:10px" value="${esc(draft.dt || "")}">` : ""}</fieldset>
            <button type="button" class="btn block" data-act="sendPoll">${say("Разослать родителям", "Разослать (в демо никому не уйдёт)")}</button>
            <p class="muted small">Каждый родитель из чата получит сообщение с кнопками в личку, а в чат класса уйдёт копия. Тем, кто молчит, бот сам напомнит за сутки и за 2 часа до срока.</p>`);
          const sel = document.getElementById("f-chat");
          if (draft.chat_id) sel.value = draft.chat_id;
        };
        const save = () => {
          draft.chat_id = document.getElementById("f-chat")?.value;
          draft.text = document.getElementById("f-text")?.value;
          draft.opts = document.getElementById("f-opts")?.value ?? draft.opts;
          draft.amount = document.getElementById("f-amount")?.value ?? draft.amount;
          draft.dt = document.getElementById("f-dt")?.value ?? draft.dt;
        };
        ACTIONS.tpl = (el) => { save(); draft.template = el.dataset.key; draw(); };
        ACTIONS.dl = (el) => { save(); draft.deadline = el.dataset.key; draw(); };
        ACTIONS.sendPoll = async (el) => {
          save();
          const label = el.textContent;
          if (!draft.text || draft.text.trim().length < 3) return invalid("f-text", "Напишите текст сбора — хотя бы пару слов");
          const options = draft.template === "choice" ? (draft.opts || "").split("\n").map((s) => s.trim()).filter(Boolean) : [];
          if (draft.template === "choice" && (options.length < 2 || options.length > 10)) return invalid("f-opts", "Нужно от 2 до 10 вариантов, каждый с новой строки");
          const amount = draft.template === "money" ? Number(draft.amount) : null;
          if (draft.template === "money" && !(amount >= 1 && amount <= 100000)) return invalid("f-amount", "Укажите сумму с каждого: от 1 до 100 000 ₽");
          let deadline = draft.deadline || null;
          if (deadline === "custom") { if (!draft.dt) return invalid("f-dt", "Выберите дату и время срока"); deadline = draft.dt; }
          el.disabled = true; el.textContent = "Рассылаю…";
          try {
            const res = await api("/api/polls", { method: "POST", body: { chat_id: Number(draft.chat_id), template: draft.template, text: draft.text, options, deadline, amount } });
            haptic("success");
            const r = res.report;
            toast(say(`Отправлено в личку: ${r.delivered} из ${r.total}`, "Сбор создан. В настоящем классе родители получили бы его в личку"));
            S.stack = [{ name: "polls", args: {} }];
            S.route = null;
            go("pollDetail", { id: res.poll.id }, false);
            S.stack = [{ name: "polls", args: {} }];
            syncBack();
          } catch (e) { toast(e.message, true); el.disabled = false; el.textContent = label; }
        };
        draw();
      } catch (e) { fail(e); }
    },

    async faq() {
      loading();
      try {
        const [items, openQ] = await Promise.all([api("/api/faq"), api("/api/faq/open")]);
        S.openQ = openQ;
        const openCard = openQ.length ? `<div class="card faq open-q">
            <div class="card-title">Спрашивают, а ответа в справочнике нет</div>
            <p class="muted small">Ответьте один раз, и дальше бот будет отвечать на такие вопросы сам.</p>
            <ul class="list">${openQ.map((q) => `<li><span>${esc(q.question)} <span class="chip" title="Сколько раз спросили">${q.count} ${plural(q.count, "раз", "раза", "раз")}</span></span><button type="button" class="btn sm" data-act="openAnswer" data-key="${esc(q.key)}">Ответить</button></li>`).join("")}</ul></div>` : "";
        render(`${pageHead("Справочник", '<button type="button" class="btn head-action" data-act="editFaq"><svg class="i" aria-hidden="true"><use href="#i-plus"/></svg>Добавить ответ</button>')}
          <p class="muted lead-in">Здесь ответы на частые вопросы родителей. Бот отвечает ими сам, в любое время, и подписывает, что ответ сохранили вы.</p>
          ${openCard}
          <div class="card faq">
            <label class="card-title tag-faq" for="faq-test">Проверить, как ответит бот</label>
            <form class="row search-row" data-form="testFaq" style="margin-top:8px"><input type="search" id="faq-test" placeholder="Например: во сколько собрание?" enterkeyhint="search"><button type="submit" class="btn sm">Проверить</button></form>
            <div id="faq-test-out" aria-live="polite"></div>
          </div>
          ${items.length ? "" : '<div class="empty"><div class="empty-ico" aria-hidden="true"><svg><use href="#i-book"/></svg></div><b>Справочник пока пуст</b>Добавьте вопросы, которые родители задают чаще всего: время собрания, форма, где взять справку.</div>'}
          ${items.map((e) => `<button type="button" class="card faq tap" data-act="editFaq" data-id="${e.id}">
            <span class="card-title">${esc(e.question)}</span>
            <span class="faq-answer">${esc(e.answer)}</span>
            <span class="row chips">${e.shared ? '<span class="chip accent">база школы</span>' : ""}<span class="chip">ответил ${e.hits} ${plural(e.hits, "раз", "раза", "раз")}</span>${e.misses ? `<span class="chip">«не то»: ${e.misses}</span>` : ""}${e.keywords ? `<span class="muted small">ключевые слова: ${esc(e.keywords)}</span>` : ""}</span>
          </button>`).join("")}`);
        S.faq = items;
      } catch (e) { fail(e); }
    },

    async shelf() {
      loading();
      const teacher = S.me.is_teacher || S.me.is_subject_teacher;
      render(`<h1>${teacher ? "Полка материалов" : "Материалы класса"}</h1>
        <form class="row search-row" data-form="shelfSearch" role="search" style="margin-bottom:12px"><input type="search" id="shelf-q" aria-label="Поиск по материалам" placeholder="Памятка, экскурсия, бланк…" enterkeyhint="search" value="${esc(S.shelfQ || "")}"><button type="submit" class="btn sm">Найти</button></form>
        ${teacher ? (isDemo()
          ? '<p class="muted small">В демо файлы не загружаются. В настоящем боте достаточно переслать файл боту в личку с подписью — он сам положит его на полку.</p>'
          : `<label class="btn outline block upload"><svg class="i" aria-hidden="true"><use href="#i-upload"/></svg>Загрузить файл<input type="file" id="shelf-file" class="sr-only"></label>
          <p class="muted small">Или просто перешлите файл боту в личку с подписью — он сам положит его на полку.</p>`) : ""}
        <div id="shelf-list" aria-live="polite"><div class="skeleton" aria-busy="true"><i></i><i></i></div></div>`);
      const file = document.getElementById("shelf-file");
      if (file) file.addEventListener("change", () => uploadFile(file.files[0]));
      await loadShelf();
    },

    async more() {
      loading();
      try {
        const [st, inbox] = await Promise.all([api("/api/stats"), api("/api/inbox")]);
        const s = S.me.settings;
        const tzs = [["Europe/Kaliningrad", "Калининград"], ["Europe/Moscow", "Москва"], ["Europe/Samara", "Самара"], ["Asia/Yekaterinburg", "Екатеринбург"], ["Asia/Omsk", "Омск"], ["Asia/Novosibirsk", "Новосибирск"], ["Asia/Krasnoyarsk", "Красноярск"], ["Asia/Irkutsk", "Иркутск"], ["Asia/Yakutsk", "Якутск"], ["Asia/Vladivostok", "Владивосток"], ["Asia/Magadan", "Магадан"], ["Asia/Kamchatka", "Камчатка"]];
        const kinds = Object.entries(st.labels).map(([k, label]) => {
          const v = st.total.by_kind[k];
          return v ? `<li><span>${esc(label)}: <b>${v.count}</b></span><span class="muted">≈ ${fmtMinutes(v.minutes)}</span></li>` : "";
        }).join("");
        const pending = inbox.filter((i) => !i.delivered);
        const pilotHtml = pilotCard(st.pilot);
        render(`<h1>Польза и настройки</h1>
          <button type="button" class="btn ghost block link-card" data-act="goSchool"><svg class="i" aria-hidden="true"><use href="#i-school"/></svg><span>${S.me.school ? esc(S.me.school.name) : "Школа: общая база ответов и сводка"}</span><svg class="i chev" aria-hidden="true"><use href="#i-chev"/></svg></button>
          <button type="button" class="btn ghost block link-card" data-act="goIntegrations"><svg class="i" aria-hidden="true"><use href="#i-list"/></svg><span>Интеграции: календарь, API, вебхуки</span><svg class="i chev" aria-hidden="true"><use href="#i-chev"/></svg></button>
          <div class="card">
            <div class="overline">Бот сэкономил вам за 7 дней</div>
            <div class="stat-big" style="margin:8px 0">${fmtMinutes(st.week.minutes)}</div>
            <div class="muted">за всё время — <b>${fmtMinutes(st.total.minutes)}</b></div>
            ${kinds ? `<ul class="list" style="margin-top:12px">${kinds}</ul>` : '<p class="muted small">Счётчик начнёт расти после первого сбора и первых ответов из справочника.</p>'}
          </div>
          ${pilotHtml}

          <div class="card quiet">
            <div class="card-title tag-quiet">Тихий режим</div>
            <p class="muted small">Вне рабочего времени бот продолжает отвечать родителям тем, что знает, а всё новое приносит вам утром одной карточкой. Если в сообщении есть слово «срочно» или другое слово из списка ниже — передаст сразу.</p>
            <label class="switch"><span>Тихий режим</span><input type="checkbox" id="s-quiet" ${s.quiet_enabled ? "checked" : ""}></label>
            <div class="row" style="margin:10px 0">
              <label class="field" style="flex:1;margin:0"><span>Начало дня</span><input type="time" id="s-start" value="${mins(s.work_start)}"></label>
              <label class="field" style="flex:1;margin:0"><span>Конец дня</span><input type="time" id="s-end" value="${mins(s.work_end)}"></label>
            </div>
            <label class="switch"><span>Выходные — не беспокоить</span><input type="checkbox" id="s-we" ${s.weekends_off ? "checked" : ""}></label>
            <label class="switch"><span>Предлагать сохранять мои ответы из чата</span><input type="checkbox" id="s-sugg" ${s.suggest_save ? "checked" : ""}></label>
            <label class="field" style="margin-top:10px"><span>Часовой пояс</span><select id="s-tz">${tzs.map(([v, l]) => `<option value="${v}" ${v === s.tz ? "selected" : ""}>${l}</option>`).join("")}</select></label>
            <label class="field"><span>Слова, по которым сообщение срочное <em class="hint">через запятую</em></span><input type="text" id="s-urgent" value="${esc(s.urgent_words)}"></label>
            <button type="button" class="btn block" data-act="saveSettings">Сохранить настройки</button>
          </div>

          <div class="card">
            <div class="card-title">Классы</div>
            ${S.me.chats.map((c) => `<div class="class-row">
              <div class="card-head"><b class="name">${esc(c.title || c.chat_id)}</b><span class="chip ${c.bot_is_admin ? "done" : "warn"}">${c.bot_is_admin ? "бот видит список класса" : "бот не видит список"}</span></div>
              ${c.bot_is_admin ? "" : '<p class="muted small">Назначьте бота администратором чата класса в MAX — тогда он увидит, кто в классе, и сможет рассылать сборы.</p>'}
              <label class="switch"><span>Отвечать на вопросы прямо в чате класса</span><input type="checkbox" data-chat-auto="${c.chat_id}" ${c.autoanswer ? "checked" : ""}></label>
              <div class="row tools"><button type="button" class="btn sm ghost" data-act="syncChat" data-id="${c.chat_id}">Обновить список класса</button><button type="button" class="btn sm ghost" data-act="history" data-id="${c.chat_id}">Собрать файлы из чата</button><button type="button" class="btn sm ghost" data-act="invite" data-id="${c.chat_id}">Пригласить учителя-предметника</button></div>
            </div>`).join("")}
          </div>

          <div class="card">
            <div class="card-title">Входящие${pending.length ? ` <span class="chip accent">ждут утра: ${pending.length}</span>` : ""}</div>
            ${inbox.length ? `<ul class="list">${inbox.slice(0, 20).map((i) => `<li class="inbox-li"><div class="muted small">${esc(i.time)} · ${esc(i.label)}${i.from_name ? " · " + esc(i.from_name) : ""}${i.delivered ? "" : " · придёт утром"}</div>${esc(i.text)}</li>`).join("")}</ul>` : '<p class="muted small">Пусто. Сюда попадает то, что пришло вне рабочего времени.</p>'}
          </div>`);
        $view.querySelectorAll("[data-chat-auto]").forEach((el) => el.addEventListener("change", async () => {
          try {
            await api(`/api/chats/${el.dataset.chatAuto}`, { method: "PUT", body: { autoanswer: el.checked } });
            const c = S.me.chats.find((x) => String(x.chat_id) === el.dataset.chatAuto); if (c) c.autoanswer = el.checked;
            toast(el.checked ? "Автоответ включён" : "Автоответ выключен");
          } catch (e) { toast(e.message, true); el.checked = !el.checked; }
        }));
      } catch (e) { fail(e); }
    },

    // ================================================================ РОДИТЕЛЬ
    async my() {
      loading();
      try {
        const [items, abs, wk] = await Promise.all([api("/api/my/polls"), api("/api/my/absences"), api("/api/my/week").catch(() => null)]);
        S.myAbs = abs;
        const marks = abs.items.length ? `<div class="card absent"><div class="card-title">Вы отметили, что ребёнок не придёт</div>
            <ul class="list">${abs.items.map((a) => `<li><span>${esc(a.who)} — ${esc(a.day_text)}, ${esc(a.reason_text)}</span><button type="button" class="btn sm ghost" data-act="absCancel" data-id="${a.id}">Отменить</button></li>`).join("")}</ul></div>` : "";
        const empty = S.me.member_chats.length
          ? `<div class="empty"><div class="empty-ico ok" aria-hidden="true"><svg><use href="#i-check"/></svg></div><b>Новых сборов нет</b>Когда учитель попросит согласие, справку или деньги на экскурсию, это появится здесь${say(" и придёт вам в личку с ботом", "")}.</div>`
          : `<div class="empty"><b>Пока здесь пусто</b>Сборы появятся, когда учитель подключит бота к чату вашего класса.</div>`;
        render(`<h1>Сборы</h1>
          ${abs.classes.length ? `<button type="button" class="btn outline block" data-act="absNew" style="margin-bottom:12px">Ребёнок не придёт в школу</button>` : ""}
          ${marks}
          ${wk ? weekCard(wk) : ""}
          ${items.length ? `<p class="muted lead-in">Учитель ждёт вашего ответа. Нажмите подходящую кнопку — ответ сразу увидит учитель.</p>` : empty}
          ${items.map((p) => {
            const body = p.text.split("\n").slice(1).join("\n").trim();
            return `<div class="card">
            <div class="card-head"><span class="muted small">${esc(p.chat_title)}${p.teacher ? " · " + esc(p.teacher) : ""}</span>${p.deadline ? `<span class="chip accent">до ${esc(p.deadline_text)}</span>` : ""}</div>
            <div class="bubble"><b>${esc(p.title)}</b>${body ? "\n" + esc(body) : ""}</div>
            ${p.amount ? `<div class="amount"><span class="muted">Сумма</span><b>${rub(p.amount)}</b></div>` : ""}
            <div class="options" role="group" aria-label="Ваш ответ">${p.options.map((o, i) => `<button type="button" class="option ${p.my_answer === i ? "selected" : ""}" aria-pressed="${p.my_answer === i}" data-act="answer" data-id="${p.id}" data-opt="${i}">${esc(o)}</button>`).join("")}</div>
          </div>`;
          }).join("")}`);
      } catch (e) { fail(e); }
    },

    async ask() {
      render(`<h1>Спросить</h1>
        <p class="muted lead-in">Отвечу тем, что сохранил учитель. Если готового ответа нет — передам вопрос ему.</p>
        <form data-form="ask">
          <label class="sr-only" for="ask-q">Ваш вопрос</label>
          <textarea id="ask-q" placeholder="Например: во сколько собрание?" style="min-height:88px" maxlength="500"></textarea>
          <button type="submit" class="btn block" style="margin-top:10px">Спросить</button>
        </form>
        <div id="ask-out" style="margin-top:14px" aria-live="polite"></div>`);
    },

    // первый экран для того, у кого ещё нет ни класса, ни роли
    async welcome() {
      const bot = S.me.bot_link;
      render(`<h1>Здравствуйте${S.me.user.first_name ? ", " + esc(S.me.user.first_name) : ""}!</h1>
        <p class="muted lead-in">«Перемена» помогает классному руководителю и родителям. Выберите, кто вы.</p>
        <div class="card welcome">
          <div class="card-title">Вы учитель?</div>
          <p class="muted small">Подключите класс — это займёт пару минут.</p>
          <ol class="steps-list">
            <li>Откройте чат класса с родителями в MAX и добавьте туда бота «Перемена».</li>
            <li>Назначьте бота администратором чата — так он увидит, кто в классе.</li>
            <li>Вернитесь сюда: появятся сборы, справочник ответов и доска класса.</li>
          </ol>
          <div class="btns">
            ${bot ? `<a class="btn" href="${esc(bot)}" target="_blank" rel="noopener">Открыть бота в MAX</a>` : ""}
            <button type="button" class="btn outline" data-act="recheck">Я добавил бота — проверить</button>
          </div>
        </div>
        <div class="card welcome">
          <div class="card-title">Вы родитель?</div>
          <p class="muted small">Ничего делать не нужно. Сборы и объявления появятся здесь, когда учитель подключит бота к чату вашего класса.</p>
        </div>`);
    },
  };

  // ================================================================== ДОСКА, ШКОЛА
  // копирование: сначала Clipboard API, если браузер не дал — старый способ через выделение текста
  async function copyText(value) {
    try { await navigator.clipboard.writeText(value); return true; } catch (_) { /* нет доступа к буферу */ }
    const ta = document.createElement("textarea");
    ta.value = value; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  // «Поделиться»: внутри MAX — штатное окно мессенджера, в браузере — копируем ссылку
  async function share(text, link) {
    if (!link) { toast("Ссылка появится, когда бот будет подключён к MAX", true); return; }
    if (IN_MAX && WA?.shareMaxContent) {
      try { WA.shareMaxContent({ text, link }); return; } catch (_) { /* старые версии MAX — копируем */ }
    }
    if (await copyText(link)) { toast("Ссылка скопирована — вставьте её в сообщение"); return; }
    // совсем старый браузер: показываем ссылку выделенной, чтобы скопировать вручную
    modal(`<h2 class="modal-title">Скопируйте ссылку</h2>
      <input type="text" readonly value="${esc(link)}" id="share-link" aria-label="Ссылка">
      <div class="btns one"><button class="btn" value="cancel" formnovalidate>Готово</button></div>`, () => {});
    const inp = document.getElementById("share-link"); inp.focus(); inp.select();
  }

  function boardCard(a, staff) {
    const head = a.kind === "hw" ? esc(a.subject || "Без предмета") : "Объявление";
    const when = a.for_date_text ? `<span class="chip accent">на ${esc(a.for_date_text)}</span>` : "";
    let status = "";
    if (staff) {
      status = a.publish_mode === "silent" ? '<span class="chip">не в чате, только для ответов</span>'
        : a.published ? '<span class="chip done">опубликовано в чате</span>' : `<span class="chip">выйдет ${esc(a.publish_at_text)}</span>`;
    }
    return `<div class="card ${a.kind === "hw" ? "faq" : "quiet"}">
      <div class="card-head"><div class="card-title">${head}</div>${when}</div>
      <div class="muted small">${esc(a.chat_title)} · ${esc(a.date)}</div>
      <div class="pre">${esc(a.text)}</div>
      ${staff ? `<div class="row spread" style="margin-top:10px">${status}${a.can_delete ? `<button type="button" class="btn sm ghost icon-btn" data-act="delBoard" data-id="${a.id}" aria-label="Удалить запись"><svg class="i" aria-hidden="true"><use href="#i-trash"/></svg></button>` : ""}</div>` : ""}
    </div>`;
  }

  function quietNow() {
    const s = S.me.settings;
    if (!s || !s.quiet_enabled) return false;
    const d = new Date(), m = d.getHours() * 60 + d.getMinutes();
    if (s.weekends_off && (d.getDay() === 0 || d.getDay() === 6)) return true;
    return m < s.work_start || m >= s.work_end;
  }

  Object.assign(VIEWS, {
    async board() {
      loading();
      try {
        const staff = S.me.is_teacher || S.me.is_subject_teacher;
        const [items, subjects] = await Promise.all([api("/api/board"), staff ? api("/api/subjects") : []]);
        if (!staff) {
          const hw = items.filter((a) => a.kind === "hw"), news = items.filter((a) => a.kind === "news");
          render(`<h1>Доска класса</h1>
            ${items.length ? "" : '<div class="empty"><div class="empty-ico" aria-hidden="true"><svg><use href="#i-board"/></svg></div><b>Пока пусто</b>Здесь появятся домашние задания и объявления учителей.</div>'}
            ${hw.length ? `<h2>Домашние задания</h2>${hw.map((a) => boardCard(a, false)).join("")}` : ""}
            ${news.length ? `<h2>Объявления</h2>${news.map((a) => boardCard(a, false)).join("")}` : ""}`);
          return;
        }
        const draft = S.boardDraft || (S.boardDraft = { kind: "hw", for_date: "tomorrow", publish: quietNow() ? "later" : "now" });
        const chats = S.me.board_chats;
        const draw = () => {
          const chat = chats.find((c) => String(c.chat_id) === String(draft.chat_id)) || chats[0];
          draft.chat_id = chat.chat_id;
          const subj = draft.subject ?? chat.subject ?? "";
          render(`<h1>Доска класса</h1>
            <div class="card">
              <fieldset class="field"><legend>Что записать</legend><div class="seg">${[["hw", "Домашнее задание"], ["news", "Объявление"]].map(([k, l]) => segBtn("bdKind", k, l, draft.kind === k)).join("")}</div></fieldset>
              <label class="field"><span>Класс</span><select id="bd-chat">${chats.map((c) => `<option value="${c.chat_id}" ${c.chat_id === chat.chat_id ? "selected" : ""}>${esc(c.title)}${c.subject ? " · " + esc(c.subject) : ""}</option>`).join("")}</select></label>
              ${draft.kind === "hw" ? `<label class="field"><span>Предмет</span><input type="text" id="bd-subj" list="bd-subjects" value="${esc(subj)}" placeholder="Алгебра"><datalist id="bd-subjects">${subjects.map((x) => `<option value="${esc(x)}">`).join("")}</datalist></label>` : ""}
              <label class="field"><span>${draft.kind === "hw" ? "Задание" : "Текст объявления"}</span><textarea id="bd-text" placeholder="${draft.kind === "hw" ? "№245, 247; правило на с. 63" : "Завтра субботник, форма свободная"}">${esc(draft.text || "")}</textarea></label>
              ${draft.kind === "hw" ? `<fieldset class="field"><legend>К какому дню</legend><div class="seg">${[["tomorrow", "Завтра"], ["after", "Послезавтра"], ["monday", "К понедельнику"], ["", "Без даты"]].map(([k, l]) => segBtn("bdDate", k, l, draft.for_date === k)).join("")}</div></fieldset>` : ""}
              <fieldset class="field"><legend>Когда показать в чате класса</legend><div class="seg">${[["now", "Сейчас"], ["later", "Утром"], ["silent", "Не публиковать"]].map(([k, l]) => segBtn("bdPub", k, l, draft.publish === k)).join("")}</div>
                <p class="muted small">${quietNow() ? "Сейчас нерабочее время — если опубликовать утром, родителям не придут уведомления ночью. " : ""}В любом случае бот ответит по этой записи на вопрос «что задали?».</p></fieldset>
              <button type="button" class="btn block" data-act="bdSave">Записать</button>
            </div>
            <h2>Последние записи</h2>
            ${items.length ? items.map((a) => boardCard(a, true)).join("") : '<p class="muted">Записей пока нет.</p>'}`);
        };
        const save = () => {
          draft.chat_id = document.getElementById("bd-chat")?.value ?? draft.chat_id;
          const subjEl = document.getElementById("bd-subj");
          if (subjEl) draft.subject = subjEl.value;
          draft.text = document.getElementById("bd-text")?.value ?? draft.text;
        };
        ACTIONS.bdKind = (el) => { save(); draft.kind = el.dataset.k; draw(); };
        ACTIONS.bdDate = (el) => { save(); draft.for_date = el.dataset.k; draw(); };
        ACTIONS.bdPub = (el) => { save(); draft.publish = el.dataset.k; draw(); };
        ACTIONS.bdSave = async (el) => {
          save();
          if (!draft.text || draft.text.trim().length < 2) return invalid("bd-text", draft.kind === "hw" ? "Напишите задание" : "Напишите текст объявления");
          el.disabled = true;
          try {
            const a = await api("/api/board", { method: "POST", body: { chat_id: Number(draft.chat_id), kind: draft.kind,
              subject: draft.subject || "", text: draft.text, for_date: draft.for_date || null, publish_mode: draft.publish } });
            haptic("success");
            toast(a.publish_mode === "silent" ? "Записано. Бот ответит по этой записи"
              : say(a.publish_mode === "later" ? `Записано. Опубликую в чате ${a.publish_at_text}` : "Опубликовано в чате класса",
                "Записано. В настоящем классе запись появилась бы в чате"));
            draft.text = "";
            VIEWS.board();
          } catch (e) { toast(e.message, true); el.disabled = false; }
        };
        const drawAndBind = () => {
          draw();
          // при смене класса подставляем предмет предметника
          document.getElementById("bd-chat").addEventListener("change", () => { save(); draft.subject = undefined; drawAndBind(); });
        };
        ACTIONS.bdKind = (el) => { save(); draft.kind = el.dataset.k; drawAndBind(); };
        ACTIONS.bdDate = (el) => { save(); draft.for_date = el.dataset.k; drawAndBind(); };
        ACTIONS.bdPub = (el) => { save(); draft.publish = el.dataset.k; drawAndBind(); };
        drawAndBind();
      } catch (e) { fail(e); }
    },

    async school() {
      loading();
      const me = S.me;
      if (!me.school) {
        render(`<h1>Школа</h1>
          <p class="muted">Объедините классы школы: общая база ответов (звонки, форма, справки) сразу работает у каждого классного руководителя, а администрация видит сводку и запускает сборы на параллель одной кнопкой.</p>
          <div class="card"><label class="card-title" for="sc-code">Вступить по коду</label>
            <form class="row search-row" data-form="scJoin" style="margin-top:8px"><input type="text" id="sc-code" placeholder="Код от завуча" autocapitalize="characters" autocomplete="off" style="text-transform:uppercase"><button type="submit" class="btn sm">Вступить</button></form></div>
          ${me.is_teacher || me.is_subject_teacher ? `<div class="card"><label class="card-title" for="sc-name">Создать школу</label>
            <p class="muted small">Вы станете администратором и получите код для коллег.</p>
            <form class="row search-row" data-form="scCreate"><input type="text" id="sc-name" placeholder="МБОУ СОШ № 7" maxlength="300"><button type="submit" class="btn sm">Создать</button></form></div>` : ""}`);
        return;
      }
      try {
        if (!me.is_admin) {
          const faqs = await api("/api/school/faq");
          render(`<h1>${esc(me.school.name)}</h1>
            <p class="muted">Общая база ответов школы — бот отвечает по ней родителям всех классов. Добавить карточку в базу можно в «Справочнике».</p>
            ${faqs.map((e) => `<div class="card faq"><div class="card-title">${esc(e.question)}</div><div>${esc(e.answer)}</div></div>`).join("") || '<p class="muted">Пока пусто.</p>'}`);
          return;
        }
        const d = await api("/api/school");
        render(`<h1>${esc(me.school.name)}</h1>
          <div class="card">
            <div class="card-title">Пригласить коллег</div>
            <p class="muted small">Классные руководители вступают по коду <b class="code">${esc(me.school.code)}</b> или по ссылке.</p>
            <div class="row"><button type="button" class="btn sm" data-act="shareLink" data-link="${esc(me.school.teacher_link)}" data-text="Подключитесь к школе в «Перемене»">${IN_MAX ? "Поделиться ссылкой для учителей" : "Скопировать ссылку для учителей"}</button>
            <button type="button" class="btn sm ghost" data-act="shareLink" data-link="${esc(me.school.admin_link)}" data-text="Доступ администратора школы в «Перемене»">Ссылка для администрации</button></div>
          </div>
          ${pageHead("Классы", '<button type="button" class="btn head-action" data-act="newCampaign"><svg class="i" aria-hidden="true"><use href="#i-plus"/></svg>Сбор на параллель</button>', "h2")}
          ${d.classes.length ? d.classes.map((c) => {
            const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
            return `<div class="card"><div class="card-head"><b class="name">${esc(c.title || c.chat_id)}</b>${c.bot_is_admin ? "" : '<span class="chip warn">бот не видит список класса</span>'}</div>
              <div class="muted small">${esc(c.teacher)} · ${c.open_polls ? `${c.open_polls} ${plural(c.open_polls, "открытый сбор", "открытых сбора", "открытых сборов")}` : "открытых сборов нет"}</div>
              ${c.open_polls && c.total ? `<div class="bar" role="img" aria-label="Ответили ${pct}%"><i style="width:${pct}%"></i></div><div class="small">Ответили на ${pct}% запросов${c.open_polls > 1 ? ` по ${c.open_polls} сборам` : ""}</div>` : ""}</div>`;
          }).join("") : '<p class="muted">Пока никто из классных руководителей не вступил в школу. Отправьте им ссылку.</p>'}
          ${d.campaigns.length ? `<h2>Сборы на параллель</h2>${d.campaigns.map((c) => {
            const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
            return `<button type="button" class="card tap poll-card" data-act="openCampaign" data-id="${c.id}"><span class="card-title">${esc(c.title)}</span>
              <span class="row spread poll-nums"><span class="big sm">${c.answered}<small>/${c.total}</small></span><span class="muted small">${c.classes.length} ${plural(c.classes.length, "класс", "класса", "классов")}</span></span>
              <span class="bar"><i style="width:${pct}%"></i></span></button>`;
          }).join("")}` : ""}`);
        S.schoolClasses = d.classes;
      } catch (e) { fail(e); }
    },

    async newCampaign() {
      S.templates = S.templates || await api("/api/templates");
      const classes = S.schoolClasses || (await api("/api/school")).classes;
      const draft = { template: "consent", deadline: "friday", chats: new Set(classes.map((c) => c.chat_id)) };
      const draw = () => {
        const tpl = S.templates.find((t) => t.key === draft.template);
        render(`<h1>Сбор на параллель</h1>
          <p class="muted">Каждый класс получит сбор, который ведёт его классный руководитель: рассылка в личку, напоминания, итог. Вы видите общий результат.</p>
          <div class="card"><div class="card-title">Классы</div>
            ${classes.map((c) => `<label class="switch"><span>${esc(c.title || c.chat_id)} <span class="muted small">${esc(c.teacher)}</span></span><input type="checkbox" data-camp-chat="${c.chat_id}" ${draft.chats.has(c.chat_id) ? "checked" : ""}></label>`).join("")}</div>
          <fieldset class="field"><legend>Что собираем</legend><div class="seg">${S.templates.filter((t) => t.key !== "money").map((t) => segBtn("cmpTpl", t.key, esc(t.label), t.key === draft.template, "key")).join("")}</div></fieldset>
          <label class="field"><span>Текст сообщения <em class="hint">первая строка станет заголовком</em></span><textarea id="cmp-text" maxlength="3000" placeholder="${esc(example(tpl.hint))}">${esc(draft.text || "")}</textarea></label>
          ${draft.template === "choice" ? `<label class="field"><span>Варианты ответа <em class="hint">каждый с новой строки, от 2 до 10</em></span><textarea id="cmp-opts" style="min-height:90px">${esc(draft.opts || "")}</textarea></label>` : `<p class="muted small buttons-preview">Родители увидят кнопки: ${tpl.options.map((o) => `<span class="chip">${esc(o)}</span>`).join(" ")}</p>`}
          <fieldset class="field"><legend>Срок ответа</legend><div class="seg">${[["tomorrow", "Завтра 18:00"], ["friday", "Пятница 18:00"], ["week", "Через неделю"], ["", "Без срока"]].map(([k, l]) => segBtn("cmpDl", k, l, draft.deadline === k)).join("")}</div></fieldset>
          <button type="button" class="btn block" data-act="cmpSend" ${draft.chats.size ? "" : "disabled"}>${draft.chats.size ? `Запустить в ${draft.chats.size} ${plural(draft.chats.size, "классе", "классах", "классах")}` : "Выберите хотя бы один класс"}</button>`);
        $view.querySelectorAll("[data-camp-chat]").forEach((el) => el.addEventListener("change", () => {
          save(); const id = Number(el.dataset.campChat); el.checked ? draft.chats.add(id) : draft.chats.delete(id); draw();
        }));
      };
      const save = () => {
        draft.text = document.getElementById("cmp-text")?.value ?? draft.text;
        draft.opts = document.getElementById("cmp-opts")?.value ?? draft.opts;
      };
      ACTIONS.cmpTpl = (el) => { save(); draft.template = el.dataset.key; draw(); };
      ACTIONS.cmpDl = (el) => { save(); draft.deadline = el.dataset.k; draw(); };
      ACTIONS.cmpSend = async (el) => {
        save();
        if (!draft.chats.size) return toast("Выберите хотя бы один класс", true);
        if (!draft.text || draft.text.trim().length < 3) return invalid("cmp-text", "Напишите текст сбора — хотя бы пару слов");
        const options = draft.template === "choice" ? (draft.opts || "").split("\n").map((s) => s.trim()).filter(Boolean) : [];
        if (draft.template === "choice" && (options.length < 2 || options.length > 10)) return invalid("cmp-opts", "Нужно от 2 до 10 вариантов, каждый с новой строки");
        const label = el.textContent;
        el.disabled = true; el.textContent = "Запускаю…";
        try {
          const res = await api("/api/school/campaigns", { method: "POST", body: { chat_ids: [...draft.chats], template: draft.template, text: draft.text, options, deadline: draft.deadline || null } });
          haptic("success");
          toast(`Запущено в ${res.reports.length} ${plural(res.reports.length, "классе", "классах", "классах")}`);
          S.stack = [{ name: "school", args: {} }];
          S.route = null;
          go("campaign", { id: res.campaign.id }, false);
          S.stack = [{ name: "school", args: {} }];
          syncBack();
        } catch (e) { toast(e.message, true); el.disabled = false; el.textContent = label; }
      };
      draw();
    },

    async campaign({ id }) {
      loading();
      try {
        const c = await api(`/api/school/campaigns/${id}`);
        const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
        render(`<div class="card">
            <h1 class="card-title lg">${esc(c.title)}</h1>
            <div class="muted small">${c.classes.length} ${plural(c.classes.length, "класс", "класса", "классов")}${c.deadline_text && c.deadline_text !== "без срока" ? " · до " + esc(c.deadline_text) : ""}</div>
            <div class="big" style="margin-top:18px">${c.answered}<small>/${c.total}</small></div>
            <div class="bar" role="img" aria-label="Ответили ${pct}%"><i style="width:${pct}%"></i></div>
            <ul class="list">${c.options.map((o, i) => `<li><span>${esc(o)}</span><b>${c.per_option[i] || 0}</b></li>`).join("")}</ul>
            <button type="button" class="btn block" data-act="cmpRemind" data-id="${c.id}" style="margin-top:12px" ${c.answered < c.total ? "" : "disabled"}>${c.answered < c.total ? "Напомнить всем, кто молчит" : "Ответили все"}</button>
          </div>
          <h2>По классам</h2>
          ${c.classes.sort((a, b) => (a.answered / (a.total || 1)) - (b.answered / (b.total || 1))).map((r) => {
            const p = r.total ? Math.round((100 * r.answered) / r.total) : 0;
            return `<div class="card"><div class="card-head"><b class="name">${esc(r.title)}</b><span class="nums">${r.answered}/${r.total}</span></div>
              <div class="bar"><i style="width:${p}%"></i></div>
              ${r.missing.length ? `<div class="muted small">Не ответили: ${r.missing.slice(0, 15).map(esc).join(", ")}${r.missing.length > 15 ? " …" : ""}</div>` : (r.total ? '<div class="ok small">Ответили все</div>' : '<div class="muted small">В списке класса пока никого</div>')}</div>`;
          }).join("")}`);
      } catch (e) { fail(e); }
    },
  });

  ACTIONS.delBoard = async (el) => {
    try { await api(`/api/board/${el.dataset.id}`, { method: "DELETE" }); toast("Удалено"); VIEWS.board(); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.goSchool = () => go("school");
  ACTIONS.shareLink = (el) => share(el.dataset.text, el.dataset.link);
  ACTIONS.newCampaign = () => go("newCampaign");
  ACTIONS.openCampaign = (el) => go("campaign", { id: el.dataset.id });
  ACTIONS.cmpRemind = async (el) => {
    el.disabled = true;
    try {
      const r = await api(`/api/school/campaigns/${el.dataset.id}/remind`, { method: "POST" });
      const n = r.sent + r.group;
      toast(say(`Напомнил ${n} ${plural(n, "раз", "раза", "раз")}`, "Готово. В настоящих классах родители получили бы напоминание"));
    } catch (e) { toast(e.message, true); }
    el.disabled = false;
  };
  async function reloadMe() { S.me = await api("/api/me"); }
  ACTIONS.scJoin = async () => {
    const code = document.getElementById("sc-code").value.trim();
    if (code.length < 4) return invalid("sc-code", "Введите код от завуча — это 4 или больше букв и цифр");
    try { const r = await api("/api/school/join", { method: "POST", body: { code } }); await reloadMe(); toast(`Вы в школе «${r.name}»`); go("school", {}, false); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.scCreate = async () => {
    const name = document.getElementById("sc-name").value.trim();
    if (name.length < 3) return invalid("sc-name", "Укажите название школы — хотя бы 3 буквы");
    try { await api("/api/school", { method: "POST", body: { name } }); await reloadMe(); haptic("success"); toast("Школа создана"); go("school", {}, false); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.invite = async (el) => {
    try {
      const [inv, subs] = await Promise.all([api(`/api/chats/${el.dataset.id}/invite`, { method: "POST" }), api(`/api/chats/${el.dataset.id}/subjects`)]);
      modal(`<h2 class="modal-title">Пригласить учителя-предметника</h2>
        <p class="muted small">По этой ссылке учитель сможет публиковать домашку и объявления в этот класс — ему не нужны права администратора в чате. Ссылка действует 7 дней.</p>
        ${inv.link ? `<div class="bubble small link-box">${esc(inv.link)}</div>` : '<p class="muted small">Ссылка появится, когда бот будет подключён к MAX.</p>'}
        ${subs.length ? `<h2>Уже подключены</h2><ul class="list">${subs.map((x) => `<li><span>${esc(x.name)} · ${esc(x.subject || "предмет не указан")}</span><button class="btn sm ghost" value="rm:${x.user_id}" formnovalidate>Отключить</button></li>`).join("")}</ul>` : ""}
        <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button>${inv.link ? `<button class="btn" value="share">${IN_MAX ? "Поделиться" : "Скопировать ссылку"}</button>` : ""}</div>`,
      async (_fd, action) => {
        if (action === "share") { share("Приглашение в класс в «Перемене»", inv.link); return true; }
        if (action?.startsWith("rm:")) { await api(`/api/chats/${el.dataset.id}/subjects/${action.slice(3)}`, { method: "DELETE" }); toast("Отключено"); }
      });
    } catch (e) { toast(e.message, true); }
  };

  // ------------------------------------------------------------------ действия: сборы
  // ------------------------------------------------------------------ «Ребёнок не придёт» / частые вопросы
  ACTIONS.absNew = () => {
    const cls = S.myAbs.classes;
    modal(`<h2 class="modal-title">Ребёнок не придёт</h2>
      ${cls.length > 1 ? `<label class="field"><span>Класс</span><select name="chat">${cls.map((c) => `<option value="${c.chat_id}">${esc(c.title)}</option>`).join("")}</select></label>` : ""}
      <label class="field"><span>Фамилия и имя ребёнка</span><input type="text" name="child" required minlength="2" maxlength="100" value="${esc(cls[0].child_name)}" placeholder="Иванов Пётр" autocomplete="off"></label>
      <fieldset class="field"><legend>Когда</legend>
        <div class="radios"><label class="radio"><input type="radio" name="day" value="today" checked><span>Сегодня</span></label><label class="radio"><input type="radio" name="day" value="tomorrow"><span>Завтра</span></label></div></fieldset>
      <label class="field"><span>Причина</span><select name="reason"><option value="sick">Болеет</option><option value="family">Семейные обстоятельства</option><option value="other">Другая причина</option></select></label>
      <label class="field"><span>Комментарий для учителя <em class="hint">необязательно</em></span><input type="text" name="comment" maxlength="200"></label>
      <p class="muted small">Учитель увидит это в списке «Сегодня не придут» — писать в общий чат не нужно.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Отметить</button></div>`,
    async (fd) => {
      const child = String(fd.get("child") || "").trim();
      if (child.length < 2) { toast("Напишите фамилию и имя ребёнка", true); return true; }
      const chat = Number(fd.get("chat") || cls[0].chat_id);
      await api("/api/my/absences", { method: "POST", body: { chat_id: chat, day: fd.get("day"), reason: fd.get("reason"), child_name: child, comment: fd.get("comment") || "" } });
      haptic("success"); toast("Отмечено — учитель увидит"); VIEWS.my();
    });
  };
  ACTIONS.absCancel = async (el) => {
    try { await api(`/api/my/absences/${el.dataset.id}`, { method: "DELETE" }); toast("Отметка снята"); VIEWS.my(); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.openAnswer = (el) => {
    const q = S.openQ.find((x) => x.key === el.dataset.key);
    modal(`<h2 class="modal-title">Ответ на частый вопрос</h2>
      <p class="muted small">Спросили ${q.count} ${plural(q.count, "раз", "раза", "раз")}. Ответьте один раз — дальше бот будет отвечать сам.</p>
      <label class="field"><span>Вопрос</span><input type="text" name="question" required minlength="2" maxlength="1000" value="${esc(q.question)}"></label>
      <label class="field"><span>Ваш ответ</span><textarea name="answer" required maxlength="3500"></textarea></label>
      ${S.me.school ? `<label class="switch"><span>Добавить в общую базу школы</span><input type="checkbox" name="shared"></label>` : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Сохранить</button></div>`,
    async (fd) => {
      const r = await api("/api/faq/open", { method: "POST", body: { key: q.key, question: fd.get("question"), answer: fd.get("answer"), shared: fd.get("shared") === "on" } });
      haptic("success");
      toast(r.resolved ? `Сохранено. Закрыто ${r.resolved} ${plural(r.resolved, "вопрос", "вопроса", "вопросов")} родителей` : "Сохранено");
      VIEWS.faq();
    });
  };

  ACTIONS.openPoll = (el) => go("pollDetail", { id: el.dataset.id });
  ACTIONS.goBack = () => back();
  ACTIONS.retry = () => { if (S.route) go(S.route.name, S.route.args, false); else location.reload(); };
  ACTIONS.recheck = async (el) => {
    el.disabled = true;
    try {
      await reloadMe();
      if (isNewcomer(S.me)) { toast("Пока ни одного класса. Проверьте, что бот добавлен в чат и назначен администратором", true); el.disabled = false; return; }
      S.stack = []; S.route = null;
      go(tabsFor(S.me)[0][0], {}, false);
    } catch (e) { toast(e.message, true); el.disabled = false; }
  };
  ACTIONS.newPoll = () => go("newPoll");
  ACTIONS.toggleList = () => {
    const l = document.getElementById("fullList");
    l.hidden = !l.hidden;
    $view.querySelectorAll('[data-act="toggleList"]').forEach((b) => b.setAttribute("aria-expanded", String(!l.hidden)));
    if (!l.hidden) l.scrollIntoView({ behavior: REDUCED() ? "auto" : "smooth", block: "start" });
  };
  ACTIONS.remind = async (el) => {
    el.disabled = true;
    try {
      const r = await api(`/api/polls/${el.dataset.id}/remind`, { method: "POST" });
      haptic("success");
      toast(say(r.sent || r.group ? `Напомнил: ${r.sent} в личку${r.group ? `, ${r.group} — в чате` : ""}` : "Напоминать некому — все уже ответили",
        "Готово. В настоящем классе молчащие родители получили бы напоминание в личку"));
    } catch (e) { toast(e.message, true); }
    el.disabled = false;
  };
  ACTIONS.closePoll = (el) => modal(`<h2 class="modal-title">Закрыть сбор?</h2><p class="muted">Ответы больше не будут приниматься. ${say("Итог бот пришлёт вам в личку.", "В настоящем боте итог пришёл бы вам в личку.")}</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn danger" value="ok">Закрыть сбор</button></div>`,
    async () => { await api(`/api/polls/${el.dataset.id}/close`, { method: "POST" }); toast("Сбор закрыт"); VIEWS.pollDetail({ id: el.dataset.id }); });
  ACTIONS.deletePoll = (el) => modal(`<h2 class="modal-title">Удалить сбор?</h2><p class="muted">Сбор пропадёт из списка вместе с ответами родителей, вернуть его не получится. Если итоги нужны для отчёта, сначала скачайте таблицу.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn danger" value="ok">Удалить</button></div>`,
    async () => {
      await api(`/api/polls/${el.dataset.id}`, { method: "DELETE" });
      toast("Сбор удалён");
      S.stack = [];
      go("polls", {}, false);
    });
  // таблица с итогами: в MAX — штатное скачивание мессенджера, в браузере — обычная ссылка на файл
  ACTIONS.export = (el) => {
    const url = location.origin + el.dataset.url;
    const name = `sbor_${el.dataset.id}.csv`;
    if (IN_MAX && WA?.downloadFile) {
      try { WA.downloadFile(url, name); return; } catch (_) { /* старые версии MAX — обычная ссылка */ }
    }
    const a = document.createElement("a");
    a.href = url; a.download = name; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
    toast("Таблица скачивается — её можно открыть в Excel");
  };

  // ------------------------------------------------------------------ действия: справочник
  ACTIONS.testFaq = async () => {
    const q = document.getElementById("faq-test").value.trim();
    const out = document.getElementById("faq-test-out");
    if (q.length < 2) return invalid("faq-test", "Напишите вопрос так, как его задал бы родитель");
    out.innerHTML = '<p class="muted small" style="margin-top:10px">Ищу…</p>';
    try {
      const hits = await api(`/api/faq/test?q=${encodeURIComponent(q)}`);
      out.innerHTML = hits.length ? hits.map((h, i) => `<div class="test-hit">
          <div class="small ${i === 0 && h.auto ? "ok" : "muted"}">${i === 0 && h.auto ? "Бот ответит сам" : "Бот предложит как похожий вариант"} · совпадение ${Math.round(h.score * 100)}%${h.group_auto ? " · ответит и в чате класса, если это включено" : ""}</div>
          <b>${esc(h.entry.question)}</b><div>${esc(h.entry.answer)}</div></div>`).join("")
        : '<p class="muted small" style="margin-top:10px">Готового ответа нет — бот предложит родителю передать вопрос вам. Добавьте ответ или ключевые слова к похожему.</p>';
    } catch (e) { out.innerHTML = ""; toast(e.message, true); }
  };
  ACTIONS.editFaq = (el) => {
    const e = (S.faq || []).find((x) => String(x.id) === el.dataset.id) || { question: "", answer: "", keywords: "", own: true };
    if (e.id && !e.own) {
      modal(`<h2 class="modal-title">${esc(e.question)}</h2><p>${esc(e.answer)}</p>
        <p class="muted small">Это ответ из общей базы школы, его ведёт автор. Если для вашего класса нужен другой ответ, сделайте свою копию — бот будет отвечать ею, а не школьной.</p>
        <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button><button class="btn" value="copy">Сделать свою копию</button></div>`,
      async () => { S.faqCopy = e; ACTIONS.editFaq({ dataset: {} }); return true; });
      return;
    }
    const base = !e.id && S.faqCopy ? { ...S.faqCopy, id: undefined, shared: false } : e;
    S.faqCopy = null;
    modal(`<h2 class="modal-title">${e.id ? "Ответ в справочнике" : "Новый ответ"}</h2>
      <label class="field"><span>Вопрос — так, как его задают родители</span><input type="text" name="question" required minlength="2" maxlength="1000" value="${esc(base.question)}" placeholder="Во сколько родительское собрание?"></label>
      <label class="field"><span>Ваш ответ</span><textarea name="answer" required maxlength="3500" placeholder="В четверг в 18:30, кабинет 214.">${esc(base.answer)}</textarea></label>
      <label class="field"><span>Ключевые слова <em class="hint">через запятую, необязательно</em></span><input type="text" name="keywords" value="${esc(base.keywords)}" placeholder="собрание, родительское"></label>
      ${S.me.school ? `<label class="switch"><span>Добавить в общую базу школы — бот ответит родителям всех классов</span><input type="checkbox" name="shared" ${base.shared ? "checked" : ""}></label>` : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="save">Сохранить</button></div>
      ${e.id ? '<button class="btn danger block" value="delete" style="margin-top:10px" formnovalidate>Удалить ответ</button>' : ""}`,
    async (fd, action) => {
      if (action === "delete") { await api(`/api/faq/${e.id}`, { method: "DELETE" }); toast("Удалено"); }
      else {
        if (String(fd.get("question") || "").trim().length < 2) { toast("Напишите вопрос — хотя бы пару букв", true); return true; }
        if (!String(fd.get("answer") || "").trim()) { toast("Напишите ответ", true); return true; }
        const body = { question: fd.get("question"), answer: fd.get("answer"), keywords: fd.get("keywords") || null, shared: fd.get("shared") === "on" };
        await api(e.id ? `/api/faq/${e.id}` : "/api/faq", { method: e.id ? "PUT" : "POST", body });
        haptic("success"); toast("Сохранено");
      }
      VIEWS.faq();
    });
  };

  // ------------------------------------------------------------------ действия: полка
  async function loadShelf() {
    const box = document.getElementById("shelf-list");
    try {
      const q = S.shelfQ || "";
      const items = await api(`/api/materials?q=${encodeURIComponent(q)}`);
      S.materials = items;
      const staff = S.me.is_teacher || S.me.is_subject_teacher;
      box.innerHTML = items.length ? items.map((m) => `<div class="card shelf">
          <div class="file-head"><span class="file-ico" aria-hidden="true"><svg><use href="#i-${{ image: "image", video: "video", audio: "audio" }[m.type] || "file"}"/></svg></span>
            <div class="file-meta"><div class="card-title name">${esc(m.title || m.filename || "Файл")}</div>
            ${m.title && m.filename ? `<div class="muted small name">${esc(m.filename)}</div>` : ""}</div></div>
          <div class="muted small" style="margin-top:6px">${m.chat_title ? "отправляли в " + esc(m.chat_title) + ", " : "на полке с "}${esc(m.date)}${m.tags ? " · " + m.tags.split(",").map((t) => "#" + esc(t.trim())).join(" ") : ""}</div>
          <div class="row tools">
            <button type="button" class="btn sm outline" data-act="sendMe" data-id="${m.id}">${say("Прислать мне в MAX", "Прислать мне")}</button>
            ${staff ? `<button type="button" class="btn sm ghost" data-act="sendChat" data-id="${m.id}">Отправить в чат класса</button><button type="button" class="btn sm ghost" data-act="editMat" data-id="${m.id}">Изменить</button>` : ""}
          </div></div>`).join("")
        : `<div class="empty"><div class="empty-ico" aria-hidden="true"><svg><use href="#i-folder"/></svg></div><b>${q ? "Ничего не нашлось" : "Пока пусто"}</b>${q ? "Попробуйте другие слова: например, «памятка» или «экскурсия»." : (staff ? "Перешлите боту файл с подписью — он появится здесь." : "Здесь появятся материалы, которые учитель отправлял в чат класса.")}</div>`;
    } catch (e) { box.innerHTML = `<div class="empty"><b>Не получилось загрузить</b>${esc(e.message)}<div><button type="button" class="btn outline" data-act="shelfSearch" style="margin-top:14px">Попробовать ещё раз</button></div></div>`; }
  }
  ACTIONS.shelfSearch = () => { S.shelfQ = document.getElementById("shelf-q").value.trim(); loadShelf(); };
  ACTIONS.sendMe = async (el) => {
    if (isDemo()) {
      toast("В демо файлы не отправляются. В настоящем боте файл пришёл бы вам в личку — оттуда его можно открыть или скачать");
      return;
    }
    el.disabled = true;
    try { await api(`/api/materials/${el.dataset.id}/send`, { method: "POST", body: {} }); haptic("success"); toast("Отправил файл вам в личку с ботом"); }
    catch (e) { toast(e.message, true); }
    el.disabled = false;
  };
  ACTIONS.sendChat = (el) => {
    const chats = S.me.board_chats;
    modal(`<h2 class="modal-title">Отправить в чат класса</h2>
      <label class="field"><span>Класс</span><select name="chat">${chats.map((c) => `<option value="${c.chat_id}">${esc(c.title)}</option>`).join("")}</select></label>
      ${isDemo() ? '<p class="muted small">В демо в чат ничего не уходит — в настоящем классе файл появился бы в чате.</p>' : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Отправить</button></div>`,
    async (fd) => {
      await api(`/api/materials/${el.dataset.id}/send`, { method: "POST", body: { chat_id: Number(fd.get("chat")) } });
      haptic("success"); toast(say("Отправлено в чат класса", "Готово. В настоящем классе файл появился бы в чате"));
    });
  };
  ACTIONS.editMat = (el) => {
    const m = S.materials.find((x) => String(x.id) === el.dataset.id);
    modal(`<h2 class="modal-title">${esc(m.filename)}</h2>
      <label class="field"><span>Подпись <em class="hint">по ней файл находится в поиске</em></span><input type="text" name="title" value="${esc(m.title)}"></label>
      <label class="field"><span>Теги <em class="hint">через запятую</em></span><input type="text" name="tags" value="${esc(m.tags)}" placeholder="безопасность, памятка"></label>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="save">Сохранить</button></div>
      <button class="btn danger block" value="delete" style="margin-top:10px" formnovalidate>Убрать с полки</button>`,
    async (fd, action) => {
      if (action === "delete") await api(`/api/materials/${m.id}`, { method: "DELETE" });
      else await api(`/api/materials/${m.id}`, { method: "PUT", body: { title: fd.get("title"), tags: fd.get("tags") } });
      toast("Готово"); loadShelf();
    });
  };
  async function uploadFile(file) {
    if (!file) return;
    modal(`<h2 class="modal-title">${esc(file.name)}</h2>
      <label class="field"><span>Подпись <em class="hint">по ней файл находится в поиске</em></span><input type="text" name="title" placeholder="Памятка по пожарной безопасности"></label>
      <label class="field"><span>Теги <em class="hint">через запятую</em></span><input type="text" name="tags" placeholder="безопасность"></label>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Загрузить</button></div>`,
    async (fd) => {
      const form = new FormData();
      form.append("file", file); form.append("title", fd.get("title") || ""); form.append("tags", fd.get("tags") || "");
      toast("Загружаю…");
      await api("/api/materials/upload", { method: "POST", form });
      haptic("success"); toast("Файл на полке"); loadShelf();
    });
  }

  // ------------------------------------------------------------------ действия: настройки
  ACTIONS.saveSettings = async () => {
    const body = {
      quiet_enabled: document.getElementById("s-quiet").checked,
      weekends_off: document.getElementById("s-we").checked,
      suggest_save: document.getElementById("s-sugg").checked,
      work_start: toMins(document.getElementById("s-start").value),
      work_end: toMins(document.getElementById("s-end").value),
      tz: document.getElementById("s-tz").value,
      urgent_words: document.getElementById("s-urgent").value,
    };
    try { S.me.settings = await api("/api/settings", { method: "PUT", body }); haptic("success"); toast("Сохранено"); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.syncChat = async (el) => {
    try {
      const r = await api(`/api/chats/${el.dataset.id}/sync`, { method: "POST" });
      toast(r.members < 0 ? "Бот не видит участников. Назначьте его администратором чата класса в MAX"
        : `Список класса обновлён: ${r.class_list.length} ${plural(r.class_list.length, "человек", "человека", "человек")}`, r.members < 0);
    } catch (e) { toast(e.message, true); }
  };
  ACTIONS.history = async (el) => {
    el.disabled = true;
    try {
      const r = await api(`/api/chats/${el.dataset.id}/history`, { method: "POST" });
      toast(r.added ? `На полку добавлено ${r.added} ${plural(r.added, "файл", "файла", "файлов")} из чата` : "Новых файлов в чате не нашлось");
    }
    catch (e) { toast(e.message, true); }
    el.disabled = false;
  };

  // ------------------------------------------------------------------ действия: родитель
  ACTIONS.answer = async (el) => {
    const card = el.closest(".card");
    if (el.disabled) return;
    card.querySelectorAll(".option").forEach((b) => (b.disabled = true));
    try {
      await api(`/api/my/polls/${el.dataset.id}/answer`, { method: "POST", body: { option: Number(el.dataset.opt) } });
      card.querySelectorAll(".option").forEach((b) => { b.classList.toggle("selected", b === el); b.setAttribute("aria-pressed", String(b === el)); });
      haptic("success");
      toast(`Ответ «${el.textContent.trim()}» отправлен учителю. Передумаете — нажмите другой`);
    } catch (e) { toast(e.message, true); }
    card.querySelectorAll(".option").forEach((b) => (b.disabled = false));
  };
  ACTIONS.ask = async (btn) => {
    const q = document.getElementById("ask-q").value.trim();
    const out = document.getElementById("ask-out");
    if (q.length < 2) return invalid("ask-q", "Напишите вопрос — хотя бы пару слов");
    out.innerHTML = '<div class="skeleton" aria-busy="true"><i></i></div>';
    if (btn) btn.disabled = true;
    try {
      const r = await api(`/api/ask?q=${encodeURIComponent(q)}`);
      let html = "";
      if (r.answer) {
        html += `<div class="card faq answer-card"><div class="muted small">${esc(r.answer.question)}</div><div class="answer-text">${esc(r.answer.answer)}</div>
          <div class="answer-foot">Из справочника класса · ответ сохранил учитель</div>
          <button type="button" class="btn sm ghost" data-act="notThat" data-id="${r.answer.id}" style="margin-top:10px">Это не то</button></div>`;
      }
      if (r.suggestions.length) {
        html += `<h2>${r.answer ? "Ещё похожее" : "Возможно, вы об этом"}</h2>` + r.suggestions.map((s) => `<div class="card faq"><b>${esc(s.question)}</b><div style="margin-top:4px">${esc(s.answer)}</div></div>`).join("");
      }
      if (r.materials.length) {
        html += "<h2>Подходящие материалы</h2>" + r.materials.map((m) => `<div class="card shelf"><b class="name">${esc(m.title || m.filename)}</b>${m.title ? `<div class="muted small name">${esc(m.filename)}</div>` : ""}
          <button type="button" class="btn sm outline" data-act="sendMe" data-id="${m.id}" style="margin-top:8px">Прислать мне</button></div>`).join("");
      }
      if (!r.answer) {
        html += `<div class="card"><p style="margin-top:0">${r.suggestions.length || r.materials.length ? "Не то, что нужно?" : "Готового ответа пока нет."} Могу передать вопрос учителю — он ответит в рабочее время.</p>
          <button type="button" class="btn block" data-act="forward">Передать вопрос учителю</button></div>`;
      }
      out.innerHTML = html;
      S.lastQ = q;
    } catch (e) { out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
    if (btn) btn.disabled = false;
  };
  ACTIONS.notThat = async (el) => {
    try { await api(`/api/faq/${el.dataset.id}/not-that`, { method: "POST" }); } catch (_) { /* не критично */ }
    el.outerHTML = '<p class="muted small" style="margin:10px 0 0">Спасибо, учитель увидит, что ответ не подошёл.</p><button type="button" class="btn sm" data-act="forward" style="margin-top:10px">Передать вопрос учителю</button>';
  };
  ACTIONS.forward = async (el) => {
    el.disabled = true;
    try {
      await api("/api/ask/forward", { method: "POST", body: { text: S.lastQ || document.getElementById("ask-q")?.value || "" } });
      haptic("success");
      el.outerHTML = `<p class="ok">${say("Передано учителю. Ответ придёт вам в личку с ботом.", "Вопрос записан. В настоящем боте его увидел бы учитель, а ответ пришёл бы вам в личку.")}</p>`;
    } catch (e) { toast(e.message, true); el.disabled = false; }
  };

  // ------------------------------------------------------------------ интеграции: календарь, API, вебхуки, печать
  // ссылка наружу: внутри MAX — штатным окном мессенджера, в браузере — новая вкладка
  function openUrl(url, win) {
    if (IN_MAX && WA?.openLink) { try { WA.openLink(url); if (win) win.close(); return; } catch (_) { /* старые версии MAX */ } }
    if (win) { win.location.href = url; return; }
    window.open(url, "_blank", "noopener");
  }
  // вкладку открываем сразу по нажатию, адрес подставляем после запроса — иначе браузер сочтёт её всплывающим окном
  const blankTab = () => { if (IN_MAX) return null; const w = window.open("", "_blank"); if (w) w.opener = null; return w; };
  const absUrl = (u) => (/^https?:/.test(u) ? u : location.origin + u);
  const when = (iso) => (iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");

  function calendarCard(cal, teacher) {
    return `<div class="card">
      <div class="card-title">Календарь</div>
      <p class="muted small">${teacher ? "Сроки сборов с числом ответивших и домашка ваших классов" : "Сборы, где ждут вашего ответа (с напоминанием за 3 часа), и домашние задания"} появятся в вашем календаре и будут обновляться сами.</p>
      <div class="bubble small link-box">${esc(cal.url)}</div>
      <div class="row tools">
        <button type="button" class="btn sm" data-act="calCopy">Скопировать ссылку</button>
        <button type="button" class="btn sm outline" data-act="openUrl" data-url="${esc(cal.google)}">Google Календарь</button>
        <button type="button" class="btn sm ghost" data-act="openUrl" data-url="${esc(cal.webcal)}">Apple и Outlook</button>
      </div>
      <p class="muted small">Яндекс Календарь: «Подписаться на календарь» → «По ссылке» → вставьте ссылку. Ссылка личная: по ней виден ваш календарь.</p>
      <button type="button" class="btn sm ghost" data-act="calRotate">Сменить ссылку (старая перестанет работать)</button>
    </div>`;
  }

  function weekCard(w) {
    const hw = w.homework.slice(0, 10);
    if (!hw.length && !w.news.length) return "";
    let day = null;
    const hwHtml = hw.map((h) => {
      const head = h.day !== day ? `<li><b>${esc(h.day_text)}</b><span></span></li>` : "";
      day = h.day;
      return `${head}<li><span>${esc(h.subject)}: ${esc(h.text)}</span></li>`;
    }).join("");
    return `<div class="card">
      <div class="card-title">На этой неделе</div>
      ${hwHtml ? `<ul class="list">${hwHtml}</ul>` : ""}
      ${w.news.length ? `<h2>Объявления</h2><ul class="list">${w.news.map((n) => `<li><span>${esc(n.text)}</span><span class="muted small">${esc(n.date)}</span></li>`).join("")}</ul>` : ""}
      <button type="button" class="btn sm ghost" data-act="calOpen" style="margin-top:10px">Сроки и домашка — в мой календарь</button>
    </div>`;
  }

  Object.assign(VIEWS, {
    async integrations() {
      loading();
      try {
        const d = await api("/api/integrations");
        S.integ = d;
        const keys = d.api_keys.map((k) => `<li><span><b>${esc(k.name)}</b><br><span class="muted small">${esc(k.prefix)}… · ${k.can_write ? "чтение и запуск сборов" : "только чтение"} · запросов: ${k.uses}${k.last_used_at ? " · последний " + when(k.last_used_at) : ""}</span></span>
          <button type="button" class="btn sm ghost" data-act="keyRevoke" data-id="${k.id}">Отозвать</button></li>`).join("");
        const hooks = d.webhooks.map((h) => `<div class="class-row">
            <div class="card-head"><b class="name">${esc(h.url)}</b><span class="chip ${h.active ? "done" : "warn"}">${h.active ? "включён" : "выключен"}</span></div>
            <div class="muted small">${h.events.map((e) => esc(d.events[e] || e)).join(" · ")}</div>
            <div class="muted small">${h.last_status ? `${esc(h.last_status)} · ${when(h.last_at)}` : "доставок ещё не было"}</div>
            <div class="row tools">
              <button type="button" class="btn sm" data-act="hookTest" data-id="${h.id}">Проверить</button>
              <button type="button" class="btn sm ghost" data-act="hookLog" data-id="${h.id}">Журнал</button>
              <button type="button" class="btn sm ghost" data-act="hookSecret" data-id="${h.id}">Секрет подписи</button>
              <button type="button" class="btn sm ghost" data-act="hookToggle" data-id="${h.id}" data-on="${h.active ? 0 : 1}">${h.active ? "Выключить" : "Включить"}</button>
              <button type="button" class="btn sm ghost icon-btn" data-act="hookDel" data-id="${h.id}" aria-label="Удалить вебхук"><svg class="i" aria-hidden="true"><use href="#i-trash"/></svg></button>
            </div></div>`).join("");
        render(`<h1>Интеграции</h1>
          <p class="muted lead-in">«Перемена» сама отдаёт данные класса туда, где с ними уже работают: в календарь, на школьный портал, в электронный журнал или таблицу. Переносить вручную ничего не нужно.</p>
          ${calendarCard(d.calendar, true)}
          ${d.can_integrate ? `<div class="card">
            <div class="card-title">Открытый API</div>
            <p class="muted small">Школьный портал, электронный журнал или 1С забирают сборы, ответы, отсутствующих и домашку по ключу. Ключ видит только ваши классы${S.me.is_admin ? " (у администрации — всю школу)" : ""}; с правом записи — может запускать сборы.</p>
            <div class="bubble small link-box">${esc(d.api_base)}</div>
            ${keys ? `<ul class="list">${keys}</ul>` : '<p class="muted small">Ключей пока нет.</p>'}
            <div class="row tools"><button type="button" class="btn sm" data-act="keyNew">Создать ключ</button>
              <button type="button" class="btn sm ghost" data-act="openUrl" data-url="${esc(d.docs_url)}">Описание API (OpenAPI)</button></div>
          </div>
          <div class="card">
            <div class="card-title">Вебхуки</div>
            <p class="muted small">«Перемена» сама сообщает вашей системе о событиях: родитель ответил на сбор, сбор закрыт, ребёнок не придёт. Каждое сообщение подписано (HMAC-SHA256) — подделать его нельзя.</p>
            ${hooks || '<p class="muted small">Вебхуков пока нет.</p>'}
            <button type="button" class="btn sm" data-act="hookNew" style="margin-top:10px">Добавить вебхук</button>
          </div>` : ""}`);
      } catch (e) { fail(e); }
    },
  });
  TAB_OF.integrations = "more";

  ACTIONS.goIntegrations = () => go("integrations");
  ACTIONS.openUrl = (el) => openUrl(absUrl(el.dataset.url));
  ACTIONS.calCopy = async () => {
    const url = S.integ?.calendar?.url;
    if (url && await copyText(url)) toast("Ссылка скопирована — вставьте её в календарь");
    else toast("Не удалось скопировать — выделите ссылку и скопируйте вручную", true);
  };
  ACTIONS.calRotate = () => modal(`<h2 class="modal-title">Сменить ссылку на календарь?</h2>
      <p class="muted">Календари, подписанные по старой ссылке, перестанут обновляться — подпишитесь заново по новой.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Сменить</button></div>`,
    async () => {
      S.integ.calendar = await api("/api/integrations/calendar/rotate", { method: "POST" });
      toast("Ссылка заменена");
      if (S.route?.name === "integrations") VIEWS.integrations(); else ACTIONS.calOpen();
    });
  ACTIONS.calOpen = async () => {
    try {
      S.integ = await api("/api/integrations");
      modal(`<h2 class="modal-title">Сроки в мой календарь</h2>${calendarCard(S.integ.calendar, false)}
        <div class="btns one"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button></div>`, () => {});
      // кнопки внутри окна обрабатываем так же, как на странице
      $modalBody.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", (ev) => { ev.preventDefault(); ACTIONS[b.dataset.act]?.(b, ev); }));
    } catch (e) { toast(e.message, true); }
  };

  // ключи API: показываем один раз и сразу даём проверить настоящим запросом
  ACTIONS.keyNew = () => modal(`<h2 class="modal-title">Новый ключ API</h2>
      <label class="field"><span>Для чего ключ</span><input type="text" name="name" maxlength="100" placeholder="Электронный журнал" required></label>
      <label class="switch"><span>Разрешить запускать сборы</span><input type="checkbox" name="write"></label>
      <p class="muted small">Ключ покажу один раз — сохраните его в настройках вашей системы. В «Перемене» хранится только его отпечаток.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Создать</button></div>`,
    async (fd) => {
      const k = await api("/api/integrations/keys", { method: "POST", body: { name: String(fd.get("name") || "").trim(), can_write: fd.get("write") === "on" } });
      haptic("success");
      S.newKey = k.key;
      const curl = `curl -H "Authorization: Bearer ${k.key}" ${S.integ.api_base}/polls?status=open`;
      modal(`<h2 class="modal-title">Ключ создан</h2>
        <div class="bubble small link-box">${esc(k.key)}</div>
        <p class="muted small">Пример запроса — открытые сборы с числом ответивших:</p>
        <div class="bubble small link-box">${esc(curl)}</div>
        <div id="key-try" aria-live="polite"></div>
        <div class="btns"><button class="btn ghost" value="copy">Скопировать ключ</button><button class="btn" value="try">Проверить запрос</button></div>
        <div class="btns one"><button class="btn ghost" value="cancel" formnovalidate>Готово</button></div>`,
      async (_fd, action) => {
        if (action === "copy") { toast(await copyText(S.newKey) ? "Ключ скопирован" : "Выделите ключ и скопируйте вручную"); return true; }
        const out = document.getElementById("key-try");
        out.innerHTML = '<p class="muted small">Запрашиваю…</p>';
        const r = await fetch("/api/v1/polls?status=open&limit=3", { headers: { Authorization: "Bearer " + S.newKey } });
        const data = await r.json();
        out.innerHTML = `<p class="small ${r.ok ? "ok" : "err"}">GET /api/v1/polls → HTTP ${r.status}</p><div class="bubble small pre">${esc(JSON.stringify(data, null, 2).slice(0, 1500))}</div>`;
        return true;
      });
      if (S.route?.name === "integrations") VIEWS.integrations();
      return true;
    });
  ACTIONS.keyRevoke = (el) => modal(`<h2 class="modal-title">Отозвать ключ?</h2><p class="muted">Система, которая им пользуется, сразу потеряет доступ.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn danger" value="ok">Отозвать</button></div>`,
    async () => { await api(`/api/integrations/keys/${el.dataset.id}`, { method: "DELETE" }); toast("Ключ отозван"); VIEWS.integrations(); });

  // вебхуки
  function secretModal(secret, fresh) {
    modal(`<h2 class="modal-title">${fresh ? "Вебхук добавлен" : "Секрет подписи"}</h2>
      <p class="muted small">Каждый запрос подписан: заголовок <b>X-Peremena-Signature</b> = sha256=HMAC-SHA256(секрет, «X-Peremena-Timestamp» + «.» + тело). Сверьте подпись — и вы знаете, что сообщение от «Перемены».</p>
      <div class="bubble small link-box">${esc(secret)}</div>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Готово</button><button class="btn" value="copy">Скопировать секрет</button></div>`,
    async () => { toast(await copyText(secret) ? "Секрет скопирован" : "Выделите секрет и скопируйте вручную"); return true; });
  }
  ACTIONS.hookNew = () => {
    const ev = S.integ.events;
    modal(`<h2 class="modal-title">Новый вебхук</h2>
      <label class="field"><span>Адрес вашей системы <em class="hint">только https://</em></span><input type="url" name="url" required maxlength="1000" placeholder="https://portal.school.ru/hooks/peremena"></label>
      <fieldset class="field"><legend>О чём сообщать</legend>${Object.entries(ev).map(([k, l]) => `<label class="switch"><span>${esc(l)}</span><input type="checkbox" name="ev" value="${esc(k)}" checked></label>`).join("")}</fieldset>
      ${isDemo() ? '<p class="muted small">В демо вебхук никуда не отправляет — в журнале будет видно, что ушло бы.</p>' : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Добавить</button></div>`,
    async (fd) => {
      const r = await api("/api/integrations/webhooks", { method: "POST", body: { url: String(fd.get("url") || "").trim(), events: fd.getAll("ev") } });
      haptic("success");
      VIEWS.integrations();
      secretModal(r.secret, true);
      return true;
    });
  };
  ACTIONS.hookTest = async (el) => {
    el.disabled = true;
    try {
      const r = await api(`/api/integrations/webhooks/${el.dataset.id}/test`, { method: "POST" });
      if (r.status === "ok") toast(`Доставлено: ваша система ответила HTTP ${r.code}`);
      else if (r.status === "demo") toast("Демо: запрос с подписью сформирован, но наружу не уходил — откройте «Журнал»");
      else toast(`Не доставлено: ${r.error || "HTTP " + r.code}`, true);
      VIEWS.integrations();
    } catch (e) { toast(e.message, true); el.disabled = false; }
  };
  ACTIONS.hookLog = async (el) => {
    try {
      const items = await api(`/api/integrations/webhooks/${el.dataset.id}/deliveries`);
      const label = { ok: '<span class="chip done">доставлено</span>', demo: '<span class="chip">демо</span>', error: '<span class="chip warn">ошибка</span>' };
      modal(`<h2 class="modal-title">Журнал доставок</h2>
        ${items.length ? `<ul class="list">${items.map((d) => `<li class="inbox-li"><div class="muted small">${when(d.created_at)} · ${esc(S.integ.events[d.event] || d.event)} ${label[d.status] || ""}${d.code ? " · HTTP " + d.code : ""}${d.error ? " · " + esc(d.error) : ""}</div>
          <details><summary class="small">Что ушло</summary><div class="bubble small pre">${esc(d.payload)}</div></details></li>`).join("")}</ul>` : '<p class="muted small">Доставок ещё не было. Нажмите «Проверить».</p>'}
        <div class="btns one"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button></div>`, () => {});
    } catch (e) { toast(e.message, true); }
  };
  ACTIONS.hookSecret = async (el) => {
    try { secretModal((await api(`/api/integrations/webhooks/${el.dataset.id}/secret`)).secret, false); } catch (e) { toast(e.message, true); }
  };
  ACTIONS.hookToggle = async (el) => {
    try { await api(`/api/integrations/webhooks/${el.dataset.id}`, { method: "PUT", body: { active: el.dataset.on === "1" } }); VIEWS.integrations(); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.hookDel = (el) => modal(`<h2 class="modal-title">Удалить вебхук?</h2><p class="muted">Система перестанет получать события, журнал доставок удалится.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn danger" value="ok">Удалить</button></div>`,
    async () => { await api(`/api/integrations/webhooks/${el.dataset.id}`, { method: "DELETE" }); toast("Удалено"); VIEWS.integrations(); });

  // печать: ведомость для подписи и бланки согласия с QR на онлайн-ответ
  ACTIONS.print = async (el) => {
    const win = blankTab();
    try {
      const links = await api(`/api/polls/${el.dataset.id}/print-links`);
      openUrl(absUrl(links[el.dataset.kind]), win);
    } catch (e) { if (win) win.close(); toast(e.message, true); }
  };

  // ------------------------------------------------------------------ демо-режим
  function demoBanner() {
    const parent = !S.me.is_teacher;
    document.body.classList.add("demo");
    document.querySelector(".topbar").insertAdjacentHTML("afterend", `<div class="demo-bar" role="note">
      <span class="demo-text"><b>Демо-класс.</b> ${parent ? "Вы смотрите глазами родителя." : "Вы смотрите глазами классного руководителя."} Все люди вымышленные, сообщения никому не уходят.</span>
      <span class="demo-actions">
        <button type="button" data-role="${parent ? "teacher" : "parent"}">${parent ? "Посмотреть глазами учителя" : "Посмотреть глазами родителя"}</button>
        <a href="/login">Войти по-настоящему</a>
      </span></div>`);
    document.querySelector(".demo-bar [data-role]").addEventListener("click", async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true;
      b.textContent = "Переключаю…";
      try {
        const r = await fetch(`/api/demo/role?role=${b.dataset.role}`,
          { method: "POST", headers: { "X-Peremena": "1" }, credentials: "same-origin" });
        if (!r.ok) throw new Error("Не получилось переключиться. Обновите страницу и попробуйте ещё раз");
        location.href = "/app/";
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = parent ? "Посмотреть глазами учителя" : "Посмотреть глазами родителя"; }
    });
  }

  // ------------------------------------------------------------------ старт
  async function boot() {
    if (location.protocol === "file:") {
      render(`<div class="empty"><b>Кабинет открыт как файл</b>Откройте его по ссылке с сайта «Перемены» или из бота в MAX — данные приходят с сервера.
        ${DEV ? '<br><br>Для разработки: запустите сервис (<code>python -m app run</code>) и откройте <code>http://localhost:8080/app/</code>' : ""}</div>`);
      return;
    }
    await maxReady();
    bindMaxBack();
    // Внутри MAX вход по подписи мини-приложения; в браузере — веб-кабинет с сессией сайта
    S.web = !IN_MAX;
    if (S.web && !DEV_USER) {
      // сначала тихая проверка сессии — без ошибки 401 в консоли у того, кто ещё не вошёл
      try {
        const s = await (await fetch("/api/auth/session", { credentials: "same-origin" })).json();
        if (!s.logged_in) { location.href = "/login"; return; }
      } catch (_) { /* проверим через /api/me ниже */ }
    }
    try {
      S.me = await api("/api/me");
    } catch (e) {
      if (S.web && e.status === 401) { location.href = "/login"; return; }
      fail(e);
      return;
    }
    if (S.web) {
      document.body.classList.add("web");
      const who = document.getElementById("who");
      who.insertAdjacentHTML("afterend",
        `<div class="web-links"><a href="/">На сайт</a><button type="button" id="logout">${S.me.demo ? "Выйти из демо" : "Выйти"}</button></div>`);
      document.getElementById("logout").addEventListener("click", async () => {
        try { await fetch("/api/auth/logout", { method: "POST", headers: { "X-Peremena": "1" } }); } catch (_) { /* всё равно уходим */ }
        location.href = "/";
      });
    }
    if (S.me.demo) demoBanner();
    const role = S.me.is_teacher ? "классный руководитель" : S.me.is_subject_teacher ? "учитель-предметник"
      : S.me.is_admin ? "администрация" : S.me.member_chats.length ? "родитель" : "";
    const who = document.getElementById("who");
    who.textContent = S.me.user.name + (role ? " · " + role : "");
    who.title = who.textContent;
    if (isNewcomer(S.me)) { S.route = { name: "welcome", args: {} }; drawTabs(); VIEWS.welcome(); return; }
    const start = WA?.initDataUnsafe?.start_param || params.get("start") || "";
    const tabs = tabsFor(S.me).map((t) => t[0]);
    if (S.me.is_teacher && /^poll_\d+$/.test(start)) {
      S.route = { name: "polls", args: {} };
      go("pollDetail", { id: start.slice(5) });
      return;
    }
    go(tabs.includes(start) ? start : tabs[0], {}, false);
  }
  boot();
})();
