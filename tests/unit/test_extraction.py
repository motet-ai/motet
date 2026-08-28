"""
Motet - Text Extraction Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
"""

import pytest
from unittest.mock import MagicMock, patch
from motet.core.media.text_extraction import extract_text_from_bytes

def test_extract_plain_text():
    content = b"Hello world"
    text = extract_text_from_bytes(content, "text/plain")
    assert text == "Hello world"

def test_extract_pdf():
    # Mock pdfplumber; implementation wraps each page with "--- Page N (Text Layer) ---"
    with patch("pdfplumber.open") as mock_open:
        mock_pdf = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "PDF content"
        mock_pdf.pages = [mock_page]
        mock_open.return_value.__enter__.return_value = mock_pdf

        text = extract_text_from_bytes(b"fake pdf", "application/pdf")
        assert "PDF content" in text
        assert "Page 1" in text or text.strip() == "PDF content"

def test_extract_docx():
    # Mock docx
    with patch("docx.Document") as mock_doc_cls:
        mock_doc = MagicMock()
        mock_para = MagicMock()
        mock_para.text = "Docx content"
        mock_doc.paragraphs = [mock_para]
        mock_doc_cls.return_value = mock_doc
        
        text = extract_text_from_bytes(b"fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert text == "Docx content"

def test_unsupported_type():
    # Should handle gracefully (return empty string or similar, but code raises error currently? No, returns "")
    # Check implementation: returns "" and logs warning
    text = extract_text_from_bytes(b"data", "application/unknown")
    assert text == ""


