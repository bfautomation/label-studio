from unittest.mock import patch

import pytest
from io_storages.azure_blob.models import AzureBlobExportStorage, AzureBlobExportStorageLink
from projects.tests.factories import ProjectFactory
from tasks.tests.factories import AnnotationFactory, TaskFactory

pytestmark = pytest.mark.django_db


@patch('io_storages.azure_blob.models.AzureBlobExportStorage.delete_annotation')
@patch('io_storages.azure_blob.models.AzureBlobExportStorage.save_annotation')
def test_export_storage_changes_only_on_sync(mock_save, mock_delete, django_capture_on_commit_callbacks):
    """Saving or deleting an annotation leaves export storage alone; a manual sync is what writes it."""
    project = ProjectFactory()
    storage = AzureBlobExportStorage.objects.create(project=project, container='exports', can_delete_objects=True)

    with django_capture_on_commit_callbacks(execute=True):
        annotation = AnnotationFactory(project=project, task=TaskFactory(project=project))
    mock_save.assert_not_called()

    storage.sync()
    mock_save.assert_called_once()

    AzureBlobExportStorageLink.objects.create(storage=storage, annotation=annotation)
    with django_capture_on_commit_callbacks(execute=True):
        annotation.delete()
    mock_delete.assert_not_called()
