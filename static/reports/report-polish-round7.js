(() => {
  const form = document.querySelector('[data-report-create-form]');
  if (!form) return;

  const dynamics = form.querySelector('#id_include_monthly_dynamics');
  const tableToggle = form.querySelector('#id_include_monthly_dynamics_table');
  const trigger = form.querySelector('[data-manual-dynamics-open-round2]');
  const modal = form.querySelector('[data-manual-dynamics-modal-round2]');
  if (!dynamics || !trigger || !modal) return;

  trigger.textContent = 'Редактировать';
  trigger.title = 'Редактировать таблицу динамики';

  const modalTitle = modal.querySelector('#topvisor-dynamics-modal-title-round2');
  if (modalTitle) modalTitle.textContent = 'Редактировать таблицы динамики';

  const oldNested = trigger.closest('.nested-setting');
  const tableLabel = tableToggle?.closest('label.url-option');
  tableLabel?.remove();

  const action = document.createElement('div');
  action.className = 'monthly-dynamics-edit-action';
  action.append(trigger);

  const dynamicsLabel = dynamics.closest('label.url-option');
  if (dynamicsLabel) dynamicsLabel.after(action);
  else oldNested?.prepend(action);

  if (oldNested && !oldNested.querySelector('label,button,a,details')) oldNested.remove();

  const syncState = () => {
    if (tableToggle) tableToggle.checked = dynamics.checked;
    trigger.disabled = !dynamics.checked;
    action.hidden = !dynamics.checked;
  };
  dynamics.addEventListener('change', syncState);
  syncState();

  form.addEventListener(
    'submit',
    () => {
      if (tableToggle) tableToggle.checked = dynamics.checked;
    },
    true,
  );
})();
