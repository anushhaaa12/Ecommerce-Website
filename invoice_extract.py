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
        left_texts: list[str]           text from the left half of each page
                                          (helps separate side-by-side blocks
                                          like BILL TO vs SHIP TO)
        right_texts: list[str]          text from the right half of each page
        ocr_pages: list[int]            1-indexed page numbers that required OCR
        page_count: int
    """
    page_texts = []
    page_tables = []
    left_texts = []
    right_texts = []
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

            left_text, right_text = "", ""
            try:
                mid_x = page.width / 2
                left_text = page.crop((0, 0, mid_x, page.height)).extract_text() or ""
                right_text = page.crop((mid_x, 0, page.width, page.height)).extract_text() or ""
            except Exception as e:
                logger.warning(f"Column crop failed on page {i} of {pdf_path}: {e}")

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
            left_texts.append(left_text)
            right_texts.append(right_text)

    return page_texts, page_tables, left_texts, right_texts, ocr_pages, page_count


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
    "United States": ["united states", "usa", "u.s.a", r", [a-z]{2} \d{5}"],
    "United Kingdom": ["united kingdom", r"\buk\b", "vat reg", "sort code"],
    "India": ["india", "gstin", r"\bpan:"],
    "Canada": ["canada", "gst/hst", "postal code"],
    "Australia": ["australia", "abn:"],
    "Germany": ["germany", "ust-idnr", "deutschland"],
}

# Weaker signals (currency codes, bank-routing details) that are useful as a
# last-resort fallback but unreliable on their own - they often reflect the
# vendor's remittance bank rather than the customer's actual location, so
# they're deliberately excluded when scanning the Bill To address directly.
COUNTRY_WEAK_SIGNALS = {
    "United States": [r"\busd\b", "aba#", "swift: bof", "ein:"],
    "United Kingdom": [r"\bgbp\b"],
    "India": [r"\binr\b"],
    "Canada": [r"\bcad\b"],
    "Australia": [r"\baud\b"],
    "Germany": [r"\beur\b"],
}


def _fuzzy_contains(haystack_lower, needle):
    if needle in haystack_lower:
        return True
    return False


def detect_country(bill_to_text, full_text):
    """
    Country detection (1b), prioritized in this order:
      1. Strong signals (explicit country name, postal/tax-ID formats) found
         within the Bill To address specifically - this is the customer's
         actual billing country, which is what matters for classification.
      2. Strong signals anywhere in the document, if the Bill To address
         didn't yield a match (e.g. address was blank/unparsed).
      3. Weak signals (currency, bank routing codes) anywhere in the
         document, as a last resort - flagged as lower-confidence evidence
         since these can reflect the vendor's bank rather than the
         customer's location.
    """
    bill_to_lower = (bill_to_text or "").lower()
    full_lower = (full_text or "").lower()

    for c, patterns in COUNTRY_SIGNALS.items():
        hits = [p for p in patterns if re.search(p, bill_to_lower)]
        if hits:
            return c, hits, "Bill To address"

    for c, patterns in COUNTRY_SIGNALS.items():
        hits = [p for p in patterns if re.search(p, full_lower)]
        if hits:
            return c, hits, "document-wide (Bill To address was inconclusive)"

    for c, patterns in COUNTRY_WEAK_SIGNALS.items():
        hits = [p for p in patterns if re.search(p, full_lower)]
        if hits:
            return c, hits, "weak/incidental signals only (e.g. currency, bank routing - verify manually)"

    return "Unknown", [], None


def classify_invoice(full_text):
    """Classify invoice type (1a) and memo flag (1c). Country (1b) is
    handled separately by detect_country(), since it needs the Bill To
    address specifically rather than whole-document text."""
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

    # --- 1c. Memo check (independent confirmation) ---
    is_credit_memo = bool(re.search(r"credit memo|credit note", text_lower))
    is_debit_memo = bool(re.search(r"debit memo|debit note", text_lower))
    memo_flag = "Credit Memo" if is_credit_memo else ("Debit Memo" if is_debit_memo else "None")

    return {
        "invoice_type": invoice_type,
        "type_confidence": confidence,
        "type_evidence": evidence_keywords,
        "memo_flag": memo_flag,
    }


# ---------------------------------------------------------------------------
# Field extraction (Part 2)
# ---------------------------------------------------------------------------
# Two extraction strategies are combined, since invoice layouts mix ruled
# tables with free-flowing side-by-side text blocks:
#   1. Ruled-table label/value scanning (extract_table_label_values) - used
#      for header info blocks and terms blocks that are drawn as bordered
#      tables (e.g. NUMBER | PAGE NUMBER | PO NUMBER header row + value row).
#   2. Column-aware regex (extract_labeled_fields) - used for BILL TO / SHIP
#      TO blocks, which are typically plain text placed side-by-side rather
#      than inside a ruled table. Running regex separately on the left-half
#      and right-half of the page (rather than the whole page's flowing
#      text) prevents the two blocks' words from interleaving.
# Table-derived values take priority; regex fills in anything the table
# scan didn't find.

# Master label -> (section, canonical field name), used by the table scanner.
MASTER_LABELS = {
    "number": ("Header", "Invoice Number"),
    "page number": ("Header", "Page Number"),
    "po number": ("Header", "PO Number"),
    "transaction date": ("Header", "Transaction Date"),
    "order date": ("Header", "Order Date"),
    "previous transaction #": ("Header", "Previous Transaction Number"),
    "previous transaction number": ("Header", "Previous Transaction Number"),
    "customer number": ("Header", "Customer Number"),
    "so number": ("Header", "SO Number"),
    "bill to number": ("Header", "Bill-To Number"),
    "terms": ("Terms", "Terms"),
    "ship date": ("Terms", "Ship Date"),
    "acceptance code": ("Terms", "Acceptance Code"),
    "due date": ("Terms", "Due Date"),
    "carrier / service level": ("Terms", "Carrier/Service Level"),
    "carrier/service level": ("Terms", "Carrier/Service Level"),
    "currency": ("Terms", "Currency"),
}


def _norm_label(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower()).rstrip(":#")


def extract_table_label_values(page_tables):
    """
    Scans non-line-item tables for label-row/value-row pairs (a row whose
    cells are mostly recognized labels, immediately followed by a row of
    values in the same column positions). Returns a dict of
    {"Header": {...}, "Terms": {...}} plus a list of (label, value, page)
    tuples for anything table-based that wasn't in MASTER_LABELS (folded
    into Additional Fields later).
    """
    result = {"Header": {}, "Terms": {}}
    leftover = []

    for page_num, tables in enumerate(page_tables, start=1):
        for table in tables:
            if not table or len(table) < 2:
                continue
            # Skip anything that looks like the line-item table - that is
            # handled separately and much more strictly in extract_line_items.
            if _looks_like_line_item_header(table[0]):
                continue

            row_idx = 0
            while row_idx < len(table) - 1:
                row = table[row_idx]
                if not row:
                    row_idx += 1
                    continue
                label_hits = sum(1 for c in row if c and _norm_label(c) in MASTER_LABELS)
                if label_hits >= 2:
                    value_row = table[row_idx + 1]
                    for col_idx, cell in enumerate(row):
                        if not cell:
                            continue
                        norm = _norm_label(cell)
                        if norm in MASTER_LABELS:
                            section, field = MASTER_LABELS[norm]
                            val = None
                            if value_row and col_idx < len(value_row):
                                val = value_row[col_idx]
                                val = val.strip() if isinstance(val, str) else val
                                val = val if val not in ("", None) else None
                            if val is not None and not result[section].get(field):
                                result[section][field] = val
                    row_idx += 2
                    continue
                # Same-row "Label: Value" merged cells (e.g. "Account#: 12338-57430")
                for cell in row:
                    if not cell:
                        continue
                    m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /#\-]{2,40})\s*:\s*(.+?)\s*$", str(cell))
                    if m:
                        label, val = m.group(1).strip(), m.group(2).strip()
                        norm = _norm_label(label)
                        if norm in MASTER_LABELS and val:
                            section, field = MASTER_LABELS[norm]
                            if not result[section].get(field):
                                result[section][field] = val
                        elif val:
                            leftover.append((label, val, page_num))
                row_idx += 1

    return result, leftover


def _looks_like_line_item_header(header_row):
    """Quick check used to keep the header-table scanner out of the
    line-item table (line items are handled separately and more strictly)."""
    if not header_row:
        return False
    joined = " ".join(_norm_label(c) for c in header_row if c)
    return ("quantity" in joined and ("ordered" in joined or "shipped" in joined)) or \
           ("unit selling price" in joined)


# Column-aware regex for BILL TO / SHIP TO blocks (run on cropped column
# text so the two side-by-side blocks don't interleave), plus vendor
# name / remit-to which are also in the left column on this layout.
LEFT_COLUMN_PATTERNS = {
    "Header": [
        ("Company/Vendor Name", [r"([A-Z][A-Za-z0-9 ,.&]{2,60}(?:INC|SYSTEMS|LLC|LTD|CORP)[.,]?)"]),
        ("Remit-To Details", [r"REMIT TO\s*:?\s*\n(.+?)(?:\nBILL TO|\n\n)"]),
        ("Account Number", [r"Account\s*#?[:\s]*\n?\s*([A-Z0-9\-]+)"]),
    ],
    "BillTo": [
        ("Customer/Company Name", [r"BILL TO\s*:?\s*\n([A-Za-z0-9 ,.&\-]+)\n"]),
        ("Billing Address", [r"BILL TO\s*:?\s*\n[A-Za-z0-9 ,.&\-]+\n(.+?)(?:\nCustomer Registration|\n\n)"]),
        ("Customer Registration #", [r"BILL TO.*?Customer Registration\s*#\s*:?\s*([^\n]*)"]),
        ("Bill To Contact Person", [r"Bill To Contact Person\s*:?\s*([^\n]*)"]),
        ("Phone Number", [r"BILL TO.*?Phone No\.?\s*:?\s*([^\n]*)"]),
    ],
}

RIGHT_COLUMN_PATTERNS = {
    "ShipTo": [
        ("Ship-To Customer/Company", [r"SHIP TO\s*:?\s*\n([A-Za-z0-9 ,.&\-]+)\n"]),
        ("Shipping Address", [r"SHIP TO\s*:?\s*\n[A-Za-z0-9 ,.&\-]+\n(.+?)(?:\nCustomer Registration|\n\n)"]),
        ("Customer Registration #", [r"SHIP TO.*?Customer Registration\s*#\s*:?\s*([^\n]*)"]),
        ("Ship To Contact Person", [r"Ship to Contact Person\s*:?\s*([^\n]*)"]),
        ("Phone Number", [r"SHIP TO.*?Phone No\.?\s*:?\s*([^\n]*)"]),
    ],
}

# If a captured value itself looks like the start of another known label
# (happens when a field is genuinely blank and the regex spills into
# whatever text follows on the same line), treat it as blank instead of
# returning garbage.
_NEXT_LABEL_MARKERS = [
    "bill to", "ship to", "customer registration", "contact person", "phone no",
    "terms", "ship date", "acceptance code", "due date", "carrier", "currency",
    "quote number", "group line id",
]


def _looks_like_next_label(value):
    if not value:
        return False
    norm = value.strip().lower()
    if any(norm.startswith(marker) for marker in _NEXT_LABEL_MARKERS):
        return True
    # Long values that contain several label-like words are almost always a
    # blank field that bled into unrelated text further down the page,
    # rather than a genuine value.
    if len(norm) > 25 and sum(1 for marker in _NEXT_LABEL_MARKERS if marker in norm) >= 1:
        return True
    return False




def _first_match(text, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            val = m.group(1).strip()
            val = re.sub(r"\s{2,}", " ", val)
            if _looks_like_next_label(val):
                return None
            return val if val else None
    return None


def extract_labeled_fields(full_text, left_text, right_text, page_tables):
    """Combines table-based and column-aware regex extraction into the
    standard Header / BillTo / ShipTo / Terms section shape."""
    result = {"Header": {}, "BillTo": {}, "ShipTo": {}, "Terms": {}}

    # 1. Ruled-table label/value scan (most reliable for this layout style)
    table_fields, table_leftover = extract_table_label_values(page_tables)
    for section in ("Header", "Terms"):
        result[section].update(table_fields.get(section, {}))

    # 2. Column-aware regex fallback / supplement
    for section, fields in LEFT_COLUMN_PATTERNS.items():
        for label, patterns in fields:
            if result[section].get(label):
                continue
            val = _first_match(left_text, patterns)
            result[section][label] = val
    for section, fields in RIGHT_COLUMN_PATTERNS.items():
        for label, patterns in fields:
            if result[section].get(label):
                continue
            val = _first_match(right_text, patterns)
            result[section][label] = val

    # Ensure every expected field key exists even if nothing matched
    all_expected = {
        "Header": ["Company/Vendor Name", "Invoice Number", "Page Number", "PO Number",
                    "Transaction Date", "Order Date", "Previous Transaction Number",
                    "Customer Number", "SO Number", "Bill-To Number", "Account Number",
                    "Remit-To Details"],
        "BillTo": ["Customer/Company Name", "Billing Address", "Customer Registration #",
                    "Bill To Contact Person", "Phone Number"],
        "ShipTo": ["Ship-To Customer/Company", "Shipping Address", "Customer Registration #",
                    "Ship To Contact Person", "Phone Number"],
        "Terms": ["Terms", "Ship Date", "Acceptance Code", "Due Date",
                    "Carrier/Service Level", "Currency"],
    }
    for section, labels in all_expected.items():
        for label in labels:
            result[section].setdefault(label, None)

    return result, table_leftover


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
    if not norm:
        return None
    if norm in HEADER_ALIASES:
        return HEADER_ALIASES[norm]
    # Substring containment handles verbose real-world headers, e.g.
    # "description and classification of goods/ invoice description"
    # containing the shorter alias "description and classification of goods".
    contain_matches = [(alias, canon) for alias, canon in HEADER_ALIASES.items()
                        if len(alias) >= 6 and (alias in norm or norm in alias)]
    if contain_matches:
        # Prefer the longest matching alias (most specific)
        contain_matches.sort(key=lambda x: len(x[0]), reverse=True)
        return contain_matches[0][1]
    best, best_score = None, 0.0
    for alias, canon in HEADER_ALIASES.items():
        score = SequenceMatcher(None, norm, alias).ratio()
        if score > best_score:
            best, best_score = canon, score
    # Fuzzy-only fallback (no substring hit) needs a fairly high bar, since
    # this function also screens candidate tables for line-item detection -
    # a loose match here could misclassify an unrelated table.
    return best if best_score > 0.75 else None


# Columns that must appear before a table is treated as the line-item
# table. Requiring a quantity/price column together with a description
# column avoids false positives on other ruled tables in the document
# (e.g. the NUMBER / PAGE NUMBER / PO NUMBER header block, which also has
# 3+ short column headers but is not a line-item table).
LINE_ITEM_DISCRIMINATOR_COLUMNS = {
    "Quantity Ordered", "Quantity Shipped", "Unit Selling Price",
    "Extended Amount (Excl. Tax)", "Extended Amount (Incl. Tax)",
}


def extract_line_items(page_tables):
    """
    Scans tables across all pages for ones that look like the line-item table
    (matches a description column plus at least one quantity/price column)
    and stitches multi-page tables into one continuous list, preserving
    original order.
    """
    line_items = []
    active_col_map = None  # carries over across pages for tables that continue

    for page_num, tables in enumerate(page_tables, start=1):
        for table in tables:
            if not table or len(table) < 1:
                continue
            header_row = table[0]
            col_map = {}
            for idx, h in enumerate(header_row):
                canon = _fuzzy_canon(h)
                if canon:
                    col_map[idx] = canon

            has_description = "Description / Classification of Goods" in col_map.values()
            has_discriminator = any(c in LINE_ITEM_DISCRIMINATOR_COLUMNS for c in col_map.values())
            is_line_item_table = has_description and has_discriminator

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

def extract_additional_fields(full_text, labeled_fields, table_leftover=None):
    """
    Catch-all: pulls any 'Label: value' style lines from the flowing text
    that weren't already captured by the structured extractors above, plus
    any table-based label/value pairs (table_leftover) that didn't match a
    known field. De-duplicates identical (label, value) pairs, since Cisco-
    style invoices repeat the same header/footer block on every page.
    """
    captured_labels = set()
    for section in labeled_fields.values():
        captured_labels.update(k.lower() for k in section.keys())

    seen = set()
    additional = []

    for line in full_text.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /#\-]{2,40})\s*:\s*(.+?)\s*$", line)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
            key = (label.lower(), value.lower())
            if label.lower() not in captured_labels and value and key not in seen:
                additional.append((label, value))
                seen.add(key)

    for label, value, _page in (table_leftover or []):
        key = (label.lower(), value.lower())
        if label.lower() not in captured_labels and value and key not in seen:
            additional.append((label, value))
            seen.add(key)

    return additional


# ---------------------------------------------------------------------------
# Top-level orchestration for a single PDF
# ---------------------------------------------------------------------------

def process_invoice(pdf_path):
    filename = os.path.basename(pdf_path)
    page_texts, page_tables, left_texts, right_texts, ocr_pages, page_count = get_page_texts(pdf_path)
    full_text = "\n".join(page_texts)
    left_text = "\n".join(left_texts)
    right_text = "\n".join(right_texts)

    classification = classify_invoice(full_text)
    labeled_fields, table_leftover = extract_labeled_fields(full_text, left_text, right_text, page_tables)
    line_items = extract_line_items(page_tables)
    additional_fields = extract_additional_fields(full_text, labeled_fields, table_leftover)

    # Country (1b) is detected from the Bill To address specifically -
    # see detect_country() for why this is more reliable than a
    # whole-document scan (avoids false hits from the vendor's own
    # remit-to/bank details).
    bill_to_text = "\n".join(str(v) for v in labeled_fields.get("BillTo", {}).values() if v)
    country, country_evidence, country_source = detect_country(bill_to_text, full_text)
    classification["country"] = country
    classification["country_evidence"] = country_evidence
    classification["country_source"] = country_source

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
