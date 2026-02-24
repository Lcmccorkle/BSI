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

def get_page_count(pdf_bytes: bytes) -> int:
    """Count pages via pdfplumber (works for both text & scanned PDFs)."""
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0

def extract_text_plumber_per_page(pdf_bytes: bytes):
    """Yield normalized text for each page with generator semantics."""
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            yield normalize_text(t)

def try_ocr_page_images(pdf_bytes: bytes):
    """Yield OCR text per page as images are generated (if OCR available)."""
    # This will raise if Poppler/Tesseract aren't available; caller catches and disables OCR.
    images = convert_from_bytes(pdf_bytes, dpi=300)
    for img in images:
        t = pytesseract.image_to_string(img, config="--psm 6")
        yield normalize_text(t or "")

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
        m2 = re.search(r"(?:B[\\/]?\\s*L|BOL)[^\n]{0,80}?(\d{5,})", text, re.IGNORECASE)
        if m2:
            bol = m2.group(1).strip()
    if not bol:
        return None

    # --- Ship To (prefer last non-KS to avoid origin address) ---
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

    # --- Quantity (ordered or shipped) ---
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9,]+)", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9,]+)", text)
    qty = qty.replace(",", "") if qty else ""

    # --- Packaging (e.g., "435.00 lb DRUM") ---
    pack = ""
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text, re.IGNORECASE,
    )
    if m:
        pack = m.group(1)

    # --- Product name (prefer parentheses after description lines) ---
    product = ""
    for pat in [
        r"QUANTITY,\s*\(([^)]+)\)",
        r"NOT REGULATED BY DOT,?\s*\(([^)]+)\)",
        r"UN\d{3,4}[^()\n]*\(([^)]+)\)",
    ]:
        product = extract_field(pat, text)
        if product:
            break

    # Fallback: between 'Product' and 'CUST. NO.'
    if not product:
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

def parse_pdf_with_progress(pdf_bytes: bytes, file_label: str, show_overall=None):
    """
    Parse a single PDF and update a per-file progress bar page-by-page.
    `show_overall` is an optional callback to bump the overall progress.
    """
    rows = []
    total_pages = get_page_count(pdf_bytes) or 1  # avoid division by zero
    file_box = st.container()
    file_box.markdown(f"**Processing:** {file_label} · {total_pages} page(s)")
    pbar = file_box.progress(0, text="Reading pages…")

    # 1) Native text pass (streaming page by page)
    native_len_sum = 0
    native_texts = []
    for i, t in enumerate(extract_text_plumber_per_page(pdf_bytes), start=1):
        native_texts.append(t)
        native_len_sum += len(t)
        # Try to parse as we go
        if t:
            row = parse_page(t)
            if row:
                rows.append(row)
        # Update per-file progress
        pbar.progress(min(i / total_pages, 1.0), text=f"Reading pages… ({i}/{total_pages})")
        if show_overall:
            show_overall()

    # 2) Decide if OCR fallback is worthwhile (only if OCR available)
    need_ocr = (native_len_sum < 80) and OCR_AVAILABLE
    if need_ocr:
        pbar.progress(0.0, text="Running OCR…")
        try:
            ocr_rows = []
            for i, t in enumerate(try_ocr_page_images(pdf_bytes), start=1):
                # Parse OCR text per page
                if t:
                    row = parse_page(t)
                    if row:
                        ocr_rows.append(row)
                pbar.progress(min(i / total_pages, 1.0), text=f"OCR {i}/{total_pages}")
                if show_overall:
                    show_overall()

            # If OCR produced more data than native, prefer OCR result
            if len(ocr_rows) > len(rows):
                rows = ocr_rows

        except Exception as e:
            # OCR disabled during run; show note but continue with native results
            st.caption(f"⚠️ OCR unavailable for {file_label}. Proceeding with native text only. ({e})")

    # Finalize bar
    pbar.progress(1.0, text="Done")
    return rows

# ---------- UI ----------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    # Overall progress bar across files (optional, looks nice when many files)
    total_files = len(uploaded)
    done_files = 0
    overall = st.progress(0, text=f"Overall: 0/{total_files} files")

    def bump_overall():
        # Called per page; we only advance on file completion to avoid jitter,
        # so do nothing here. (Kept for future refinements.)
        pass

    all_rows = []
    for uf in uploaded:
        pdf_bytes = uf.read()
        rows = parse_pdf_with_progress(pdf_bytes, uf.name, show_overall=bump_overall)
        if not rows:
            st.warning(f"⚠️ Could not extract any BOL rows from: {uf.name}")
        else:
            st.success(f"✅ Parsed {len(rows)} row(s) from {uf.name}")
            all_rows.extend(rows)
        # Advance overall on file completion
        done_files += 1
        overall.progress(done_files / total_files, text=f"Overall: {done_files}/{total_files} files")

    # Results → Excel
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

    # Informational note about OCR availability
    if not OCR_AVAILABLE:
        st.caption(
            "OCR is disabled unless Python libs + system tools are installed. "
            f"Proceeding with native text only.{(' (' + OCR_REASON + ')') if OCR_REASON else ''}"
        )

else:
    st.caption("Drop in your BOL PDFs to get started.")
