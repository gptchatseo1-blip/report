(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const notice = form.querySelector('[data-report-form-notice]');
  const createButton = form.querySelector('.report-submit-actions > button[type="submit"]');
  const calendars = [...form.querySelectorAll('[data-calendar]')];

  const selectedCount = calendar => (
    calendar.querySelectorAll('.calendar-source input[type="checkbox"]:checked').length
  );

  function clearResolvedWarning(calendar) {
    if (selectedCount(calendar) < 2) return;
    calendar.classList.remove('field-invalid');
    calendar.querySelectorAll(':scope > .errorlist').forEach(error => error.remove());
  }

  function updateCreateAvailability() {
    calendars.forEach(clearResolvedWarning);
    const datesReady = calendars.length > 0 && calendars.every(calendar => selectedCount(calendar) >= 2);
    if (!createButton) return;
    if (datesReady) {
      createButton.disabled = false;
      createButton.removeAttribute('aria-disabled');
      createButton.removeAttribute('title');
    }
  }

  calendars.forEach(calendar => {
    calendar.addEventListener('click', () => requestAnimationFrame(updateCreateAvailability));
    calendar.addEventListener('change', updateCreateAvailability);
  });
  updateCreateAvailability();

  form.addEventListener('submit', event => {
    const invalid = calendars.find(calendar => selectedCount(calendar) < 2);
    if (invalid || event.defaultPrevented) return;
    if (notice) {
      notice.textContent = 'Отчёт создаётся…';
      notice.dataset.kind = 'progress';
      notice.hidden = false;
    }
    if (createButton) {
      createButton.disabled = true;
      createButton.textContent = 'Создание отчёта…';
    }
  });
})();
