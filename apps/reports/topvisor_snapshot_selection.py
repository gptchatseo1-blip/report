"""Conservative selection helpers for Topvisor ranking snapshots."""

from collections import defaultdict

from apps.topvisor.services import provider_tops

_MIN_BASELINE_KEYWORDS = 100
_SUSPECT_RATIO = 0.25
_RECOVERY_RATIO = 0.5


def snapshot_keyword_total(snapshot):
    """Return the provider keyword total without loading position rows."""
    exact = provider_tops(snapshot).get("all")
    if exact is not None:
        try:
            return max(int(exact), 0)
        except (TypeError, ValueError):
            pass
    return max(int(snapshot.tracked_keyword_count or 0), 0)


def partial_snapshot_ids(snapshots):
    """Identify an isolated, sharp keyword-volume collapse in one segment.

    Raw snapshots remain stored.  Only an isolated last drop, or a drop followed
    by a recovery, is treated as incomplete.  Two consecutive low-volume checks
    are accepted as a possible intentional change of the tracked keyword set.
    """
    grouped = defaultdict(list)
    for snapshot in snapshots:
        grouped[
            (
                str(snapshot.search_engine or "").casefold(),
                " ".join(str(snapshot.region or "").split()).casefold(),
                str(snapshot.topvisor_configuration_id or ""),
            )
        ].append(snapshot)

    partial = set()
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item.snapshot_date, item.created_at, item.pk))
        totals = [snapshot_keyword_total(item) for item in ordered]
        for index in range(1, len(ordered)):
            previous = totals[index - 1]
            current = totals[index]
            if previous < _MIN_BASELINE_KEYWORDS or current > previous * _SUSPECT_RATIO:
                continue
            following = totals[index + 1] if index + 1 < len(totals) else None
            if following is None or following >= previous * _RECOVERY_RATIO:
                partial.add(ordered[index].pk)
    return partial


def stable_snapshots(snapshots):
    snapshots = list(snapshots)
    partial = partial_snapshot_ids(snapshots)
    return [snapshot for snapshot in snapshots if snapshot.pk not in partial]
