from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

BLANK_LINE_RUN_PATTERN = re.compile(r'\n{4,}')
PDF_PAGE_COUNTER_PATTERN = re.compile(r'(?im)^(?:page\s+)?\d+\s+of\s+\d+\s*$')


class UnsupportedDocumentError(Exception):
    pass


class DocumentConverterConfigurationError(Exception):
    pass


def _build_docling_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=PdfPipelineOptions(do_ocr=False))},
    )


@dataclass
class ConvertedDocument:
    document_id: str
    original_filename: str
    canonical_markdown: str
    source_format: str


@dataclass
class LocalDocumentConverter:
    docling_converter: Any = None
    conversion_lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if self.docling_converter is None:
            try:
                self.docling_converter = _build_docling_converter()
            except Exception as exc:
                raise DocumentConverterConfigurationError('Document converter could not be initialized.') from exc

    def convert_uploaded_file(self, file_path: Path, original_filename: str) -> ConvertedDocument:
        canonical_markdown = self.convert(file_path)
        return ConvertedDocument(
            document_id=uuid4().hex,
            original_filename=original_filename,
            canonical_markdown=canonical_markdown,
            source_format=file_path.suffix.lower().lstrip('.'),
        )

    def convert(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == '.txt':
            raw_text = self._read_txt(path)
            canonical_markdown = self.normalize_text_upload(raw_text)
        elif suffix == '.docx':
            raw_text = self._read_docx(path)
            canonical_markdown = self.normalize_docling_markdown(raw_text)
        elif suffix == '.pdf':
            raw_text = self._read_pdf(path)
            canonical_markdown = self.normalize_docling_markdown(raw_text)
            self._validate_pdf_text_quality(canonical_markdown)
        else:
            raise UnsupportedDocumentError(f'Unsupported file type: {suffix or "unknown"}.')

        if not canonical_markdown.strip():
            raise UnsupportedDocumentError('Document conversion yielded no extractable text.')
        return canonical_markdown

    @staticmethod
    def _normalize_common(text: str) -> str:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = normalized.strip('\n')
        normalized = BLANK_LINE_RUN_PATTERN.sub('\n\n\n', normalized)
        return normalized

    @classmethod
    def normalize_text_upload(cls, text: str) -> str:
        return cls._normalize_common(text)

    @classmethod
    def normalize_docling_markdown(cls, text: str) -> str:
        return text

    @staticmethod
    def _read_txt(path: Path) -> str:
        try:
            return path.read_text(encoding='utf-8')
        except UnicodeDecodeError as exc:
            raise UnsupportedDocumentError('TXT upload must be valid UTF-8 text.') from exc

    def _read_docx(self, path: Path) -> str:
        if not zipfile.is_zipfile(path):
            raise UnsupportedDocumentError('Password-protected or corrupted DOCX uploads are not supported in v1.')
        return self._convert_with_docling(
            path,
            'Password-protected or corrupted DOCX uploads are not supported in v1.',
        )

    def _read_pdf(self, path: Path) -> str:
        return self._convert_with_docling(path, 'PDF upload is corrupted or unreadable.')

    def _convert_with_docling(self, path: Path, unreadable_message: str) -> str:
        converter = self.docling_converter
        if converter is None:
            raise DocumentConverterConfigurationError('Document converter is not configured.')

        try:
            with self.conversion_lock:
                result = converter.convert(path)
                markdown = str(result.document.export_to_markdown())
        except Exception as exc:  # noqa: BLE001
            lower_message = str(exc).lower()
            if isinstance(exc, ImportError) or 'cannot open shared object file' in lower_message:
                raise DocumentConverterConfigurationError(
                    'Document converter service is missing native runtime dependencies.'
                ) from exc
            if 'password' in lower_message or 'encrypted' in lower_message:
                if path.suffix.lower() == '.pdf':
                    raise UnsupportedDocumentError('Password-protected PDFs are not supported in v1.') from exc
                raise UnsupportedDocumentError(
                    'Password-protected or corrupted DOCX uploads are not supported in v1.'
                ) from exc
            raise UnsupportedDocumentError(unreadable_message) from exc

        if not markdown.strip():
            if path.suffix.lower() == '.pdf':
                raise UnsupportedDocumentError('Scanned PDFs without extractable text are not supported in v1.')
            raise UnsupportedDocumentError('DOCX upload yielded no extractable text.')
        return markdown

    @staticmethod
    def _validate_pdf_text_quality(markdown: str) -> None:
        text = re.sub(r'<!--.*?-->', '', markdown, flags=re.DOTALL)
        text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
        if not any(
            any(character.isalnum() for character in line)
            for line in text.splitlines()
            if not PDF_PAGE_COUNTER_PATTERN.fullmatch(line.strip())
        ):
            raise UnsupportedDocumentError(
                'Scanned PDFs or PDFs without substantive extractable text are not supported in v1.'
            )


_converter_initialization_lock = Lock()


@lru_cache(maxsize=1)
def _create_document_converter() -> LocalDocumentConverter:
    return LocalDocumentConverter()


def get_shared_document_converter() -> LocalDocumentConverter:
    with _converter_initialization_lock:
        return _create_document_converter()
