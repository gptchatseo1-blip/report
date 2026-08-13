import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.projects.models import Project


@pytest.mark.django_db(transaction=True)
def test_latest_project_credential_is_moved_to_global_settings():
    project = Project.objects.create(name="Migration", domain="migration.example")
    executor = MigrationExecutor(connection)
    executor.migrate([("topvisor", "0003_topvisorconnection")])
    old_apps = executor.loader.project_state([("topvisor", "0003_topvisorconnection")]).apps
    TopvisorConnection = old_apps.get_model("topvisor", "TopvisorConnection")
    verified_at = timezone.now()
    TopvisorConnection.objects.create(
        project_id=project.pk,
        user_id="shared-user",
        api_key_encrypted=b"encrypted-key",
        api_key_last_four="-key",
        last_verified_at=verified_at,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("topvisor", "0004_global_topvisor_credential")])
    new_apps = executor.loader.project_state([("topvisor", "0004_global_topvisor_credential")]).apps
    TopvisorCredential = new_apps.get_model("topvisor", "TopvisorCredential")
    credential = TopvisorCredential.objects.get(pk=1)

    assert credential.user_id == "shared-user"
    assert bytes(credential.api_key_encrypted) == b"encrypted-key"
    assert credential.api_key_last_four == "-key"
    assert credential.last_verified_at == verified_at
