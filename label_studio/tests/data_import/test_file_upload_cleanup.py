import os
import threading

import pytest
from data_export.models import ConvertedFormat, Export
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import Collector
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
    """The project API deletes with signals disconnected except those that delete stored files."""
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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('when', ['before', 'after'])
@pytest.mark.parametrize(
    'destroy',
    [
        lambda project: ProjectAPI().perform_destroy(project),
        lambda project: destroy_organization(project.organization),
    ],
    ids=['project', 'organization'],
)
def test_upload_committed_during_a_deletion_keeps_its_row_and_file_together(
    destroy, when, settings, tmp_path, monkeypatch
):
    """A deletion removes the files of exactly the uploads it deletes, even one another connection commits while it
    runs: committed before Django looks up the rows to delete, it goes with its file; committed after, the database
    refuses the deletion and both uploads keep their files."""
    if connection.vendor != 'postgresql':
        pytest.skip('needs a second connection committing while the deletion runs')
    settings.MEDIA_ROOT = str(tmp_path)
    project = ProjectFactory()
    # see test_deleting_an_organization_deletes_its_projects_files
    JWTSettings.objects.filter(organization=project.organization).delete()
    first = FileUpload(user=project.created_by, project=project)
    first.file.save('fictional.wav', ContentFile(b'fictional audio'), save=True)
    late = {}

    def commit_late_upload():
        upload = FileUpload(user=project.created_by, project=project)
        upload.file.save('fictional-late.wav', ContentFile(b'fictional audio'), save=True)
        late.update(pk=upload.pk, path=upload.file.path)
        connections.close_all()

    def in_another_connection(work):
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()

    # commit the late upload around the first deletion's outermost lookup; nested and later lookups run unchanged
    lookup = Collector.collect
    depth = 0

    def collect(self, *args, **kwargs):
        nonlocal depth
        outermost = depth == 0 and not late
        if outermost and when == 'before':
            in_another_connection(commit_late_upload)
        depth += 1
        try:
            return lookup(self, *args, **kwargs)
        finally:
            depth -= 1
            if outermost and when == 'after':
                in_another_connection(commit_late_upload)

    monkeypatch.setattr(Collector, 'collect', collect)

    if when == 'before':
        destroy(project)
    else:
        with pytest.raises(IntegrityError):
            destroy(project)

    files = {first.pk: first.file.path, late['pk']: late['path']}
    remaining = set(FileUpload.objects.filter(pk__in=files).values_list('pk', flat=True))
    assert remaining == (set() if when == 'before' else set(files))
    assert {pk for pk, path in files.items() if os.path.exists(path)} == remaining
