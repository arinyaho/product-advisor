"""Bounded text extraction from untrusted downloaded documents."""

import io
import json
import sys
import zipfile


class Omitted(ValueError):
    pass


def extract(data, mime):
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return data.decode("utf-8-sig"), "UTF-8 text"
    stream = io.BytesIO(data)
    if "openxmlformats" in mime:
        with zipfile.ZipFile(stream) as archive:
            if sum(v.file_size for v in archive.infolist()) > 200_000_000:
                raise Omitted("expanded_file_size_limit")
    if mime == "application/pdf":
        from pypdf import PdfReader
        pdf = PdfReader(stream)
        if pdf.is_encrypted:
            raise Omitted("encrypted_pdf")
        return "\n\n".join(page.extract_text() or "" for page in pdf.pages), "PDF text layer; no OCR, image or layout interpretation"
    if mime.endswith("wordprocessingml.document"):
        from docx import Document
        from docx.table import Table
        document = Document(stream)
        parts = []
        for item in document.iter_inner_content():
            parts.append("\n".join("\t".join(cell.text for cell in row.cells) for row in item.rows) if isinstance(item, Table) else item.text)
        return "\n\n".join(parts), "Document body paragraphs and tables; headers, footers, drawings and tracked-change interpretation excluded"
    if mime.endswith("spreadsheetml.sheet"):
        from openpyxl import load_workbook
        book = load_workbook(stream, read_only=True, data_only=False)
        parts, cells = [], 0
        try:
            for sheet in book:
                parts.append(sheet.title)
                for row in sheet.iter_rows(values_only=True):
                    cells += len(row)
                    if cells > 2_000_000:
                        raise Omitted("spreadsheet_cell_limit")
                    parts.append("\t".join("" if v is None else str(v) for v in row))
        finally:
            book.close()
        return "\n".join(parts), "All worksheets; values and formula expressions, without formula evaluation, charts or images"
    if mime.endswith("presentationml.presentation"):
        from pptx import Presentation
        def shapes_text(shapes):
            parts = []
            for shape in shapes:
                if hasattr(shape, "shapes"):
                    parts.extend(shapes_text(shape.shapes))
                if shape.has_text_frame:
                    parts.append(shape.text)
                if shape.has_table:
                    parts.extend("\t".join(cell.text for cell in row.cells) for row in shape.table.rows)
            return parts
        parts = []
        for slide in Presentation(stream).slides:
            parts.extend(shapes_text(slide.shapes))
            if slide.has_notes_slide:
                parts.append(slide.notes_slide.notes_text_frame.text)
        return "\n\n".join(parts), "Slide text, tables and speaker notes; images and charts excluded"
    raise Omitted("unsupported_mime_type")


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    try:
        data = sys.stdin.buffer.read(50_000_001)
        if len(data) > 50_000_000: raise Omitted("download_size_limit")
        text, coverage = extract(data, sys.argv[1])
        if len(text) > 2_000_000: raise Omitted("text_size_limit")
        print(json.dumps({"text": text, "coverage": coverage}))
    except Exception as error:
        print(json.dumps({"omission": str(error) if isinstance(error, Omitted) else "document_parse_" + type(error).__name__}))


if __name__ == "__main__":
    main()
