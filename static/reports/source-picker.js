document.querySelectorAll("[data-domain-picker]").forEach((form) => {
  const select = form.querySelector("[data-domain-select]");
  const confirmation = form.querySelector("[data-domain-confirm]");
  if (!select || !confirmation) return;
  const checkbox = confirmation.querySelector('input[type="checkbox"]');

  function updateConfirmation() {
    const option = select.selectedOptions[0];
    const mismatch = option && option.dataset.domainMismatch === "true";
    confirmation.hidden = !mismatch;
    checkbox.required = mismatch;
    if (!mismatch) checkbox.checked = false;
  }

  select.addEventListener("change", updateConfirmation);
  updateConfirmation();
});

document.querySelectorAll("[data-goal-picker]").forEach((form) => {
  const count = form.querySelector("[data-goal-count]");
  const checkboxes = [...form.querySelectorAll('input[name="goals"]')];
  if (!count) return;

  function updateCount() {
    count.textContent = checkboxes.filter((input) => input.checked).length;
  }

  checkboxes.forEach((input) => input.addEventListener("change", updateCount));
  updateCount();
});
