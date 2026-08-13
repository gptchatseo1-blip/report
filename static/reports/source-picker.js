document.querySelectorAll("[data-domain-picker]").forEach((form) => {
  const select = form.querySelector("[data-domain-select]");
  const confirmation = form.querySelector("[data-domain-confirm]");
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
