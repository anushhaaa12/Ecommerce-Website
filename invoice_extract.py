"""
invoice_extract.py
-------------------
Classification + full-field extraction logic for invoice PDFs.
Handles both text-based PDFs (via pdfplumber) and scanned/image PDFs
(via pytesseract OCR fallback, page by page).
"""

import os
import re
import logging
from difflib import SequenceMatcher

import pdfplumber

logger = logging.getLogger("invoice_extract")

# Optional OCR deps - imported lazily so a machine without them can still
# run on pure text PDFs. Both are pip-only (no system binaries required):
#   - PyMuPDF (fitz) renders PDF pages to images without needing poppler.
#   - easyocr does OCR without needing the tesseract system binary
#     (it downloads a small recognition model on first use).
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

OCR_AVAILABLE = FITZ_AVAILABLE and EASYOCR_AVAILABLE

_easyocr_reader = None


def _get_ocr_reader():
    """Lazily initialize the easyocr reader (loads model weights once)."""
    global _easyocr_reader
    if _easyocr_reader is None:
        logger.info("Loading easyocr model (first-time use may take a moment)...")
        _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader


def _render_page_to_image(pdf_path, page_index_zero_based, dpi=300):
    """Render one PDF page to a numpy image array using PyMuPDF."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index_zero_based]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        import numpy as np
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:  # RGBA -> RGB
            img = img[:, :, :3]
        return img
    finally:
        doc.close()


def _ocr_page(pdf_path, page_index_zero_based, dpi=300):
    """OCR a single page and return the extracted text."""
    image = _render_page_to_image(pdf_path, page_index_zero_based, dpi=dpi)
    reader = _get_ocr_reader()
    results = reader.readtext(image, detail=0, paragraph=True)
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Low-level page reading (text + OCR fallback)
# ---------------------------------------------------------------------------

def get_page_texts(pdf_path, ocr_dpi=300, min_text_chars=20):
    """
    Returns:
        page_texts: list[str]           text for each page (OCR'd if needed)
        page_tables: list[list[list]]   raw tables per page (pdfplumber only;
                                          empty list for OCR'd/scanned pages)
        ocr_pages: list[int]            1-indexed page numbers that required OCR
        page_count: int
    """
    page_texts = []
    page_tables = []
    ocr_pages = []

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = []
            try:
                tables = page.extract_tables() or []
            except Exception as e:
                logger.warning(f"Table extraction failed on page {i} of {pdf_path}: {e}")

            if len(text.strip()) < min_text_chars:
                # Likely a scanned/image page -> OCR fallback
                if OCR_AVAILABLE:
                    try:
                        ocr_text = _ocr_page(pdf_path, i - 1, dpi=ocr_dpi)
                        if len(ocr_text.strip()) > len(text.strip()):
                            text = ocr_text
                            ocr_pages.append(i)
                            tables = []  # no reliable table structure from OCR text
                    except Exception as e:
                        logger.warning(f"OCR failed on page {i} of {pdf_path}: {e}")
                        text = text or "[unclear]"
                else:
                    if not text.strip():
                        text = "[unclear]"

            page_texts.append(text)
            page_tables.append(tables)

    return page_texts, page_tables, ocr_pages, page_count


# ---------------------------------------------------------------------------
# Classification (Part 1)
# ---------------------------------------------------------------------------

TYPE_KEYWORDS = {
    "Credit Memo": ["credit memo", "credit note", "amount credited", "this is a credit"],
    "Debit Memo": ["debit memo", "debit note", "additional charge", "adjustment invoice"],
    "Utilities": ["meter reading", "kwh", "usage period", "utility", "service address",
                  "electric", "water bill", "gas bill", "telecom", "metered"],
    "Services": ["professional services", "consulting", "hourly rate", "timesheet",
                 "service period", "labor charges", "statement of work", "sow"],
    "Goods": ["quantity ordered", "quantity shipped", "unit selling price", "ship date",
              "carrier", "item no", "part number", "sku"],
}

COUNTRY_SIGNALS = {
    "United States": ["united states", "usa", "u.s.a", r"\busd\b", "aba#", "swift: bof",
                       "ein:", r", [a-z]{2} \d{5}"],
    "United Kingdom": ["united kingdom", "uk", r"\bgbp\b", "vat reg", "sort code"],
    "India": ["india", r"\binr\b", "gstin", "pan:"],
    "Canada": ["canada", r"\bcad\b", "gst/hst", "postal code"],
    "Australia": ["australia", r"\baud\b", "abn:"],
    "Germany": ["germany", r"\beur\b", "ust-idnr", "deutschland"],
}


def _fuzzy_contains(haystack_lower, needle):
    if needle in haystack_lower:
        return True
    return False


def classify_invoice(full_text):
    """Classify invoice type (1a), country (1b), and memo flag (1c)."""
    text_lower = full_text.lower()

    # --- 1a. Invoice type ---
    scores = {}
    matched_terms = {}
    for label, keywords in TYPE_KEYWORDS.items():
        hits = []
        for kw in keywords:
            if re.search(kw, text_lower):
                hits.append(kw)
        if hits:
            scores[label] = len(hits)
            matched_terms[label] = hits

    # Explicit header title takes priority
    header_title = None
    m = re.search(r"(tax invoice|credit memo|debit memo|utility bill|invoice)", text_lower)
    if m:
        header_title = m.group(1)

    if header_title == "credit memo":
        invoice_type = "Credit Memo"
        confidence = 95
    elif header_title == "debit memo":
        invoice_type = "Debit Memo"
        confidence = 95
    elif header_title == "utility bill":
        invoice_type = "Utilities"
        confidence = 90
    elif scores:
        invoice_type = max(scores, key=scores.get)
        top = scores[invoice_type]
        total_possible = len(TYPE_KEYWORDS[invoice_type])
        confidence = min(95, int(40 + (top / max(total_possible, 1)) * 55))
    else:
        invoice_type = "Other/Unclassified"
        confidence = 0

    evidence_keywords = matched_terms.get(invoice_type, [])
    if header_title:
        evidence_keywords = [f"header title: '{header_title}'"] + evidence_keywords

    # --- 1b. Country ---
    country = "Unknown"
    country_evidence = []
    for c, patterns in COUNTRY_SIGNALS.items():
        hits = [p for p in patterns if re.search(p, text_lower)]
        if hits:
            country = c
            country_evidence = hits
            break

    # --- 1c. Memo check (independent confirmation) ---
    is_credit_memo = bool(re.search(r"credit memo|credit note", text_lower))
    is_debit_memo = bool(re.search(r"debit memo|debit note", text_lower))
    memo_flag = "Credit Memo" if is_credit_memo else ("Debit Memo" if is_debit_memo else "None")

    return {
        "invoice_type": invoice_type,
        "type_confidence": confidence,
        "type_evidence": evidence_keywords,
        "country": country,
        "country_evidence": country_evidence,
        "memo_flag": memo_flag,
    }


# ---------------------------------------------------------------------------
# Field extraction (Part 2) - label/value regex over full text
# ---------------------------------------------------------------------------

# Each entry: (output_label, list of regex alternatives to try, in priority order)
# Regex captures the value up to end of line / next likely delimiter.
FIELD_PATTERNS = {
    "Header": [
        ("Company/Vendor Name", [r"^([A-Z][A-Za-z0-9 ,.&]+(?:INC|SYSTEMS|LLC|LTD|CORP)[.,]?)\s*$"]),
        ("Invoice Number", [r"\bNUMBER\b[:\s]*\n?\s*([A-Z0-9\-]+)", r"invoice\s*#?\s*[:\s]\s*([A-Z0-9\-]+)"]),
        ("Page Number", [r"PAGE NUMBER[:\s]*\n?\s*(\d+\s*/\s*\d+)"]),
        ("PO Number", [r"PO NUMBER[:\s]*\n?\s*(PO[_\-]?[A-Z0-9]+|\S+)"]),
        ("Transaction Date", [r"TRANSACTION DATE[:\s]*\n?\s*([\d\-A-Za-z]+)"]),
        ("Order Date", [r"ORDER DATE[:\s]*\n?\s*([\d\-A-Za-z]+)"]),
        ("Previous Transaction Number", [r"PREVIOUS TRANSACTION\s*#?[:\s]*\n?\s*([A-Z0-9\-]*)"]),
        ("Customer Number", [r"CUSTOMER NUMBER[:\s]*\n?\s*([A-Z0-9\-]+)"]),
        ("SO Number", [r"SO NUMBER[:\s]*\n?\s*([A-Z0-9\-]+)"]),
        ("Bill-To Number", [r"BILL TO NUMBER[:\s]*\n?\s*([A-Z0-9\-]+)"]),
        ("Account Number", [r"Account#?[:\s]*\n?\s*([A-Z0-9\-]+)"]),
        ("Remit-To Details", [r"REMIT TO\s*:?\s*\n(.+?)(?:\nBILL TO|\nSHIP TO|\n\n)"]),
    ],
    "BillTo": [
        ("Customer/Company Name", [r"BILL TO\s*:?\s*\n([A-Za-z0-9 ,.&\-]+)\n"]),
        ("Billing Address", [r"BILL TO\s*:?\s*\n[A-Za-z0-9 ,.&\-]+\n(.+?)(?:\nCustomer Registration|\nSHIP TO|\n\n)"]),
        ("Customer Registration #", [r"BILL TO.*?Customer Registration\s*#\s*:?\s*([A-Za-z0-9\-]*)"]),
        ("Bill To Contact Person", [r"Bill To Contact Person\s*:?\s*([A-Za-z0-9 ,.\-]*)"]),
        ("Phone Number", [r"BILL TO.*?Phone No\s*:?\s*([A-Za-z0-9 ,.\-]*)"]),
    ],
    "ShipTo": [
        ("Ship-To Customer/Company", [r"SHIP TO\s*:?\s*\n([A-Za-z0-9 ,.&\-]+)\n"]),
        ("Shipping Address", [r"SHIP TO\s*:?\s*\n[A-Za-z0-9 ,.&\-]+\n(.+?)(?:\nCustomer Registration|\nTERMS|\n\n)"]),
        ("Customer Registration #", [r"SHIP TO.*?Customer Registration\s*#\s*:?\s*([A-Za-z0-9\-]*)"]),
        ("Ship To Contact Person", [r"Ship to Contact Person\s*:?\s*([A-Za-z0-9 ,.\-]*)"]),
        ("Phone Number", [r"SHIP TO.*?Phone No\s*:?\s*([A-Za-z0-9 ,.\-]*)"]),
    ],
    "Terms": [
        ("Terms", [r"\bTERMS\b[:\s]*\n?\s*([A-Za-z0-9 ]+?)(?:\n|SHIP DATE)"]),
        ("Ship Date", [r"SHIP DATE[:\s]*\n?\s*([\d\-A-Za-z]*)"]),
        ("Acceptance Code", [r"ACCEPTANCE CODE[:\s]*\n?\s*([A-Za-z0-9 ]*)"]),
        ("Due Date", [r"DUE DATE[:\s]*\n?\s*([\d\-A-Za-z]+)"]),
        ("Carrier/Service Level", [r"CARRIER\s*/\s*SERVICE LEVEL[:\s]*\n?\s*([A-Za-z0-9 ]+)"]),
        ("Currency", [r"CURRENCY[:\s]*\n?\s*([A-Z]{3})"]),
    ],
}


def _first_match(text, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            val = m.group(1).strip()
            val = re.sub(r"\s{2,}", " ", val)
            return val if val else None
    return None


def extract_labeled_fields(full_text):
    """Extract header / bill-to / ship-to / terms fields via regex over full text."""
    result = {}
    for section, fields in FIELD_PATTERNS.items():
        section_result = {}
        for label, patterns in fields:
            val = _first_match(full_text, patterns)
            section_result[label] = val if val is not None else None
        result[section] = section_result
    return result


# ---------------------------------------------------------------------------
# Line item table extraction (Part 2.5)
# ---------------------------------------------------------------------------

CANON_COLUMNS = [
    "PO Line No.", "Item No.", "Description / Classification of Goods",
    "Ship/Install Location", "Quote Number", "Group Line ID",
    "Quantity Ordered", "Quantity Shipped", "Unit Selling Price",
    "Tax Indicator", "Tax Rate (%)", "Extended Amount (Excl. Tax)",
    "Tax Amount", "Extended Amount (Incl. Tax)",
]

# Fuzzy header aliases -> canonical column
HEADER_ALIASES = {
    "po line no": "PO Line No.", "po line": "PO Line No.",
    "item no": "Item No.", "item number": "Item No.",
    "description": "Description / Classification of Goods",
    "description and classification of goods": "Description / Classification of Goods",
    "quantity ordered": "Quantity Ordered", "ordered": "Quantity Ordered",
    "quantity shipped": "Quantity Shipped", "shipped": "Quantity Shipped",
    "unit selling price": "Unit Selling Price",
    "tax": "Tax Indicator",
    "tax rate": "Tax Rate (%)", "tax rate (%)": "Tax Rate (%)",
    "extended amount (excluding taxes)": "Extended Amount (Excl. Tax)",
    "extended amount excluding taxes": "Extended Amount (Excl. Tax)",
    "tax amount": "Tax Amount",
    "extended amount (including taxes)": "Extended Amount (Incl. Tax)",
    "extended amount including taxes": "Extended Amount (Incl. Tax)",
    "quote number": "Quote Number",
    "group line id": "Group Line ID",
}


def _norm_header(h):
    if h is None:
        return ""
    h = re.sub(r"\s+", " ", h.strip().lower())
    h = h.replace("\n", " ")
    return h


def _fuzzy_canon(header_text):
    norm = _norm_header(header_text)
    if norm in HEADER_ALIASES:
        return HEADER_ALIASES[norm]
    best, best_score = None, 0.0
    for alias, canon in HEADER_ALIASES.items():
        score = SequenceMatcher(None, norm, alias).ratio()
        if score > best_score:
            best, best_score = canon, score
    return best if best_score > 0.6 else None


def extract_line_items(page_tables):
    """
    Scans tables across all pages for ones that look like the line-item table
    (matches >= 3 canonical column headers) and stitches multi-page tables
    into one continuous list, preserving original order.
    """
    line_items = []
    active_col_map = None  # carries over across pages for tables that continue

    for page_num, tables in enumerate(page_tables, start=1):
        for table in tables:
            if not table or len(table) < 1:
                continue
            header_row = table[0]
            col_map = {}
            matched = 0
            for idx, h in enumerate(header_row):
                canon = _fuzzy_canon(h)
                if canon:
                    col_map[idx] = canon
                    matched += 1

            is_line_item_table = matched >= 3
            if is_line_item_table:
                active_col_map = col_map
                data_rows = table[1:]
            elif active_col_map and header_row and not any(header_row):
                # Continuation table on a new page with blank/no header
                col_map = active_col_map
                data_rows = table
            else:
                continue

            for row in data_rows:
                if row is None or all(c is None or str(c).strip() == "" for c in row):
                    continue
                item = {canon: None for canon in CANON_COLUMNS}
                item["_source_page"] = page_num
                extra = {}
                for idx, cell in enumerate(row):
                    val = cell.strip() if isinstance(cell, str) else cell
                    val = val if val not in ("", None) else None
                    if idx in col_map:
                        item[col_map[idx]] = val
                    elif val is not None:
                        extra[f"col_{idx}"] = val
                if extra:
                    item["_extra_columns"] = extra
                line_items.append(item)

    return line_items


# ---------------------------------------------------------------------------
# Additional / unmapped fields (Part 2.6) - best-effort catch-all
# ---------------------------------------------------------------------------

def extract_additional_fields(full_text, labeled_fields):
    """
    Very lightweight catch-all: pulls any 'Label: value' style lines that
    weren't already captured by the structured extractors above.
    """
    captured_labels = set()
    for section in labeled_fields.values():
        captured_labels.update(k.lower() for k in section.keys())

    additional = []
    for line in full_text.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /#\-]{2,40})\s*:\s*(.+?)\s*$", line)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
            if label.lower() not in captured_labels and value:
                additional.append((label, value))
    return additional


# ---------------------------------------------------------------------------
# Top-level orchestration for a single PDF
# ---------------------------------------------------------------------------

def process_invoice(pdf_path):
    filename = os.path.basename(pdf_path)
    page_texts, page_tables, ocr_pages, page_count = get_page_texts(pdf_path)
    full_text = "\n".join(page_texts)

    classification = classify_invoice(full_text)
    labeled_fields = extract_labeled_fields(full_text)
    line_items = extract_line_items(page_tables)
    additional_fields = extract_additional_fields(full_text, labeled_fields)

    # --- Validation self-check ---
    validation_notes = []
    if ocr_pages:
        validation_notes.append(f"OCR fallback used on page(s): {ocr_pages}")

    printed_total_match = re.search(
        r"total\s+line\s+items?\s*[:\s]\s*(\d+)", full_text, re.IGNORECASE
    )
    if printed_total_match:
        printed_total = int(printed_total_match.group(1))
        if printed_total != len(line_items):
            validation_notes.append(
                f"Line item count mismatch: extracted {len(line_items)}, "
                f"document states {printed_total}"
            )

    expected_by_type = {
        "Goods": ["Quantity Ordered", "Quantity Shipped", "Unit Selling Price"],
        "Utilities": [],
        "Services": [],
    }
    for field in expected_by_type.get(classification["invoice_type"], []):
        if not any(item.get(field) for item in line_items):
            validation_notes.append(f"Expected field '{field}' missing across all line items")

    if not validation_notes:
        validation_notes.append("No issues detected")

    return {
        "filename": filename,
        "page_count": page_count,
        "ocr_pages": ocr_pages,
        "classification": classification,
        "labeled_fields": labeled_fields,
        "line_items": line_items,
        "additional_fields": additional_fields,
        "validation_notes": validation_notes,
        "status": "OK",
        "error": None,
    }
