from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from trafilatura import bare_extraction

from editorial_core.acquisition import SanitizedSource, sanitize_hostile_html


MAX_NORMALIZED_CHARACTERS = 2_000_000
MAX_PDF_PAGES = 200
MAX_STRUCTURED_DEPTH = 40
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 1_000
MAX_CELL_CHARACTERS = 100_000


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    injection_markers: tuple[str, ...]
    removed_active_elements: int
    extractor: str
    metadata: dict[str, Any]


def _decode_text(body: bytes) -> str:
    # UTF-8 is the storage contract. Replacement is deterministic and retains evidence
    # offsets even when a publisher serves malformed byte sequences.
    return body.decode("utf-8", errors="replace")


def _normalize_untrusted_text(text: str) -> SanitizedSource:
    # Escaping before the hostile-HTML parser guarantees source text can never become
    # active markup while reusing the same prompt-injection marker detector.
    import html

    return sanitize_hostile_html(
        f"<article><p>{html.escape(text)}</p></article>",
        maximum_characters=MAX_NORMALIZED_CHARACTERS + 100,
    )


def _bounded(text: str) -> str:
    if len(text) > MAX_NORMALIZED_CHARACTERS:
        raise ValueError("normalized source exceeds the configured character limit")
    return text


def _metadata_value(document: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(document, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()[:1_000]
    return None


def _extract_html(body: bytes, final_url: str) -> ExtractedDocument:
    decoded = _decode_text(body)
    hostile_scan = sanitize_hostile_html(decoded)
    document = bare_extraction(
        decoded,
        url=final_url,
        output_format="python",
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        include_links=False,
        include_images=False,
        deduplicate=False,
    )
    extracted = _metadata_value(document, "text") if document is not None else None
    # Very short pages and unusual layouts may not satisfy Trafilatura's article
    # heuristic. The already-sanitized visible text is a safe, deterministic fallback.
    text = extracted if extracted and len(extracted) >= 40 else hostile_scan.text
    isolated = _normalize_untrusted_text(_bounded(text))
    markers = tuple(dict.fromkeys((*hostile_scan.injection_markers, *isolated.injection_markers)))
    metadata = {
        key: value
        for key, value in {
            "title": _metadata_value(document, "title") if document else None,
            "author": _metadata_value(document, "author") if document else None,
            "publisher": _metadata_value(document, "sitename", "hostname") if document else None,
            "publication_date": _metadata_value(document, "date") if document else None,
        }.items()
        if value
    }
    return ExtractedDocument(
        text=isolated.text,
        injection_markers=markers,
        removed_active_elements=hostile_scan.removed_active_elements,
        extractor="trafilatura-2.1.0",
        metadata=metadata,
    )


def _extract_pdf(body: bytes) -> ExtractedDocument:
    if not body.startswith(b"%PDF-"):
        raise ValueError("source declared PDF but does not have a PDF header")
    try:
        reader = PdfReader(io.BytesIO(body), strict=True)
    except (PdfReadError, ValueError, TypeError) as exc:
        raise ValueError("source PDF could not be parsed") from exc
    if reader.is_encrypted:
        raise ValueError("encrypted PDFs are not accepted")
    page_count = len(reader.pages)
    if page_count > MAX_PDF_PAGES:
        raise ValueError(f"source PDF exceeds the {MAX_PDF_PAGES}-page limit")
    pages: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text(extraction_mode="plain") or ""
        except Exception as exc:  # pypdf exposes several parser-specific exception types
            raise ValueError(f"source PDF page {index} could not be extracted") from exc
        page_text = page_text.replace("\x00", "")
        total += len(page_text)
        if total > MAX_NORMALIZED_CHARACTERS:
            raise ValueError("normalized source exceeds the configured character limit")
        pages.append(f"[Page {index}]\n{page_text.strip()}")
    isolated = _normalize_untrusted_text("\n\n".join(pages).strip())
    raw_metadata = reader.metadata or {}
    metadata = {
        key: value
        for key, value in {
            "title": str(raw_metadata.get("/Title", "")).strip()[:1_000] or None,
            "author": str(raw_metadata.get("/Author", "")).strip()[:1_000] or None,
            "publisher": str(raw_metadata.get("/Creator", "")).strip()[:1_000] or None,
        }.items()
        if value
    }
    metadata["page_count"] = page_count
    return ExtractedDocument(
        text=isolated.text,
        injection_markers=isolated.injection_markers,
        removed_active_elements=0,
        extractor="pypdf-6.14.2",
        metadata=metadata,
    )


def _validate_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_STRUCTURED_DEPTH:
        raise ValueError("structured source exceeds the nesting-depth limit")
    if isinstance(value, dict):
        for item in value.values():
            _validate_json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_depth(item, depth + 1)


def _extract_json(body: bytes) -> ExtractedDocument:
    try:
        value = json.loads(_decode_text(body))
    except json.JSONDecodeError as exc:
        raise ValueError("source declared JSON but could not be parsed") from exc
    _validate_json_depth(value)
    text = _bounded(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    isolated = _normalize_untrusted_text(text)
    return ExtractedDocument(
        isolated.text,
        isolated.injection_markers,
        0,
        "stdlib-json-canonical-v1",
        {"root_type": type(value).__name__},
    )


def _extract_xml(body: bytes) -> ExtractedDocument:
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ValueError("source declared XML but could not be parsed safely") from exc

    parts: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > MAX_STRUCTURED_DEPTH:
            raise ValueError("structured source exceeds the nesting-depth limit")
        tag = str(node.tag).rsplit("}", 1)[-1]
        values = [str(value).strip() for value in node.attrib.values() if str(value).strip()]
        if node.text and node.text.strip():
            values.append(node.text.strip())
        if values:
            parts.append(f"{tag}: {' | '.join(values)}")
        for child in node:
            visit(child, depth + 1)
        if node.tail and node.tail.strip():
            parts.append(node.tail.strip())

    visit(root)
    isolated = _normalize_untrusted_text(_bounded("\n".join(parts)))
    return ExtractedDocument(
        isolated.text,
        isolated.injection_markers,
        0,
        "defusedxml-0.7.1",
        {"root_element": str(root.tag).rsplit("}", 1)[-1]},
    )


def _extract_csv(body: bytes) -> ExtractedDocument:
    stream = io.StringIO(_decode_text(body), newline="")
    reader = csv.reader(stream)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    row_count = 0
    for row_count, row in enumerate(reader, start=1):
        if row_count > MAX_CSV_ROWS:
            raise ValueError("source CSV exceeds the row limit")
        if len(row) > MAX_CSV_COLUMNS:
            raise ValueError("source CSV exceeds the column limit")
        if any(len(cell) > MAX_CELL_CHARACTERS for cell in row):
            raise ValueError("source CSV contains an oversized cell")
        writer.writerow(row)
        if output.tell() > MAX_NORMALIZED_CHARACTERS:
            raise ValueError("normalized source exceeds the configured character limit")
    isolated = _normalize_untrusted_text(output.getvalue().strip())
    return ExtractedDocument(
        isolated.text,
        isolated.injection_markers,
        0,
        "stdlib-csv-v1",
        {"row_count": row_count},
    )


def _extract_plain_text(body: bytes) -> ExtractedDocument:
    decoded = _bounded(_decode_text(body).replace("\x00", ""))
    isolated = _normalize_untrusted_text(decoded)
    return ExtractedDocument(
        isolated.text,
        isolated.injection_markers,
        0,
        "plain-text-isolation-v1",
        {},
    )


def extract_source_document(body: bytes, content_type: str, final_url: str) -> ExtractedDocument:
    if content_type in {"text/html", "application/xhtml+xml"}:
        return _extract_html(body, final_url)
    if content_type == "application/pdf":
        return _extract_pdf(body)
    if content_type in {"application/json", "application/ld+json"}:
        return _extract_json(body)
    if content_type in {
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
        "text/xml",
    }:
        return _extract_xml(body)
    if content_type in {"text/csv", "application/csv"}:
        return _extract_csv(body)
    if content_type == "text/plain":
        return _extract_plain_text(body)
    raise ValueError(f"no extractor is configured for source MIME type: {content_type}")
