import os

import pytest
from data_export.models import ConvertedFormat, DataExport, Export
from django.core.files.base import ContentFile
from projects.tests.factories import ProjectFactory
from tasks.tests.factories import TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / 'media')
    return tmp_path


@pytest.fixture
def export(media_root):
    project = ProjectFactory()
    export = Export.objects.create(project=project, created_by=project.created_by)
    export.file.save('fictional.json', ContentFile(b'[]'), save=True)
    return export


def test_deleting_an_export_deletes_its_file(export, django_capture_on_commit_callbacks):
    """A stored export removed on its own takes its file with it."""
    path = export.file.path

    with django_capture_on_commit_callbacks(execute=True):
        export.delete()

    assert not os.path.exists(path)


def test_deleting_the_project_deletes_export_and_converted_files(export, django_capture_on_commit_callbacks):
    """Deleting a project removes the files of its exports and their converted formats."""
    converted = ConvertedFormat.objects.create(project=export.project, export=export, export_type='CSV')
    converted.file.save('fictional.csv', ContentFile(b'id'), save=True)
    paths = [export.file.path, converted.file.path]

    with django_capture_on_commit_callbacks(execute=True):
        export.project.delete()

    assert not any(os.path.exists(path) for path in paths)


def test_download_export_leaves_no_copy_on_disk(settings, tmp_path):
    """Building a download export keeps no working copy of the data in the export directory."""
    settings.EXPORT_DIR = str(tmp_path)
    project = ProjectFactory()
    TaskFactory(project=project)

    out, _, filename = DataExport.generate_export_file(project, [{'id': 1, 'data': {}}], 'JSON', False, {})
    out.close()

    assert filename.endswith('.json')
    assert os.listdir(tmp_path) == []
