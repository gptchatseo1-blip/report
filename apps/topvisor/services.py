import hashlib
import json
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot

from .client import TopvisorClient
from .models import TopvisorProjectMapping


def response_checksum(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_depth(raw) -> int:
    from apps.reports.calculations import normalize_ranking_depth

    return normalize_ranking_depth(raw)


def configuration_id(configuration):
    """Keep Topvisor's concrete variant ID, rather than collapsing device variants."""
    value = configuration.get("id") or configuration.get("searcher_id")
    if value is None:
        raise ValueError("Topvisor configuration has no stable identifier")
    return str(value)


@transaction.atomic
def store_snapshot(*, mapping: TopvisorProjectMapping, configuration, snapshot_date: date, payload):
    """Idempotently replace a single immutable normalized API snapshot."""
    config_id = configuration_id(configuration)
    depth_raw = configuration.get("depth", configuration.get("check_depth"))
    depth = normalize_depth(depth_raw)
    rows = payload.get("positions", payload.get("rows", []))
    retrieved_at = timezone.now()
    defaults = {
        "search_engine": str(
            configuration.get("search_engine", configuration.get("searcher", ""))
        ).lower(),
        "region": str(configuration.get("region_name", configuration.get("region", ""))),
        "tracked_keyword_count": int(payload.get("tracked_keyword_count", len(rows))),
        "ranking_depth": depth,
        "depth_raw": str(depth_raw),
        "depth_retrieved_at": retrieved_at,
        "depth_source": RankingSnapshot.DepthSource.TOPVISOR_API,
        "visibility": Decimal(str(payload["visibility"]))
        if payload.get("visibility") is not None
        else None,
        "visibility_raw": payload.get("visibility"),
        "response_checksum": response_checksum(payload),
        "retrieved_at": retrieved_at,
    }
    existing = RankingSnapshot.objects.filter(
        project=mapping.project,
        snapshot_date=snapshot_date,
        topvisor_configuration_id=config_id,
    ).first()
    unchanged = existing is not None and existing.response_checksum == defaults["response_checksum"]
    snapshot, created = RankingSnapshot.objects.update_or_create(
        project=mapping.project,
        snapshot_date=snapshot_date,
        topvisor_configuration_id=config_id,
        defaults=defaults,
    )
    if unchanged and snapshot.positions.exists():
        return snapshot, False
    snapshot.positions.all().delete()
    positions = []
    for row in rows:
        raw_position = row.get("position")
        position = (
            int(raw_position)
            if str(raw_position).isdigit() and int(raw_position) <= depth
            else None
        )
        query = str(row.get("query", row.get("name", ""))).strip()
        positions.append(
            KeywordPosition(
                ranking_snapshot=snapshot,
                query=query,
                normalized_query=" ".join(query.casefold().split()),
                frequency=int(row.get("frequency", row.get("ws", 0))),
                position_raw=str(raw_position or ""),
                position_value=position,
                position_status=KeywordPosition.Status.RANKED
                if position
                else KeywordPosition.Status.NOT_FOUND,
                group_name=str(row.get("group", row.get("group_name", "")) or ""),
                target_url=str(row.get("url", "") or ""),
                normalized_target_url=str(row.get("url", "") or ""),
            )
        )
    KeywordPosition.objects.bulk_create(positions, batch_size=1000)
    return snapshot, created


def available_projects(client=None):
    return tuple((client or TopvisorClient()).iter_projects())
