import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class SerphuntError(Exception):
    pass


class SerphuntClient:
    def __init__(self, api_key):
        self.api_key = api_key

    def _post(self, path, payload):
        request = Request(
            f"{settings.SERPHUNT_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.SERPHUNT_REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SerphuntError(
                "Serphunt временно недоступен или вернул некорректный ответ."
            ) from exc
        if not isinstance(data, dict):
            raise SerphuntError("Serphunt вернул некорректный ответ.")
        if data.get("error"):
            raise SerphuntError("Serphunt отклонил запрос. Проверьте API-ключ и параметры.")
        return data

    def balance(self):
        return self._post("get/billing/balance", {})

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
