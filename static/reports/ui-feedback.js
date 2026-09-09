(() => {
  const closeNotice = notice => notice?.classList.add('is-closing');

  document.querySelectorAll('[data-flash-notice]').forEach(notice => {
    notice.querySelector('[data-flash-close]')?.addEventListener('click', () => closeNotice(notice));
    if (!notice.classList.contains('flash-notice--error')) {
      window.setTimeout(() => closeNotice(notice), 6500);
    }
  });

  document.querySelectorAll('[data-yandex-sync-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      if (event.defaultPrevented || !form.reportValidity()) return;
      event.preventDefault();
      const label = form.dataset.syncLabel || 'данных';
      const button = form.querySelector('[data-sync-submit]');
      const progress = form.querySelector('[data-sync-progress]');
      if (button) {
        button.disabled = true;
        button.textContent = `Синхронизация ${label}…`;
      }
      if (progress) {
        progress.hidden = false;
        progress.textContent = `Синхронизация ${label} запускается…`;
      }
      try {
        let response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin',
          headers: {'X-Requested-With': 'XMLHttpRequest'}, body: new FormData(form),
        });
        let data = await response.json();
        if (!response.ok && response.status !== 202) throw new Error(data.message);
        if (progress) progress.textContent = data.message || `Идёт синхронизация ${label}…`;
        while (data.queued && data.status_url) {
          await new Promise(resolve => window.setTimeout(resolve, 2000));
          response = await fetch(data.status_url, {credentials: 'same-origin'});
          data = await response.json();
          if (!response.ok && response.status !== 202) throw new Error(data.message);
        }
        if (!data.ok) throw new Error(data.message);
        if (progress) progress.textContent = data.message || `Синхронизация ${label} завершена.`;
        window.setTimeout(() => window.location.reload(), 700);
      } catch (error) {
        if (progress) progress.textContent = error.message || `Синхронизация ${label} не выполнена.`;
        if (button) {
          button.disabled = false;
          button.textContent = `Синхронизировать ${label}`;
        }
      }
    });
  });
})();
