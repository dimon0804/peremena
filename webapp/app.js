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
  const mins = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const toMins = (s) => { const [h, m] = (s || "0:0").split(":").map(Number); return h * 60 + (m || 0); };
  const fmtMinutes = (t) => { t = Math.round(t); const h = Math.floor(t / 60), m = t % 60; return h ? (m ? `${h} ч ${m} мин` : `${h} ч`) : `${m} мин`; };

  function haptic(kind) {
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
    const headers = {};
    if (WA?.initData) headers["X-Max-Init-Data"] = WA.initData;
    let url = path;
    if (!WA?.initData && DEV_USER) url += (url.includes("?") ? "&" : "?") + "dev_user=" + encodeURIComponent(DEV_USER);
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    const res = await fetch(url, { method, headers, body: payload });
    let data = null;
    try { data = await res.json(); } catch (_) { /* пустой ответ */ }
    if (!res.ok) {
      const msg = (data && (data.detail?.[0]?.msg || data.detail)) || `Ошибка ${res.status}`;
      throw new Error(typeof msg === "string" ? msg : "Ошибка запроса");
    }
    return data;
  }

  function render(html) { $view.innerHTML = html; window.scrollTo(0, 0); }
  function loading() { $view.innerHTML = '<div class="loading">Загружаю…</div>'; }
  function fail(e) { render(`<div class="empty"><b>Не получилось</b>${esc(e.message || e)}</div>`); }

  // ------------------------------------------------------------------ навигация
  const TEACHER_TABS = [
    ["polls", "📋", "Сборы"], ["faq", "📖", "Справочник"], ["shelf", "🗂", "Полка"], ["more", "⏱", "Ещё"],
  ];
  const PARENT_TABS = [["my", "📋", "Сборы"], ["ask", "❓", "Спросить"], ["shelf", "🗂", "Материалы"]];

  function drawTabs(active) {
    const tabs = S.me.is_teacher ? TEACHER_TABS : PARENT_TABS;
    $tabbar.hidden = false;
    $tabbar.innerHTML = tabs.map(([k, ico, label]) =>
      `<button data-tab="${k}" class="${k === active ? "on" : ""}"><span class="ico">${ico}</span>${label}</button>`).join("");
  }
  $tabbar.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) { S.stack = []; go(b.dataset.tab, {}, false); }
  });

  function go(name, args = {}, push = true) {
    if (push && S.route) S.stack.push(S.route);
    S.route = { name, args };
    const tab = { pollDetail: "polls", newPoll: "polls" }[name] || name;
    drawTabs(tab);
    syncBack();
    (VIEWS[name] || VIEWS[S.me.is_teacher ? "polls" : "my"])(args);
  }
  function back() {
    const prev = S.stack.pop();
    if (!prev) return;
    S.route = prev;
    drawTabs({ pollDetail: "polls", newPoll: "polls" }[prev.name] || prev.name);
    syncBack();
    VIEWS[prev.name](prev.args);
  }
  function syncBack() {
    try {
      if (S.stack.length) WA?.BackButton?.show(); else WA?.BackButton?.hide();
    } catch (_) { /* в браузере нет */ }
  }
  try { WA?.BackButton?.onClick(back); } catch (_) { /* в браузере нет */ }

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
    $modal.showModal();
  }

  // ================================================================== УЧИТЕЛЬ
  const pollCard = (p) => {
    const pct = p.total ? Math.round((100 * p.answered) / p.total) : 0;
    const status = p.closed ? '<span class="chip done">закрыт</span>' : `<span class="chip accent">срок: ${esc(p.deadline_text)}</span>`;
    return `<div class="card tap" data-act="openPoll" data-id="${p.id}">
      <div class="row spread"><div class="card-title">${esc(p.title)}</div>${status}</div>
      <div class="muted small">${esc(p.chat_title)}</div>
      <div class="row spread" style="margin-top:10px"><div class="big" style="font-size:30px">${p.answered}<small>/${p.total}</small></div>
      <div class="muted small">${p.missing.length ? "не ответили: " + p.missing.length : (p.total ? "ответили все ✓" : "")}</div></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  };

  const VIEWS = {
    async polls() {
      loading();
      try {
        const items = await api("/api/polls");
        const open = items.filter((p) => !p.closed), closed = items.filter((p) => p.closed);
        render(`<h1>Сборы</h1>
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
            ${p.closed ? "" : `<div class="btns">
              <button class="btn" data-act="remind" data-id="${p.id}" ${p.missing.length ? "" : "disabled"}>Напомнить</button>
              <button class="btn outline" data-act="toggleList">Список</button></div>`}
          </div>
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
            const res = await api("/api/polls", { method: "POST", body: { chat_id: Number(draft.chat_id), template: draft.template, text: draft.text, options, deadline } });
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
        const items = await api("/api/faq");
        render(`<h1>Справочник</h1>
          <div class="card faq">
            <div class="card-title tag-faq">Проверить, как ответит бот</div>
            <div class="row" style="margin-top:8px"><input type="search" id="faq-test" placeholder="Например: во сколько собрание?" style="flex:1"><button class="btn sm" data-act="testFaq">Проверить</button></div>
            <div id="faq-test-out"></div>
          </div>
          ${items.length ? "" : '<div class="empty"><b>Справочник пуст</b>Добавьте вопросы, которые задают чаще всего. Бот будет отвечать на них родителям в любое время — и подписывать, что это сохранили вы.</div>'}
          ${items.map((e) => `<div class="card faq tap" data-act="editFaq" data-id="${e.id}">
            <div class="card-title">${esc(e.question)}</div>
            <div style="margin-top:4px">${esc(e.answer)}</div>
            <div class="row" style="margin-top:8px"><span class="chip">ответов: ${e.hits}</span>${e.misses ? `<span class="chip">«не то»: ${e.misses}</span>` : ""}${e.keywords ? `<span class="muted small">🔑 ${esc(e.keywords)}</span>` : ""}</div>
          </div>`).join("")}
          <button class="btn fab" data-act="editFaq">＋ Добавить</button>`);
        S.faq = items;
      } catch (e) { fail(e); }
    },

    async shelf() {
      loading();
      const teacher = S.me.is_teacher;
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
              <div class="row"><button class="btn sm ghost" data-act="syncChat" data-id="${c.chat_id}">🔄 Обновить список</button><button class="btn sm ghost" data-act="history" data-id="${c.chat_id}">📥 Файлы из истории</button></div>
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
        const items = await api("/api/my/polls");
        render(`<h1>Сборы</h1>
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

  // ------------------------------------------------------------------ действия: сборы
  ACTIONS.openPoll = (el) => go("pollDetail", { id: el.dataset.id });
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
    const e = (S.faq || []).find((x) => String(x.id) === el.dataset.id) || { question: "", answer: "", keywords: "" };
    modal(`<h2 style="margin-top:0">${e.id ? "Карточка справочника" : "Новая карточка"}</h2>
      <label class="field"><span>Вопрос — так, как спрашивают</span><input type="text" name="question" required value="${esc(e.question)}"></label>
      <label class="field"><span>Ваш ответ</span><textarea name="answer" required>${esc(e.answer)}</textarea></label>
      <label class="field"><span>Ключевые слова через запятую (необязательно)</span><input type="text" name="keywords" value="${esc(e.keywords)}" placeholder="собрание, родительское"></label>
      <div class="btns"><button class="btn ghost" value="cancel" formnovalidate>Отмена</button><button class="btn" value="save">Сохранить</button></div>
      ${e.id ? '<button class="btn danger block" value="delete" style="margin-top:10px" formnovalidate>Удалить карточку</button>' : ""}`,
    async (fd, action) => {
      if (action === "delete") { await api(`/api/faq/${e.id}`, { method: "DELETE" }); toast("Удалено"); }
      else {
        const body = { question: fd.get("question"), answer: fd.get("answer"), keywords: fd.get("keywords") || null };
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
            ${S.me.is_teacher ? `<button class="btn sm ghost" data-act="sendChat" data-id="${m.id}">➡️ В чат</button><button class="btn sm ghost" data-act="editMat" data-id="${m.id}">✏️</button>` : ""}
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
    const chats = S.me.chats;
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

  // ------------------------------------------------------------------ старт
  async function boot() {
    if (!WA?.initData && !DEV_USER) {
      render('<div class="empty"><b>Откройте «Перемену» в MAX</b>Мини-приложение работает внутри мессенджера: кнопка «Открыть» в чате с ботом.</div>');
      return;
    }
    try {
      S.me = await api("/api/me");
    } catch (e) { fail(e); return; }
    document.getElementById("who").textContent = S.me.user.name + (S.me.is_teacher ? " · учитель" : "");
    const start = WA?.initDataUnsafe?.start_param || params.get("start") || "";
    if (S.me.is_teacher) {
      if (/^poll_\d+$/.test(start)) { S.route = { name: "polls", args: {} }; go("pollDetail", { id: start.slice(5) }); return; }
      go(["faq", "shelf", "more"].includes(start) ? start : "polls", {}, false);
    } else {
      go(start === "ask" ? "ask" : start === "shelf" ? "shelf" : "my", {}, false);
    }
  }
  boot();
})();
