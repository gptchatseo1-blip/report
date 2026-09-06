(() => {
  const closeNotice = notice => notice?.classList.add('is-closing');

  document.querySelectorAll('[data-flash-notice]').forEach(notice => {
    notice.querySelector('[data-flash-close]')?.addEventListener('click', () => closeNotice(notice));
    if (!notice.classList.contains('flash-notice--error')) {
      window.setTimeout(() => closeNotice(notice), 6500);
    }
  });

  document.querySelectorAll('[data-yandex-sync-form]').forEach(form => {
    form.addEventListener('submit', event => {
      if (event.defaultPrevented || !form.reportValidity()) return;
      const label = form.dataset.syncLabel || 'данных';
      const button = form.querySelector('[data-sync-submit]');
      const progress = form.querySelector('[data-sync-progress]');
      if (button) {
        button.disabled = true;
        button.textContent = `Синхронизация ${label}…`;
      }
      if (progress) {
        progress.hidden = false;
        progress.textContent = `Идёт синхронизация ${label}. Не закрывайте страницу.`;
      }
    });
  });
})();
