(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const notice = form.querySelector('[data-report-form-notice]');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  let saveTimer;

  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const useCurrentManualEditor = form.dataset.manualEditorVersion === '2';
  const manualContainer = useCurrentManualEditor
    ? null
    : form.querySelector('[data-topvisor-manual-segments]');
  const manualStatus = form.querySelector('[data-topvisor-manual-status]');
  const reportMonth = form.querySelector('[name=month]');
  reportMonth?.addEventListener('change', () => {
    document.querySelectorAll('[data-sync-month-for]').forEach(input => {
      input.value = reportMonth.value;
    });
  });
  const readJson = (id, fallback = []) => {
    try { return JSON.parse(document.getElementById(id)?.textContent || JSON.stringify(fallback)); }
    catch (_error) { return fallback; }
  };
  const defaultRows = readJson('topvisor-editor-defaults');
  const defaultSegments = readJson('topvisor-editor-segments');
  const savedRows = (() => {
    try { return JSON.parse(manualField?.value || '[]'); }
    catch (_error) { return []; }
  })();
  const segmentKey = row => `${String(row.engine || '').toLowerCase()}\u0000${String(row.region || '').trim().toLowerCase()}`;
  const rowKey = row => `${segmentKey(row)}\u0000${String(row.month || '').slice(0, 7)}`;
  const manualRows = defaultRows.map(row => ({...row, _manual: false}));
  savedRows.forEach(row => {
    const index = row.month ? manualRows.findIndex(item => rowKey(item) === rowKey(row)) : -1;
    if (index >= 0) manualRows[index] = {...manualRows[index], ...row, _manual: true};
    else manualRows.push({...row, _manual: true});
  });
  let manualDirty = false;

  function changeManualRows(delay = 500) {
    if (!manualField) return;
    manualField.value = JSON.stringify(
      manualRows.filter(row => row._manual && row.month).map(({_manual, ...row}) => row)
    );
    manualDirty = true;
    if (manualStatus) manualStatus.textContent = 'Сохранение…';
    scheduleSave(delay);
  }

  function renderManualRows() {
    if (!manualContainer || !manualField) return;
    manualContainer.replaceChildren();
    const segments = new Map(defaultSegments.map(item => [segmentKey(item), {...item}]));
    manualRows.forEach(row => {
      if (row.engine || row.region) segments.set(segmentKey(row), {...segments.get(segmentKey(row)), ...row});
    });
    if (!segments.size) {
      const empty = document.createElement('p');
      empty.textContent = 'Подключите поисковую систему и регион, чтобы заполнить таблицу.';
      manualContainer.append(empty);
      return;
    }
    segments.forEach(segment => {
      const section = document.createElement('section');
      section.className = 'manual-segment-card';
      const title = document.createElement('h5');
      const engineLabel = segment.engine_label || (segment.engine === 'yandex' ? 'Яндекс' : segment.engine === 'google' ? 'Google' : segment.engine || 'Поиск');
      title.textContent = `${engineLabel} · ${segment.region || 'Регион не указан'}`;
      section.append(title);
      const wrap = document.createElement('div'); wrap.className = 'table-wrap';
      const table = document.createElement('table'); table.className = 'manual-topvisor-table';
      table.innerHTML = `<thead><tr><th>Месяц</th><th>в топ 3</th><th>в топ 10</th><th>в топ ${Number(segment.ranking_depth || 30) >= 30 ? '11–30' : '11–20'}</th><th><span class="sr-only">Действия</span></th></tr></thead>`;
      const body = document.createElement('tbody');
      const segmentRows = manualRows.filter(row => segmentKey(row) === segmentKey(segment));
      segmentRows.sort((a, b) => String(a.month || '').localeCompare(String(b.month || '')));
      segmentRows.forEach(row => {
      const tr = document.createElement('tr');
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
          row._manual = true;
          if (type === 'month') row[name] = input.value ? `${input.value}-01` : '';
          else {
            const numbers = input.value.match(/-?\d+(?:[.,]\d+)?/g) || [];
            row[`${name}_percent`] = Math.max(0, Math.min(100, Number((numbers[0] || '0').replace(',', '.'))));
            row[name] = Math.max(0, Math.round(Number(numbers[1] || numbers[0] || 0)));
          }
          changeManualRows(700);
        });
        td.append(input);
        tr.append(td);
      });
      const actions = document.createElement('td');
      if (row._manual) {
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'delete-button'; remove.textContent = '×';
        remove.title = 'Удалить ручную строку';
        remove.addEventListener('click', () => {
          manualRows.splice(manualRows.indexOf(row), 1); renderManualRows(); changeManualRows(150);
        });
        actions.append(remove);
      }
      tr.append(actions); body.append(tr);
    });
      if (!segmentRows.length) body.innerHTML = '<tr><td colspan="5" class="manual-empty-row">Данных пока нет</td></tr>';
      table.append(body); wrap.append(table); section.append(wrap);
      const add = document.createElement('button');
      add.type = 'button'; add.className = 'secondary manual-add-row'; add.textContent = '+ Добавить строку';
      add.addEventListener('click', () => {
        manualRows.push({configuration_id: '', engine: segment.engine || '', region: segment.region || '', month: '', total: 0, top3: 0, top10: 0, top11_30: 0, top3_percent: 0, top10_percent: 0, top11_30_percent: 0, _manual: true});
        renderManualRows();
      });
      section.append(add); manualContainer.append(section);
    });
    manualField.value = JSON.stringify(
      manualRows.filter(row => row._manual && row.month).map(({_manual, ...row}) => row)
    );
  }
  renderManualRows();

  function updateDependencies() {
    form.querySelectorAll('[data-dependent-on]').forEach(container => {
      const parent = document.getElementById(container.dataset.dependentOn);
      const disabled = !parent?.checked;
      container.classList.toggle('disabled-setting', disabled);
      if (container.hasAttribute('data-hide-when-disabled')) container.hidden = disabled;
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
    if (manualField) payload.topvisor_manual_rows = manualField.value || '[]';
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
      if (manualDirty) {
        manualDirty = false;
        if (manualStatus) manualStatus.textContent = 'Сохранено';
      }
      return true;
    } catch (error) {
      if (manualDirty && manualStatus) manualStatus.textContent = 'Ошибка автосохранения';
      showNotice(error.message || 'Не удалось сохранить настройки проекта.', 'error');
      return false;
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
      clearTimeout(saveTimer);
      if (!await saveSettings()) {
        if (status) status.textContent = 'Сначала исправьте ошибку сохранения настроек.';
        return;
      }
      for (const source of sources) {
        const label = source.dataset.globalSourceLabel || 'Источник';
        if (status) status.textContent = `Синхронизация: ${label}…`;
        try {
          const response = await fetch(source.action, {method: 'POST', credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}, body: new FormData(source)});
          const contentType = response.headers.get('content-type') || '';
          const data = contentType.includes('application/json')
            ? await response.json()
            : {ok: false, message: `Сервер вернул некорректный ответ (${response.status}).`};
          if (!response.ok || !data.ok) throw new Error(data.message || 'ошибка синхронизации');
          if (source.dataset.sourceName && data.periods) {
            const card = document.querySelector(`[data-source-period-picker][data-source-name="${source.dataset.sourceName}"]`);
            replacePeriods(card, data.periods, source.dataset.sourceName);
          }
          results.push({label, ok: true, message: data.message || 'завершено'});
        } catch (error) {
          results.push({label, ok: false, message: error.message || 'ошибка синхронизации'});
        }
      }
      if (status) status.textContent = results.map(item => `${item.label} — ${item.ok ? 'готово' : `ошибка: ${item.message}`}`).join('; ');
    } finally {
      button.disabled = false;
    }
  });
})();
