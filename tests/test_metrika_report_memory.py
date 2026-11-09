from datetime import date

import pytest
from django.db import connection
from django.db.models import Func, JSONField

from apps.metrics.models import SourceSnapshot
from apps.projects.models import Project
from apps.reports import services


@pytest.mark.django_db
def test_large_metrika_source_keeps_postgres_query_memory_bounded(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Measures PostgreSQL TOAST allocations; exercised by the Docker CI job.")

    project = Project.objects.create(name="Memory regression", domain="memory.example")
    snapshot = SourceSnapshot.objects.create(
        project=project,
        source=SourceSnapshot.Source.METRIKA,
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        checksum="memory-regression",
        payload={
            "schema_version": "metrika-v2",
            # A large unselected variant still has to be detoasted for every
            # metadata extraction. Repeating it in one SELECT used to retain
            # dozens of complete copies in the database backend.
            "unused_variant": "x" * (8 * 1024 * 1024),
            "detail_variants": {
                "search": {
                    "humans": {
                        "landing_pages_total": {"visits": 123},
                        "search_engines": [{"name": "Yandex", "visits": 100}],
                    }
                }
            },
        },
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT sum(total_bytes) FROM pg_backend_memory_contexts")
        baseline = int(cursor.fetchone()[0])
        cursor.execute("CREATE TEMP TABLE report_branch_memory (bytes bigint)")
        cursor.execute(
            """
            CREATE FUNCTION pg_temp.measure_report_branch(value jsonb) RETURNS jsonb
            LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO report_branch_memory
                    SELECT sum(total_bytes) FROM pg_backend_memory_contexts;
                RETURN value;
            END
            $$
            """
        )

    original_annotations = services._metrika_payload_annotations

    def measured_annotations(options):
        return {
            name: Func(
                expression,
                function="pg_temp.measure_report_branch",
                output_field=JSONField(),
            )
            for name, expression in original_annotations(options).items()
        }

    monkeypatch.setattr(services, "_metrika_payload_annotations", measured_annotations)
    facts = services.build_source_facts(
        project=project,
        report_month=date(2026, 10, 1),
        selected_snapshot_ids={
            SourceSnapshot.Source.METRIKA: [snapshot.pk],
            SourceSnapshot.Source.WEBMASTER: [],
        },
        display_options={"configuration_version": 3},
    )

    period = facts["sources"][SourceSnapshot.Source.METRIKA]["period_details"][0]
    selected = period["payload"]["detail_variants"]["search"]["humans"]
    assert selected["landing_pages_total"] == {"visits": 123}
    assert selected["search_engines"] == [{"name": "Yandex", "visits": 100}]
    assert "unused_variant" not in period["payload"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*), max(bytes) FROM report_branch_memory")
        samples, peak = cursor.fetchone()
    assert samples >= 20, "Memory must be sampled throughout extraction, including late branches"
    assert peak - baseline < 64 * 1024 * 1024, (
        f"Metrika branch extraction allocated {peak - baseline:,} extra bytes "
        "for an 8 MiB source; repeated detoast copies must not accumulate"
    )
