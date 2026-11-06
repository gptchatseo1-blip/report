"""Stable identities and Reporting API filters for existing URL-group settings."""

import hashlib
import json
import re
from fnmatch import translate


def url_group_key(group):
    patterns = sorted(
        {
            json.dumps(pattern, ensure_ascii=False, sort_keys=True)
            for pattern in group.get("patterns") or []
        }
    )
    return hashlib.sha256(json.dumps(patterns, ensure_ascii=False).encode()).hexdigest()


def combined_url_group(groups):
    return {"patterns": [pattern for group in groups for pattern in group.get("patterns") or []]}


def url_group_filter(group):
    """Match the exporter's full-URL, path and host/path candidates in the API.

    One OR filter covers every mask so intersecting masks count a visit once.
    Regex rules retain their case sensitivity; other existing rules ignore case.
    """
    full_url, paths = [], []
    for pattern in group.get("patterns") or []:
        kind = pattern.get("type") if isinstance(pattern, dict) else ""
        value = str(pattern.get("value") if isinstance(pattern, dict) else pattern).strip()
        if not value:
            continue
        if kind == "regex":
            full_url.append(f"(?:{value})")
            continue
        if kind in {"starts_with", "contains"}:
            prefix = "^" if kind == "starts_with" else ""
            full_url.append(f"(?i:{prefix}{re.escape(value)})")
            continue
        wildcard = "*" in value or "?" in value
        if wildcard:
            # fnmatch's atomic groups and Python end-anchor are not RE2 syntax.
            expression = translate(value).removeprefix("(?s:").removesuffix(")\\Z")
            expression = expression.replace("(?>", "(?:")
        else:
            expression = re.escape(value)
        end = "$" if wildcard else ""
        full_url.extend(
            (
                f"(?i:^{expression}{end})",
                f"(?i:^[a-z][a-z0-9+.-]*://{expression}" + ("(?:[?#].*)?$)" if wildcard else ")"),
            )
        )
        paths.append(f"(?i:^{expression}{end})")

    clauses = []
    for dimension, expressions in (("ym:s:startURL", full_url), ("ym:s:startURLPath", paths)):
        if expressions:
            expression = "|".join(dict.fromkeys(expressions))
            quoted = expression.replace("\\", "\\\\").replace("'", "\\'")
            clauses.append(f"{dimension}=~'{quoted}'")
    return "(" + " OR ".join(clauses) + ")" if clauses else ""
