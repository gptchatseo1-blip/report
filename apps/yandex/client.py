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

from .credentials import get_oauth_credentials
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
REGION_AREA = "ym:s:regionArea"
REGION_CITY = "ym:s:regionCity"
GOAL_REACHES = "ym:s:goal{id}reaches"
GOAL_VISITS = "ym:s:goal{id}visits"
GOAL_CONVERSION_RATE = "ym:s:goal{id}conversionRate"


class YandexAPIError(Exception):
    """Safe provider error; response bodies and credentials are never exposed."""

    def __init__(self, message, *, http_status=None, error_code=""):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


class YandexUnauthorized(YandexAPIError):
    pass


def exchange_token(parameters, *, credentials=None):
    credentials = credentials or get_oauth_credentials()
    if not credentials:
        raise YandexAPIError("OAuth Яндекса не настроен.")
    body = urllib.parse.urlencode(
        {
            **parameters,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
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
        if "scope" in result:
            raw_scopes = result.get("scope") or []
            self.connection.scopes = (
                raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
            )
        self.connection.save(
            update_fields=[
                "access_token_encrypted",
                "refresh_token_encrypted",
                "expires_at",
                "scopes",
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
                try:
                    payload = json.loads(exc.read())
                except (ValueError, TypeError):
                    payload = {}
                error_code = str(payload.get("error_code") or payload.get("code") or "")
                if exc.code in (401, 403):
                    raise YandexUnauthorized(
                        "Подключение Яндекса требует повторной авторизации для Метрики.",
                        http_status=exc.code,
                        error_code=error_code,
                    ) from None
                raise YandexAPIError(
                    f"Метрика временно недоступна (HTTP {exc.code}).",
                    http_status=exc.code,
                    error_code=error_code,
                ) from None
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
        return self._request(f"management/v1/counter/{counter_id}/goals").get("goals", [])

    def stat(self, **params):
        return self._request("stat/v1/data", params)


class WebmasterClient(MetrikaClient):
    """Read-only client for the Yandex Webmaster API v4.1."""

    def _request(self, path, params=None, *, refreshed=False, json_body=None):
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{settings.YANDEX_WEBMASTER_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"OAuth {decrypt_token(self.connection.access_token_encrypted)}"
        }
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(json_body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{url}?{query}" if query else url,
            data=data,
            headers=headers,
            method="POST" if json_body is not None else "GET",
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
                    return self._request(path, params, refreshed=True, json_body=json_body)
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
                try:
                    payload = json.loads(exc.read())
                except (ValueError, TypeError):
                    payload = {}
                error_code = str(payload.get("error_code", ""))
                if exc.code == 403 and error_code in {"HOST_NOT_VERIFIED", "HOST_NOT_LOADED"}:
                    raise YandexAPIError(
                        f"Сайт Вебмастера недоступен (HTTP {exc.code}).",
                        http_status=exc.code,
                        error_code=error_code,
                    ) from None
                if exc.code in (401, 403):
                    raise YandexUnauthorized(
                        "Подключение Яндекса требует повторной авторизации для Вебмастера."
                    ) from None
                raise YandexAPIError(
                    f"Вебмастер временно недоступен (HTTP {exc.code}).",
                    http_status=exc.code,
                    error_code=error_code,
                ) from None
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt < settings.YANDEX_MAX_RETRIES:
                    self.sleep(0.5 * 2**attempt + random.uniform(0, 0.25))
                    continue
                raise YandexAPIError("Не удалось получить корректный ответ Вебмастера.") from None

    def user(self):
        return self._request("user")

    def hosts(self, user_id):
        response = self._request(f"user/{urllib.parse.quote(str(user_id), safe='')}/hosts")
        return response.get("hosts", [])

    def host(self, user_id, host_id):
        return self._request(self._host_path(user_id, host_id))

    @staticmethod
    def _host_path(user_id, host_id, suffix=""):
        user = urllib.parse.quote(str(user_id), safe="")
        host = urllib.parse.quote(str(host_id), safe="")
        return f"user/{user}/hosts/{host}{suffix}"

    def summary(self, user_id, host_id):
        return self._request(self._host_path(user_id, host_id, "/summary"))

    def squ_history(self, user_id, host_id, **params):
        return self._request(self._host_path(user_id, host_id, "/sqi-history"), params)

    def search_urls_history(self, user_id, host_id, **params):
        return self._request(
            self._host_path(user_id, host_id, "/search-urls/in-search/history"), params
        )

    def search_urls_samples(self, user_id, host_id, *, max_rows=5000):
        """Return a bounded URL sample used for the in-search path legend."""
        offset = 0
        rows = []
        available = None
        while len(rows) < max_rows:
            limit = min(100, max_rows - len(rows))
            response = self._request(
                self._host_path(user_id, host_id, "/search-urls/in-search/samples"),
                {"offset": offset, "limit": limit},
            )
            batch = response.get("samples", [])
            available = response.get("count", available)
            rows.extend(batch)
            if (
                not batch
                or len(batch) < limit
                or (available is not None and len(rows) >= available)
            ):
                break
            offset += len(batch)
        return {
            "count": available if available is not None else len(rows),
            "samples": rows,
            "truncated": available is not None and len(rows) < available,
        }

    def indexing_history(self, user_id, host_id, **params):
        return self._request(
            self._host_path(user_id, host_id, "/search-urls/events/history"), params
        )

    def search_query_history(self, user_id, host_id, **params):
        return self._request(
            self._host_path(user_id, host_id, "/search-queries/all/history"), params
        )

    def popular_search_queries(self, user_id, host_id, **params):
        return self._request(self._host_path(user_id, host_id, "/search-queries/popular"), params)

    def query_analytics(
        self,
        user_id,
        host_id,
        *,
        date_from,
        date_to,
        search_location="ALL_LOCATIONS_ORGANIC",
        page_size=500,
    ):
        """Return all query rows for the same placement filter as Webmaster UI."""
        offset = 0
        rows = []
        available = None
        path = self._host_path(user_id, host_id, "/query-analytics/list")
        while available is None or offset < available:
            body = {
                "offset": offset,
                "limit": page_size,
                "device_type_indicator": "ALL",
                "search_location": search_location,
                "text_indicator": "QUERY",
                "filters": {
                    "statistic_filters": [
                        {
                            "statistic_field": "IMPRESSIONS",
                            "operation": "GREATER_EQUAL",
                            "value": "0",
                            "from": str(date_from),
                            "to": str(date_to),
                        }
                    ]
                },
                "sort_by_date": {
                    "date": str(date_to),
                    "statistic_field": "CLICKS",
                    "by": "DESC",
                },
            }
            response = self._request(path, json_body=body)
            batch = response.get("text_indicator_to_statistics") or []
            rows.extend(batch)
            try:
                available = int(response.get("count", len(rows)))
            except (TypeError, ValueError):
                available = len(rows)
            if not batch or len(batch) < page_size:
                break
            offset += len(batch)
        return {
            "count": available if available is not None else len(rows),
            "text_indicator_to_statistics": rows,
            "search_location": search_location,
            "date_from": str(date_from),
            "date_to": str(date_to),
        }
