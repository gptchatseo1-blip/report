(() => {
  const monthNames = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
  const weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const formatDate = value => value.split('-').reverse().join('.');
  const monthIndex = date => date.getFullYear() * 12 + date.getMonth();
  const fromIndex = index => new Date(Math.floor(index / 12), index % 12, 1);

  document.querySelectorAll('[data-calendar]').forEach(root => {
    const inputs = [...root.querySelectorAll('.calendar-source input[type=checkbox]')];
    if (!inputs.length) return;
    const allowed = new Map(inputs.map(input => [input.value, input]));
    const latest = new Date([...allowed.keys()].sort().at(-1) + 'T12:00:00');
    let endMonth = monthIndex(latest);
    const monthsRoot = root.querySelector('[data-months]');
    const summary = root.querySelector('[data-summary]');
    const label = root.dataset.label;
    const mobile = () => matchMedia('(max-width: 700px)').matches;

    function updateSummary() {
      const chosen = inputs.filter(input => input.checked).map(input => input.value).sort();
      summary.textContent = chosen.length < 2
        ? (inputs.length === 1 ? 'Для сравнения нужна ещё минимум одна дата.' : `${label} — выберите минимум две даты.`)
        : `${label} — период: ${formatDate(chosen[0])}–${formatDate(chosen.at(-1))}. Точек на графике: ${chosen.length}`;
    }

    function render() {
      const count = mobile() ? 1 : 3;
      const start = endMonth - count + 1;
      monthsRoot.replaceChildren();
      for (let index = start; index <= endMonth; index += 1) {
        const shown = fromIndex(index);
        const year = shown.getFullYear();
        const month = shown.getMonth();
        const section = document.createElement('section');
        section.className = 'calendar-month';
        section.innerHTML = `<h4>${monthNames[month]} ${year}</h4><div class="calendar-week" aria-hidden="true">${weekdays.map(day => `<span>${day}</span>`).join('')}</div><div class="calendar-grid" role="grid"></div>`;
        const grid = section.lastElementChild;
        const offset = (new Date(year, month, 1).getDay() + 6) % 7;
        for (let blank = 0; blank < offset; blank += 1) grid.append(document.createElement('span'));
        const days = new Date(year, month + 1, 0).getDate();
        for (let day = 1; day <= days; day += 1) {
          const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
          const input = allowed.get(key);
          const button = document.createElement('button');
          button.type = 'button';
          button.dataset.date = key;
          button.disabled = !input;
          button.className = input?.checked ? 'selected' : '';
          button.setAttribute('role', 'gridcell');
          button.setAttribute('aria-pressed', input?.checked ? 'true' : 'false');
          button.setAttribute('aria-label', `${label}, ${day}.${month + 1}.${year}${input ? '' : ' — нет сохранённых позиций'}`);
          button.innerHTML = `<span>${day}</span>${input ? '<i aria-hidden="true"></i>' : ''}`;
          if (input) button.addEventListener('click', () => { input.checked = !input.checked; render(); updateSummary(); });
          grid.append(button);
        }
        monthsRoot.append(section);
      }
    }
    root.querySelector('[data-prev]').addEventListener('click', () => { endMonth -= mobile() ? 1 : 3; render(); });
    root.querySelector('[data-next]').addEventListener('click', () => { endMonth += mobile() ? 1 : 3; render(); });
    let wasMobile = mobile();
    addEventListener('resize', () => { if (mobile() !== wasMobile) { wasMobile = mobile(); render(); } });
    render();
    updateSummary();
  });
})();
