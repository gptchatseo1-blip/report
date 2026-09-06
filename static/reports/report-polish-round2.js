(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form || form.dataset.reportPolishRound2Ready === '1') return;
  form.dataset.reportPolishRound2Ready = '1';

  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const oldTrigger = form.querySelector('[data-manual-dynamics-open]');
  const oldBackdrop = form.querySelector('[data-manual-dynamics-modal]');
  if (!manualField || !oldTrigger) return;

  const readJson = (id, fallback = []) => {
    try {
      return JSON.parse(document.getElementById(id)?.textContent || JSON.stringify(fallback));
    } catch (_error) {
      return fallback;
    }
  };
  const defaults = readJson('topvisor-editor-defaults');
  const defaultSegments = readJson('topvisor-editor-segments');
  const saved = (() => {
    try {
      return JSON.parse(manualField.value || '[]');
    } catch (_error) {
      return [];
    }
  })();
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const normalized = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  const segmentKey = row => `${normalized(row.engine)}\u0000${normalized(row.region)}`;
  const rowKey = row => `${segmentKey(row)}\u0000${String(row.month || '').slice(0, 7)}`;
  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object || {}, key);
  const numeric = value => {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(String(value).replace('%', '').replace(',', '.').trim());
    return Number.isFinite(parsed) ? parsed : null;
  };
  const numberEqual = (left, right) => {
    const a = numeric(left);
    const b = numeric(right);
    return a === b || (a !== null && b !== null && Math.abs(a - b) < 1e-9);
  };
  const formatNumber = value => {
    const parsed = numeric(value);
    if (parsed === null) return '';
    return String(Math.round(parsed * 100) / 100).replace('.', ',');
  };
  const formatVisibility = value => {
    const rendered = formatNumber(value);
    return rendered === '' ? '' : `${rendered}%`;
  };
  const topFields = ['total', 'top3', 'top10', 'top11_30', 'top3_percent', 'top10_percent', 'top11_30_percent'];

  const selectedMonths = {yandex: new Set(), google: new Set()};
  Object.keys(selectedMonths).forEach(engine => {
    form.querySelectorAll(`input[name="${engine}_dates"]:checked`).forEach(input => {
      selectedMonths[engine].add(String(input.value || '').slice(0, 7));
    });
  });

  const savedByKey = new Map(saved.filter(row => row?.month).map(row => [rowKey(row), row]));
  let uid = 0;
  const rows = defaults.map(source => {
    const stored = savedByKey.get(rowKey(source));
    const base = {...source};
    const legacyStored = Boolean(stored && !hasOwn(stored, 'include_in_report'));
    const include = stored && hasOwn(stored, 'include_in_report')
      ? stored.include_in_report !== false
      : legacyStored
        ? true
        : selectedMonths[normalized(source.engine)]?.has(String(source.month || '').slice(0, 7)) || false;
    const manualOverride = stored && hasOwn(stored, 'manual_override')
      ? stored.manual_override !== false
      : Boolean(stored);
    const merged = {...source, ...(stored || {})};
    return {
      ...merged,
      _uid: ++uid,
      _base: base,
      _manualOnly: false,
      _manualVisibility: manualOverride && stored && hasOwn(stored, 'visibility') ? stored.visibility : null,
      _include: include,
      _deleted: Boolean(stored?.deleted),
    };
  });

  saved.forEach(source => {
    if (!source?.month || rows.some(row => rowKey(row) === rowKey(source))) return;
    rows.push({
      ...source,
      _uid: ++uid,
      _base: null,
      _manualOnly: true,
      _manualVisibility: source.visibility ?? null,
      _include: hasOwn(source, 'include_in_report') ? source.include_in_report !== false : true,
      _deleted: Boolean(source.deleted),
    });
  });

  // Preserve the previous two-month behaviour until the user explicitly selects more rows.
  Object.keys(selectedMonths).forEach(engine => {
    if (selectedMonths[engine].size) return;
    const groups = new Map();
    rows.filter(row => !row._manualOnly && normalized(row.engine) === engine).forEach(row => {
      const key = segmentKey(row);
      const items = groups.get(key) || [];
      items.push(row);
      groups.set(key, items);
    });
    groups.forEach(items => {
      if (items.some(row => savedByKey.has(rowKey(row)))) return;
      items.sort((a, b) => String(a.month || '').localeCompare(String(b.month || '')));
      items.slice(-2).forEach(row => { row._include = true; });
    });
  });

  const effectiveVisibility = row => (
    row._manualVisibility !== null && row._manualVisibility !== undefined
      ? row._manualVisibility
      : row._base?.visibility ?? row.automatic_visibility ?? row.visibility ?? null
  );
  const topChanged = row => {
    if (!row._base) return true;
    if (String(row.month || '').slice(0, 7) !== String(row._base.month || '').slice(0, 7)) return true;
    return topFields.some(name => !numberEqual(row[name] ?? 0, row._base[name] ?? 0));
  };
  const manualOverride = row => row._manualOnly || row._manualVisibility !== null || topChanged(row);

  const serializableRows = () => rows.filter(row => row.month).map(row => ({
    configuration_id: String(row.configuration_id || ''),
    engine: String(row.engine || '').toLowerCase(),
    region: String(row.region || '').trim(),
    month: String(row.month).length === 7 ? `${row.month}-01` : String(row.month),
    include_in_report: Boolean(row._include),
    deleted: Boolean(row._deleted),
    manual_override: manualOverride(row),
    visibility: row._manualVisibility,
    automatic_visibility: row._base?.visibility ?? row.automatic_visibility ?? row.visibility ?? null,
    total: Math.max(0, Math.round(Number(row.total || 0))),
    top3: Math.max(0, Math.round(Number(row.top3 || 0))),
    top10: Math.max(0, Math.round(Number(row.top10 || 0))),
    top11_30: Math.max(0, Math.round(Number(row.top11_30 || 0))),
    top3_percent: Math.max(0, Math.min(100, Number(row.top3_percent || 0))),
    top10_percent: Math.max(0, Math.min(100, Number(row.top10_percent || 0))),
    top11_30_percent: Math.max(0, Math.min(100, Number(row.top11_30_percent || 0))),
  }));

  const updateHidden = () => {
    manualField.value = JSON.stringify(serializableRows());
  };

  let saveTimer = null;
  let dirty = false;
  let revision = 0;
  const status = document.createElement('span');
  status.className = 'manual-save-status';
  status.setAttribute('aria-live', 'polite');
  const setStatus = (text, kind = '') => {
    status.textContent = text;
    if (kind) status.dataset.kind = kind;
    else delete status.dataset.kind;
  };
  const markDirty = (delay = 650) => {
    updateHidden();
    dirty = true;
    revision += 1;
    setStatus('Сохранение…', 'progress');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { void saveSettings(); }, delay);
  };
  async function saveSettings() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!dirty) return true;
    const savingRevision = revision;
    try {
      const response = await fetch(form.dataset.settingsUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify({topvisor_manual_rows: manualField.value || '[]'}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось сохранить настройки.');
      if (savingRevision === revision) {
        dirty = false;
        setStatus('Сохранено', 'success');
      } else {
        setStatus('Сохранение…', 'progress');
        saveTimer = setTimeout(() => { void saveSettings(); }, 100);
      }
      return true;
    } catch (_error) {
      dirty = true;
      setStatus('Ошибка автосохранения', 'error');
      return false;
    }
  }
  async function flushSave() {
    clearTimeout(saveTimer);
    saveTimer = null;
    updateHidden();
    return dirty ? saveSettings() : true;
  }

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'manual-dynamics-trigger manual-dynamics-trigger-round2';
  trigger.textContent = 'Скорректировать';
  trigger.dataset.manualDynamicsOpenRound2 = '';

  const backdrop = document.createElement('div');
  backdrop.className = 'report-modal-backdrop report-modal-backdrop-round2';
  backdrop.hidden = true;
  backdrop.dataset.manualDynamicsModalRound2 = '';
  backdrop.innerHTML = `
    <section class="report-modal" role="dialog" aria-modal="true" aria-labelledby="topvisor-dynamics-modal-title-round2" aria-describedby="topvisor-dynamics-modal-description-round2">
      <div class="report-modal-header">
        <div>
          <h3 class="report-modal-title" id="topvisor-dynamics-modal-title-round2">Скорректировать таблицы динамики</h3>
          <p class="report-modal-description" id="topvisor-dynamics-modal-description-round2">Для каждой поисковой системы и региона данные подгружаются автоматически. Значения можно редактировать, добавлять и удалять. Галочкой в колонке «Действия» выберите месяцы для таблицы отчёта. Изменения сохраняются автоматически.</p>
        </div>
        <button type="button" class="report-modal-close" data-manual-modal-close-round2 aria-label="Закрыть">×</button>
      </div>
      <div class="report-modal-body"><div class="manual-segment-editor" data-round2-manual-segments></div></div>
      <div class="report-modal-footer" data-round2-manual-footer>
        <button type="button" class="secondary" data-manual-modal-close-round2>Закрыть</button>
      </div>
    </section>`;
  const modal = backdrop.querySelector('.report-modal');
  const container = backdrop.querySelector('[data-round2-manual-segments]');
  const footer = backdrop.querySelector('[data-round2-manual-footer]');
  const closeButton = backdrop.querySelector('.report-modal-close');
  footer.prepend(status);

  oldTrigger.replaceWith(trigger);
  oldBackdrop?.remove();
  form.append(backdrop);

  const segmentMap = () => {
    const segments = new Map(defaultSegments.map(item => [segmentKey(item), {...item}]));
    rows.forEach(row => {
      if (!row.engine && !row.region) return;
      const key = segmentKey(row);
      const current = segments.get(key) || {};
      segments.set(key, {
        ...current,
        engine: row.engine || current.engine,
        region: row.region || current.region,
        ranking_depth: row.ranking_depth || current.ranking_depth,
      });
    });
    return segments;
  };

  const parseTopInput = (input, row, name) => {
    const numbers = input.value.match(/-?\d+(?:[.,]\d+)?/g) || [];
    if (!numbers.length) return false;
    const percent = Number(numbers[0].replace(',', '.'));
    const count = Number((numbers[1] || numbers[0]).replace(',', '.'));
    if (!Number.isFinite(percent) || !Number.isFinite(count) || percent < 0 || percent > 100 || count < 0) return false;
    row[`${name}_percent`] = Math.max(0, Math.min(100, percent));
    row[name] = Math.max(0, Math.round(count));
    return true;
  };
  const topValue = (row, name) => {
    const percent = row[`${name}_percent`] ?? (row.total ? Number(row[name] || 0) * 100 / Number(row.total) : 0);
    return `${formatNumber(percent)}% (${Math.max(0, Math.round(Number(row[name] || 0)))})`;
  };

  function renderRows(focusUid = null) {
    container.replaceChildren();
    const segments = segmentMap();
    if (!segments.size) {
      const empty = document.createElement('p');
      empty.textContent = 'Подключите поисковую систему и регион, чтобы заполнить таблицу.';
      container.append(empty);
      return;
    }
    segments.forEach(segment => {
      const section = document.createElement('section');
      section.className = 'manual-segment-card';
      const title = document.createElement('h5');
      const engineLabel = segment.engine_label || (normalized(segment.engine) === 'yandex' ? 'Яндекс' : normalized(segment.engine) === 'google' ? 'Google' : segment.engine || 'Поиск');
      title.textContent = `${engineLabel} · ${segment.region || 'Регион не указан'}`;
      section.append(title);

      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      const table = document.createElement('table');
      table.className = 'manual-topvisor-table manual-topvisor-table-round2';
      const lastRange = Number(segment.ranking_depth || 30) >= 30 ? '11–30' : '11–20';
      table.innerHTML = `<thead><tr><th>Месяц</th><th>Видимость</th><th>в топ 3</th><th>в топ 10</th><th>в топ ${lastRange}</th><th>Действия</th></tr></thead>`;
      const tbody = document.createElement('tbody');
      const segmentRows = rows.filter(row => !row._deleted && segmentKey(row) === segmentKey(segment));
      segmentRows.sort((a, b) => String(a.month || '').localeCompare(String(b.month || '')));

      segmentRows.forEach(row => {
        const tr = document.createElement('tr');
        tr.dataset.manualRowUid = String(row._uid);

        const monthCell = document.createElement('td');
        const monthInput = document.createElement('input');
        monthInput.type = 'month';
        monthInput.value = String(row.month || '').slice(0, 7);
        monthInput.setAttribute('aria-label', 'Месяц');
        monthInput.addEventListener('input', () => {
          const candidate = monthInput.value;
          const duplicate = candidate && rows.some(other => (
            other !== row && !other._deleted && segmentKey(other) === segmentKey(row) && String(other.month || '').slice(0, 7) === candidate
          ));
          monthInput.setCustomValidity(duplicate ? 'Такой месяц уже есть в этой таблице.' : '');
          if (duplicate) return;
          const deletedMatch = candidate && rows.find(other => (
            other !== row && other._deleted && segmentKey(other) === segmentKey(row) && String(other.month || '').slice(0, 7) === candidate
          ));
          if (deletedMatch) rows.splice(rows.indexOf(deletedMatch), 1);
          row.month = candidate ? `${candidate}-01` : '';
          markDirty(700);
        });
        monthCell.append(monthInput);
        tr.append(monthCell);

        const visibilityCell = document.createElement('td');
        const visibilityInput = document.createElement('input');
        visibilityInput.type = 'text';
        visibilityInput.inputMode = 'decimal';
        visibilityInput.value = formatVisibility(effectiveVisibility(row));
        visibilityInput.setAttribute('aria-label', 'Видимость');
        visibilityInput.addEventListener('input', () => {
          const raw = visibilityInput.value.trim();
          if (!raw) {
            row._manualVisibility = null;
            visibilityInput.setCustomValidity('');
            markDirty(700);
            return;
          }
          const parsed = numeric(raw);
          if (parsed === null || parsed < 0 || parsed > 100) {
            visibilityInput.setCustomValidity('Введите видимость от 0 до 100%.');
            setStatus('Некорректная видимость', 'error');
            return;
          }
          visibilityInput.setCustomValidity('');
          row._manualVisibility = parsed;
          markDirty(700);
        });
        visibilityInput.addEventListener('blur', () => {
          if (!visibilityInput.value.trim() && row._base) visibilityInput.value = formatVisibility(row._base.visibility);
          else if (visibilityInput.validity.valid) visibilityInput.value = formatVisibility(effectiveVisibility(row));
        });
        visibilityCell.append(visibilityInput);
        tr.append(visibilityCell);

        ['top3', 'top10', 'top11_30'].forEach(name => {
          const td = document.createElement('td');
          const input = document.createElement('input');
          input.type = 'text';
          input.inputMode = 'decimal';
          input.value = topValue(row, name);
          input.setAttribute('aria-label', 'Процент и количество');
          input.addEventListener('input', () => {
            if (!parseTopInput(input, row, name)) return;
            markDirty(700);
          });
          input.addEventListener('blur', () => { input.value = topValue(row, name); });
          td.append(input);
          tr.append(td);
        });

        const actionCell = document.createElement('td');
        actionCell.className = 'manual-row-actions-round2';
        const include = document.createElement('input');
        include.type = 'checkbox';
        include.checked = Boolean(row._include);
        include.className = 'manual-row-include';
        include.setAttribute('aria-label', 'Выводить месяц в таблице отчёта');
        include.title = 'Выводить месяц в таблице отчёта';
        include.addEventListener('change', () => {
          row._include = include.checked;
          markDirty(120);
        });
        actionCell.append(include);

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'manual-row-delete-round2';
        remove.textContent = '×';
        remove.title = 'Удалить строку из редактора';
        remove.setAttribute('aria-label', remove.title);
        remove.addEventListener('click', () => {
          if (row._manualOnly) rows.splice(rows.indexOf(row), 1);
          else {
            row._deleted = true;
            row._include = false;
          }
          markDirty(100);
          renderRows();
        });
        actionCell.append(remove);
        tr.append(actionCell);
        tbody.append(tr);
      });

      if (!segmentRows.length) tbody.innerHTML = '<tr><td colspan="6" class="manual-empty-row">Данных пока нет</td></tr>';
      table.append(tbody);
      wrap.append(table);
      section.append(wrap);

      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'secondary manual-add-row';
      add.textContent = '+ Добавить строку';
      add.addEventListener('click', () => {
        const reference = rows.find(row => segmentKey(row) === segmentKey(segment));
        const row = {
          configuration_id: reference?.configuration_id || '',
          engine: segment.engine || '',
          region: segment.region || '',
          month: '',
          visibility: null,
          automatic_visibility: null,
          total: 0,
          top3: 0,
          top10: 0,
          top11_30: 0,
          top3_percent: 0,
          top10_percent: 0,
          top11_30_percent: 0,
          ranking_depth: segment.ranking_depth || 30,
          _uid: ++uid,
          _base: null,
          _manualOnly: true,
          _manualVisibility: null,
          _include: true,
          _deleted: false,
        };
        rows.push(row);
        renderRows(row._uid);
      });
      section.append(add);
      container.append(section);
    });
    updateHidden();
    if (focusUid !== null) container.querySelector(`[data-manual-row-uid="${focusUid}"] input[type=month]`)?.focus();
  }

  let lastFocused = null;
  const focusableSelector = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])';
  function openModal() {
    if (trigger.disabled) return;
    lastFocused = document.activeElement;
    backdrop.hidden = false;
    document.body.classList.add('report-modal-open');
    renderRows();
    requestAnimationFrame(() => closeButton.focus());
  }
  async function closeModal() {
    if (backdrop.hidden) return true;
    const savedOk = await flushSave();
    if (!savedOk) return false;
    backdrop.hidden = true;
    document.body.classList.remove('report-modal-open');
    (lastFocused === trigger ? trigger : lastFocused)?.focus?.();
    return true;
  }

  trigger.addEventListener('click', openModal);
  backdrop.querySelectorAll('[data-manual-modal-close-round2]').forEach(button => button.addEventListener('click', () => { void closeModal(); }));
  backdrop.addEventListener('click', event => { if (event.target === backdrop) void closeModal(); });
  modal.addEventListener('click', event => event.stopPropagation());
  backdrop.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !backdrop.hidden) {
      event.preventDefault();
      void closeModal();
      return;
    }
    if (event.key !== 'Tab' || backdrop.hidden) return;
    const focusable = [...modal.querySelectorAll(focusableSelector)].filter(node => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  const dynamics = form.querySelector('#id_include_monthly_dynamics');
  const updateTrigger = () => { trigger.disabled = Boolean(dynamics && !dynamics.checked); };
  dynamics?.addEventListener('change', updateTrigger);
  updateTrigger();

  // The current report POST must always contain the latest checkbox/input state,
  // even when autosave is still debounced.
  form.addEventListener('submit', () => { updateHidden(); }, true);
  renderRows();
})();
