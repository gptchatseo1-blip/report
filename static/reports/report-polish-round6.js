(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const modal = document.querySelector('[data-manual-dynamics-modal-round2]');
  const trigger = document.querySelector('[data-manual-dynamics-open-round2]');
  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  if (!modal || !trigger || !manualField) return;

  const readJson = id => {
    try {
      return JSON.parse(document.getElementById(id)?.textContent || '[]');
    } catch (_error) {
      return [];
    }
  };
  const normalized = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  const engineLabel = engine => (
    normalized(engine) === 'yandex' ? 'Яндекс' : normalized(engine) === 'google' ? 'Google' : String(engine || 'Поиск')
  );
  const segmentTitle = segment => `${engineLabel(segment.engine)} · ${segment.region || 'Регион не указан'}`;
  const segmentsByTitle = new Map(readJson('topvisor-editor-segments').map(segment => [segmentTitle(segment), segment]));

  const currentUrl = new URL(window.location.href);
  const basePath = currentUrl.pathname.endsWith('/') ? currentUrl.pathname : `${currentUrl.pathname}/`;
  const refreshUrl = `${basePath}topvisor-editor/refresh/`;
  const clearUrl = `${basePath}topvisor-editor/clear/`;
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

  async function clearSegment(button, segment) {
    if (!confirm(`Очистить ручные изменения для «${segmentTitle(segment)}» и вернуть автоматические данные?`)) return;
    button.disabled = true;
    setLocalStatus('Очистка…', 'progress');
    try {
      await persistCurrentRows();
      const data = await postJson(clearUrl, {engine: segment.engine, region: segment.region});
      reloadAndReopen(data.message || 'Ручные данные очищены.');
    } catch (error) {
      button.disabled = false;
      setLocalStatus(error.message || 'Ошибка очистки', 'error');
    }
  }

  function decorateSegments() {
    modal.querySelectorAll('.manual-segment-card').forEach(section => {
      if (section.querySelector('[data-topvisor-clear-segment]')) return;
      const title = section.querySelector('h5')?.textContent?.trim() || '';
      const segment = segmentsByTitle.get(title);
      if (!segment) return;

      const add = section.querySelector('.manual-add-row');
      if (!add) return;
      const actions = document.createElement('div');
      actions.dataset.topvisorSegmentMaintenance = '';
      actions.style.display = 'flex';
      actions.style.alignItems = 'center';
      actions.style.justifyContent = 'space-between';
      actions.style.gap = '.75rem';
      actions.style.marginTop = '.75rem';

      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'delete-button';
      clear.dataset.topvisorClearSegment = '';
      clear.textContent = 'Очистить';
      clear.title = 'Удалить ручные изменения и вернуть автоматические данные';
      clear.addEventListener('click', () => { void clearSegment(clear, segment); });

      add.before(actions);
      actions.append(clear, add);
    });
  }

  const footer = modal.querySelector('[data-round2-manual-footer]');
  if (footer && !footer.querySelector('[data-topvisor-refresh-editor]')) {
    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.className = 'secondary';
    refresh.dataset.topvisorRefreshEditor = '';
    refresh.textContent = 'Обновить';
    refresh.title = 'Перечитать автоматические данные из последних синхронизированных снимков';
    refresh.style.marginRight = 'auto';
    refresh.addEventListener('click', () => { void refreshAll(refresh); });
    footer.prepend(refresh);
  }

  const container = modal.querySelector('[data-round2-manual-segments]');
  if (container) {
    decorateSegments();
    new MutationObserver(decorateSegments).observe(container, {childList: true, subtree: true});
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
