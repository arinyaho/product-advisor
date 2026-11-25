"""Exercise real Office readers and the isolated extraction boundary."""

import io
import json
import subprocess
import sys

import pytest

from company_context.extract_document import extract, Omitted


def test_word_and_all_sheet_content_preserves_original_values():
    from docx import Document
    from openpyxl import Workbook
    doc = Document(); doc.add_paragraph("Original paragraph")
    table = doc.add_table(rows=1, cols=2); table.cell(0, 0).text = "Buyer"; table.cell(0, 1).text = "Pilot"
    stream = io.BytesIO(); doc.save(stream)
    text, _ = extract(stream.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert text == "Original paragraph\n\nBuyer\tPilot"
    book = Workbook(); book.active.title = "First"; book.active.append(["Price", "=1+2"])
    book.create_sheet("Second").append(["Acceptance", 7])
    stream = io.BytesIO(); book.save(stream); book.close()
    text, _ = extract(stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert text == "First\nPrice\t=1+2\nSecond\nAcceptance\t7"


def test_slides_include_speaker_notes_and_encrypted_pdf_is_omitted():
    from pptx import Presentation
    from pypdf import PdfWriter
    deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[0]); slide.shapes.title.text = "Proposal"
    slide.notes_slide.notes_text_frame.text = "Customer-facing notes"
    stream = io.BytesIO(); deck.save(stream)
    text, _ = extract(stream.getvalue(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert "Proposal" in text and "Customer-facing notes" in text
    pdf = PdfWriter(); pdf.add_blank_page(width=100, height=100); pdf.encrypt("synthetic-test-password")
    stream = io.BytesIO(); pdf.write(stream)
    with pytest.raises(Omitted, match="encrypted_pdf"): extract(stream.getvalue(), "application/pdf")


def test_isolated_parser_reports_unsupported_encoding_without_a_traceback():
    result = subprocess.run([sys.executable, "-m", "company_context.extract_document", "text/plain"], input=b"\xff", capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"omission": "document_parse_UnicodeDecodeError"}
    assert result.stderr == b""
