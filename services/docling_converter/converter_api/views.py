import json
import mimetypes
import secrets
import tempfile
from pathlib import Path

from django.conf import settings
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_conversion import (
    DocumentConverterConfigurationError,
    UnsupportedDocumentError,
    get_shared_document_converter,
)


class ConvertDocumentView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    authentication_classes = []
    permission_classes = []

    converter_factory = staticmethod(get_shared_document_converter)

    def post(self, request):
        expected_auth_value = settings.DOCLING_CONVERTER_AUTH_HEADER_VALUE
        if not expected_auth_value and settings.DOCLING_CONVERTER_API_TOKEN:
            expected_auth_value = f'Token {settings.DOCLING_CONVERTER_API_TOKEN}'

        if expected_auth_value:
            header_name = settings.DOCLING_CONVERTER_AUTH_HEADER_NAME or 'Authorization'
            if not secrets.compare_digest(request.headers.get(header_name, '').encode(), expected_auth_value.encode()):
                return Response({'detail': 'Unauthorized.'}, status=status.HTTP_401_UNAUTHORIZED)

        uploaded_file = request.FILES.get('file')
        if uploaded_file is None:
            return Response({'detail': 'A file upload is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if uploaded_file.size > settings.DOCLING_CONVERTER_MAX_FILE_SIZE:
            return Response(
                {'detail': 'Uploaded file exceeds the size limit.'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        if Path(uploaded_file.name).suffix.lower() not in {'.pdf', '.docx', '.txt'}:
            return Response({'error': 'Unsupported file type.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        metadata_value = request.data.get('metadata')
        metadata = {}
        if metadata_value:
            try:
                metadata = json.loads(metadata_value)
            except ValueError:
                return Response({'detail': 'metadata must be valid JSON.'}, status=status.HTTP_400_BAD_REQUEST)
            if not isinstance(metadata, dict):
                return Response({'detail': 'metadata must be a JSON object.'}, status=status.HTTP_400_BAD_REQUEST)

        suffix = Path(uploaded_file.name).suffix
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / f'document{suffix}'
            with temp_path.open('wb') as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)

            try:
                converted = self.converter_factory().convert_uploaded_file(temp_path, uploaded_file.name)
            except UnsupportedDocumentError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            except DocumentConverterConfigurationError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        mime_type = (
            uploaded_file.content_type or mimetypes.guess_type(uploaded_file.name)[0] or 'application/octet-stream'
        )
        response_payload = {
            'document_id': converted.document_id,
            'filename': converted.original_filename,
            'mime_type': mime_type,
            'markdown': converted.canonical_markdown,
            'converter': 'docling',
            'metadata': {
                'source_format': converted.source_format,
                'upload_metadata': metadata,
            },
        }
        return Response(response_payload, status=status.HTTP_200_OK)
