/* Кнопка «Печать» на ведомости и бланках: встроенные скрипты запрещены политикой CSP, поэтому отдельный файл. */
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-print]")) window.print();
});
