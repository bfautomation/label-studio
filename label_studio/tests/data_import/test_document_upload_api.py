import gzip
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from data_import.functions import (
    async_document_upload_file,
    maybe_finalize_document_upload_import,
    set_document_upload_file_background_failure,
)
from data_import.models import DocumentConversionArtifact, FileUpload
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from projects.models import ProjectImport
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from tasks.models import Task

LABEL_CONFIG = """
<View>
  <Labels name="pii" toName="md" choice="single">
    <Label value="PERSON" />
  </Labels>
  <Text name="md" value="$markdown" granularity="word" />
</View>
"""


def raising_document_preprocessor(context):
    raise RuntimeError(f'boom for {context["original_filename"]}')


def conditional_document_preprocessor(context):
    if context['original_filename'].startswith('bad'):
        raise RuntimeError('custom preprocessor failure')

    with open(context['uploaded_file_path'], encoding='utf-8') as stream:
        body = stream.read()

    return [
        {
            'data': {
                'doc_id': f'doc-{context["file_upload_id"]}',
                'source_filename': context['original_filename'],
                'source_mime_type': context['mime_type'],
                'markdown': body,
            }
        }
    ]


@override_settings(DOCUMENT_UPLOAD_JOB_TIMEOUT_SECONDS=3600)
class TestProjectFileUploadAPI(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory(label_config=LABEL_CONFIG)
        cls.user = cls.project.created_by

    def setUp(self):
        self.client.force_authenticate(user=self.user)
        self.upload_url = reverse('data_import:api-projects:project-upload-files', kwargs={'pk': self.project.id})
        self.import_url = lambda import_id: reverse(
            'projects:api:project-imports',
            kwargs={'pk': self.project.id, 'import_pk': import_id},
        )
        self.retry_url = lambda import_id: reverse(
            'data_import:api-projects:project-document-upload-retry',
            kwargs={'pk': self.project.id, 'import_pk': import_id},
        )
        self.queue_patcher = patch('data_import.api.queue_async_available', return_value=True)
        self.start_job_patcher = patch(
            'data_import.api.start_job_async_or_sync',
            side_effect=lambda *args, **kwargs: SimpleNamespace(id=f'job-{args[1] if len(args) > 1 else "1"}'),
        )
        self.queue_async_available_mock = self.queue_patcher.start()
        self.start_job_async_mock = self.start_job_patcher.start()
        self.addCleanup(self.queue_patcher.stop)
        self.addCleanup(self.start_job_patcher.stop)

    def _upload(self, files, metadata=None):
        payload = {'files': files}
        if metadata is not None:
            payload['metadata'] = json.dumps(metadata)
        return self.client.post(self.upload_url, data=payload, format='multipart')

    def _process_import(self, import_id):
        project_import = ProjectImport.objects.get(id=import_id)
        for file_upload_id in project_import.file_upload_ids:
            file_upload = FileUpload.objects.get(id=file_upload_id)
            if file_upload.preprocessing_status == FileUpload.PreprocessingStatus.PENDING:
                async_document_upload_file(import_id, file_upload_id, self.user.id)
        maybe_finalize_document_upload_import(import_id)
        return self.client.get(self.import_url(import_id))

    def test_upload_txt_file_creates_task_with_markdown(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])

        self.assertEqual(response.status_code, 202)
        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()['results'][0]['status'], 'success')

        task = Task.objects.get(project=self.project)
        artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
        self.assertEqual(task.data['markdown'], 'hello text')
        self.assertEqual(task.data['source_filename'], 'example.txt')
        self.assertIsNotNone(task.file_upload_id)
        self.assertEqual(task.meta['artifact_id'], artifact.id)
        self.assertEqual(artifact.provider, DocumentConversionArtifact.Provider.LOCAL_TEXT)
        self.assertTrue(artifact.metadata['artifact_synthesized'])
        with artifact.raw_markdown_file.open() as stream:
            self.assertEqual(stream.read().decode('utf-8'), 'hello text')

    def test_upload_md_file_creates_task_with_markdown(self):
        response = self._upload([SimpleUploadedFile('example.md', b'# Title\n\nBody', content_type='text/markdown')])

        self.assertEqual(response.status_code, 202)
        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        task = Task.objects.get(project=self.project)
        artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
        self.assertEqual(task.data['markdown'], '# Title\n\nBody')
        self.assertEqual(status_response.json()['results'][0]['tasks_created'], 1)
        self.assertEqual(task.meta['artifact_id'], artifact.id)

    def test_markdown_is_normalized_to_lf_before_task_creation(self):
        response = self._upload(
            [SimpleUploadedFile('example.md', b'# Title\r\n\r\nBody\r\n', content_type='text/markdown')]
        )

        import_id = response.json()['import']
        self._process_import(import_id)

        task = Task.objects.get(project=self.project)
        artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
        self.assertEqual(task.data['markdown'], '# Title\n\nBody\n')
        with artifact.annotation_markdown_file.open() as stream:
            self.assertEqual(stream.read().decode('utf-8'), '# Title\n\nBody\n')

    def test_markdown_tables_are_passed_through(self):
        response = self._upload(
            [SimpleUploadedFile('table.md', b'| a | b |\n| --- | --- |\n| 1 | 2 |', content_type='text/markdown')]
        )

        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        task = Task.objects.get(project=self.project)
        self.assertEqual(status_response.json()['results'][0]['status'], 'success')
        self.assertEqual(task.data['markdown'], '| a | b |\n| --- | --- |\n| 1 | 2 |')

    def test_hard_line_break_markdown_is_passed_through(self):
        response = self._upload(
            [SimpleUploadedFile('breaks.md', b'Line one  \nLine two', content_type='text/markdown')]
        )

        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        task = Task.objects.get(project=self.project)
        self.assertEqual(status_response.json()['results'][0]['status'], 'success')
        self.assertEqual(task.data['markdown'], 'Line one  \nLine two')

    @override_settings(
        FILE_UPLOAD_PREPROCESSOR='label_studio.tests.data_import.test_document_upload_api.conditional_document_preprocessor'
    )
    def test_upload_multiple_files_returns_per_file_results(self):
        response = self._upload(
            [
                SimpleUploadedFile('good.txt', b'good file', content_type='text/plain'),
                SimpleUploadedFile('bad.md', b'bad file', content_type='text/markdown'),
            ]
        )

        self.assertEqual(response.status_code, 202)
        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        self.assertEqual(len(status_response.json()['results']), 2)
        self.assertEqual(status_response.json()['results'][0]['status'], 'success')
        self.assertEqual(status_response.json()['results'][1]['status'], 'error')
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_upload_enqueues_one_job_per_valid_file(self):
        response = self._upload(
            [
                SimpleUploadedFile('good.txt', b'good file', content_type='text/plain'),
                SimpleUploadedFile('also-good.md', b'# good', content_type='text/markdown'),
                SimpleUploadedFile('bad.zip', b'bad', content_type='application/zip'),
            ]
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.start_job_async_mock.call_count, 2)
        for call in self.start_job_async_mock.call_args_list:
            self.assertEqual(call.args[0].__name__, 'async_document_upload_file')

    def test_unsupported_extension_is_rejected_cleanly(self):
        response = self._upload([SimpleUploadedFile('archive.zip', b'bad', content_type='application/zip')])

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload['results'][0]['status'], 'error')
        self.assertIn('Unsupported file extension', payload['results'][0]['error'])
        self.assertEqual(Task.objects.filter(project=self.project).count(), 0)
        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.FAILED)

        project_import = ProjectImport.objects.get(id=payload['import'])
        self.assertEqual(project_import.status, ProjectImport.Status.COMPLETED)

    @override_settings(
        FILE_UPLOAD_PREPROCESSOR='label_studio.tests.data_import.test_document_upload_api.raising_document_preprocessor'
    )
    def test_preprocessor_exception_returns_per_file_error(self):
        response = self._upload([SimpleUploadedFile('broken.txt', b'broken', content_type='text/plain')])

        self.assertEqual(response.status_code, 202)
        import_id = response.json()['import']
        status_response = self._process_import(import_id)

        result = status_response.json()['results'][0]
        self.assertEqual(result['status'], 'error')
        self.assertIn('boom for broken.txt', result['error'])
        self.assertEqual(Task.objects.filter(project=self.project).count(), 0)

    def test_created_tasks_belong_to_correct_project(self):
        other_project = ProjectFactory(label_config=LABEL_CONFIG, organization=self.project.organization)
        response = self._upload([SimpleUploadedFile('project.txt', b'belongs here', content_type='text/plain')])

        self._process_import(response.json()['import'])

        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)
        self.assertEqual(Task.objects.filter(project=other_project).count(), 0)

    def test_uploaded_file_metadata_is_persisted(self):
        response = self._upload(
            [SimpleUploadedFile('meta.md', b'metadata body', content_type='text/markdown')],
            metadata={'source': 'qa'},
        )

        self._process_import(response.json()['import'])

        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.original_filename, 'meta.md')
        self.assertEqual(upload.mime_type, 'text/markdown')
        self.assertEqual(upload.file_size, len(b'metadata body'))
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.COMPLETED)
        self.assertEqual(upload.preprocessing_metadata, {'source': 'qa'})
        self.assertEqual(len(upload.created_task_ids), 1)
        self.assertIsNotNone(upload.uploaded_at)
        self.assertEqual(upload.conversion_artifact.provider, DocumentConversionArtifact.Provider.LOCAL_TEXT)

    def test_filenames_are_sanitized(self):
        response = self._upload([SimpleUploadedFile('../Bad Name.md', b'body', content_type='text/markdown')])

        self._process_import(response.json()['import'])

        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.original_filename, 'Bad Name.md')
        self.assertEqual(upload.sanitized_filename, 'Bad_Name.md')
        self.assertTrue(upload.file.name.endswith('Bad_Name.md'))
        self.assertNotIn('..', upload.file.name)

    @override_settings(DOCUMENT_UPLOAD_MAX_FILE_SIZE=5)
    def test_oversized_files_are_rejected(self):
        response = self._upload([SimpleUploadedFile('large.txt', b'123456', content_type='text/plain')])

        self.assertEqual(response.status_code, 202)
        result = response.json()['results'][0]
        self.assertEqual(result['status'], 'error')
        self.assertIn('maximum allowed size', result['error'])
        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.FAILED)

    @patch('data_import.functions.emit_webhooks_for_instance', side_effect=RuntimeError('webhook down'))
    def test_webhook_failure_does_not_mark_completed_file_as_failed(self, _mock_emit):
        response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])

        status_response = self._process_import(response.json()['import'])

        self.assertEqual(status_response.json()['results'][0]['status'], 'success')
        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.COMPLETED)
        self.assertEqual(len(upload.created_task_ids), 1)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_queue_unavailable_returns_503_before_creating_rows(self):
        with patch('data_import.api.queue_async_available', return_value=False):
            response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])

        self.assertEqual(response.status_code, 503)
        self.assertEqual(FileUpload.objects.count(), 0)
        self.assertEqual(ProjectImport.objects.count(), 0)

    def test_enqueue_failure_returns_503_and_marks_rows_failed(self):
        with patch('data_import.api.start_job_async_or_sync', side_effect=RuntimeError('queue boom')):
            response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload['detail'], 'queue boom')
        project_import = ProjectImport.objects.get(id=payload['import'])
        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(project_import.status, ProjectImport.Status.FAILED)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.FAILED)
        self.assertEqual(upload.preprocessing_error, 'queue boom')

    def test_retry_enqueue_failure_restores_processing_files(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])
        import_id = response.json()['import']
        upload = FileUpload.objects.get(project=self.project)
        upload.preprocessing_status = FileUpload.PreprocessingStatus.PROCESSING
        upload.save(update_fields=['preprocessing_status'])
        ProjectImport.objects.filter(id=import_id).update(status=ProjectImport.Status.FAILED, error='stalled')

        with patch('data_import.api.start_job_async_or_sync', side_effect=RuntimeError('queue boom')):
            retry_response = self.client.post(self.retry_url(import_id), data={}, format='json')

        self.assertEqual(retry_response.status_code, 503)
        upload.refresh_from_db()
        project_import = ProjectImport.objects.get(id=import_id)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.PROCESSING)
        self.assertEqual(project_import.status, ProjectImport.Status.FAILED)

    @override_settings(
        FILE_UPLOAD_PREPROCESSOR='data_import.document_preprocessing.azure_document_intelligence_preprocessor',
        DOCUMENT_UPLOAD_ALLOWED_EXTENSIONS=['.txt', '.md', '.markdown', '.pdf', '.docx'],
        AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT='https://example.cognitiveservices.azure.com',
        AZURE_DOCUMENT_INTELLIGENCE_KEY='test-key',
        AZURE_DOCUMENT_INTELLIGENCE_SYNC_TIMEOUT_SECONDS=60,
        AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS=5,
    )
    @patch('data_import.document_preprocessing.requests.get')
    @patch('data_import.document_preprocessing.requests.post')
    def test_pdf_upload_uses_azure_document_intelligence(self, mock_post, mock_get):
        mock_post.return_value = Mock(
            status_code=202,
            headers={'Operation-Location': 'https://example.cognitiveservices.azure.com/operations/123'},
        )
        mock_get.return_value = Mock(
            status_code=200,
            headers={},
            json=Mock(
                return_value={
                    'status': 'succeeded',
                    'id': 'operation-123',
                    'analyzeResult': {'content': '# Azure Markdown\n\nPatient: Jane Doe'},
                }
            ),
        )

        response = self._upload([SimpleUploadedFile('scan.pdf', b'%PDF-1.4 fake', content_type='application/pdf')])
        self.assertEqual(response.status_code, 202)
        self._process_import(response.json()['import'])

        task = Task.objects.get(project=self.project)
        artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
        self.assertEqual(task.data['markdown'], '# Azure Markdown\n\nPatient: Jane Doe')
        self.assertEqual(task.meta['artifact_id'], artifact.id)
        self.assertEqual(artifact.provider, DocumentConversionArtifact.Provider.AZURE_DOCUMENT_INTELLIGENCE)
        self.assertEqual(artifact.provider_operation_id, 'operation-123')
        with artifact.raw_markdown_file.open() as stream:
            self.assertEqual(stream.read().decode('utf-8'), '# Azure Markdown\n\nPatient: Jane Doe')
        with artifact.raw_result_file.open('rb') as stream:
            payload = json.loads(gzip.decompress(stream.read()).decode('utf-8'))
        self.assertEqual(payload['id'], 'operation-123')

    def test_deleting_upload_cleans_up_artifact_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                response = self._upload([SimpleUploadedFile('example.txt', b'hello text', content_type='text/plain')])

                self._process_import(response.json()['import'])
                artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
                markdown_path = artifact.raw_markdown_file.path
                annotation_path = artifact.annotation_markdown_file.path
                self.assertTrue(os.path.exists(markdown_path))
                self.assertTrue(os.path.exists(annotation_path))

                with self.captureOnCommitCallbacks(execute=True):
                    FileUpload.objects.filter(project=self.project).delete()

                self.assertFalse(os.path.exists(markdown_path))
                self.assertFalse(os.path.exists(annotation_path))

    def test_artifact_storage_failure_removes_already_written_files(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=tmpdir):
            response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
            field = DocumentConversionArtifact._meta.get_field('annotation_markdown_file')
            with patch.object(field, 'generate_filename', side_effect=OSError('storage full')):
                status_response = self._process_import(response.json()['import'])
            self.assertEqual(status_response.json()['results'][0]['status'], 'error')
            self.assertFalse(DocumentConversionArtifact.objects.exists())
            self.assertFalse(Task.objects.filter(project=self.project).exists())
            artifact_dir = os.path.join(tmpdir, 'upload', 'artifacts')
            self.assertEqual([name for _, _, files in os.walk(artifact_dir) for name in files], [])

    def test_finalization_failure_is_persisted_after_transaction_rollback(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
        import_id = response.json()['import']
        with patch(
            'data_import.functions._finalize_document_upload_import_locked',
            side_effect=RuntimeError('counter failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'counter failure'):
                self._process_import(import_id)
        project_import = ProjectImport.objects.get(id=import_id)
        self.assertEqual(project_import.status, ProjectImport.Status.FAILED)
        self.assertEqual(project_import.error, 'counter failure')
        self.assertIsNotNone(project_import.finished_at)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_stale_job_cannot_claim_replacement_attempt(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
        upload = FileUpload.objects.get(project=self.project)
        async_document_upload_file(response.json()['import'], upload.id, self.user.id, expected_job_id='old-job')
        upload.refresh_from_db()
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.PENDING)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_replaced_processing_attempt_cannot_commit_tasks(self):
        from data_import.document_preprocessing import preprocess_uploaded_document

        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])

        def replace_attempt(**kwargs):
            result = preprocess_uploaded_document(**kwargs)
            FileUpload.objects.filter(id=kwargs['file_upload'].id).update(
                preprocessing_job_id='replacement-job',
                preprocessing_status=FileUpload.PreprocessingStatus.PENDING,
            )
            return result

        with patch('data_import.functions.preprocess_uploaded_document', side_effect=replace_attempt):
            self._process_import(response.json()['import'])
        upload = FileUpload.objects.get(project=self.project)
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.PENDING)
        self.assertFalse(Task.objects.filter(project=self.project).exists())
        self.assertFalse(DocumentConversionArtifact.objects.exists())

    def test_stale_job_failure_cannot_fail_replacement_attempt(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
        upload = FileUpload.objects.get(project=self.project)
        job = SimpleNamespace(id='old-job', args=(response.json()['import'], upload.id, self.user.id))
        set_document_upload_file_background_failure(job, None, RuntimeError, RuntimeError('old failure'), None)
        upload.refresh_from_db()
        self.assertEqual(upload.preprocessing_status, FileUpload.PreprocessingStatus.PENDING)
        self.assertEqual(upload.preprocessing_error, '')

    def test_retry_does_not_finalize_while_another_file_job_is_running(self):
        response = self._upload(
            [
                SimpleUploadedFile('complete.txt', b'body', content_type='text/plain'),
                SimpleUploadedFile('running.txt', b'body', content_type='text/plain'),
            ]
        )
        import_id = response.json()['import']
        uploads = list(FileUpload.objects.filter(project=self.project).order_by('id'))
        uploads[0].preprocessing_status = FileUpload.PreprocessingStatus.COMPLETED
        uploads[0].save(update_fields=['preprocessing_status'])
        uploads[1].preprocessing_status = FileUpload.PreprocessingStatus.PROCESSING
        uploads[1].save(update_fields=['preprocessing_status'])
        ProjectImport.objects.filter(id=import_id).update(status=ProjectImport.Status.FAILED)
        self.start_job_async_mock.reset_mock()
        with patch('data_import.functions.get_job_health', return_value='running'):
            retry = self.client.post(self.retry_url(import_id), data={}, format='json')
        self.assertEqual(retry.status_code, 400)
        self.start_job_async_mock.assert_not_called()

    def test_rolled_back_artifact_deletion_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=tmpdir):
            response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
            self._process_import(response.json()['import'])
            artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
            path = artifact.raw_markdown_file.path
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    artifact.delete()
                    raise RuntimeError('rollback')
            self.assertTrue(os.path.exists(path))
            self.assertTrue(DocumentConversionArtifact.objects.filter(file_upload__project=self.project).exists())

    def test_successful_file_job_is_idempotent(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
        import_id = response.json()['import']
        self._process_import(import_id)
        self._process_import(import_id)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)
        self.assertEqual(DocumentConversionArtifact.objects.filter(file_upload__project=self.project).count(), 1)

    def test_fast_worker_and_later_enqueue_failure_finalize_batch(self):
        def enqueue_or_fail(func, import_id, upload_id, user_id, **kwargs):
            if enqueue_or_fail.called:
                raise RuntimeError('queue failed after first file')
            enqueue_or_fail.called = True
            func(import_id, upload_id, user_id, expected_job_id=kwargs['expected_job_id'])
            return SimpleNamespace(id=kwargs['job_id'])

        enqueue_or_fail.called = False
        with patch('data_import.api.start_job_async_or_sync', side_effect=enqueue_or_fail):
            response = self._upload(
                [
                    SimpleUploadedFile('first.txt', b'body', content_type='text/plain'),
                    SimpleUploadedFile('second.txt', b'body', content_type='text/plain'),
                ]
            )
        self.assertEqual(response.status_code, 202)
        project_import = ProjectImport.objects.get(id=response.json()['import'])
        self.assertEqual(project_import.status, ProjectImport.Status.COMPLETED)
        self.assertEqual(project_import.task_count, 1)
        self.assertEqual([item['status'] for item in response.json()['results']], ['success', 'error'])

    def test_annotation_artifact_matches_normalized_task_and_preserves_raw_text(self):
        raw = '\ufeffCafe\u0301\r\nLine two\r'
        response = self._upload([SimpleUploadedFile('unicode.md', raw.encode('utf-8'), content_type='text/markdown')])
        self._process_import(response.json()['import'])
        task = Task.objects.get(project=self.project)
        artifact = DocumentConversionArtifact.objects.get(file_upload__project=self.project)
        self.assertEqual(task.data['markdown'], 'Caf\u00e9\nLine two\n')
        with artifact.raw_markdown_file.open('rb') as stream:
            self.assertEqual(stream.read().decode('utf-8'), raw)
        with artifact.annotation_markdown_file.open('rb') as stream:
            self.assertEqual(stream.read().decode('utf-8'), task.data['markdown'])

    def test_custom_preannotations_are_reflected_in_batch_counts(self):
        from data_import.document_preprocessing import preprocess_uploaded_document

        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])

        def with_preannotations(**kwargs):
            result = preprocess_uploaded_document(**kwargs)
            labels = [
                {
                    'from_name': 'pii',
                    'to_name': 'md',
                    'type': 'labels',
                    'value': {'start': 0, 'end': 4, 'text': 'body', 'labels': ['PERSON']},
                }
            ]
            result.tasks_payload[0]['annotations'] = [{'result': labels}]
            result.tasks_payload[0]['predictions'] = [{'result': labels, 'model_version': 'test'}]
            return result

        with patch('data_import.functions.preprocess_uploaded_document', side_effect=with_preannotations):
            self._process_import(response.json()['import'])
        batch = ProjectImport.objects.get(id=response.json()['import'])
        self.assertEqual(batch.task_count, 1)
        self.assertEqual(batch.annotation_count, 1)
        self.assertEqual(batch.prediction_count, 1)

    def test_finalization_retry_does_not_overwrite_fast_worker_completion(self):
        response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
        import_id = response.json()['import']
        self._process_import(import_id)
        ProjectImport.objects.filter(id=import_id).update(status=ProjectImport.Status.FAILED, error='counter failure')

        def finalize_inline(func, batch_id, **kwargs):
            func(batch_id)
            return SimpleNamespace(id=kwargs['job_id'])

        with patch('data_import.api.start_job_async_or_sync', side_effect=finalize_inline):
            retry = self.client.post(self.retry_url(import_id), data={}, format='json')
        self.assertEqual(retry.status_code, 202)
        batch = ProjectImport.objects.get(id=import_id)
        self.assertEqual(batch.status, ProjectImport.Status.COMPLETED)
        self.assertEqual(batch.job_id, '')

    def test_batch_creation_failure_removes_uploaded_source_files(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=tmpdir):
            with patch('data_import.api.ProjectImport.objects.create', side_effect=RuntimeError('database failure')):
                response = self._upload([SimpleUploadedFile('example.txt', b'body', content_type='text/plain')])
                self.assertEqual(response.status_code, 500)
            self.assertFalse(FileUpload.objects.filter(project=self.project).exists())
            self.assertEqual([name for _, _, files in os.walk(tmpdir) for name in files], [])
