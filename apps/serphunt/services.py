import hashlib
import json
import time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.topvisor.services import calculate_visibility

from .client import SerphuntClient, SerphuntError
from .models import SerphuntCredential, SerphuntSyncRun


def _checksum(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _config_id(engine, mapping):
    return f"serphunt:{engine}:{mapping.region_id}:{mapping.device}:{mapping.language}"


def configurations(mapping):
    return [
        {
            "id": _config_id(engine, mapping),
            "search_engine": engine,
            "region": mapping.region_name or str(mapping.region_id),
            "region_id": mapping.region_id,
            "device": mapping.device,
            "language": mapping.language,
            "normalized_depth": mapping.google_search_depth if engine == "google" else 100,
        }
        for engine in mapping.search_engines
    ]


def _result_rows(result, mapping, engine):
    rows = []
    prefix = f"{mapping.region_id}_{mapping.device}_{mapping.language}"
    bucket = result.get(prefix) or {}
    for query, engines in bucket.items():
        pages = (engines or {}).get(engine) or {}
        matches = (
            [item for item in pages.values() if isinstance(item, dict)]
            if isinstance(pages, dict)
            else []
        )
        ranked_matches = [item for item in matches if str(item.get("position", "")).isdigit()]
        match = (
            min(ranked_matches, key=lambda item: int(item["position"])) if ranked_matches else {}
        )
        rows.append(
            {
                "query": query,
                "frequency": 1,
                "position": match.get("position"),
                "url": match.get("relevance_url", ""),
            }
        )
    return rows


@transaction.atomic
def _store_result(mapping, payload):
    result = payload.get("result") or {}
    today = timezone.localdate()
    loaded = 0
    for config in configurations(mapping):
        engine = config["search_engine"]
        rows = _result_rows(result, mapping, engine)
        depth = config["normalized_depth"]
        visibility = calculate_visibility(rows)
        snapshot, _created = RankingSnapshot.objects.update_or_create(
            project=mapping.project,
            snapshot_date=today,
            topvisor_configuration_id=config["id"],
            defaults={
                "search_engine": engine,
                "region": config["region"],
                "tracked_keyword_count": len(rows),
                "ranking_depth": depth,
                "depth_raw": str(depth),
                "depth_retrieved_at": timezone.now(),
                "depth_source": RankingSnapshot.DepthSource.SERPHUNT_API,
                "visibility": Decimal(str(visibility)) if visibility is not None else None,
                "visibility_raw": {
                    "value": str(visibility) if visibility is not None else None,
                    "source": "calculated_from_serphunt_positions_equal_weight",
                },
                "response_checksum": _checksum(payload),
                "retrieved_at": timezone.now(),
                "provenance": {
                    "method": "serphunt_api",
                    "retrieved_at": timezone.now().isoformat(),
                },
            },
        )
        snapshot.positions.all().delete()
        KeywordPosition.objects.bulk_create(
            [
                KeywordPosition(
                    ranking_snapshot=snapshot,
                    query=row["query"],
                    normalized_query=" ".join(row["query"].casefold().split()),
                    frequency=1,
                    position_raw=str(row.get("position") or ""),
                    position_value=(
                        int(row["position"])
                        if str(row.get("position", "")).isdigit() and int(row["position"]) <= depth
                        else None
                    ),
                    position_status=(
                        KeywordPosition.Status.RANKED
                        if str(row.get("position", "")).isdigit() and int(row["position"]) <= depth
                        else KeywordPosition.Status.NOT_FOUND
                    ),
                    group_name="",
                    target_url=row.get("url", ""),
                    normalized_target_url=row.get("url", ""),
                )
                for row in rows
            ],
            batch_size=1000,
        )
        loaded += len(rows)
    mapping.last_successful_sync_at = timezone.now()
    mapping.save(update_fields=["last_successful_sync_at", "updated_at"])
    return loaded


def sync_positions(mapping):
    credential = SerphuntCredential.objects.filter(pk=1).first()
    if not credential:
        raise SerphuntError("Общий API-ключ Serphunt не настроен.")
    client = SerphuntClient(credential.get_api_key())
    run = mapping.sync_runs.filter(status=SerphuntSyncRun.Status.RUNNING).first()
    if not run:
        started = client.start_positions(mapping)
        task_id = str(started.get("task_id") or "")
        if not task_id:
            raise SerphuntError("Serphunt не вернул идентификатор задания.")
        run = SerphuntSyncRun.objects.create(mapping=mapping, task_id=task_id)
    try:
        for _attempt in range(20):
            payload = client.result(run.task_id)
            if payload.get("result") is not None:
                run.loaded_keyword_count = _store_result(mapping, payload)
                run.status = SerphuntSyncRun.Status.SUCCESS
                run.completed_at = timezone.now()
                run.save(update_fields=["loaded_keyword_count", "status", "completed_at"])
                return run
            message = payload.get("message") or {}
            message_code = message.get("code") if isinstance(message, dict) else str(message)
            if message_code != "TASK_IN_PROGRESS":
                raise SerphuntError("Serphunt не вернул результаты проверки.")
            time.sleep(0.5)
        run.error_message = (
            "Задание ещё выполняется. Повторите синхронизацию через несколько секунд."
        )
        run.save(update_fields=["error_message"])
        return run
    except SerphuntError as exc:
        run.status = SerphuntSyncRun.Status.FAILED
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at"])
        return run
