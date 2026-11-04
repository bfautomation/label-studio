import os

import pytest
from data_export.models import Export
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.db import transaction
from jwt_auth.models import JWTSettings
from organizations.functions import destroy_organization
from projects.tests.factories import ProjectFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def upload(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    project = ProjectFactory()
    upload = FileUpload(user=project.created_by, project=project)
    upload.file.save('fictional.wav', ContentFile(b'fictional audio'), save=True)
    assert os.path.exists(upload.file.path)
    return upload


def test_deleting_an_upload_deletes_its_file(upload, django_capture_on_commit_callbacks):
    """An upload removed on its own takes its stored file with it."""
    path = upload.file.path

    with django_capture_on_commit_callbacks(execute=True):
        upload.delete()

    assert not os.path.exists(path)


def test_deleting_the_project_deletes_its_uploaded_files(upload, django_capture_on_commit_callbacks):
    """Deleting a project removes the files of the uploads deleted along with it."""
    path = upload.file.path

    with django_capture_on_commit_callbacks(execute=True):
        upload.project.delete()

    assert not FileUpload.objects.filter(pk=upload.pk).exists()
    assert not os.path.exists(path)


def test_rolled_back_deletion_keeps_the_file(upload, django_capture_on_commit_callbacks):
    """The file is only deleted once the deletion of its record is committed."""
    path, upload_id = upload.file.path, upload.pk

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            upload.delete()
            raise RuntimeError('deletion abandoned')

    assert FileUpload.objects.filter(pk=upload_id).exists()
    assert os.path.exists(path)


def _stored_files(project):
    """Give the project an upload and a stored export, and return the paths of their files."""
    upload = FileUpload(user=project.created_by, project=project)
    upload.file.save('fictional.wav', ContentFile(b'fictional audio'), save=True)
    export = Export.objects.create(project=project, created_by=project.created_by)
    export.file.save('fictional.json', ContentFile(b'[]'), save=True)
    paths = [upload.file.path, export.file.path]
    assert all(os.path.exists(path) for path in paths)
    return paths


def test_deleting_a_project_through_the_api_deletes_its_files(
    business_client, configured_project, settings, tmp_path, django_capture_on_commit_callbacks
):
    """The project API deletes with signals disconnected, so it cleans up the project's files itself."""
    settings.MEDIA_ROOT = str(tmp_path)
    paths = _stored_files(configured_project)

    with django_capture_on_commit_callbacks(execute=True):
        response = business_client.delete(f'/api/projects/{configured_project.id}/')

    assert response.status_code == 204
    assert not any(os.path.exists(path) for path in paths)


def test_deleting_an_organization_deletes_its_projects_files(settings, tmp_path, django_capture_on_commit_callbacks):
    """Deleting an organization removes the files of every project in it."""
    settings.MEDIA_ROOT = str(tmp_path)
    project = ProjectFactory()
    paths = _stored_files(project)
    # destroy_organization leaves the organization's JWT settings row behind (DO_NOTHING), which the database
    # rejects; that is separate from file cleanup, so remove the row first
    JWTSettings.objects.filter(organization=project.organization).delete()

    with django_capture_on_commit_callbacks(execute=True):
        destroy_organization(project.organization)

    assert not any(os.path.exists(path) for path in paths)
