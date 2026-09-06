(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const modal = document.querySelector('[data-manual-dynamics-modal-round2]');
  const trigger = document.querySelector('[data-manual-dynamics-open-round2]');
  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  if (!modal || !trigger || !manualField) return;

  const currentUrl = new URL(window.location.href);
  const basePath = currentUrl.pathname.endsWith('/') ? currentUrl.pathname : `${currentUrl.pathname}/`;
  const refreshUrl = `${basePath}topvisor-editor/refresh/`;
  const reopenKey = `topvisor-editor-maintenance:${basePath}`;

  const setLocalStatus = (message, kind = 'success') => {
    const status = modal.querySelector('.manual-save-status');
    if (!status) return;
    status.textContent = message;
    status.dataset.kind = kind;
  };

  async function postJson(url, payload = {}) {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
      body: JSON.stringify(payload),
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = {};
    }
    if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось выполнить действие.');
    return data;
  }

  async function persistCurrentRows() {
    const response = await fetch(form.dataset.settingsUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
      body: JSON.stringify({topvisor_manual_rows: manualField.value || '[]'}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось сохранить текущие правки.');
  }

  const reloadAndReopen = message => {
    sessionStorage.setItem(reopenKey, JSON.stringify({message}));
    window.location.reload();
  };

  async function refreshAll(button) {
    button.disabled = true;
    setLocalStatus('Обновление…', 'progress');
    try {
      await persistCurrentRows();
      const data = await postJson(refreshUrl);
      reloadAndReopen(data.message || 'Данные обновлены.');
    } catch (error) {
      button.disabled = false;
      setLocalStatus(error.message || 'Ошибка обновления', 'error');
    }
  }

  const footer = modal.querySelector('[data-round2-manual-footer]');
  if (footer && !footer.querySelector('[data-topvisor-refresh-editor]')) {
    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.className = 'secondary';
    refresh.dataset.topvisorRefreshEditor = '';
    refresh.textContent = 'Обновить';
    refresh.title = 'Обновить автоматические значения только в строках без галочки';
    refresh.style.marginRight = 'auto';
    refresh.addEventListener('click', () => { void refreshAll(refresh); });
    footer.prepend(refresh);
  }

  const container = modal.querySelector('[data-round2-manual-segments]');
  if (container && !modal.querySelector('[data-topvisor-refresh-hint]')) {
    const hint = document.createElement('p');
    hint.dataset.topvisorRefreshHint = '';
    hint.className = 'manual-refresh-hint';
    hint.textContent = 'Кнопка «Обновить» обновляет только строки без галочки. Отмеченные строки сохраняются без изменений и используются в отчёте.';
    container.after(hint);
  }

  const reopen = sessionStorage.getItem(reopenKey);
  if (reopen) {
    sessionStorage.removeItem(reopenKey);
    let message = 'Данные обновлены.';
    try {
      message = JSON.parse(reopen).message || message;
    } catch (_error) {
      // Keep the default message.
    }
    trigger.click();
    requestAnimationFrame(() => setLocalStatus(message, 'success'));
  }
})();
