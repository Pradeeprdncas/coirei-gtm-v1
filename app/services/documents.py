"""Text-only document parser; intentionally no OCR in this MVP."""
from io import BytesIO


def extract_document(filename: str, content: bytes) -> str:
    suffix = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if suffix in ('txt','md'):
        return content.decode('utf-8', errors='replace')[:120_000]
    if suffix == 'pdf':
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(content))
        if len(pdf.pages) > 100:
            raise ValueError('PDF exceeds 100 pages')
        return '\n'.join(p.extract_text() or '' for p in pdf.pages)[:120_000]
    if suffix == 'docx':
        from docx import Document
        doc = Document(BytesIO(content))
        return '\n'.join(p.text for p in doc.paragraphs)[:120_000]
    raise ValueError('Only PDF, DOCX, TXT and MD files are supported')
