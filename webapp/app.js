/* «Перемена» — мини-приложение MAX. Без сборки: чистый JS, работает в WebView MAX и в браузере (dev). */
(() => {
  "use strict";

  const WA = window.WebApp || null;
  const params = new URLSearchParams(location.search);
  const DEV_USER = params.get("dev_user");
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
  const rub = (n) => `${Number(n || 0).toLocaleString("ru-RU")} ₽`;
  const mins = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const toMins = (s) => { const [h, m] = (s || "0:0").split(":").map(Number); return h * 60 + (m || 0); };
  const fmtMinutes = (t) => { t = Math.round(t); const h = Math.floor(t / 60), m = t % 60; return h ? (m ? `${h} ч ${m} мин` : `${h} ч`) : `${m} мин`; };

  // Внутри MAX есть «мост» (BackButton, вибрация); в браузере его вызовы сыплют ошибками в консоль
  const IN_MAX = !!WA?.initData;

  function haptic(kind) {
    if (!IN_MAX) return;
    try { WA?.HapticFeedback?.notificationOccurred(kind); } catch (_) { /* не на всех платформах */ }
  }

  let toastTimer;
  function toast(msg, isErr) {
    $toast.textContent = msg;
    $toast.hidden = false;
    $toast.style.background = isErr ? "#b91c1c" : "";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => ($toast.hidden = true), 2600);
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
      throw new Error("Нет связи с сервером «Перемены». Проверьте интернет; если вы разработчик — запущен ли сервис (python -m app run).");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* пустой ответ */ }
    if (!res.ok) {
      const msg = (data && (data.detail?.[0]?.msg || data.detail)) || `Ошибка ${res.status}`;
      const err = new Error(typeof msg === "string" ? msg : "Ошибка запроса");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function render(html) {
    // в браузере нет системной кнопки «Назад» мессенджера — рисуем свою
    const back = !IN_MAX && S.stack.length ? '<button class="back" data-act="goBack">← Назад</button>' : "";
    $view.innerHTML = back + html;
    window.scrollTo(0, 0);
  }
  function loading() { $view.innerHTML = '<div class="loading">Загружаю…</div>'; }
  function fail(e) { render(`<div class="empty"><b>Не получилось</b>${esc(e.message || e)}</div>`); }

  // ------------------------------------------------------------------ навигация
  const PARENT_TABS = [["my", "📋", "Сборы"], ["board", "📌", "Доска"], ["ask", "❓", "Спросить"], ["shelf", "🗂", "Материалы"]];
  const PARENT_ROUTES = new Set(["my", "board", "ask", "shelf"]);

  // вкладки собираются из ролей: классный руководитель, предметник, администратор, родитель
  function tabsFor(me) {
    const t = [];
    if (me.is_teacher) t.push(["polls", "📋", "Сборы"]);
    if (me.is_teacher || me.is_subject_teacher) t.push(["board", "📌", "Доска"]);
    if (me.is_teacher) t.push(["faq", "📖", "Справочник"]);
    if (me.is_teacher || me.is_subject_teacher) t.push(["shelf", "🗂", "Полка"]);
    if (me.is_admin || ((me.is_teacher || me.is_subject_teacher) && !me.is_teacher)) t.push(["school", "🏛", "Школа"]);
    if (me.is_teacher) t.push(["more", "⏱", "Ещё"]);
    return t.length ? t : PARENT_TABS;
  }
  const isStaff = () => S.me.is_teacher || S.me.is_subject_teacher || S.me.is_admin;

  function drawTabs(active) {
    const tabs = tabsFor(S.me);
    $tabbar.hidden = false;
    $tabbar.innerHTML = tabs.map(([k, ico, label]) =>
      `<button data-tab="${k}" class="${k === active ? "on" : ""}"><span class="ico">${ico}</span>${label}</button>`).join("");
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
  if (IN_MAX) {
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

  // ------------------------------------------------------------------ модальное окно
  function modal(html, onSubmit) {
    $modalBody.innerHTML = html;
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
  const pollCard = (p) => {
    const pct = p.total ? Math.round((100 * p.answered) / p.total) : 0;
    const status = p.closed ? '<span class="chip done">закрыт</span>' : `<span class="chip accent">срок: ${esc(p.deadline_text)}</span>`;
    return `<div class="card tap" data-act="openPoll" data-id="${p.id}">
      <div class="row spread"><div class="card-title">${esc(p.title)}</div>${status}</div>
      <div class="muted small">${esc(p.chat_title)}</div>
      <div class="row spread" style="margin-top:10px"><div class="big" style="font-size:30px">${p.answered}<small>/${p.total}</small></div>
      <div class="muted small">${p.money ? `💰 ${rub(p.money.collected)} из ${rub(p.money.expected)} · ` : ""}${p.missing.length ? "не ответили: " + p.missing.length : (p.total ? "ответили все ✓" : "")}</div></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  };

  const VIEWS = {
    async polls() {
      loading();
      try {
        const [items, att] = await Promise.all([api("/api/polls"), api("/api/attendance")]);
        const open = items.filter((p) => !p.closed), closed = items.filter((p) => p.closed);
        const absent = att.items.length
          ? `<div class="card absent"><div class="row spread"><div class="card-title">🤒 Сегодня не придут: ${att.items.length}</div><span class="muted small">${esc(att.day_text)}</span></div>
              <ul class="list">${att.items.map((a) => `<li><span>${esc(a.who)}</span><span class="muted">${esc(a.reason_text)}${a.comment ? " · " + esc(a.comment) : ""}</span></li>`).join("")}</ul></div>`
          : `<div class="card absent small muted">🤒 Сегодня все на месте — никто не отметил отсутствие.</div>`;
        render(`<h1>Сборы</h1>
          ${absent}
          ${items.length ? "" : `<div class="empty"><b>Сборов пока нет</b>Согласия, справки, «ознакомлен» — бот разошлёт каждому в личку и сам посчитает ответы.</div>`}
          ${open.map(pollCard).join("")}
          ${closed.length ? `<h2 class="muted">Завершённые</h2>${closed.map(pollCard).join("")}` : ""}
          <button class="btn fab" data-act="newPoll">＋ Новый сбор</button>`);
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
        render(`
          <div class="card">
            <div class="card-title" style="font-size:19px">${esc(p.title)}</div>
            <div class="muted small">${esc(p.chat_title)} · ${p.closed ? "сбор закрыт" : "срок: " + esc(p.deadline_text)}</div>
            <div class="big" style="margin-top:18px">${p.answered}/${p.total}</div>
            <div class="bar"><i style="width:${pct}%"></i></div>
            <div class="ok small">✓ ${p.answered} ${plural(p.answered, "человек ответил", "человека ответили", "человек ответили")}</div>
            ${p.missing.length ? `<h2>Не ответили</h2><div class="names">${p.missing.map(esc).join("<br>")}</div>`
              : p.total ? '<h2 class="ok">Ответили все 🎉</h2>' : '<p class="muted small">Список класса пуст — назначьте бота администратором чата.</p>'}
            ${p.closed
              // у закрытого сбора «Напомнить» не нужно, а список — нужен: кто что ответил, для отчёта
              ? '<div class="btns"><button class="btn outline" data-act="toggleList">Список</button></div>'
              : `<div class="btns">
              <button class="btn" data-act="remind" data-id="${p.id}" ${p.missing.length ? "" : "disabled"}>Напомнить</button>
              <button class="btn outline" data-act="toggleList">Список</button></div>`}
          </div>
          ${p.money ? `<div class="card money">
            <div class="card-title">💰 Собрано ${rub(p.money.collected)} из ${rub(p.money.expected)}</div>
            <div class="bar"><i style="width:${p.money.expected ? Math.round(100 * p.money.collected / p.money.expected) : 0}%"></i></div>
            <p class="muted small">Подтверждено ${p.money.paid} из ${p.money.participants}. Отметьте, кто сдал: родитель получит подтверждение.</p>
            <ul class="list">${p.targets.filter((t) => t.answer !== 2).map((t) => `<li><label class="paid-row"><input type="checkbox" data-paid="${t.id}" ${t.paid ? "checked" : ""}> ${esc(t.name)}</label><span class="muted small">${t.answer === 0 ? "перевёл" : t.answer === 1 ? "наличные" : t.answer == null ? "не ответил" : esc(p.options[t.answer])}</span></li>`).join("")}</ul>
          </div>` : ""}
          <div class="card"><div class="card-title">Ответы</div><ul class="list">${byOpt}</ul></div>
          <div class="card" id="fullList" hidden>
            <div class="card-title">Полный список</div>
            <ul class="list">${p.targets.map((t) => `<li><span>${esc(t.name)}</span><span class="${t.answer == null ? "muted" : ""}">${t.answer == null ? (t.delivered ? "не ответил" : "не в боте") : esc(p.options[t.answer])}</span></li>`).join("")}</ul>
          </div>
          <div class="card"><div class="card-title">Текст сбора</div><div class="bubble" style="margin-top:8px">${esc(p.text)}</div></div>
          <div class="row">
            <button class="btn ghost" data-act="export" data-url="${esc(p.export_url)}" data-id="${p.id}">⬇️ Выгрузить CSV</button>
            ${p.closed ? "" : `<button class="btn danger" data-act="closePoll" data-id="${p.id}">Закрыть сбор</button>`}
          </div>`);
      } catch (e) { fail(e); }
    },

    async newPoll() {
      loading();
      try {
        S.templates = S.templates || await api("/api/templates");
        const chats = S.me.chats;
        if (!chats.length) { render('<div class="empty"><b>Нет подключённых классов</b>Добавьте бота в чат класса и назначьте администратором.</div>'); return; }
        const draft = { template: "consent", deadline: "friday" };
        const draw = () => {
          const tpl = S.templates.find((t) => t.key === draft.template);
          render(`<h1>Новый сбор</h1>
            <label class="field"><span>Класс</span><select id="f-chat">${chats.map((c) => `<option value="${c.chat_id}">${esc(c.title || c.chat_id)}</option>`).join("")}</select></label>
            <div class="field"><span class="muted small" style="font-weight:600">Что собираем</span>
              <div class="seg" style="margin-top:6px">${S.templates.map((t) => `<button data-act="tpl" data-key="${t.key}" class="${t.key === draft.template ? "on" : ""}">${esc(t.label)}</button>`).join("")}</div></div>
            <label class="field" style="margin-top:14px"><span>Текст (первая строка — заголовок)</span>
              <textarea id="f-text" placeholder="${esc(tpl.hint.replace(/^Например: /, ""))}">${esc(draft.text || "")}</textarea></label>
            ${draft.template === "choice"
              ? `<label class="field"><span>Варианты — каждый с новой строки (2–10)</span><textarea id="f-opts" style="min-height:90px">${esc(draft.opts || "")}</textarea></label>`
              : `<p class="muted small">Кнопки: ${tpl.options.map(esc).join(" · ")}</p>`}
            ${draft.template === "money" ? `<label class="field"><span>Сумма с каждого, ₽</span><input type="number" id="f-amount" min="1" max="100000" inputmode="numeric" value="${esc(draft.amount || "")}" placeholder="300"></label>` : ""}
            <div class="field"><span class="muted small" style="font-weight:600">Срок ответа</span>
              <div class="seg" style="margin-top:6px">${[["today", "Сегодня 18:00"], ["tomorrow", "Завтра 18:00"], ["friday", "Пятница 18:00"], ["week", "Через неделю"], ["custom", "Своя дата"], ["", "Без срока"]]
                .map(([k, l]) => `<button data-act="dl" data-key="${k}" class="${k === draft.deadline ? "on" : ""}">${l}</button>`).join("")}</div>
              ${draft.deadline === "custom" ? `<input type="datetime-local" id="f-dt" style="margin-top:10px" value="${esc(draft.dt || "")}">` : ""}</div>
            <button class="btn block" data-act="sendPoll" style="margin-top:18px">Разослать</button>
            <p class="muted small">Каждый участник чата получит сообщение в личку с кнопками, в чат класса уйдёт копия. Бот сам напомнит молчащим за сутки и за 2 часа до срока.</p>`);
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
          if (!draft.text || draft.text.trim().length < 3) return toast("Напишите текст сбора", true);
          const options = draft.template === "choice" ? (draft.opts || "").split("\n").map((s) => s.trim()).filter(Boolean) : [];
          let deadline = draft.deadline || null;
          if (deadline === "custom") { if (!draft.dt) return toast("Выберите дату", true); deadline = draft.dt; }
          el.disabled = true; el.textContent = "Рассылаю…";
          try {
            const amount = draft.template === "money" ? Number(draft.amount) : null;
            if (draft.template === "money" && !(amount > 0)) { el.disabled = false; el.textContent = "Разослать"; return toast("Укажите сумму", true); }
            const res = await api("/api/polls", { method: "POST", body: { chat_id: Number(draft.chat_id), template: draft.template, text: draft.text, options, deadline, amount } });
            haptic("success");
            const r = res.report;
            toast(`Доставлено в личку: ${r.delivered} из ${r.total}`);
            S.stack = [{ name: "polls", args: {} }];
            S.route = null;
            go("pollDetail", { id: res.poll.id }, false);
            S.stack = [{ name: "polls", args: {} }];
            syncBack();
          } catch (e) { toast(e.message, true); el.disabled = false; el.textContent = "Разослать"; }
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
            <div class="card-title">❓ Спрашивают, а ответа в справочнике нет</div>
            <p class="muted small">Ответьте один раз — дальше бот будет отвечать на такие вопросы сам.</p>
            <ul class="list">${openQ.map((q) => `<li><span>${esc(q.question)} <span class="chip">${q.count}×</span></span><button class="btn sm" data-act="openAnswer" data-key="${esc(q.key)}">Ответить</button></li>`).join("")}</ul></div>` : "";
        render(`<h1>Справочник</h1>
          ${openCard}
          <div class="card faq">
            <div class="card-title tag-faq">Проверить, как ответит бот</div>
            <div class="row" style="margin-top:8px"><input type="search" id="faq-test" placeholder="Например: во сколько собрание?" style="flex:1"><button class="btn sm" data-act="testFaq">Проверить</button></div>
            <div id="faq-test-out"></div>
          </div>
          ${items.length ? "" : '<div class="empty"><b>Справочник пуст</b>Добавьте вопросы, которые задают чаще всего. Бот будет отвечать на них родителям в любое время — и подписывать, что это сохранили вы.</div>'}
          ${items.map((e) => `<div class="card faq tap" data-act="editFaq" data-id="${e.id}">
            <div class="card-title">${esc(e.question)}</div>
            <div style="margin-top:4px">${esc(e.answer)}</div>
            <div class="row" style="margin-top:8px">${e.shared ? '<span class="chip accent">🏛 база школы</span>' : ""}<span class="chip">ответов: ${e.hits}</span>${e.misses ? `<span class="chip">«не то»: ${e.misses}</span>` : ""}${e.keywords ? `<span class="muted small">🔑 ${esc(e.keywords)}</span>` : ""}</div>
          </div>`).join("")}
          <button class="btn fab" data-act="editFaq">＋ Добавить</button>`);
        S.faq = items;
      } catch (e) { fail(e); }
    },

    async shelf() {
      loading();
      const teacher = S.me.is_teacher || S.me.is_subject_teacher;
      render(`<h1>${teacher ? "Полка" : "Материалы класса"}</h1>
        <div class="row" style="margin-bottom:12px"><input type="search" id="shelf-q" placeholder="памятка, #экскурсия, бланк…" style="flex:1" value="${esc(S.shelfQ || "")}"><button class="btn sm" data-act="shelfSearch">Найти</button></div>
        ${teacher ? `<label class="btn outline block" style="margin-bottom:12px">⬆️ Загрузить файл<input type="file" id="shelf-file" hidden></label>
          <p class="muted small" style="margin-top:-4px">Или просто перешлите файл боту в личку с подписью — он сам положит его на полку.</p>` : ""}
        <div id="shelf-list"><div class="loading">Загружаю…</div></div>`);
      const input = document.getElementById("shelf-q");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") ACTIONS.shelfSearch(); });
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
        render(`<h1>Польза и настройки</h1>
          <button class="btn ghost block" data-act="goSchool" style="margin-bottom:12px">🏛 ${S.me.school ? esc(S.me.school.name) : "Школа: общая база ответов и сводка"}</button>
          <div class="card">
            <div class="muted small" style="font-weight:600">СЭКОНОМЛЕНО ЗА 7 ДНЕЙ</div>
            <div class="stat-big" style="margin:8px 0">${fmtMinutes(st.week.minutes)}</div>
            <div class="muted">за всё время — <b>${fmtMinutes(st.total.minutes)}</b></div>
            ${kinds ? `<ul class="list" style="margin-top:12px">${kinds}</ul>` : '<p class="muted small">Счётчик начнёт расти после первого сбора и первых ответов из справочника.</p>'}
          </div>

          <div class="card quiet">
            <div class="card-title tag-quiet">🌙 Тихий режим</div>
            <p class="muted small">Вне рабочего времени бот продолжает отвечать родителям тем, что знает, а всё новое приносит вам утром одной карточкой. Срочное — по слову-маркеру — сразу.</p>
            <label class="switch"><span>Тихий режим</span><input type="checkbox" id="s-quiet" ${s.quiet_enabled ? "checked" : ""}></label>
            <div class="row" style="margin:10px 0">
              <label class="field" style="flex:1;margin:0"><span>Начало дня</span><input type="time" id="s-start" value="${mins(s.work_start)}"></label>
              <label class="field" style="flex:1;margin:0"><span>Конец дня</span><input type="time" id="s-end" value="${mins(s.work_end)}"></label>
            </div>
            <label class="switch"><span>Выходные — не беспокоить</span><input type="checkbox" id="s-we" ${s.weekends_off ? "checked" : ""}></label>
            <label class="switch"><span>Предлагать сохранять мои ответы из чата</span><input type="checkbox" id="s-sugg" ${s.suggest_save ? "checked" : ""}></label>
            <label class="field" style="margin-top:10px"><span>Часовой пояс</span><select id="s-tz">${tzs.map(([v, l]) => `<option value="${v}" ${v === s.tz ? "selected" : ""}>${l}</option>`).join("")}</select></label>
            <label class="field"><span>Слова-маркеры срочного (через запятую)</span><input type="text" id="s-urgent" value="${esc(s.urgent_words)}"></label>
            <button class="btn block" data-act="saveSettings">Сохранить</button>
          </div>

          <div class="card">
            <div class="card-title">🏫 Классы</div>
            ${S.me.chats.map((c) => `<div style="padding:10px 0;border-top:1px solid var(--line)">
              <div class="row spread"><b>${esc(c.title || c.chat_id)}</b><span class="chip ${c.bot_is_admin ? "done" : ""}">${c.bot_is_admin ? "бот — администратор" : "бот не администратор"}</span></div>
              <label class="switch"><span>Автоответ прямо в чате</span><input type="checkbox" data-chat-auto="${c.chat_id}" ${c.autoanswer ? "checked" : ""}></label>
              <div class="row"><button class="btn sm ghost" data-act="syncChat" data-id="${c.chat_id}">🔄 Обновить список</button><button class="btn sm ghost" data-act="history" data-id="${c.chat_id}">📥 Файлы из истории</button><button class="btn sm ghost" data-act="invite" data-id="${c.chat_id}">🔗 Пригласить предметника</button></div>
            </div>`).join("")}
          </div>

          <div class="card">
            <div class="card-title">📥 Входящие${pending.length ? ` <span class="chip accent">ждут утра: ${pending.length}</span>` : ""}</div>
            ${inbox.length ? `<ul class="list">${inbox.slice(0, 20).map((i) => `<li style="display:block"><div class="muted small">${esc(i.time)} · ${esc(i.label)}${i.from_name ? " · " + esc(i.from_name) : ""}${i.delivered ? "" : " · ⏳"}</div>${esc(i.text)}</li>`).join("")}</ul>` : '<p class="muted small">Пусто. Сюда попадает то, что пришло вне рабочего времени.</p>'}
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
        const [items, abs] = await Promise.all([api("/api/my/polls"), api("/api/my/absences")]);
        S.myAbs = abs;
        const marks = abs.items.length ? `<div class="card absent"><div class="card-title">🤒 Отмечено отсутствие</div>
            <ul class="list">${abs.items.map((a) => `<li><span>${esc(a.who)} — ${esc(a.day_text)}, ${esc(a.reason_text)}</span><button class="btn sm ghost" data-act="absCancel" data-id="${a.id}">Отменить</button></li>`).join("")}</ul></div>` : "";
        render(`<h1>Сборы</h1>
          ${abs.classes.length ? `<button class="btn outline block" data-act="absNew" style="margin-bottom:12px">🤒 Ребёнок не придёт</button>` : ""}
          ${marks}
          ${items.length ? "" : `<div class="empty"><b>Всё отвечено ✓</b>${S.me.member_chats.length ? "Новые сборы от учителя появятся здесь и в личке с ботом." : "Как только учитель добавит бота в чат класса, сборы будут приходить сюда."}</div>`}
          ${items.map((p) => `<div class="card">
            <div class="row spread"><span class="muted small">${esc(p.chat_title)}${p.teacher ? " · " + esc(p.teacher) : ""}</span>${p.deadline ? `<span class="chip accent">до ${esc(p.deadline_text)}</span>` : ""}</div>
            <div class="bubble" style="margin-top:10px"><b>${esc(p.title)}</b>${p.text !== p.title ? "\n" + esc(p.text.split("\n").slice(1).join("\n")) : ""}</div>
            ${p.options.map((o, i) => `<button class="option ${p.my_answer === i ? "selected" : ""}" data-act="answer" data-id="${p.id}" data-opt="${i}">${esc(o)}</button>`).join("")}
          </div>`).join("")}`);
      } catch (e) { fail(e); }
    },

    async ask() {
      render(`<h1>Спросить</h1>
        <p class="muted">Отвечу тем, что сохранил учитель. Если готового ответа нет — передам вопрос ему.</p>
        <textarea id="ask-q" placeholder="Например: какая форма завтра?" style="min-height:80px"></textarea>
        <button class="btn block" data-act="ask" style="margin-top:10px">Спросить</button>
        <div id="ask-out" style="margin-top:14px"></div>`);
    },
  };

  // ================================================================== ДОСКА, ШКОЛА
  function share(text, link) {
    try {
      if (WA?.shareMaxContent) { WA.shareMaxContent({ text, link }); return; }
    } catch (_) { /* не на всех платформах */ }
    try { navigator.clipboard?.writeText(link); toast("Ссылка скопирована"); } catch (_) { toast("Скопируйте ссылку вручную"); }
  }

  function boardCard(a, staff) {
    const head = a.kind === "hw" ? `📚 ${esc(a.subject || "Без предмета")}` : "📣 Объявление";
    const when = a.for_date_text ? `<span class="chip accent">на ${esc(a.for_date_text)}</span>` : "";
    let status = "";
    if (staff) {
      status = a.publish_mode === "silent" ? '<span class="chip">🤫 только для ответов</span>'
        : a.published ? '<span class="chip done">в чате</span>' : `<span class="chip">⏳ выйдет ${esc(a.publish_at_text)}</span>`;
    }
    return `<div class="card ${a.kind === "hw" ? "faq" : "quiet"}">
      <div class="row spread"><div class="card-title">${head}</div>${when}</div>
      <div class="muted small">${esc(a.chat_title)} · ${esc(a.date)}</div>
      <div style="margin-top:8px;white-space:pre-wrap">${esc(a.text)}</div>
      ${staff ? `<div class="row spread" style="margin-top:10px">${status}${a.can_delete ? `<button class="btn sm ghost" data-act="delBoard" data-id="${a.id}">🗑</button>` : ""}</div>` : ""}
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
            ${items.length ? "" : '<div class="empty"><b>Пока пусто</b>Здесь появятся домашние задания и объявления учителей.</div>'}
            ${hw.length ? `<h2>📚 Домашние задания</h2>${hw.map((a) => boardCard(a, false)).join("")}` : ""}
            ${news.length ? `<h2>📣 Объявления</h2>${news.map((a) => boardCard(a, false)).join("")}` : ""}`);
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
              <div class="seg">${[["hw", "📚 Домашка"], ["news", "📣 Объявление"]].map(([k, l]) => `<button data-act="bdKind" data-k="${k}" class="${draft.kind === k ? "on" : ""}">${l}</button>`).join("")}</div>
              <label class="field" style="margin-top:14px"><span>Класс</span><select id="bd-chat">${chats.map((c) => `<option value="${c.chat_id}" ${c.chat_id === chat.chat_id ? "selected" : ""}>${esc(c.title)}${c.subject ? " · " + esc(c.subject) : ""}</option>`).join("")}</select></label>
              ${draft.kind === "hw" ? `<label class="field"><span>Предмет</span><input type="text" id="bd-subj" list="bd-subjects" value="${esc(subj)}" placeholder="Алгебра"><datalist id="bd-subjects">${subjects.map((x) => `<option value="${esc(x)}">`).join("")}</datalist></label>` : ""}
              <label class="field"><span>${draft.kind === "hw" ? "Задание" : "Текст объявления"}</span><textarea id="bd-text" placeholder="${draft.kind === "hw" ? "№245, 247; правило на с. 63" : "Завтра субботник, форма свободная"}">${esc(draft.text || "")}</textarea></label>
              ${draft.kind === "hw" ? `<div class="field"><span class="muted small" style="font-weight:600">К какому дню</span><div class="seg" style="margin-top:6px">${[["tomorrow", "Завтра"], ["after", "Послезавтра"], ["monday", "К понедельнику"], ["", "Без даты"]].map(([k, l]) => `<button data-act="bdDate" data-k="${k}" class="${draft.for_date === k ? "on" : ""}">${l}</button>`).join("")}</div></div>` : ""}
              <div class="field" style="margin-top:14px"><span class="muted small" style="font-weight:600">Публикация в чат</span><div class="seg" style="margin-top:6px">${[["now", "Сейчас"], ["later", "🌙 Утром"], ["silent", "🤫 Не публиковать"]].map(([k, l]) => `<button data-act="bdPub" data-k="${k}" class="${draft.publish === k ? "on" : ""}">${l}</button>`).join("")}</div>
                <p class="muted small">${quietNow() ? "Сейчас нерабочее время — утренняя публикация избавит родителей от ночных уведомлений. " : ""}В любом случае бот ответит по записи на «что задали?».</p></div>
              <button class="btn block" data-act="bdSave">Записать</button>
            </div>
            <h2>Последние записи</h2>
            ${items.length ? items.map((a) => boardCard(a, true)).join("") : '<p class="muted">Пока пусто.</p>'}`);
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
          if (!draft.text || draft.text.trim().length < 2) return toast("Напишите текст", true);
          el.disabled = true;
          try {
            const a = await api("/api/board", { method: "POST", body: { chat_id: Number(draft.chat_id), kind: draft.kind,
              subject: draft.subject || "", text: draft.text, for_date: draft.for_date || null, publish_mode: draft.publish } });
            haptic("success");
            toast(a.publish_mode === "later" ? `Опубликую ${a.publish_at_text}` : a.publish_mode === "silent" ? "Записано без публикации" : "Опубликовано в чате");
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
          <div class="card"><div class="card-title">Вступить по коду</div>
            <div class="row" style="margin-top:8px"><input type="text" id="sc-code" placeholder="Код от завуча" style="flex:1;text-transform:uppercase"><button class="btn sm" data-act="scJoin">Вступить</button></div></div>
          ${me.is_teacher || me.is_subject_teacher ? `<div class="card"><div class="card-title">Создать школу</div>
            <p class="muted small">Вы станете администратором и получите код для коллег.</p>
            <div class="row"><input type="text" id="sc-name" placeholder="МБОУ СОШ № 7" style="flex:1"><button class="btn sm" data-act="scCreate">Создать</button></div></div>` : ""}`);
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
            <div class="card-title">🔑 Пригласить коллег</div>
            <p class="muted small">Классные руководители вступают по коду <b>${esc(me.school.code)}</b> или по ссылке.</p>
            <div class="row"><button class="btn sm" data-act="shareLink" data-link="${esc(me.school.teacher_link)}" data-text="Подключитесь к школе в «Перемене»">Ссылка для учителей</button>
            <button class="btn sm ghost" data-act="shareLink" data-link="${esc(me.school.admin_link)}" data-text="Доступ администратора школы в «Перемене»">Для администрации</button></div>
          </div>
          <button class="btn block" data-act="newCampaign" style="margin-bottom:12px">📣 Сбор на параллель</button>
          <h2>Классы</h2>
          ${d.classes.length ? d.classes.map((c) => {
            const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
            return `<div class="card"><div class="row spread"><b>${esc(c.title || c.chat_id)}</b>${c.bot_is_admin ? "" : '<span class="chip">⚠️ бот не админ</span>'}</div>
              <div class="muted small">${esc(c.teacher)} · ${c.open_polls ? `открытых сборов: ${c.open_polls}` : "открытых сборов нет"}</div>
              ${c.open_polls ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="small">${c.answered}/${c.total} ответили</div>` : ""}</div>`;
          }).join("") : '<p class="muted">Пока никто из классных руководителей не вступил в школу. Отправьте им ссылку.</p>'}
          ${d.campaigns.length ? `<h2>Сборы на параллель</h2>${d.campaigns.map((c) => {
            const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
            return `<div class="card tap" data-act="openCampaign" data-id="${c.id}"><div class="card-title">${esc(c.title)}</div>
              <div class="row spread" style="margin-top:8px"><div class="big" style="font-size:28px">${c.answered}<small>/${c.total}</small></div><span class="muted small">${c.classes.length} ${plural(c.classes.length, "класс", "класса", "классов")}</span></div>
              <div class="bar"><i style="width:${pct}%"></i></div></div>`;
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
          <div class="seg" style="margin:6px 0 14px">${S.templates.filter((t) => t.key !== "money").map((t) => `<button data-act="cmpTpl" data-key="${t.key}" class="${t.key === draft.template ? "on" : ""}">${esc(t.label)}</button>`).join("")}</div>
          <label class="field"><span>Текст (первая строка — заголовок)</span><textarea id="cmp-text">${esc(draft.text || "")}</textarea></label>
          ${draft.template === "choice" ? `<label class="field"><span>Варианты — каждый с новой строки</span><textarea id="cmp-opts" style="min-height:90px">${esc(draft.opts || "")}</textarea></label>` : `<p class="muted small">Кнопки: ${tpl.options.map(esc).join(" · ")}</p>`}
          <div class="seg">${[["tomorrow", "Завтра 18:00"], ["friday", "Пятница 18:00"], ["week", "Через неделю"], ["", "Без срока"]].map(([k, l]) => `<button data-act="cmpDl" data-k="${k}" class="${draft.deadline === k ? "on" : ""}">${l}</button>`).join("")}</div>
          <button class="btn block" data-act="cmpSend" style="margin-top:18px">Запустить в ${draft.chats.size} ${plural(draft.chats.size, "классе", "классах", "классах")}</button>`);
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
        if (!draft.chats.size) return toast("Выберите классы", true);
        if (!draft.text || draft.text.trim().length < 3) return toast("Напишите текст", true);
        el.disabled = true; el.textContent = "Запускаю…";
        try {
          const options = draft.template === "choice" ? (draft.opts || "").split("\n").map((s) => s.trim()).filter(Boolean) : [];
          const res = await api("/api/school/campaigns", { method: "POST", body: { chat_ids: [...draft.chats], template: draft.template, text: draft.text, options, deadline: draft.deadline || null } });
          haptic("success");
          toast(`Запущено в ${res.reports.length} ${plural(res.reports.length, "классе", "классах", "классах")}`);
          S.stack = [{ name: "school", args: {} }];
          S.route = null;
          go("campaign", { id: res.campaign.id }, false);
          S.stack = [{ name: "school", args: {} }];
          syncBack();
        } catch (e) { toast(e.message, true); el.disabled = false; }
      };
      draw();
    },

    async campaign({ id }) {
      loading();
      try {
        const c = await api(`/api/school/campaigns/${id}`);
        const pct = c.total ? Math.round((100 * c.answered) / c.total) : 0;
        render(`<div class="card">
            <div class="card-title" style="font-size:19px">${esc(c.title)}</div>
            <div class="muted small">${c.classes.length} ${plural(c.classes.length, "класс", "класса", "классов")}${c.deadline_text && c.deadline_text !== "без срока" ? " · срок: " + esc(c.deadline_text) : ""}</div>
            <div class="big" style="margin-top:18px">${c.answered}/${c.total}</div>
            <div class="bar"><i style="width:${pct}%"></i></div>
            <ul class="list">${c.options.map((o, i) => `<li><span>${esc(o)}</span><b>${c.per_option[i] || 0}</b></li>`).join("")}</ul>
            <button class="btn block" data-act="cmpRemind" data-id="${c.id}" style="margin-top:12px" ${c.answered < c.total ? "" : "disabled"}>Напомнить всем, кто молчит</button>
          </div>
          <h2>По классам</h2>
          ${c.classes.sort((a, b) => (a.answered / (a.total || 1)) - (b.answered / (b.total || 1))).map((r) => {
            const p = r.total ? Math.round((100 * r.answered) / r.total) : 0;
            return `<div class="card"><div class="row spread"><b>${esc(r.title)}</b><span>${r.answered}/${r.total}</span></div>
              <div class="bar"><i style="width:${p}%"></i></div>
              ${r.missing.length ? `<div class="muted small">Не ответили: ${r.missing.slice(0, 15).map(esc).join(", ")}${r.missing.length > 15 ? " …" : ""}</div>` : '<div class="ok small">Все ответили ✓</div>'}</div>`;
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
    try { const r = await api(`/api/school/campaigns/${el.dataset.id}/remind`, { method: "POST" }); toast(`Напомнил: ${r.sent + r.group}`); }
    catch (e) { toast(e.message, true); }
    el.disabled = false;
  };
  async function reloadMe() { S.me = await api("/api/me"); }
  ACTIONS.scJoin = async () => {
    const code = document.getElementById("sc-code").value.trim();
    if (!code) return;
    try { const r = await api("/api/school/join", { method: "POST", body: { code } }); await reloadMe(); toast(`Вы в школе «${r.name}»`); go("school", {}, false); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.scCreate = async () => {
    const name = document.getElementById("sc-name").value.trim();
    if (name.length < 3) return toast("Укажите название", true);
    try { await api("/api/school", { method: "POST", body: { name } }); await reloadMe(); haptic("success"); toast("Школа создана"); go("school", {}, false); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.invite = async (el) => {
    try {
      const [inv, subs] = await Promise.all([api(`/api/chats/${el.dataset.id}/invite`, { method: "POST" }), api(`/api/chats/${el.dataset.id}/subjects`)]);
      modal(`<h2 style="margin-top:0">Пригласить предметника</h2>
        <p class="muted small">По ссылке учитель сможет публиковать домашку и объявления в этот класс — без прав администратора в чате. Ссылка действует 7 дней.</p>
        <div class="bubble small" style="word-break:break-all">${esc(inv.link)}</div>
        ${subs.length ? `<h2>Уже подключены</h2><ul class="list">${subs.map((x) => `<li><span>${esc(x.name)} · ${esc(x.subject || "—")}</span><button class="btn sm ghost" value="rm:${x.user_id}" formnovalidate>Отключить</button></li>`).join("")}</ul>` : ""}
        <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button><button class="btn" value="share">Поделиться</button></div>`,
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
    modal(`<h2 style="margin-top:0">🤒 Ребёнок не придёт</h2>
      ${cls.length > 1 ? `<label class="field"><span>Класс</span><select name="chat">${cls.map((c) => `<option value="${c.chat_id}">${esc(c.title)}</option>`).join("")}</select></label>` : ""}
      <label class="field"><span>Фамилия и имя ребёнка</span><input type="text" name="child" required maxlength="100" value="${esc(cls[0].child_name)}" placeholder="Иванов Пётр"></label>
      <div class="field"><span class="muted small" style="font-weight:600">Когда</span>
        <div class="seg" style="margin-top:6px"><label><input type="radio" name="day" value="today" checked> Сегодня</label>&nbsp;&nbsp;<label><input type="radio" name="day" value="tomorrow"> Завтра</label></div></div>
      <label class="field"><span>Причина</span><select name="reason"><option value="sick">Болеет</option><option value="family">Семейные обстоятельства</option><option value="other">Другая причина</option></select></label>
      <label class="field"><span>Комментарий для учителя (необязательно)</span><input type="text" name="comment" maxlength="200"></label>
      <p class="muted small">Учитель увидит это в списке «кто не придёт» — писать в общий чат не нужно.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Отметить</button></div>`,
    async (fd) => {
      const chat = Number(fd.get("chat") || cls[0].chat_id);
      await api("/api/my/absences", { method: "POST", body: { chat_id: chat, day: fd.get("day"), reason: fd.get("reason"), child_name: fd.get("child"), comment: fd.get("comment") || "" } });
      haptic("success"); toast("Отмечено — учитель увидит"); VIEWS.my();
    });
  };
  ACTIONS.absCancel = async (el) => {
    try { await api(`/api/my/absences/${el.dataset.id}`, { method: "DELETE" }); toast("Отметка снята"); VIEWS.my(); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.openAnswer = (el) => {
    const q = S.openQ.find((x) => x.key === el.dataset.key);
    modal(`<h2 style="margin-top:0">Ответ на частый вопрос</h2>
      <p class="muted small">Спросили ${q.count} раз(а). Ответьте один раз — дальше бот будет отвечать сам.</p>
      <label class="field"><span>Вопрос</span><input type="text" name="question" required value="${esc(q.question)}"></label>
      <label class="field"><span>Ваш ответ</span><textarea name="answer" required></textarea></label>
      ${S.me.school ? `<label class="switch"><span>🏛 В общую базу школы</span><input type="checkbox" name="shared"></label>` : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Сохранить</button></div>`,
    async (fd) => {
      const r = await api("/api/faq/open", { method: "POST", body: { key: q.key, question: fd.get("question"), answer: fd.get("answer"), shared: fd.get("shared") === "on" } });
      haptic("success"); toast(`Сохранено, снято вопросов: ${r.resolved}`); VIEWS.faq();
    });
  };

  ACTIONS.openPoll = (el) => go("pollDetail", { id: el.dataset.id });
  ACTIONS.goBack = () => back();
  ACTIONS.newPoll = () => go("newPoll");
  ACTIONS.toggleList = () => { const l = document.getElementById("fullList"); l.hidden = !l.hidden; if (!l.hidden) l.scrollIntoView({ behavior: "smooth" }); };
  ACTIONS.remind = async (el) => {
    el.disabled = true;
    try {
      const r = await api(`/api/polls/${el.dataset.id}/remind`, { method: "POST" });
      haptic("success");
      toast(`Напомнил: ${r.sent} в личку${r.group ? `, ${r.group} — в чате` : ""}`);
    } catch (e) { toast(e.message, true); }
    el.disabled = false;
  };
  ACTIONS.closePoll = (el) => modal(`<h2 style="margin-top:0">Закрыть сбор?</h2><p class="muted">Ответы больше не принимаются, итог придёт вам в личку.</p>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn danger" value="ok">Закрыть</button></div>`,
    async () => { await api(`/api/polls/${el.dataset.id}/close`, { method: "POST" }); toast("Сбор закрыт"); VIEWS.pollDetail({ id: el.dataset.id }); });
  ACTIONS.export = (el) => {
    const url = location.origin + el.dataset.url;
    try {
      if (WA?.downloadFile) { WA.downloadFile(url, `sbor_${el.dataset.id}.csv`); return; }
    } catch (_) { /* упадём на обычную ссылку */ }
    window.open(url, "_blank");
  };

  // ------------------------------------------------------------------ действия: справочник
  ACTIONS.testFaq = async () => {
    const q = document.getElementById("faq-test").value.trim();
    const out = document.getElementById("faq-test-out");
    if (q.length < 2) return;
    try {
      const hits = await api(`/api/faq/test?q=${encodeURIComponent(q)}`);
      out.innerHTML = hits.length ? hits.map((h, i) => `<div style="margin-top:12px">
          <div class="small ${h.auto ? "ok" : "muted"}">${i === 0 && h.auto ? "✓ Бот ответит сам" : "Бот предложит как вариант"} · совпадение ${Math.round(h.score * 100)}%${h.group_auto ? " · ответит и в чате (если включено)" : ""}</div>
          <b>${esc(h.entry.question)}</b><div>${esc(h.entry.answer)}</div></div>`).join("")
        : '<p class="muted small" style="margin-top:10px">Бот не найдёт ответа и предложит передать вопрос вам. Добавьте карточку или ключевые слова.</p>';
    } catch (e) { toast(e.message, true); }
  };
  ACTIONS.editFaq = (el) => {
    const e = (S.faq || []).find((x) => String(x.id) === el.dataset.id) || { question: "", answer: "", keywords: "", own: true };
    if (e.id && !e.own) {
      modal(`<h2 style="margin-top:0">${esc(e.question)}</h2><p>${esc(e.answer)}</p>
        <p class="muted small">🏛 Карточка из общей базы школы — её ведёт автор. Если нужен другой ответ для вашего класса, создайте свою карточку: она будет важнее школьной.</p>
        <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Закрыть</button><button class="btn" value="copy">Сделать свою</button></div>`,
      async () => { S.faqCopy = e; ACTIONS.editFaq({ dataset: {} }); return true; });
      return;
    }
    const base = !e.id && S.faqCopy ? { ...S.faqCopy, id: undefined, shared: false } : e;
    S.faqCopy = null;
    modal(`<h2 style="margin-top:0">${e.id ? "Карточка справочника" : "Новая карточка"}</h2>
      <label class="field"><span>Вопрос — так, как спрашивают</span><input type="text" name="question" required value="${esc(base.question)}"></label>
      <label class="field"><span>Ваш ответ</span><textarea name="answer" required>${esc(base.answer)}</textarea></label>
      <label class="field"><span>Ключевые слова через запятую (необязательно)</span><input type="text" name="keywords" value="${esc(base.keywords)}" placeholder="собрание, родительское"></label>
      ${S.me.school ? `<label class="switch"><span>🏛 В общую базу школы — бот ответит родителям всех классов</span><input type="checkbox" name="shared" ${base.shared ? "checked" : ""}></label>` : ""}
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="save">Сохранить</button></div>
      ${e.id ? '<button class="btn danger block" value="delete" style="margin-top:10px" formnovalidate>Удалить карточку</button>' : ""}`,
    async (fd, action) => {
      if (action === "delete") { await api(`/api/faq/${e.id}`, { method: "DELETE" }); toast("Удалено"); }
      else {
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
      box.innerHTML = items.length ? items.map((m) => `<div class="card shelf">
          <div class="card-title">${{ image: "🖼", video: "🎬", audio: "🎧" }[m.type] || "📄"} ${esc(m.filename || "Файл")}</div>
          ${m.title ? `<div>${esc(m.title)}</div>` : ""}
          <div class="muted small" style="margin-top:4px">${m.chat_title ? "отправляли в " + esc(m.chat_title) + " " : "на полке с "}${esc(m.date)}${m.tags ? " · " + m.tags.split(",").map((t) => "#" + esc(t.trim())).join(" ") : ""}</div>
          <div class="row" style="margin-top:10px">
            <button class="btn sm outline" data-act="sendMe" data-id="${m.id}">📥 Прислать мне</button>
            ${(S.me.is_teacher || S.me.is_subject_teacher) ? `<button class="btn sm ghost" data-act="sendChat" data-id="${m.id}">➡️ В чат</button><button class="btn sm ghost" data-act="editMat" data-id="${m.id}">✏️</button>` : ""}
          </div></div>`).join("")
        : `<div class="empty"><b>${q ? "Ничего не нашлось" : "Пока пусто"}</b>${q ? "Попробуйте другие слова или тег." : (S.me.is_teacher ? "Перешлите боту файл с подписью — он появится здесь." : "Здесь появятся материалы, которые учитель отправлял в чат класса.")}</div>`;
    } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  ACTIONS.shelfSearch = () => { S.shelfQ = document.getElementById("shelf-q").value.trim(); loadShelf(); };
  ACTIONS.sendMe = async (el) => {
    try { await api(`/api/materials/${el.dataset.id}/send`, { method: "POST", body: {} }); haptic("success"); toast("Отправил вам в личку с ботом"); }
    catch (e) { toast(e.message, true); }
  };
  ACTIONS.sendChat = (el) => {
    const chats = S.me.board_chats;
    modal(`<h2 style="margin-top:0">Переслать в чат</h2>
      <label class="field"><span>Класс</span><select name="chat">${chats.map((c) => `<option value="${c.chat_id}">${esc(c.title)}</option>`).join("")}</select></label>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="ok">Отправить</button></div>`,
    async (fd) => { await api(`/api/materials/${el.dataset.id}/send`, { method: "POST", body: { chat_id: Number(fd.get("chat")) } }); haptic("success"); toast("Отправлено в чат"); });
  };
  ACTIONS.editMat = (el) => {
    const m = S.materials.find((x) => String(x.id) === el.dataset.id);
    modal(`<h2 style="margin-top:0">${esc(m.filename)}</h2>
      <label class="field"><span>Подпись — по ней ищется файл</span><input type="text" name="title" value="${esc(m.title)}"></label>
      <label class="field"><span>Теги через запятую</span><input type="text" name="tags" value="${esc(m.tags)}" placeholder="безопасность, памятка"></label>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="save">Сохранить</button></div>
      <button class="btn danger block" value="delete" style="margin-top:10px">Убрать с полки</button>`,
    async (fd, action) => {
      if (action === "delete") await api(`/api/materials/${m.id}`, { method: "DELETE" });
      else await api(`/api/materials/${m.id}`, { method: "PUT", body: { title: fd.get("title"), tags: fd.get("tags") } });
      toast("Готово"); loadShelf();
    });
  };
  async function uploadFile(file) {
    if (!file) return;
    modal(`<h2 style="margin-top:0">${esc(file.name)}</h2>
      <label class="field"><span>Подпись</span><input type="text" name="title" placeholder="Памятка по пожарной безопасности"></label>
      <label class="field"><span>Теги через запятую</span><input type="text" name="tags" placeholder="безопасность"></label>
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
      toast(r.members < 0 ? "Нужны права администратора" : `В списке класса: ${r.class_list.length}`);
    } catch (e) { toast(e.message, true); }
  };
  ACTIONS.history = async (el) => {
    el.disabled = true;
    try { const r = await api(`/api/chats/${el.dataset.id}/history`, { method: "POST" }); toast(`Добавлено файлов: ${r.added}`); }
    catch (e) { toast(e.message, true); }
    el.disabled = false;
  };

  // ------------------------------------------------------------------ действия: родитель
  ACTIONS.answer = async (el) => {
    const card = el.closest(".card");
    try {
      await api(`/api/my/polls/${el.dataset.id}/answer`, { method: "POST", body: { option: Number(el.dataset.opt) } });
      card.querySelectorAll(".option").forEach((b) => b.classList.toggle("selected", b === el));
      haptic("success");
      toast("Ответ отправлен учителю");
    } catch (e) { toast(e.message, true); }
  };
  ACTIONS.ask = async () => {
    const q = document.getElementById("ask-q").value.trim();
    const out = document.getElementById("ask-out");
    if (q.length < 2) return;
    out.innerHTML = '<div class="loading">Ищу…</div>';
    try {
      const r = await api(`/api/ask?q=${encodeURIComponent(q)}`);
      let html = "";
      if (r.answer) {
        html += `<div class="card faq"><div>${esc(r.answer.answer)}</div>
          <div class="answer-foot">📖 Из справочника класса · сохранено учителем</div>
          <button class="btn sm ghost" data-act="notThat" data-id="${r.answer.id}" style="margin-top:10px">Это не то</button></div>`;
      }
      if (r.suggestions.length) {
        html += `<h2>${r.answer ? "Ещё похожее" : "Возможно, вы имели в виду"}</h2>` + r.suggestions.map((s) => `<div class="card faq"><b>${esc(s.question)}</b><div style="margin-top:4px">${esc(s.answer)}</div></div>`).join("");
      }
      if (r.materials.length) {
        html += "<h2>На полке</h2>" + r.materials.map((m) => `<div class="card shelf"><b>📄 ${esc(m.filename)}</b>${m.title ? `<div>${esc(m.title)}</div>` : ""}
          <button class="btn sm outline" data-act="sendMe" data-id="${m.id}" style="margin-top:8px">📥 Прислать мне</button></div>`).join("");
      }
      if (!r.answer) {
        html += `<div class="card"><p style="margin-top:0">${r.suggestions.length || r.materials.length ? "Не то?" : "Готового ответа пока нет."} Могу передать вопрос учителю — он ответит в рабочее время.</p>
          <button class="btn block" data-act="forward">✉️ Передать учителю</button></div>`;
      }
      out.innerHTML = html;
      S.lastQ = q;
    } catch (e) { out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  };
  ACTIONS.notThat = async (el) => {
    try { await api(`/api/faq/${el.dataset.id}/not-that`, { method: "POST" }); } catch (_) { /* не критично */ }
    el.outerHTML = '<button class="btn sm" data-act="forward" style="margin-top:10px">✉️ Передать вопрос учителю</button>';
  };
  ACTIONS.forward = async (el) => {
    el.disabled = true;
    try {
      await api("/api/ask/forward", { method: "POST", body: { text: S.lastQ || document.getElementById("ask-q")?.value || "" } });
      haptic("success");
      el.outerHTML = '<p class="ok">✓ Передано. Подробности — в личке с ботом.</p>';
    } catch (e) { toast(e.message, true); el.disabled = false; }
  };

  // ------------------------------------------------------------------ демо-режим
  function demoBanner() {
    const parent = !S.me.is_teacher;
    document.body.classList.add("demo");
    document.querySelector(".topbar").insertAdjacentHTML("afterend", `<div class="demo-bar" role="note">
      <span><b>Демо-класс.</b> Данные вымышленные, сообщения никому не уходят. Песочница удалится через сутки.</span>
      <span class="demo-actions">
        <button type="button" data-role="${parent ? "teacher" : "parent"}">${parent ? "👩‍🏫 Глазами учителя" : "👪 Глазами родителя"}</button>
        <a href="/login">Войти по-настоящему</a>
      </span></div>`);
    document.querySelector(".demo-bar [data-role]").addEventListener("click", async (ev) => {
      ev.currentTarget.disabled = true;
      try {
        const r = await fetch(`/api/demo/role?role=${ev.currentTarget.dataset.role}`,
          { method: "POST", headers: { "X-Peremena": "1" }, credentials: "same-origin" });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Не удалось переключиться");
        location.href = "/app/";
      } catch (e) { toast(e.message, true); ev.currentTarget.disabled = false; }
    });
  }

  // ------------------------------------------------------------------ старт
  async function boot() {
    if (location.protocol === "file:") {
      render(`<div class="empty"><b>Страница открыта как файл</b>Мини-приложение работает только через сервер «Перемены»: данные приходят с него.<br><br>
        Запустите сервис (<code>python -m app run</code> или <code>docker compose up</code>) и откройте<br><code>http://localhost:8080/app/</code></div>`);
      return;
    }
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
        '<div class="web-links"><a href="/">Сайт</a><button type="button" id="logout">Выйти</button></div>');
      document.getElementById("logout").addEventListener("click", async () => {
        try { await fetch("/api/auth/logout", { method: "POST", headers: { "X-Peremena": "1" } }); } catch (_) { /* всё равно уходим */ }
        location.href = "/";
      });
    }
    if (S.me.demo) demoBanner();
    const role = S.me.is_teacher ? "классный руководитель" : S.me.is_subject_teacher ? "предметник"
      : S.me.is_admin ? "администрация" : "";
    document.getElementById("who").textContent = S.me.user.name + (role ? " · " + role : "");
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
