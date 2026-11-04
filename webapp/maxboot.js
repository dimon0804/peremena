/* Загрузчик моста MAX — отдельным файлом, потому что CSP запрещает встроенные скрипты.
   Мост MAX нужен только внутри мессенджера. MAX передаёт данные запуска в адресе (#WebAppData=…),
   а при перезагрузке страницы они лежат в sessionStorage — так же их ищет сам max-web-app.js.
   В обычном браузере чужой скрипт не грузим вовсе: кабинет открывается без ожидания CDN. */
(function () {
  var key = "WebAppData", inMax = false;
  try {
    inMax = location.hash.indexOf(key) > -1 || !!sessionStorage.getItem(key);
    var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
    if (!inMax && nav && nav.name.indexOf(key) > -1) inMax = true;
  } catch (e) { /* приватный режим без sessionStorage — значит, не MAX */ }
  if (!inMax) return;
  var s = document.createElement("script");
  s.src = "https://st.max.ru/js/max-web-app.js";
  s.async = false;
  window.__maxScript = s;
  document.head.appendChild(s);
})();
