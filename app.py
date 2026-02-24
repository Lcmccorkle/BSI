
import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# ---------------- OCR availability check (safe) ----------------
OCR_AVAILABLE = False
OCR_REASON = ""
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except Exception as e:
    OCR_AVAILABLE = False
    OCR_REASON = f"python libs missing ({e.__class__.__name__})"

st.set_page_config(page_title="BOL → Inventory Extractor", page_icon="📦", layout="wide")
st.title("📦 Bill of Lading → Inventory Extractor")
st.write(
    "Upload one or more BOL PDFs. I’ll extract fields and give you a printable Excel "
    "with columns: **Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes**."
)

# ---------- Parsing helpers ----------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})")
BOL_BLOCK_RE = re.compile(
    r"(?:B[\\/]?\\s*L|BOL)\\s*(?:NO\\.?|#|NUMBER)?\\s*[:\\-]?\\s*([0-9A-Z\\-]{5,})",
    re.IGNORECASE
)

def normalize_text(s: str) -> str:
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

def try_ocr(pdf_bytes: bytes):
    """Attempt OCR; if Poppler/Tesseract aren't available, disable OCR gracefully."""
    global OCR_AVAILABLE, OCR_REASON
    if not OCR_AVAILABLE:
        return []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
        texts = []
        for img in images:
            t = pytesseract.image_to_string(img, config="--psm 6")
            texts.append(t or "")
        return [normalize_text(t) for t in texts]
    except Exception as e:
        OCR_AVAILABLE = False
        OCR_REASON = f"OCR runtime error ({e.__class__.__name__}): {e}"
        return []

def extract_field(pattern, text, default=""):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else default

def parse_page(text: str):
    # --- BOL # ---
    bol = ""
    m = BOL_BLOCK_RE.search(text)
    if m:
        bol = m.group(1).strip()
    if not bol:
        m2 = re.search(r"(?:B[\\/]?\\s*L|BOL)[^\\n]{0,80}?(\\d{5,})", text, re.IGNORECASE)
        if m2:
            bol = m2.group(1).strip()
    if not bol:
        return None

    # --- Ship To (prefer last non-KS hit to avoid origin address) ---
    ship_tos = CITY_STATE_RE.findall(text)
    ship_to = ""
    if ship_tos:
        non_ks = [s for s in ship_tos if not s.strip().endswith("KS")]
        ship_to = (non_ks[-1] if non_ks else ship_tos[-1]).replace("  ", " ").strip()

    # --- Lot numbers (can be multiple) ---
    lots = re.findall(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", text, flags=re.IGNORECASE)
    seen, lot_list = set(), []
    for l in lots:
        if l not in seen:
            seen.add(l)
            lot_list.append(l)
    lot = "; ".join(lot_list)

    # --- Quantity (ordered or shipped count) ---
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9,]+)", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9,]+)", text)
    qty = qty.replace(",", "") if qty else ""

    # --- Packaging (e.g., "435.00 lb DRUM", "2,344.00 lb TOTE", "40.00 lb PAIL", "2.00 lb CAN QT") ---
    pack = ""
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text,
        re.IGNORECASE,
    )
    if m:
        pack = m.group(1)

    # --- Product name (preferred: parentheses after description lines) ---
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
        # Fallback: heuristics between 'Product' header and 'CUST. NO.'
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

def parse_pdf(pdf_bytes: bytes):
    rows = []
    native_texts = extract_text_plumber(pdf_bytes)
    use_texts = native_texts

    # Auto fallback to OCR if native text looks suspiciously small
    if sum(len(t) for t in native_texts) < 80 and OCR_AVAILABLE:
        ocr_texts = try_ocr(pdf_bytes)
        if sum(len(t) for t in ocr_texts) > sum(len(t) for t in native_texts):
            use_texts = ocr_texts

    for text in use_texts:
        if not text:
            continue
        row = parse_page(text)
        if row:
            rows.append(row)
    return rows

# ---------- UI ----------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    if not OCR_AVAILABLE:
        st.caption("OCR disabled unless Python libs+system tools are installed. Proceeding with native text only."
                   + (f" ({OCR_REASON})" if OCR_REASON else ""))

    all_rows = []
    for uf in uploaded:
        pdf_bytes = uf.read()
        rows = parse_pdf(pdf_bytes)
        if not rows:
            st.warning(f"⚠️ Could not extract any BOL rows from: {uf.name}")
        else:
            st.success(f"✅ Parsed {len(rows)} row(s) from {uf.name}")
            all_rows.extend(rows)

    if all_rows:
        columns = ["Unloaded By","Ship To","Product","Quantity","Packaging","Lot Numbers","BOL #","Notes"]
        df = pd.DataFrame(all_rows, columns=columns)
        try:
            df["__bolnum"] = pd.to_numeric(df["BOL #"], errors="coerce")
            df.sort_values("__bolnum", inplace=True, kind="stable")
            df.drop(columns=["__bolnum"], inplace=True)
        except Exception:
            pass

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True, hide_index=True)

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Inventory", index=False)
            ws = writer.book["Inventory"]
            ws.freeze_panes = "A2"
            widths = {"A":14,"B":22,"C":36,"D":10,"E":22,"F":26,"G":12,"H":22}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w

        st.download_button(
            "⬇️ Download Excel",
            data=output.getvalue(),
            file_name="inventory_rows.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.caption("Drop in your BOL PDFs to get started.")
