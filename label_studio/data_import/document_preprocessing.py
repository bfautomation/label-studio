"""Document upload preprocessing helpers."""

import gzip
import json
import logging
import mimetypes
import os
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import import_module
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from rest_framework.exceptions import ValidationError

from label_studio.core.utils.common import load_func

logger = logging.getLogger(__name__)


class DocumentPreprocessingError(Exception):
    """Expected preprocessing failure with a user-readable message."""


@dataclass
class ArtifactDraft:
    provider: str
    raw_markdown_text: str = ''
    annotation_markdown_text: str = ''
    content_format: str = 'markdown'
    provider_model_id: str = ''
    provider_api_version: str = ''
    provider_operation_id: str = ''
    raw_result_bytes: bytes | None = None
    raw_result_filename: str = ''
    metadata: dict = field(default_factory=dict)


@dataclass
class PreprocessedDocumentResult:
    tasks_payload: list[dict]
    preprocessor_name: str
    artifact_draft: ArtifactDraft


def get_document_preprocessor():
    preprocessor_path = settings.FILE_UPLOAD_PREPROCESSOR
    if isinstance(preprocessor_path, str) and ':' in preprocessor_path:
        module_name, attribute_name = preprocessor_path.split(':', 1)
        module = import_module(module_name)
        return getattr(module, attribute_name)
    return load_func(preprocessor_path)


def validate_document_extension(filename):
    _, extension = os.path.splitext(filename or '')
    extension = extension.lower()
    if extension not in settings.DOCUMENT_UPLOAD_ALLOWED_EXTENSIONS:
        allowed = ', '.join(settings.DOCUMENT_UPLOAD_ALLOWED_EXTENSIONS)
        raise ValidationError(f'Unsupported file extension "{extension or "<none>"}". Allowed extensions: {allowed}.')
    return extension


def validate_document_file_size(file_obj):
    size = getattr(file_obj, 'size', 0) or 0
    if size > settings.DOCUMENT_UPLOAD_MAX_FILE_SIZE:
        raise ValidationError(
            f'File exceeds the maximum allowed size of {settings.DOCUMENT_UPLOAD_MAX_FILE_SIZE} bytes.'
        )


def build_document_context(*, project_id, file_upload, user_id, metadata):
    original_filename = file_upload.original_filename or os.path.basename(file_upload.file.name)
    mime_type = file_upload.mime_type or mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
    cleanup_path = None
    try:
        uploaded_file_path = file_upload.file.path
    except (AttributeError, NotImplementedError):
        suffix = os.path.splitext(original_filename)[1]
        with file_upload.file.open('rb') as source, tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            for chunk in source.chunks() if hasattr(source, 'chunks') else iter(lambda: source.read(1024 * 1024), b''):
                if not chunk:
                    break
                tmp.write(chunk)
            uploaded_file_path = tmp.name
            cleanup_path = tmp.name
    return {
        'project_id': project_id,
        'uploaded_file_path': uploaded_file_path,
        'original_filename': original_filename,
        'mime_type': mime_type,
        'uploaded_by_user_id': user_id,
        'metadata': metadata or {},
        'file_upload_id': file_upload.id,
        '_cleanup_path': cleanup_path,
    }


def normalize_annotation_markdown(markdown):
    if not isinstance(markdown, str):
        raise DocumentPreprocessingError('The active preprocessor returned non-text markdown.')

    normalized = markdown.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    return unicodedata.normalize('NFC', normalized)


def normalize_preprocessor_result(result, *, file_upload_id, original_filename, mime_type, preprocessor_name):
    if result is None:
        raise DocumentPreprocessingError('The active preprocessor returned no tasks.')

    if isinstance(result, dict):
        tasks = [result]
    elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        tasks = list(result)
    else:
        raise DocumentPreprocessingError('The active preprocessor must return a task payload or a list of payloads.')

    normalized_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            raise DocumentPreprocessingError('Each preprocessor task payload must be a JSON object.')

        task_data = task.get('data')
        if not isinstance(task_data, dict):
            raise DocumentPreprocessingError('Each preprocessor task payload must include a "data" object.')

        task_data = dict(task_data)
        task_data.setdefault('doc_id', str(uuid.uuid4()))
        task_data.setdefault('source_filename', original_filename)
        task_data.setdefault('source_mime_type', mime_type)
        if 'markdown' in task_data:
            task_data['markdown'] = normalize_annotation_markdown(task_data['markdown'])

        task_meta = task.get('meta') or {}
        if not isinstance(task_meta, dict):
            raise DocumentPreprocessingError('Each preprocessor task payload "meta" value must be an object.')
        task_meta = dict(task_meta)
        task_meta.setdefault('preprocessor', preprocessor_name)
        task_meta.setdefault('source_upload_id', str(file_upload_id))

        normalized_task = {key: value for key, value in task.items() if key not in ('data', 'meta')}
        normalized_task['data'] = task_data
        normalized_task['meta'] = task_meta
        normalized_task['file_upload_id'] = file_upload_id
        normalized_tasks.append(normalized_task)

    if not normalized_tasks:
        raise DocumentPreprocessingError('The active preprocessor returned no tasks.')
    return normalized_tasks


def _build_passthrough_task_payload(context, body):
    return [
        {
            'data': {
                'doc_id': str(uuid.uuid4()),
                'source_filename': context['original_filename'],
                'source_mime_type': context['mime_type'],
                'markdown': body,
            }
        }
    ]


def _synthesize_artifact_draft_from_tasks(context, tasks):
    extension = validate_document_extension(context['original_filename'])
    markdown = ''

    if extension in ('.txt', '.md', '.markdown'):
        with open(context['uploaded_file_path'], encoding='utf-8', newline='') as stream:
            markdown = stream.read()
        provider = 'local_text'
    else:
        first_markdown = next(
            (
                task.get('data', {}).get('markdown')
                for task in tasks
                if isinstance(task.get('data', {}).get('markdown'), str)
            ),
            '',
        )
        markdown = first_markdown or ''
        provider = 'custom_preprocessor'

    return ArtifactDraft(
        provider=provider,
        raw_markdown_text=markdown,
        annotation_markdown_text=next(
            (task['data']['markdown'] for task in tasks if isinstance(task['data'].get('markdown'), str)),
            normalize_annotation_markdown(markdown),
        ),
        metadata={'artifact_synthesized': True},
    )


def _coerce_preprocessed_document_result(
    result, *, context, file_upload_id, original_filename, mime_type, preprocessor_name
):
    if isinstance(result, PreprocessedDocumentResult):
        tasks = normalize_preprocessor_result(
            result.tasks_payload,
            file_upload_id=file_upload_id,
            original_filename=original_filename,
            mime_type=mime_type,
            preprocessor_name=preprocessor_name,
        )
        first_task_markdown = next(
            (
                task.get('data', {}).get('markdown')
                for task in tasks
                if isinstance(task.get('data', {}).get('markdown'), str)
            ),
            '',
        )
        annotation_markdown_text = result.artifact_draft.annotation_markdown_text or first_task_markdown
        if annotation_markdown_text:
            annotation_markdown_text = normalize_annotation_markdown(annotation_markdown_text)
        return PreprocessedDocumentResult(
            tasks_payload=tasks,
            preprocessor_name=preprocessor_name,
            artifact_draft=ArtifactDraft(
                provider=result.artifact_draft.provider,
                raw_markdown_text=result.artifact_draft.raw_markdown_text,
                annotation_markdown_text=annotation_markdown_text,
                content_format=result.artifact_draft.content_format,
                provider_model_id=result.artifact_draft.provider_model_id,
                provider_api_version=result.artifact_draft.provider_api_version,
                provider_operation_id=result.artifact_draft.provider_operation_id,
                raw_result_bytes=result.artifact_draft.raw_result_bytes,
                raw_result_filename=result.artifact_draft.raw_result_filename,
                metadata=dict(result.artifact_draft.metadata or {}),
            ),
        )

    tasks = normalize_preprocessor_result(
        result,
        file_upload_id=file_upload_id,
        original_filename=original_filename,
        mime_type=mime_type,
        preprocessor_name=preprocessor_name,
    )
    return PreprocessedDocumentResult(
        tasks_payload=tasks,
        preprocessor_name=preprocessor_name,
        artifact_draft=_synthesize_artifact_draft_from_tasks(context, tasks),
    )


def preprocess_uploaded_document(*, project_id, file_upload, user_id, metadata):
    context = build_document_context(
        project_id=project_id,
        file_upload=file_upload,
        user_id=user_id,
        metadata=metadata,
    )
    try:
        preprocessor = get_document_preprocessor()
        preprocessor_name = f'{preprocessor.__module__}.{preprocessor.__name__}'
        result = preprocessor(context)
        return _coerce_preprocessed_document_result(
            result,
            context=context,
            file_upload_id=file_upload.id,
            original_filename=context['original_filename'],
            mime_type=context['mime_type'],
            preprocessor_name=preprocessor_name,
        )
    finally:
        cleanup_path = context.get('_cleanup_path')
        if cleanup_path and os.path.exists(cleanup_path):
            os.unlink(cleanup_path)


def _build_document_converter_headers():
    headers = {}
    if settings.DOCUMENT_CONVERTER_AUTH_HEADER_NAME and settings.DOCUMENT_CONVERTER_AUTH_HEADER_VALUE:
        headers[settings.DOCUMENT_CONVERTER_AUTH_HEADER_NAME] = settings.DOCUMENT_CONVERTER_AUTH_HEADER_VALUE
    return headers


def convert_document_with_external_service(context):
    if not settings.DOCUMENT_CONVERTER_URL:
        raise DocumentPreprocessingError('External document converter URL is not configured.')

    request_data = {}
    metadata = context.get('metadata') or {}
    if metadata:
        request_data['metadata'] = json.dumps(metadata)

    try:
        with open(context['uploaded_file_path'], 'rb') as stream:
            response = requests.post(
                settings.DOCUMENT_CONVERTER_URL,
                data=request_data,
                files={
                    'file': (
                        context['original_filename'],
                        stream,
                        context['mime_type'] or 'application/octet-stream',
                    )
                },
                headers=_build_document_converter_headers(),
                timeout=settings.DOCUMENT_CONVERTER_TIMEOUT,
            )
    except requests.Timeout as exc:
        raise DocumentPreprocessingError('External document converter timed out.') from exc
    except requests.RequestException as exc:
        raise DocumentPreprocessingError('External document converter is unavailable.') from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DocumentPreprocessingError('External document converter returned invalid JSON.') from exc

    if not isinstance(payload, dict):
        raise DocumentPreprocessingError('External document converter returned a non-object JSON response.')

    if response.status_code >= 400:
        error_message = payload.get('error') or payload.get('detail') or 'External document conversion failed.'
        raise DocumentPreprocessingError(error_message)

    markdown = payload.get('markdown')
    if not isinstance(markdown, str) or not markdown.strip():
        raise DocumentPreprocessingError('External document converter returned empty markdown.')

    return payload


def default_document_preprocessor(context):
    filename = context['original_filename']
    extension = validate_document_extension(filename)
    uploaded_file_path = context['uploaded_file_path']

    if extension in ('.txt', '.md', '.markdown'):
        with open(uploaded_file_path, encoding='utf-8', newline='') as stream:
            body = stream.read()
        return _build_passthrough_task_payload(context, body)

    if extension == '.docx':
        raise DocumentPreprocessingError('DOCX preprocessing is not implemented by the active preprocessor.')

    if extension == '.pdf':
        raise DocumentPreprocessingError('PDF preprocessing is not implemented by the active preprocessor.')

    raise DocumentPreprocessingError(f'Unsupported file extension "{extension}".')


def external_document_service_preprocessor(context):
    filename = context['original_filename']
    extension = validate_document_extension(filename)
    uploaded_file_path = context['uploaded_file_path']

    if extension in ('.txt', '.md', '.markdown'):
        with open(uploaded_file_path, encoding='utf-8', newline='') as stream:
            body = stream.read()
        return PreprocessedDocumentResult(
            tasks_payload=_build_passthrough_task_payload(context, body),
            preprocessor_name='',
            artifact_draft=ArtifactDraft(
                provider='local_text',
                raw_markdown_text=body,
                annotation_markdown_text=body,
            ),
        )

    if extension not in ('.docx', '.pdf'):
        raise DocumentPreprocessingError(f'Unsupported file extension "{extension}".')

    payload = convert_document_with_external_service(context)
    service_document_id = payload.get('document_id')
    service_metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}

    return PreprocessedDocumentResult(
        tasks_payload=[
            {
                'data': {
                    'doc_id': service_document_id or str(uuid.uuid4()),
                    'source_filename': filename,
                    'source_mime_type': context['mime_type'],
                    'markdown': payload['markdown'],
                },
                'meta': {
                    'converter': payload.get('converter', 'docling'),
                    'converter_metadata': service_metadata,
                },
            }
        ],
        preprocessor_name='',
        artifact_draft=ArtifactDraft(
            provider='custom_preprocessor',
            raw_markdown_text=payload['markdown'],
            annotation_markdown_text=payload['markdown'],
            metadata={
                'converter': payload.get('converter', 'docling'),
                'converter_metadata': service_metadata,
                'source_document_id': service_document_id or '',
            },
        ),
    )


def _extract_azure_error_message(payload, default_message):
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            return error.get('message') or error.get('innererror', {}).get('message') or default_message
        return payload.get('message') or payload.get('detail') or default_message
    return default_message


def _build_azure_di_headers():
    if not settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT:
        raise DocumentPreprocessingError('Azure Document Intelligence endpoint is not configured.')
    if not settings.AZURE_DOCUMENT_INTELLIGENCE_KEY:
        raise DocumentPreprocessingError('Azure Document Intelligence key is not configured.')
    return {'Ocp-Apim-Subscription-Key': settings.AZURE_DOCUMENT_INTELLIGENCE_KEY}


def _build_azure_di_analyze_url():
    endpoint = settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT.rstrip('/')
    model_id = quote(settings.AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID, safe='')
    return (
        f'{endpoint}/documentintelligence/documentModels/{model_id}:analyze'
        f'?api-version={settings.AZURE_DOCUMENT_INTELLIGENCE_API_VERSION}'
        f'&outputContentFormat={settings.AZURE_DOCUMENT_INTELLIGENCE_OUTPUT_CONTENT_FORMAT}'
    )


def _poll_azure_result(operation_location):
    deadline = time.monotonic() + settings.AZURE_DOCUMENT_INTELLIGENCE_SYNC_TIMEOUT_SECONDS
    headers = _build_azure_di_headers()
    timeout = settings.AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS

    while True:
        if time.monotonic() >= deadline:
            raise DocumentPreprocessingError(
                'Azure Document Intelligence processing exceeded the synchronous upload timeout.'
            )

        try:
            response = requests.get(operation_location, headers=headers, timeout=timeout)
        except requests.Timeout as exc:
            raise DocumentPreprocessingError('Azure Document Intelligence polling timed out.') from exc
        except requests.RequestException as exc:
            raise DocumentPreprocessingError('Azure Document Intelligence is unavailable.') from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentPreprocessingError('Azure Document Intelligence returned invalid JSON.') from exc

        if response.status_code >= 400:
            raise DocumentPreprocessingError(
                _extract_azure_error_message(payload, 'Azure Document Intelligence conversion failed.')
            )

        status = (payload.get('status') or '').lower()
        if status == 'succeeded':
            payload.setdefault('_operation_location', operation_location)
            return payload
        if status in {'failed', 'canceled', 'cancelled'}:
            raise DocumentPreprocessingError(
                _extract_azure_error_message(payload, 'Azure Document Intelligence conversion failed.')
            )

        retry_after = response.headers.get('Retry-After')
        try:
            sleep_seconds = (
                float(retry_after) if retry_after else settings.AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS
            )
        except ValueError:
            sleep_seconds = settings.AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS

        time_remaining = deadline - time.monotonic()
        if time_remaining <= 0:
            raise DocumentPreprocessingError(
                'Azure Document Intelligence processing exceeded the synchronous upload timeout.'
            )
        time.sleep(max(0.0, min(sleep_seconds, time_remaining)))


def convert_document_with_azure_document_intelligence(context):
    analyze_url = _build_azure_di_analyze_url()
    headers = _build_azure_di_headers()
    headers['Content-Type'] = context['mime_type'] or 'application/octet-stream'
    timeout = settings.AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS

    try:
        with open(context['uploaded_file_path'], 'rb') as stream:
            response = requests.post(
                analyze_url,
                headers=headers,
                data=stream,
                timeout=timeout,
            )
    except requests.Timeout as exc:
        raise DocumentPreprocessingError('Azure Document Intelligence request timed out.') from exc
    except requests.RequestException as exc:
        raise DocumentPreprocessingError('Azure Document Intelligence is unavailable.') from exc

    if response.status_code not in (200, 202):
        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentPreprocessingError('Azure Document Intelligence returned invalid JSON.') from exc
        raise DocumentPreprocessingError(
            _extract_azure_error_message(payload, 'Azure Document Intelligence conversion failed.')
        )

    if response.status_code == 200:
        try:
            return response.json()
        except ValueError as exc:
            raise DocumentPreprocessingError('Azure Document Intelligence returned invalid JSON.') from exc

    operation_location = response.headers.get('Operation-Location')
    if not operation_location:
        raise DocumentPreprocessingError('Azure Document Intelligence did not return an operation location.')

    return _poll_azure_result(operation_location)


def azure_document_intelligence_preprocessor(context):
    filename = context['original_filename']
    extension = validate_document_extension(filename)
    uploaded_file_path = context['uploaded_file_path']

    if extension in ('.txt', '.md', '.markdown'):
        with open(uploaded_file_path, encoding='utf-8', newline='') as stream:
            body = stream.read()
        return PreprocessedDocumentResult(
            tasks_payload=_build_passthrough_task_payload(context, body),
            preprocessor_name='',
            artifact_draft=ArtifactDraft(
                provider='local_text',
                raw_markdown_text=body,
                annotation_markdown_text=body,
            ),
        )

    if extension not in ('.docx', '.pdf'):
        raise DocumentPreprocessingError(f'Unsupported file extension "{extension}".')

    payload = convert_document_with_azure_document_intelligence(context)
    analyze_result = payload.get('analyzeResult') if isinstance(payload.get('analyzeResult'), dict) else {}
    markdown = analyze_result.get('content')
    if not isinstance(markdown, str) or not markdown.strip():
        raise DocumentPreprocessingError('Azure Document Intelligence returned empty markdown.')

    operation_id = payload.get('id') if isinstance(payload.get('id'), str) else ''
    if not operation_id:
        operation_location = payload.get('_operation_location')
        if isinstance(operation_location, str):
            parsed_path = urlparse(operation_location).path.rstrip('/')
            if parsed_path:
                operation_id = parsed_path.split('/')[-1]

    raw_result_bytes = gzip.compress(json.dumps(payload).encode('utf-8'))

    return PreprocessedDocumentResult(
        tasks_payload=[
            {
                'data': {
                    'doc_id': operation_id or str(uuid.uuid4()),
                    'source_filename': filename,
                    'source_mime_type': context['mime_type'],
                    'markdown': markdown,
                },
                'meta': {
                    'converter': 'azure_document_intelligence',
                    'converter_metadata': {
                        'model_id': settings.AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID,
                        'api_version': settings.AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
                        'operation_id': operation_id,
                    },
                },
            }
        ],
        preprocessor_name='',
        artifact_draft=ArtifactDraft(
            provider='azure_document_intelligence',
            raw_markdown_text=markdown,
            annotation_markdown_text=markdown,
            provider_model_id=settings.AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID,
            provider_api_version=settings.AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
            provider_operation_id=operation_id,
            raw_result_bytes=raw_result_bytes,
            raw_result_filename=f'{os.path.splitext(os.path.basename(filename))[0] or "document"}-azure-di.json.gz',
            metadata={
                'status': payload.get('status', ''),
                'content_format': settings.AZURE_DOCUMENT_INTELLIGENCE_OUTPUT_CONTENT_FORMAT,
            },
        ),
    )


def persist_document_conversion_artifact(*, file_upload, artifact_draft):
    from data_import.models import DocumentConversionArtifact

    artifact = DocumentConversionArtifact(
        file_upload=file_upload,
        provider=artifact_draft.provider,
        content_format=artifact_draft.content_format,
        provider_model_id=artifact_draft.provider_model_id,
        provider_api_version=artifact_draft.provider_api_version,
        provider_operation_id=artifact_draft.provider_operation_id,
        metadata=artifact_draft.metadata,
    )
    artifact.save()

    try:
        if artifact_draft.raw_result_bytes is not None:
            artifact.raw_result_file.save(
                artifact_draft.raw_result_filename or 'raw-result.json.gz',
                ContentFile(artifact_draft.raw_result_bytes),
                save=False,
            )
        if artifact_draft.raw_markdown_text:
            artifact.raw_markdown_file.save(
                'raw-markdown.md',
                ContentFile(artifact_draft.raw_markdown_text.encode('utf-8')),
                save=False,
            )
        if artifact_draft.annotation_markdown_text:
            artifact.annotation_markdown_file.save(
                'annotation-markdown.md',
                ContentFile(artifact_draft.annotation_markdown_text.encode('utf-8')),
                save=False,
            )

        artifact.save()
    except Exception:
        cleanup_persisted_document_artifact_files(artifact)
        raise
    return artifact


def cleanup_persisted_document_artifact_files(artifact):
    if not artifact:
        return
    for field_name in ('raw_result_file', 'raw_markdown_file', 'annotation_markdown_file'):
        field = getattr(artifact, field_name, None)
        if field:
            try:
                field.delete(save=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'Failed to clean up artifact file after rollback for artifact=%s field=%s: %s',
                    artifact.pk,
                    field_name,
                    exc,
                )
