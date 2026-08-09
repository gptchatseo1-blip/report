"""Read-only Yandex Metrika API client; exceptions never expose credentials."""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .crypto import decrypt_token, encrypt_token

CORE_METRICS = (
    "ym:s:visits",
    "ym:s:users",
    "ym:s:newUsers",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:avgVisitDurationSeconds",
)
LAST_SIGN_TRAFFIC_SOURCE = "ym:s:lastSignTrafficSource"
GOAL_REACHES = "ym:s:goal{id}reaches"
GOAL_CONVERSION_RATE = "ym:s:goal{id}conversionRate"


class YandexAPIError(Exception):
    pass


class YandexUnauthorized(YandexAPIError):
    pass


def exchange_token(parameters):
    required = (
        settings.YANDEX_CLIENT_ID,
        settings.YANDEX_CLIENT_SECRET,
        settings.YANDEX_REDIRECT_URI,
    )
    if not all(required):
        raise YandexAPIError("OAuth Яндекса не настроен.")
    body = urllib.parse.urlencode(
        {
            **parameters,
            "client_id": settings.YANDEX_CLIENT_ID,
            "client_secret": settings.YANDEX_CLIENT_SECRET,
        }
    ).encode()
    request = urllib.request.Request(settings.YANDEX_OAUTH_TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=settings.YANDEX_REQUEST_TIMEOUT_SECONDS
        ) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise YandexAPIError("Не удалось получить токен Яндекса.") from None
    if not result.get("access_token"):
        raise YandexAPIError("Яндекс не выдал токен доступа.")
    return result


class MetrikaClient:
    def __init__(self, connection, *, sleep=time.sleep, opener=urllib.request.urlopen):
        self.connection = connection
        self.sleep = sleep
        self.opener = opener

    def _refresh(self):
        if not self.connection.refresh_token_encrypted:
            raise YandexUnauthorized("Подключение Яндекса требует повторной авторизации.")
        result = exchange_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": decrypt_token(self.connection.refresh_token_encrypted),
            }
        )
        self.connection.access_token_encrypted = encrypt_token(result["access_token"])
        if result.get("refresh_token"):
            self.connection.refresh_token_encrypted = encrypt_token(result["refresh_token"])
        self.connection.expires_at = (
            timezone.now() + timedelta(seconds=int(result.get("expires_in", 0)))
            if result.get("expires_in")
            else None
        )
        self.connection.save(
            update_fields=[
                "access_token_encrypted",
                "refresh_token_encrypted",
                "expires_at",
                "updated_at",
            ]
        )

    def _request(self, path, params=None, *, refreshed=False):
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{settings.YANDEX_METRIKA_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        request = urllib.request.Request(
            f"{url}?{query}" if query else url,
            headers={
                "Authorization": f"OAuth {decrypt_token(self.connection.access_token_encrypted)}"
            },
        )
        for attempt in range(settings.YANDEX_MAX_RETRIES + 1):
            try:
                with self.opener(
                    request, timeout=settings.YANDEX_REQUEST_TIMEOUT_SECONDS
                ) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed:
                    self._refresh()
                    return self._request(path, params, refreshed=True)
                if (
                    exc.code == 429 or 500 <= exc.code < 600
                ) and attempt < settings.YANDEX_MAX_RETRIES:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    self.sleep(
                        float(retry_after)
                        if retry_after
                        else 0.5 * 2**attempt + random.uniform(0, 0.25)
                    )
                    continue
                raise YandexAPIError(f"Метрика временно недоступна (HTTP {exc.code}).") from None
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt < settings.YANDEX_MAX_RETRIES:
                    self.sleep(0.5 * 2**attempt + random.uniform(0, 0.25))
                    continue
                raise YandexAPIError("Не удалось получить корректный ответ Метрики.") from None

    def _pages(self, path, key, params=None, page_size=1000):
        offset = 1
        while True:
            result = self._request(
                path, {**(params or {}), "per_page": page_size, "offset": offset}
            )
            rows = result.get(key, [])
            yield from rows
            if len(rows) < page_size:
                break
            offset += len(rows)

    def counters(self):
        return self._pages("management/v1/counters", "counters")

    def counter(self, counter_id):
        return self._request(f"management/v1/counter/{counter_id}").get("counter", {})

    def goals(self, counter_id):
        return self._pages(f"management/v1/counter/{counter_id}/goals", "goals")

    def stat(self, **params):
        return self._request("stat/v1/data", params)
