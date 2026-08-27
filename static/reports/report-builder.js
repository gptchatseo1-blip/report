(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const notice = form.querySelector('[data-report-form-notice]');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  let saveTimer;

  function showNotice(message, kind = 'success') {
    if (!notice) return;
    notice.textContent = message;
    notice.dataset.kind = kind;
    notice.hidden = false;
  }

  function settingsPayload() {
    const payload = {};
    form.querySelectorAll('.report-options input, .report-options select, .report-options textarea').forEach(field => {
      if (!field.name || field.type === 'file') return;
      if (field.dataset.topvisorConfigurationId) {
        payload.topvisor_report_urls ||= {};
        if (field.value.trim()) payload.topvisor_report_urls[field.dataset.topvisorConfigurationId] = field.value.trim();
        return;
      }
      if (field.type === 'checkbox') {
        if (field.name === 'metrika_bar_search_engines') {
          payload[field.name] ||= [];
          if (field.checked) payload[field.name].push(field.value);
        } else {
          payload[field.name] = field.checked;
        }
      } else {
        payload[field.name] = field.value;
      }
    });
    return payload;
  }

  async function saveSettings() {
    try {
      const response = await fetch(form.dataset.settingsUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(settingsPayload()),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось сохранить настройки.');
    } catch (error) {
      showNotice(error.message || 'Не удалось сохранить настройки проекта.', 'error');
    }
  }

  function scheduleSave(delay = 500) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveSettings, delay);
  }

  form.querySelector('.report-options')?.addEventListener('change', () => scheduleSave(150));
  form.querySelector('.report-options')?.addEventListener('input', event => {
    if (event.target.matches('textarea, input[type=url], input[type=text]')) scheduleSave(700);
  });

  form.querySelectorAll('[data-clear-setting]').forEach(button => {
    button.addEventListener('click', () => {
      if (!confirm('Очистить сохранённый список только для этого проекта?')) return;
      const field = document.getElementById(button.dataset.clearSetting);
      if (!field) return;
      field.value = '';
      field.dispatchEvent(new Event('input', {bubbles: true}));
    });
  });

  form.addEventListener('submit', event => {
    const invalid = [...form.querySelectorAll('[data-calendar]')].find(calendar => (
      calendar.querySelectorAll('.calendar-source input[type=checkbox]:checked').length < 2
    ));
    if (!invalid) return;
    event.preventDefault();
    showNotice('Выберите периоды для формирования отчёта.', 'error');
    invalid.classList.add('field-invalid');
    invalid.scrollIntoView({behavior: 'smooth', block: 'center'});
  });

  function replacePeriods(card, periods, sourceName) {
    const optionsRoot = card.querySelector('[data-period-options]');
    const range = card.querySelector('[data-period-range]');
    const details = card.querySelector('[data-period-details]');
    const empty = card.querySelector('[data-source-empty]');
    if (!optionsRoot) return;
    optionsRoot.replaceChildren();
    periods.forEach((period, index) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = sourceName;
      input.value = period.id;
      input.dataset.periodMonth = period.month;
      input.checked = index < 3;
      const text = document.createElement('span');
      text.textContent = period.label;
      label.append(input, text);
      optionsRoot.append(label);
    });
    range.hidden = periods.length === 0;
    details.hidden = periods.length === 0;
    empty.hidden = periods.length > 0;
    card.dispatchEvent(new CustomEvent('source-periods-updated'));
  }

  document.querySelectorAll('[data-ajax-sync-form]').forEach(syncForm => {
    syncForm.addEventListener('submit', async event => {
      event.preventDefault();
      const sourceName = syncForm.dataset.sourceName;
      const card = document.querySelector(`[data-source-period-picker][data-source-name="${sourceName}"]`);
      const button = card?.querySelector('[data-source-sync-button]');
      const status = card?.querySelector('[data-source-sync-notice]');
      if (button) button.disabled = true;
      if (status) {
        status.hidden = false;
        status.dataset.kind = 'progress';
        status.textContent = 'Синхронизация выполняется…';
      }
      try {
        const response = await fetch(syncForm.action, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'X-Requested-With': 'XMLHttpRequest'},
          body: new FormData(syncForm),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.message || 'Синхронизация не выполнена.');
        replacePeriods(card, data.periods || [], sourceName);
        const lastSynced = card?.querySelector('[data-last-synced]');
        if (lastSynced) lastSynced.textContent = data.last_synced_at;
        if (status) {
          status.dataset.kind = 'success';
          status.textContent = data.message;
        }
      } catch (error) {
        if (status) {
          status.dataset.kind = 'error';
          status.textContent = error.message || 'Синхронизация не выполнена.';
        }
      } finally {
        if (button) button.disabled = false;
      }
    });
  });
})();
