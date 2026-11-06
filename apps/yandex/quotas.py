"""Persistent pacing shared by the Metrika worker and web processes."""

import hashlib
import re
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone

from .models import MetrikaQuotaState


class QuotaBlocked(Exception):
    def __init__(self, code, until):
        self.code = code
        self.until = until


def error_type(payload):
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors") or []
    candidates = [error.get("error_type") for error in errors if isinstance(error, dict)]
    candidates.extend((payload.get("error_type"), payload.get("error_code")))
    return next(
        (
            str(value)[:80]
            for value in candidates
            if value and str(value).replace("_", "").isalnum()
        ),
        "",
    )


def quota_details(code, payload, retry_after, now):
    # Some provider responses describe a five-minute quota with a generic UID code.
    messages = [payload.get("message", "")] if isinstance(payload, dict) else []
    if isinstance(payload, dict):
        messages.extend(
            e.get("message", "") for e in (payload.get("errors") or []) if isinstance(e, dict)
        )
    description = " ".join(str(value) for value in messages).casefold()
    short_window = bool(re.search(r"\b(?:5|five)[\s-]*(?:min|мин)", description))
    daily = code in {"quota_requests_by_uid", "quota_requests_by_counter_id"} and not short_window
    if daily:
        utc = now.astimezone(UTC)
        until = (utc + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)
        return True, until
    delay = 2 if code == "quota_requests_by_ip" else 5 if code == "quota_parallel_requests" else 305
    if retry_after is not None:
        try:
            delay = max(1, float(retry_after))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                delay = max(1, (when - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return False, now + timedelta(seconds=delay)


def quota_message(code, until=None, *, daily=None):
    if code == "quota_requests_by_uid" and daily is not False:
        message = "Исчерпана суточная квота Метрики для аккаунта Яндекса."
    elif code == "quota_requests_by_counter_id" and daily is not False:
        message = "Исчерпана суточная квота Метрики для счётчика."
    elif code == "quota_requests_by_ip":
        message = "Превышен лимит частоты запросов Метрики с IP-адреса сервера."
    elif code == "quota_parallel_requests":
        message = "Превышен лимит одновременных запросов Метрики."
    else:
        message = "Лимит запросов Яндекс.Метрики временно исчерпан: ограничена частота запросов."
    if until:
        from zoneinfo import ZoneInfo

        when = until.astimezone(ZoneInfo("Europe/Moscow"))
        message += f" Повторить можно после {when:%d.%m.%Y %H:%M:%S} МСК."
    return message


class MetrikaQuota:
    def __init__(self, *, clock=timezone.now):
        self.clock = clock

    @staticmethod
    def daily_keys(connection, params):
        identity = (
            connection.account_id
            or connection.account_login.casefold()
            or f"owner:{connection.user_id}"
        )
        account = hashlib.sha256(identity.encode()).hexdigest()
        counter = hashlib.sha256(str((params or {}).get("ids", "")).encode()).hexdigest()
        return f"account:{account}", f"counter:{counter}"

    def acquire(self, connection, path, params):
        interval = settings.YANDEX_METRIKA_MIN_INTERVAL_SECONDS
        if interval <= 0:
            return 0, ""
        now = self.clock()
        for state in MetrikaQuotaState.objects.filter(key__in=self.daily_keys(connection, params)):
            if state.next_request_at > now:
                raise QuotaBlocked(state.error_code, state.next_request_at)
        if not path.startswith("stat/"):
            return 0, ""
        state, _ = MetrikaQuotaState.objects.get_or_create(
            key="reports", defaults={"next_request_at": now}
        )
        delay = (state.next_request_at - now).total_seconds()
        if delay > 0:
            return delay, state.error_code
        claimed = MetrikaQuotaState.objects.filter(
            key=state.key, next_request_at=state.next_request_at
        ).update(next_request_at=now + timedelta(seconds=interval), error_code="")
        return (0, "") if claimed else (0.05, "")

    def defer(self, connection, params, code, until, *, daily):
        if settings.YANDEX_METRIKA_MIN_INTERVAL_SECONDS <= 0:
            return
        account, counter = self.daily_keys(connection, params)
        key = (
            (counter if code == "quota_requests_by_counter_id" else account) if daily else "reports"
        )
        MetrikaQuotaState.objects.get_or_create(key=key, defaults={"next_request_at": self.clock()})
        MetrikaQuotaState.objects.filter(key=key, next_request_at__lt=until).update(
            next_request_at=until, error_code=code
        )
