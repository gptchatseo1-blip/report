"""Small read-only Topvisor API v2 client.

Credentials are supplied explicitly from a project-scoped encrypted connection. Global
settings are used only as a temporary legacy fallback for projects without a connection.
Credentials and provider response bodies are never included in safe exceptions.
"""

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from django.conf import settings


class TopvisorError(Exception):
    """A safe error which never contains request headers or credentials."""


class TopvisorTemporaryError(TopvisorError):
    """A safe error raised after retryable provider failures are exhausted."""


@dataclass(frozen=True)
class TopvisorCredentials:
    user_id: str
    api_key: str


class TopvisorClient:
    def __init__(
        self, *, credentials=None, base_url=None, timeout=None, max_retries=None, sleep=time.sleep
    ):
        self.credentials = credentials or TopvisorCredentials(
            settings.TOPVISOR_USER_ID, settings.TOPVISOR_API_KEY
        )
        self.base_url = (base_url or settings.TOPVISOR_API_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.TOPVISOR_REQUEST_TIMEOUT_SECONDS
        self.max_retries = max_retries if max_retries is not None else settings.TOPVISOR_MAX_RETRIES
        self.sleep = sleep

    @staticmethod
    def _api_errors_are_retryable(errors):
        """Retry only API errors which Topvisor explicitly identifies as temporary.

        Topvisor sometimes reports throttling and upstream failures in ``errors`` while
        returning HTTP 200. Unknown methods and invalid parameters are permanent.
        """
        entries = errors if isinstance(errors, list) else [errors]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = entry.get("code", entry.get("status"))
            try:
                if int(code) == 429 or 500 <= int(code) < 600:
                    return True
            except (TypeError, ValueError):
                pass
            machine_code = " ".join(
                str(entry.get(key, "")) for key in ("type", "code", "status")
            ).lower()
            if any(
                marker in machine_code
                for marker in ("temporary", "timeout", "throttl", "rate_limit", "unavailable")
            ):
                return True
        return False

    def _backoff(self, attempt):
        self.sleep(0.5 * (2**attempt) + random.uniform(0, 0.25))

    def _request(self, method: str, params: dict[str, Any] | None = None):
        if not self.credentials.user_id or not self.credentials.api_key:
            raise TopvisorError("Не настроены реквизиты доступа к Topvisor.")
        body = json.dumps(params or {}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{self.base_url}/{method.lstrip('/')}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Id": self.credentials.user_id,
                "Authorization": f"bearer {self.credentials.api_key}",
            },
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read())
                if isinstance(payload, dict) and payload.get("errors"):
                    retryable = self._api_errors_are_retryable(payload["errors"])
                    if not retryable:
                        raise TopvisorError("Topvisor отклонил запрос. Проверьте реквизиты.")
                    if attempt >= self.max_retries:
                        raise TopvisorTemporaryError(
                            "Topvisor временно недоступен. Повторите попытку позже."
                        )
                    self._backoff(attempt)
                    continue
                return payload.get("result", payload) if isinstance(payload, dict) else payload
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable:
                    raise TopvisorError("Topvisor отклонил запрос. Проверьте реквизиты.") from None
                if attempt >= self.max_retries:
                    raise TopvisorTemporaryError(
                        "Topvisor временно недоступен. Повторите попытку позже."
                    ) from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = (
                    float(retry_after)
                    if retry_after
                    else 0.5 * (2**attempt) + random.uniform(0, 0.25)
                )
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt >= self.max_retries:
                    raise TopvisorTemporaryError(
                        "Topvisor временно недоступен. Повторите попытку позже."
                    ) from None
                self._backoff(attempt)

    def iter_pages(self, method, params=None, *, page_size=1000):
        page = 0
        while True:
            payload = self._request(method, {**(params or {}), "page": page, "limit": page_size})
            rows = (
                payload.get("rows", payload.get("items", []))
                if isinstance(payload, dict)
                else payload
            )
            yield from rows
            if len(rows) < page_size:
                break
            page += 1

    def check_access(self):
        return tuple(self.iter_projects())

    def iter_projects(self):
        return self.iter_pages("get/projects_2/projects", {"fields": ["id", "name", "site"]})

    def get_search_configurations(self, project_id):
        payload = self._request(
            "get/projects_2/projects",
            {
                "show_searchers_and_regions": 2,
                "limit": 1,
                "filters": [{"name": "id", "operator": "EQUALS", "values": [project_id]}],
            },
        )
        projects = (
            payload.get("rows", payload.get("items", [])) if isinstance(payload, dict) else payload
        )
        configurations = []
        for project in projects or []:
            for searcher in project.get("searchers") or []:
                for region in searcher.get("regions") or []:
                    raw_depth = region.get("depth")
                    configurations.append(
                        {
                            "searcher_id": searcher.get("id"),
                            "searcher_key": searcher.get("key", searcher.get("searcher")),
                            "searcher_name": searcher.get("name", ""),
                            "region_id": region.get("id"),
                            "region_key": region.get("key"),
                            "region_index": region.get("index"),
                            "region_name": region.get("name", ""),
                            "area_name": region.get("areaName", ""),
                            "language": region.get("lang", ""),
                            "raw_depth": raw_depth,
                            "normalized_depth": self._normalize_depth(raw_depth),
                            "device": region.get("device"),
                        }
                    )
        return configurations

    @staticmethod
    def _normalize_depth(raw_depth):
        value = int(raw_depth)
        mapping = {1: 10, 2: 20, 3: 30, 5: 50, 10: 100}
        if value in (20, 30, 50, 100):
            return value
        if value in mapping:
            return mapping[value]
        raise TopvisorError("Topvisor вернул неподдерживаемую глубину проверки.")

    def get_positions(self, project_id, **filters):
        return self.iter_pages("get/positions_2/history", {"project_id": project_id, **filters})


def credentials_for_project(project):
    """Resolve only this project's credentials, with an explicit legacy fallback."""
    from .models import TopvisorConnection

    connection = TopvisorConnection.objects.filter(project=project).first()
    if connection:
        return TopvisorCredentials(connection.user_id, connection.get_api_key()), False
    credentials = TopvisorCredentials(settings.TOPVISOR_USER_ID, settings.TOPVISOR_API_KEY)
    return credentials, bool(credentials.user_id and credentials.api_key)


def client_for_project(project):
    credentials, legacy = credentials_for_project(project)
    return TopvisorClient(credentials=credentials), legacy
