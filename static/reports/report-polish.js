(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form || form.dataset.reportPolishReady === '1') return;
  if (form.dataset.manualEditorVersion === '2') return;
  form.dataset.reportPolishReady = '1';

  const legacy = form.querySelector('[data-topvisor-manual-editor]');
  const manualField = form.querySelector('[name=topvisor_manual_rows]');
  const manualContainer = legacy?.querySelector('[data-topvisor-manual-segments]');
  const manualStatus = legacy?.querySelector('[data-topvisor-manual-status]');
  if (!legacy || !manualField || !manualContainer || !manualStatus) return;

  const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const readJson = (id, fallback = []) => {
    try { return JSON.parse(document.getElementById(id)?.textContent || JSON.stringify(fallback)); }
    catch (_error) { return fallback; }
  };
  const defaults = readJson('topvisor-editor-defaults');
  const defaultSegments = readJson('topvisor-editor-segments');
  const saved = (() => {
    try { return JSON.parse(manualField.value || '[]'); }
    catch (_error) { return []; }
  })();

  const normalized = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  const segmentKey = row => `${normalized(row.engine)}\u0000${normalized(row.region)}`;
  const rowKey = row => `${segmentKey(row)}\u0000${String(row.month || '').slice(0, 7)}`;
  const numeric = value => {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(String(value).replace('%', '').replace(',', '.').trim());
    return Number.isFinite(parsed) ? parsed : null;
  };
  const numberEqual = (left, right) => {
    const a = numeric(left); const b = numeric(right);
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
  let uid = 0;
  const rows = defaults.map(source => {
    const base = {...source};
    return {...source, _uid: ++uid, _base: base, _manualOnly: false, _manualVisibility: null};
  });
  saved.forEach(source => {
    const index = source.month ? rows.findIndex(item => rowKey(item) === rowKey(source)) : -1;
    if (index >= 0) {
      const base = rows[index]._base;
      rows[index] = {
        ...rows[index],
        ...source,
        _uid: rows[index]._uid,
        _base: base,
        _manualOnly: false,
        _manualVisibility: source.visibility === undefined ? null : source.visibility,
      };
    } else {
      rows.push({
        ...source,
        _uid: ++uid,
        _base: null,
        _manualOnly: true,
        _manualVisibility: source.visibility ?? null,
      });
    }
  });

  const hasTopOverride = row => {
    if (!row._base) return true;
    if (String(row.month || '').slice(0, 7) !== String(row._base.month || '').slice(0, 7)) return true;
    return topFields.some(name => !numberEqual(row[name] ?? 0, row._base[name] ?? 0));
  };
  const hasOverride = row => row._manualOnly || row._manualVisibility !== null || hasTopOverride(row);
  const effectiveVisibility = row => (
    row._manualVisibility !== null ? row._manualVisibility : row._base?.visibility ?? null
  );
  const serializableRows = () => rows.filter(row => row.month && hasOverride(row)).map(row => ({
    configuration_id: String(row.configuration_id || ''),
    engine: String(row.engine || '').toLowerCase(),
    region: String(row.region || '').trim(),
    month: String(row.month).length === 7 ? `${row.month}-01` : String(row.month),
    visibility: row._manualVisibility,
    total: Math.max(0, Math.round(Number(row.total || 0))),
    top3: Math.max(0, Math.round(Number(row.top3 || 0))),
    top10: Math.max(0, Math.round(Number(row.top10 || 0))),
    top11_30: Math.max(0, Math.round(Number(row.top11_30 || 0))),
    top3_percent: Math.max(0, Math.min(100, Number(row.top3_percent || 0))),
    top10_percent: Math.max(0, Math.min(100, Number(row.top10_percent || 0))),
    top11_30_percent: Math.max(0, Math.min(100, Number(row.top11_30_percent || 0))),
  }));

  let saveTimer = null;
  let manualDirty = false;
  let revision = 0;
  const updateHidden = () => { manualField.value = JSON.stringify(serializableRows()); };
  const setStatus = (text, kind = '') => {
    manualStatus.textContent = text;
    if (kind) manualStatus.dataset.kind = kind;
    else delete manualStatus.dataset.kind;
  };
  const markDirty = (delay = 650) => {
    updateHidden();
    manualDirty = true;
    revision += 1;
    setStatus('Сохранение…', 'progress');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { void saveManualSettings(); }, delay);
  };
  async function saveManualSettings() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!manualDirty) return true;
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
        manualDirty = false;
        setStatus('Сохранено', 'success');
      } else {
        setStatus('Сохранение…', 'progress');
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => { void saveManualSettings(); }, 100);
      }
      return true;
    } catch (error) {
      manualDirty = true;
      setStatus('Ошибка автосохранения', 'error');
      return false;
    }
  }
  async function flushManualSave() {
    clearTimeout(saveTimer);
    saveTimer = null;
    return manualDirty ? saveManualSettings() : true;
  }

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'manual-dynamics-trigger';
  trigger.dataset.manualDynamicsOpen = '';
  trigger.textContent = 'Скорректировать таблицы динамики';

  const backdrop = document.createElement('div');
  backdrop.className = 'report-modal-backdrop';
  backdrop.hidden = true;
  backdrop.dataset.manualDynamicsModal = '';
  backdrop.innerHTML = `
    <section class="report-modal" role="dialog" aria-modal="true" aria-labelledby="topvisor-dynamics-modal-title" aria-describedby="topvisor-dynamics-modal-description">
      <div class="report-modal-header">
        <div>
          <h3 class="report-modal-title" id="topvisor-dynamics-modal-title">Скорректировать таблицы динамики</h3>
          <p class="report-modal-description" id="topvisor-dynamics-modal-description">Для каждой поисковой системы и региона данные подгружаются автоматически. Значения можно редактировать, добавлять и удалять. Изменения сохраняются автоматически.</p>
        </div>
        <button type="button" class="report-modal-close" data-manual-modal-close aria-label="Закрыть">×</button>
      </div>
      <div class="report-modal-body" data-manual-modal-body></div>
      <div class="report-modal-footer" data-manual-modal-footer>
        <button type="button" class="secondary" data-manual-modal-close>Закрыть</button>
      </div>
    </section>`;
  const modal = backdrop.querySelector('.report-modal');
  const modalBody = backdrop.querySelector('[data-manual-modal-body]');
  const modalFooter = backdrop.querySelector('[data-manual-modal-footer]');
  const closeButton = backdrop.querySelector('.report-modal-close');
  modalBody.append(manualContainer);
  modalFooter.prepend(manualStatus);
  legacy.replaceWith(trigger);
  form.append(backdrop);

  const segmentMap = () => {
    const segments = new Map(defaultSegments.map(item => [segmentKey(item), {...item}]));
    rows.forEach(row => {
      if (!row.engine && !row.region) return;
      const key = segmentKey(row);
      const current = segments.get(key) || {};
      segments.set(key, {...current, engine: row.engine || current.engine, region: row.region || current.region, ranking_depth: row.ranking_depth || current.ranking_depth});
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
    manualContainer.replaceChildren();
    const segments = segmentMap();
    if (!segments.size) {
      const empty = document.createElement('p');
      empty.textContent = 'Подключите поисковую систему и регион, чтобы заполнить таблицу.';
      manualContainer.append(empty);
      return;
    }
    segments.forEach(segment => {
      const section = document.createElement('section');
      section.className = 'manual-segment-card';
      const segmentRows = rows.filter(row => segmentKey(row) === segmentKey(segment));
      if (segmentRows.some(hasOverride)) section.classList.add('is-manual-override');
      const title = document.createElement('h5');
      const engineLabel = segment.engine_label || (normalized(segment.engine) === 'yandex' ? 'Яндекс' : normalized(segment.engine) === 'google' ? 'Google' : segment.engine || 'Поиск');
      title.textContent = `${engineLabel} · ${segment.region || 'Регион не указан'}`;
      section.append(title);
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      const table = document.createElement('table');
      table.className = 'manual-topvisor-table';
      const lastRange = Number(segment.ranking_depth || 30) >= 30 ? '11–30' : '11–20';
      table.innerHTML = `<thead><tr><th>Месяц</th><th>Видимость</th><th>в топ 3</th><th>в топ 10</th><th>в топ ${lastRange}</th><th><span class="sr-only">Действия</span></th></tr></thead>`;
      const tbody = document.createElement('tbody');
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
          row.month = monthInput.value ? `${monthInput.value}-01` : '';
          markDirty(700);
        });
        monthCell.append(monthInput); tr.append(monthCell);

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
        visibilityCell.append(visibilityInput); tr.append(visibilityCell);

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
          td.append(input); tr.append(td);
        });

        const actionCell = document.createElement('td');
        if (hasOverride(row)) {
          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'delete-button manual-row-action';
          remove.textContent = '×';
          remove.title = row._manualOnly ? 'Удалить ручную строку' : 'Сбросить ручные изменения строки';
          remove.setAttribute('aria-label', remove.title);
          remove.addEventListener('click', () => {
            if (row._manualOnly) {
              rows.splice(rows.indexOf(row), 1);
            } else {
              const base = row._base;
              Object.keys(row).forEach(key => { if (!key.startsWith('_')) delete row[key]; });
              Object.assign(row, {...base, _manualVisibility: null});
            }
            updateHidden();
            renderRows();
            markDirty(100);
          });
          actionCell.append(remove);
        }
        tr.append(actionCell);
        tbody.append(tr);
      });
      if (!segmentRows.length) tbody.innerHTML = '<tr><td colspan="6" class="manual-empty-row">Данных пока нет</td></tr>';
      table.append(tbody); wrap.append(table); section.append(wrap);

      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'secondary manual-add-row';
      add.textContent = '+ Добавить строку';
      add.addEventListener('click', () => {
        const row = {
          configuration_id: '', engine: segment.engine || '', region: segment.region || '', month: '', visibility: null,
          total: 0, top3: 0, top10: 0, top11_30: 0, top3_percent: 0, top10_percent: 0, top11_30_percent: 0,
          ranking_depth: segment.ranking_depth || 30, _uid: ++uid, _base: null, _manualOnly: true, _manualVisibility: null,
        };
        rows.push(row);
        renderRows(row._uid);
      });
      section.append(add); manualContainer.append(section);
    });
    updateHidden();
    if (focusUid !== null) manualContainer.querySelector(`[data-manual-row-uid="${focusUid}"] input[type=month]`)?.focus();
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
    const savedOk = await flushManualSave();
    if (!savedOk) return false;
    backdrop.hidden = true;
    document.body.classList.remove('report-modal-open');
    (lastFocused === trigger ? trigger : lastFocused)?.focus?.();
    return true;
  }
  trigger.addEventListener('click', openModal);
  backdrop.querySelectorAll('[data-manual-modal-close]').forEach(button => button.addEventListener('click', () => { void closeModal(); }));
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
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  const dynamics = form.querySelector('#id_include_monthly_dynamics');
  const updateTrigger = () => { trigger.disabled = Boolean(dynamics && !dynamics.checked); };
  dynamics?.addEventListener('change', updateTrigger);
  updateTrigger();
  renderRows();
})();
