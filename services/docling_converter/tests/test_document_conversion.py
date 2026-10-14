import zipfile
from pathlib import Path

from converter_api.document_conversion import LocalDocumentConverter, UnsupportedDocumentError


class FakeDoclingResultDocument:
    def __init__(self, markdown):
        self._markdown = markdown

    def export_to_markdown(self):
        return self._markdown


class FakeDoclingResult:
    def __init__(self, markdown):
        self.document = FakeDoclingResultDocument(markdown)


class FakeDoclingConverter:
    def __init__(self, markdown=None, error=None):
        self.markdown = markdown
        self.error = error

    def convert(self, _path):
        if self.error:
            raise self.error
        return FakeDoclingResult(self.markdown)


def test_docx_markdown_is_preserved(tmp_path: Path):
    source = tmp_path / 'sample.docx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(
            '[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        )

    converter = LocalDocumentConverter(
        docling_converter=FakeDoclingConverter(
            markdown='\n---\ntitle: test\n---\nHello\n\n\n\nWorld\n<!-- image -->\n'
        )
    )

    assert converter.convert(source) == '\n---\ntitle: test\n---\nHello\n\n\n\nWorld\n<!-- image -->\n'


def test_low_signal_pdf_is_rejected(tmp_path: Path):
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.4')

    converter = LocalDocumentConverter(docling_converter=FakeDoclingConverter(markdown='Page 1 of 2\nPage 2 of 2\n'))

    try:
        converter.convert(source)
    except UnsupportedDocumentError as exc:
        assert 'Scanned PDFs' in str(exc)
    else:
        raise AssertionError('Expected UnsupportedDocumentError for low-signal PDF markdown')


def test_docling_markdown_tables_are_preserved(tmp_path: Path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    markdown = '| Name | Email |\n| --- | --- |\n| Sarah | sarah@example.com |\n| Omar | omar@example.com |'

    converter = LocalDocumentConverter(docling_converter=FakeDoclingConverter(markdown=markdown))

    assert converter.convert(source) == markdown


def test_docling_hard_breaks_are_preserved(tmp_path: Path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    markdown = 'Hello  \nWorld  \n'

    converter = LocalDocumentConverter(docling_converter=FakeDoclingConverter(markdown=markdown))

    assert converter.convert(source) == markdown


def test_pdf_with_short_non_latin_text_is_accepted(tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4')
    converter = LocalDocumentConverter(docling_converter=FakeDoclingConverter(markdown='你好世界'))
    assert converter.convert(source) == '你好世界'


def test_initialization_errors_are_reported_as_configuration_errors(monkeypatch):
    import pytest
    from converter_api import document_conversion

    def fail_initialization():
        raise ImportError('Missing native dependency')

    monkeypatch.setattr(document_conversion, '_build_docling_converter', fail_initialization)
    with pytest.raises(document_conversion.DocumentConverterConfigurationError, match='could not be initialized'):
        LocalDocumentConverter()


def test_image_only_pdf_is_rejected(tmp_path):
    import pytest

    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.4')
    converter = LocalDocumentConverter(docling_converter=FakeDoclingConverter(markdown='<!-- image -->'))
    with pytest.raises(UnsupportedDocumentError, match='Scanned PDFs'):
        converter.convert(source)


def test_shared_converter_initializes_once_for_concurrent_requests(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from converter_api import document_conversion

    started = Event()
    finish = Event()
    instances = []

    def build():
        instance = object()
        instances.append(instance)
        started.set()
        assert finish.wait(timeout=5)
        return instance

    document_conversion._create_document_converter.cache_clear()
    monkeypatch.setattr(document_conversion, 'LocalDocumentConverter', build)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(document_conversion.get_shared_document_converter)
            assert started.wait(timeout=5)
            second = pool.submit(document_conversion.get_shared_document_converter)
            finish.set()
            assert first.result(timeout=5) is second.result(timeout=5)
        assert len(instances) == 1
    finally:
        finish.set()
        document_conversion._create_document_converter.cache_clear()
