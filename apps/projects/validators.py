import re

from django.core.exceptions import ValidationError


def validate_safe_regex(value: str) -> None:
    """Reject common catastrophic-backtracking constructs and invalid expressions."""
    if len(value) > 500:
        raise ValidationError("Регулярное выражение не должно быть длиннее 500 символов.")
    # A quantified group that itself contains a quantifier is a frequent ReDoS source.
    if re.search(r"\([^)]*(?:[*+]|{\d+(?:,\d*)?\})[^)]*\)[*+{]", value):
        raise ValidationError("Потенциально опасное вложенное повторение в регулярном выражении.")
    if re.search(r"\\[1-9]", value):
        raise ValidationError("Обратные ссылки в регулярных выражениях не поддерживаются.")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValidationError(f"Некорректное регулярное выражение: {exc}") from exc
