import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_upstream_import_progress_migration_preserves_existing_document_jobs():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    fork_targets = [
        ('projects', '0035_projectimport_document_upload_fields'),
        ('data_import', '0007_fileupload_preprocessing_job_id'),
    ]
    try:
        executor.migrate(fork_targets)
        old_apps = executor.loader.project_state(fork_targets).apps
        old_import = old_apps.get_model('projects', 'ProjectImport')
        batch = old_import.objects.create(
            import_type='document_upload',
            job_id='existing-document-job',
            request_metadata={'source': 'migration-test'},
        )

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        new_import = executor.loader.project_state(latest).apps.get_model('projects', 'ProjectImport')
        upgraded = new_import.objects.get(pk=batch.pk)

        assert upgraded.job_id == 'existing-document-job'
        assert upgraded.import_type == 'document_upload'
        assert upgraded.request_metadata == {'source': 'migration-test'}
        assert upgraded.failed_by_health_check is None
        assert new_import._meta.get_field('job_id').max_length == 256
        # Upstream allows NULL when there is no single background job.
        new_import.objects.filter(pk=batch.pk).update(job_id=None)
    finally:
        MigrationExecutor(connection).migrate(latest)
