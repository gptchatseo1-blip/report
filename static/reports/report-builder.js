(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const notice = form.querySelector('[data-report-form-notice]');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  let saveTimer;

  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const manualBody = form.querySelector('[data-topvisor-manual-body]');
  const defaultRows = (() => {
    try { return JSON.parse(document.getElementById('topvisor-editor-defaults')?.textContent || '[]'); }
    catch (_error) { return []; }
  })();
  let manualRows = (() => {
    try { return JSON.parse(manualField?.value || '[]'); }
    catch (_error) { return []; }
  })();
  if (!manualRows.length) manualRows = defaultRows;

  function renderManualRows() {
    if (!manualBody || !manualField) return;
    manualBody.replaceChildren();
    const insertAt = index => {
      const source = manualRows[Math.min(index, manualRows.length - 1)] || {configuration_id: '', engine: 'yandex', region: '', month: '', total: 0, top3: 0, top10: 0, top11_30: 0};
      manualRows.splice(index, 0, {...source});
      manualField.value = JSON.stringify(manualRows);
      renderManualRows();
      scheduleSave(150);
    };
    const insertBoundary = index => {
      const boundary = document.createElement('tr');
      boundary.className = 'manual-row-boundary';
      const cell = document.createElement('td');
      cell.colSpan = 5;
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = '+';
      button.title = index === 0 ? 'Добавить строку сверху' : index === manualRows.length ? 'Добавить строку снизу' : 'Добавить строку здесь';
      button.addEventListener('click', () => insertAt(index));
      cell.append(button); boundary.append(cell); manualBody.append(boundary);
    };
    manualRows.forEach((row, index) => {
      insertBoundary(index);
      const tr = document.createElement('tr');
      tr.title = [row.engine, row.region].filter(Boolean).join(' · ');
      const fields = [['month', row.month || '', 'month'], ['top3', row.top3 ?? 0, 'metric'], ['top10', row.top10 ?? 0, 'metric'], ['top11_30', row.top11_30 ?? 0, 'metric']];
      fields.forEach(([name, value, type]) => {
        const td = document.createElement('td');
        const input = document.createElement('input');
        input.type = type === 'month' ? 'month' : 'text';
        if (type === 'month') input.value = String(value).slice(0, 7);
        else {
          const percentName = `${name}_percent`;
          const percent = row[percentName] ?? (row.total ? Math.round(Number(value || 0) * 100 / Number(row.total)) : 0);
          input.value = `${percent}% (${value})`;
          input.inputMode = 'decimal';
          input.setAttribute('aria-label', 'Процент и количество');
        }
        input.addEventListener('input', () => {
          if (type === 'month') manualRows[index][name] = input.value ? `${input.value}-01` : '';
          else {
            const numbers = input.value.match(/-?\d+(?:[.,]\d+)?/g) || [];
            manualRows[index][`${name}_percent`] = Number((numbers[0] || '0').replace(',', '.'));
            manualRows[index][name] = Math.max(0, Number(numbers[1] || numbers[0] || 0));
          }
          manualField.value = JSON.stringify(manualRows); scheduleSave(700);
        });
        td.append(input);
        tr.append(td);
      });
      const actions = document.createElement('td');
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'delete-button'; remove.textContent = '×';
      remove.title = 'Удалить строку';
      remove.addEventListener('click', () => {
        manualRows.splice(index, 1); manualField.value = JSON.stringify(manualRows);
        renderManualRows(); scheduleSave(150);
      });
      actions.append(remove); tr.append(actions); manualBody.append(tr);
    });
    insertBoundary(manualRows.length);
    manualField.value = JSON.stringify(manualRows);
  }
  renderManualRows();

  function updateDependencies() {
    form.querySelectorAll('[data-dependent-on]').forEach(container => {
      const parent = document.getElementById(container.dataset.dependentOn);
      const disabled = !parent?.checked;
      container.classList.toggle('disabled-setting', disabled);
      container.querySelectorAll('input, select, textarea, button').forEach(field => { field.disabled = disabled; });
    });
  }
  form.addEventListener('change', updateDependencies);
  updateDependencies();

  const richSource = form.querySelector('.rich-text-source');
  const richEditor = form.querySelector('[data-rich-editor]');
  if (richSource && richEditor) {
    richEditor.innerHTML = richSource.value;
    richEditor.addEventListener('input', () => {
      richSource.value = richEditor.innerHTML;
      richSource.dispatchEvent(new Event('input', {bubbles: true}));
    });
    form.querySelectorAll('[data-rich-command]').forEach(button => {
      button.addEventListener('click', () => {
        richEditor.focus();
        document.execCommand(button.dataset.richCommand, false);
        richEditor.dispatchEvent(new Event('input', {bubbles: true}));
      });
    });
    form.querySelector('[data-rich-link]')?.addEventListener('click', () => {
      const url = prompt('Укажите полный адрес ссылки, начиная с https://');
      if (!url) return;
      richEditor.focus();
      document.execCommand('createLink', false, url);
      richEditor.dispatchEvent(new Event('input', {bubbles: true}));
    });
  }

  function showNotice(message, kind = 'success') {
    if (!notice) return;
    notice.textContent = message;
    notice.dataset.kind = kind;
    notice.hidden = false;
  }

  function settingsPayload() {
    const payload = {};
    form.querySelectorAll('.report-options input, .report-options select, .report-options textarea, .project-storage-settings select').forEach(field => {
      if (!field.name || field.type === 'file' || field.form !== form) return;
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
  form.querySelector('.project-storage-settings')?.addEventListener('change', () => scheduleSave(150));

  document.querySelectorAll('[data-report-goal-picker]').forEach(picker => {
    const count = picker.querySelector('[data-goal-count]');
    const update = () => {
      if (count) count.textContent = picker.querySelectorAll('input[name=goals]:checked').length;
    };
    picker.addEventListener('change', update);
    update();
  });

  document.querySelectorAll('[data-confirm]').forEach(button => {
    button.addEventListener('click', event => {
      if (!confirm(button.dataset.confirm)) event.preventDefault();
    });
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
    const empty = card.querySelector('[data-source-empty]');
    if (!optionsRoot) return;
    optionsRoot.replaceChildren();
    periods.forEach((period, index) => {
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = sourceName;
      input.value = period.id;
      input.dataset.periodMonth = period.month;
      input.checked = index < 3;
      optionsRoot.append(input);
    });
    range.hidden = periods.length === 0;
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
        const forceRefresh = document.querySelector(`[name=force_refresh][form="${syncForm.id}"]`);
        if (forceRefresh) forceRefresh.checked = false;
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

  form.querySelector('[data-global-sync]')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const status = form.querySelector('[data-global-sync-status]');
    const sources = [...document.querySelectorAll('[data-global-source-sync]')];
    if (!sources.length) {
      if (status) status.textContent = 'Нет подключённых источников.';
      return;
    }
    button.disabled = true;
    const results = [];
    try {
      await Promise.all(sources.map(async source => {
        const label = source.dataset.globalSourceLabel || 'Источник';
        try {
          const response = await fetch(source.action, {method: 'POST', credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}, body: new FormData(source)});
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error(data.message || 'ошибка синхронизации');
          if (source.dataset.sourceName && data.periods) {
            const card = document.querySelector(`[data-source-period-picker][data-source-name="${source.dataset.sourceName}"]`);
            replacePeriods(card, data.periods, source.dataset.sourceName);
          }
          results.push({label, ok: true, message: data.message || 'завершено'});
        } catch (error) {
          results.push({label, ok: false, message: error.message || 'ошибка синхронизации'});
        }
      }));
      if (status) status.textContent = results.map(item => `${item.label} — ${item.ok ? 'готово' : `ошибка: ${item.message}`}`).join('; ');
    } finally {
      button.disabled = false;
    }
  });
})();
