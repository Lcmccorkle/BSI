import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Optional OCR fallback (only used when requested or when native text is too weak) ---
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

st.set_page_config(page_title="BOL → Inventory Extractor", page_icon="📦", layout="wide")
st.title("📦 Bill of Lading → Inventory Extractor")

st.write(
    "Upload one or more BOL PDFs. I’ll extract fields and give you a printable Excel "
    "with columns: **Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes**."
)

# ---------- Controls ----------
colA, colB = st.columns([1,1])
debug_mode = colA.checkbox("🔎 Debug Mode (show page text if no match)", value=False)
force_ocr = colB.checkbox("🖹 Force OCR for all pages (use only for scans)", value=False)

# ---------- Parsing helpers ----------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})")
LOT_RE = re.compile(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", re.IGNORECASE)

# Robust BOL locator:
# - allows "B/L", "B L", or "BOL"
# - allows "NO", "No", "Number" optionally with "." and ":" or "-"
# - captures the next 5+ digit/letter/hyphen token, even across newlines/spaces
BOL_BLOCK_RE = re.compile(
    r"(?:B[\\/]?\\s*L|BOL)\\s*(?:NO\\.?|#|NUMBER)?\\s*[:\\-]?\\s*([0-9A-Z\\-]{5,})",
    re.IGNORECASE
)

def normalize_text(s: str) -> str:
    # Collapse repeated spaces/tabs and excessive newlines
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def extract_text_plumber(pdf_bytes: bytes):
    texts = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            texts.append(t)
    return [normalize_text(t) for t in texts]

def extract_text_ocr(pdf_bytes: bytes):
    if not OCR_AVAILABLE:
        return []
    images = convert_from_bytes(pdf_bytes, dpi=300)
    texts = []
    for img in images:
        t = pytesseract.image_to_string(img, config="--psm 6")
        texts.append(t or "")
    return [normalize_text(t) for t in texts]

def extract_field(pattern, text, default=""):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else default

def parse_page(text: str, pindex: int, file_name: str):
    # --- BOL # (try robust block first) ---
    bol = ""
    m = BOL_BLOCK_RE.search(text)
    if m:
        bol = m.group(1).strip()
    if not bol:
        # Super-fallback: look near any 'B/L' or 'BOL' token and capture a big number after
        m2 = re.search(r"(?:B[\\/]?\\s*L|BOL)[^\\n]{0,80}?(\\d{5,})", text, re.IGNORECASE)
        if m2:
            bol = m2.group(1).strip()

    # If still missing, optionally dump snippet for debugging
    if not bol and debug_mode:
        with st.expander(f"Debug: raw text of page {pindex} in {file_name}"):
            st.code(text)

    if not bol:
        return None  # not a shipment page we can identify

    # --- Ship To (prefer last non-KS match—KS often appears in the FROM address) ---
    ship_tos = CITY_STATE_RE.findall(text)
    ship_to = ""
    if ship_tos:
        non_ks = [s for s in ship_tos if not s.strip().endswith("KS")]
        ship_to = (non_ks[-1] if non_ks else ship_tos[-1]).replace("  ", " ").strip()

    # --- Lot numbers ---
    lots = LOT_RE.findall(text)
    # unique (preserve order)
    lot_list, seen = [], set()
    for l in lots:
        if l not in seen:
            seen.add(l)
            lot_list.append(l)
    lot = "; ".join(lot_list)

    # --- Quantity ---
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9,]+)", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9,]+)", text)
    qty = qty.replace(",", "") if qty else ""

    # --- Packaging ---
    # Examples in your PDFs: "435.00 lb DRUM", "2,344.00 lb TOTE", "40.00 lb PAIL", "2.00 lb CAN QT"
    pack = ""
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text,
        re.IGNORECASE,
    )
    if m:
        pack = m.group(1)

    # --- Product ---
    product = ""
    for pat in [
        r"QUANTITY,\s*\(([^)]+)\)",
        r"NOT REGULATED BY DOT,?\s*\(([^)]+)\)",
        r"UN\d{3,4}[^()\n]*\(([^)]+)\)",
    ]:
        product = extract_field(pat, text)
        if product:
            break

    if not product:
        # Fallback: scan the region between 'Product' and 'CUST. NO.'
        lines = text.split("\n")
        try:
            i_prod = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Product"))
        except StopIteration:
            i_prod = 0
        try:
            i_cust = next(i for i, ln in enumerate(lines) if "CUST." in ln)
        except StopIteration:
            i_cust = len(lines)
        for ln in lines[i_prod:i_cust]:
            s = ln.strip()
            if not s or "Transporter" in s or "Destination" in s or s.startswith("#"):
                continue
            if re.search(r"[A-Za-z]", s) and 2 < len(s) < 120:
                product = s
                break

    return {
        "Unloaded By": "",
        "Ship To": ship_to,
        "Product": product,
        "Quantity": qty,
        "Packaging": pack,
        "Lot Numbers": lot,
        "BOL #": bol,
        "Notes": "",
    }

def parse_pdf(pdf_bytes: bytes, file_name: str):
    rows = []

    # Decide text source(s)
    texts_native = extract_text_plumber(pdf_bytes) if not force_ocr else []
    texts_ocr = []

    # Heuristic: if native text looks suspiciously small, try OCR too
    if not force_ocr:
        native_len = sum(len(t) for t in texts_native)
        if native_len < 80 and OCR_AVAILABLE:
            texts_ocr = extract_text_ocr(pdf_bytes)
    else:
        if OCR_AVAILABLE:
            texts_ocr = extract_text_ocr(pdf_bytes)

    # Merge: prefer native text, but if a given page is empty natively, use OCR text
    pages = max(len(texts_native), len(texts_ocr)) or len(texts_native)
    if pages == 0 and OCR_AVAILABLE:
        texts_ocr = extract_text_ocr(pdf_bytes)
        pages = len(texts_ocr)

    for i in range(pages):
        t_native = texts_native[i] if i < len(texts_native) else ""
        t_ocr = texts_ocr[i] if i < len(texts_ocr) else ""
        text = t_native if len(t_native) >= len(t_ocr) else t_ocr
        if not text:
            continue
        row = parse_page(text, i + 1, file_name)
        if row:
            rows.append(row)
        elif debug_mode:
            # surface text for inspection if no match
            with st.expander(f"Debug: no match on page {i+1} in {file_name} (showing text)"):
                st.code(text)

    return rows

# ---------- UI ----------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    all_rows = []
    for uf in uploaded:
        pdf_bytes = uf.read()
        rows = parse_pdf(pdf_bytes, uf.name)
        if not rows:
            st.warning(f"⚠️ Could not extract any BOL rows from: {uf.name}")
        else:
            st.success(f"✅ Parsed {len(rows)} row(s) from {uf.name}")
            all_rows.extend(rows)

    if all_rows:
        columns = [
            "Unloaded By",
            "Ship To",
            "Product",
            "Quantity",
            "Packaging",
            "Lot Numbers",
            "BOL #",
            "Notes",
        ]
        df = pd.DataFrame(all_rows, columns=columns)

        # Sort by BOL # when numeric
        with pd.option_context("mode.chained_assignment", None):
            try:
                df["__bolnum"] = pd.to_numeric(df["BOL #"], errors="coerce")
                df.sort_values("__bolnum", inplace=True, kind="stable")
                df.drop(columns=["__bolnum"], inplace=True)
            except Exception:
                pass

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Build a printable Excel
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Inventory", index=False)
            ws = writer.book["Inventory"]
            ws.freeze_panes = "A2"
            widths = {"A": 14, "B": 22, "C": 36, "D": 10, "E": 22, "F": 26, "G": 12, "H": 22}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w

        st.download_button(
            "⬇️ Download Excel",
            data=output.getvalue(),
            file_name="inventory_rows.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if not OCR_AVAILABLE and (force_ocr or debug_mode):
        st.info(
            "OCR libraries not detected. To enable OCR for scanned PDFs, add `pytesseract` "
            "and `pdf2image` to requirements and install system Tesseract + Poppler."
        )
else:
    st.caption("Drop in your BOL PDFs to get started.")
