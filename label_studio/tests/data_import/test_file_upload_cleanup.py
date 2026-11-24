import os

import pytest
from data_export.models import ConvertedFormat, Export
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.db import transaction
from jwt_auth.models import JWTSettings
from organizations.functions import destroy_organization
from organizations.models import Organization
from projects.api import ProjectAPI
from projects.models import Project
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
    """Give the project an upload, a stored export and a converted export, and return the paths of their files."""
    upload = FileUpload(user=project.created_by, project=project)
    upload.file.save('fictional.wav', ContentFile(b'fictional audio'), save=True)
    export = Export.objects.create(project=project, created_by=project.created_by)
    export.file.save('fictional.json', ContentFile(b'[]'), save=True)
    # created as the conversion endpoint does: linked to the export only, without a project
    converted, _ = ConvertedFormat.objects.get_or_create(export=export, export_type='CSV')
    converted.file.save('fictional.csv', ContentFile(b'id'), save=True)
    paths = [upload.file.path, export.file.path, converted.file.path]
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


def _fail(*args, **kwargs):
    raise RuntimeError('deletion failed')


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    'model, destroy',
    [
        (Project, lambda project: ProjectAPI().perform_destroy(project)),
        (Organization, lambda project: destroy_organization(project.organization)),
    ],
    ids=['project', 'organization'],
)
def test_failed_deletion_keeps_the_files(model, destroy, settings, tmp_path, monkeypatch):
    """Requests run under autocommit, so the files must wait for the deletion to commit, not be removed up front."""
    settings.MEDIA_ROOT = str(tmp_path)
    project = ProjectFactory()
    paths = _stored_files(project)
    monkeypatch.setattr(model, 'delete', _fail)

    with pytest.raises(RuntimeError):
        destroy(project)

    assert Project.objects.filter(pk=project.pk).exists()
    assert all(os.path.exists(path) for path in paths)
