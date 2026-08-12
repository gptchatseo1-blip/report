import math


def normalize_frequency(value) -> int:
    """Validate a provider frequency and normalize a real zero to the minimum value 1."""
    if value is None or isinstance(value, bool):
        raise ValueError("Frequency must be a non-negative integer")
    raw = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not raw:
        raise ValueError("Frequency must be a non-negative integer")
    try:
        numeric = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Frequency must be a non-negative integer") from exc
    if (
        not math.isfinite(numeric)
        or not numeric.is_integer()
        or numeric < 0
        or numeric > 2_147_483_647
    ):
        raise ValueError("Frequency must be a non-negative integer")
    parsed = int(numeric)
    return 1 if parsed == 0 else parsed
