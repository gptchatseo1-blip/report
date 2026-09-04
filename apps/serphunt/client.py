import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class SerphuntError(Exception):
    pass


class SerphuntClient:
    def __init__(self, api_key):
        value = str(api_key or "").strip()
        self.api_key = value[7:].strip() if value.casefold().startswith("bearer ") else value

    @staticmethod
    def _provider_error(data):
        error = data.get("error") if isinstance(data, dict) else None
        if not error:
            return ""
        if isinstance(error, dict):
            code = str(error.get("error_code") or error.get("code") or "").strip()
            description = str(error.get("description") or error.get("message") or "").strip()
            return ": ".join(part for part in (code, description) if part)
        return str(error).strip()

    def _post(self, path, payload):
        request = Request(
            f"{settings.SERPHUNT_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "SEO-Reports/1.0 (+https://report.rendom.beget.tech)",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.SERPHUNT_REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error_data = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                error_data = {}
            provider_error = self._provider_error(error_data)
            suffix = f" ({provider_error})" if provider_error else f" (HTTP {exc.code})"
            raise SerphuntError(f"Serphunt отклонил запрос{suffix}.") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SerphuntError(
                "Serphunt временно недоступен или вернул некорректный ответ."
            ) from exc
        if not isinstance(data, dict):
            raise SerphuntError("Serphunt вернул некорректный ответ.")
        provider_error = self._provider_error(data)
        if provider_error:
            raise SerphuntError(f"Serphunt отклонил запрос ({provider_error}).")
        return data

    def balance(self):
        data = self._post("get/billing/balance", {})
        return data.get("result") if isinstance(data.get("result"), dict) else data

    def start_positions(self, mapping):
        return self._post(
            "add/check",
            {
                "service": "positions",
                "page": [f"https://{mapping.project.normalized_domain}/"],
                "subdomains": int(mapping.include_subdomains),
                "keywords": mapping.keyword_list,
                "search_engine": mapping.search_engines,
                "google_search_depth": mapping.google_search_depth,
                "region": [mapping.region_id],
                "device": [mapping.device],
                "language": mapping.language,
            },
        )

    def result(self, task_id):
        return self._post("get/check/result", {"task_id": task_id})
