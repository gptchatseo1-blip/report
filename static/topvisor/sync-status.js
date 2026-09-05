(() => {
  const form = document.querySelector('[data-topvisor-sync-form]');
  if (!form || !window.fetch || !window.FormData) return;

  const button = form.querySelector('[data-topvisor-sync-button]');
  const status = form.querySelector('[data-topvisor-sync-status]');
  const message = form.querySelector('[data-topvisor-sync-message]');
  if (!button || !status || !message) return;

  const defaultLabel = button.textContent;

  const setStatus = (kind, text) => {
    status.hidden = false;
    status.classList.remove('is-running', 'is-success', 'is-error');
    status.classList.add(`is-${kind}`);
    message.textContent = text;
  };

  form.addEventListener('submit', async (event) => {
    if (form.dataset.syncRunning === '1') {
      event.preventDefault();
      return;
    }

    event.preventDefault();
    form.dataset.syncRunning = '1';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Синхронизация…';
    setStatus(
      'running',
      'Синхронизация Topvisor идёт. Загружаем историю позиций — это может занять несколько минут.'
    );

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: {'X-Requested-With': 'XMLHttpRequest'},
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || 'Синхронизация не выполнена.');
      }
      setStatus('success', `${payload.message || 'Синхронизация завершена.'} Обновляем данные…`);
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      setStatus('error', `Ошибка синхронизации: ${error.message || 'неизвестная ошибка'}`);
      form.dataset.syncRunning = '0';
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = defaultLabel;
    }
  });
})();
