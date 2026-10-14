import json
import os
from unittest.mock import Mock

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'docling_converter_service.settings')

import django
from converter_api.document_conversion import DocumentConverterConfigurationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

django.setup()


@override_settings(DOCLING_CONVERTER_API_TOKEN='secret-token')
def test_convert_endpoint_returns_normalized_payload(monkeypatch):
    from converter_api.views import ConvertDocumentView

    converter = Mock()
    converter.convert_uploaded_file.return_value = Mock(
        document_id='doc-123',
        original_filename='sample.pdf',
        canonical_markdown='# Converted\n\nHello world',
        source_format='pdf',
    )
    monkeypatch.setattr(ConvertDocumentView, 'converter_factory', staticmethod(lambda: converter))

    client = APIClient()
    response = client.post(
        '/api/convert',
        {
            'file': SimpleUploadedFile('sample.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
            'metadata': json.dumps({'source': 'test-suite'}),
        },
        format='multipart',
        HTTP_AUTHORIZATION='Token secret-token',
    )

    assert response.status_code == 200
    assert response.json() == {
        'document_id': 'doc-123',
        'filename': 'sample.pdf',
        'mime_type': 'application/pdf',
        'markdown': '# Converted\n\nHello world',
        'converter': 'docling',
        'metadata': {
            'source_format': 'pdf',
            'upload_metadata': {'source': 'test-suite'},
        },
    }


@override_settings(DOCLING_CONVERTER_API_TOKEN='secret-token')
def test_convert_endpoint_requires_token():
    client = APIClient()
    response = client.post(
        '/api/convert',
        {
            'file': SimpleUploadedFile('sample.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
        },
        format='multipart',
    )

    assert response.status_code == 401
    assert response.json() == {'detail': 'Unauthorized.'}


@override_settings(
    DOCLING_CONVERTER_AUTH_HEADER_NAME='X-Docling-Auth',
    DOCLING_CONVERTER_AUTH_HEADER_VALUE='custom-secret',
)
def test_convert_endpoint_supports_custom_auth_header(monkeypatch):
    from converter_api.views import ConvertDocumentView

    converter = Mock()
    converter.convert_uploaded_file.return_value = Mock(
        document_id='doc-456',
        original_filename='sample.docx',
        canonical_markdown='Converted',
        source_format='docx',
    )
    monkeypatch.setattr(ConvertDocumentView, 'converter_factory', staticmethod(lambda: converter))

    client = APIClient()
    response = client.post(
        '/api/convert',
        {
            'file': SimpleUploadedFile(
                'sample.docx',
                b'PK\x03\x04fake',
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            ),
        },
        format='multipart',
        HTTP_X_DOCLING_AUTH='custom-secret',
    )

    assert response.status_code == 200
    assert response.json()['document_id'] == 'doc-456'


@override_settings(DOCLING_CONVERTER_API_TOKEN='secret-token')
def test_convert_endpoint_returns_500_for_converter_configuration_errors(monkeypatch):
    from converter_api.views import ConvertDocumentView

    converter = Mock()
    converter.convert_uploaded_file.side_effect = DocumentConverterConfigurationError(
        'Document converter service is missing native runtime dependencies.'
    )
    monkeypatch.setattr(ConvertDocumentView, 'converter_factory', staticmethod(lambda: converter))

    client = APIClient()
    response = client.post(
        '/api/convert',
        {
            'file': SimpleUploadedFile('sample.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
        },
        format='multipart',
        HTTP_AUTHORIZATION='Token secret-token',
    )

    assert response.status_code == 500
    assert response.json() == {'error': 'Document converter service is missing native runtime dependencies.'}


@override_settings(DOCLING_CONVERTER_MAX_FILE_SIZE=4)
def test_convert_endpoint_rejects_oversized_upload_before_conversion(monkeypatch):
    from converter_api.views import ConvertDocumentView

    factory = Mock()
    monkeypatch.setattr(ConvertDocumentView, 'converter_factory', staticmethod(factory))
    response = APIClient().post(
        '/api/convert', {'file': SimpleUploadedFile('sample.pdf', b'12345')}, format='multipart'
    )
    assert response.status_code == 413
    factory.assert_not_called()


def test_convert_endpoint_rejects_metadata_that_is_not_an_object():
    response = APIClient().post(
        '/api/convert',
        {'file': SimpleUploadedFile('sample.pdf', b'12345'), 'metadata': '["bad"]'},
        format='multipart',
    )
    assert response.status_code == 400


def test_convert_endpoint_cleans_temporary_files_on_failure(monkeypatch):
    from converter_api.document_conversion import UnsupportedDocumentError
    from converter_api.views import ConvertDocumentView

    paths = []

    def fail_conversion(path, _filename):
        assert path.exists()
        paths.append(path)
        raise UnsupportedDocumentError('Unreadable document.')

    converter = Mock()
    converter.convert_uploaded_file.side_effect = fail_conversion
    monkeypatch.setattr(ConvertDocumentView, 'converter_factory', staticmethod(lambda: converter))
    response = APIClient().post(
        '/api/convert', {'file': SimpleUploadedFile('sample.pdf', b'12345')}, format='multipart'
    )
    assert response.status_code == 422
    assert len(paths) == 1
    assert not paths[0].exists()


@override_settings(DOCLING_CONVERTER_API_TOKEN='secret-token')
def test_convert_endpoint_rejects_non_ascii_auth_header_without_server_error():
    response = APIClient().post(
        '/api/convert',
        {'file': SimpleUploadedFile('sample.pdf', b'12345')},
        format='multipart',
        HTTP_AUTHORIZATION='Token 日本語',
    )
    assert response.status_code == 401
