import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# ---------------- OCR availability (safe check) ----------------
OCR_AVAILABLE = False
OCR_REASON = ""
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except Exception as e:
    OCR_AVAILABLE = False
    OCR_REASON = f"python libs missing ({e.__class__.__name__})"

# ---------------- Streamlit page config ----------------
st.set_page_config(page_title="BOL → Inventory Extractor", page_icon="📦", layout="wide")
st.title("📦 Bill of Lading → Inventory Extractor")
st.write(
    "Upload one or more BOL PDFs. I’ll extract fields and give you a printable Excel "
    "with columns: **Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes**."
)

# Debug toggle (shows raw text for pages with no BOL match)
debug_mode = st.checkbox("🔎 Debug Mode (show raw page text when a page can’t be parsed)", value=False)

# ---------------- Regex helpers (use raw strings with single backslashes) ----------------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})")
# We use this only as a fallback when coordinate search fails
BOL_BLOCK_RE = re.compile(
    r"(?:B[\/]?\s*L|BOL)\s*(?:NO\.?|#|NUMBER)?\s*[:\-]?\s*([0-9]{5,7})\b",
    re.IGNORECASE
)

def normalize_text(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def extract_field(pattern, text, default=""):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else default

# ---------------- Coordinate-aware BOL detection ----------------
def _find_bol_via_words(page) -> str:
    """
    Use pdfplumber's word coordinates to find the B/L label and the BOL number
    that appears to the right (same row) or just below it. Returns '' if not found.
    """
    try:
        words = page.extract_words() or []
    except Exception:
        return ""

    # Normalize word tokens
    tokens = [
        {
            "text": w.get("text", "").strip(),
            "x0": w.get("x0", 0.0), "x1": w.get("x1", 0.0),
            "top": w.get("top", 0.0), "bottom": w.get("bottom", 0.0)
        }
        for w in words
    ]
    if not tokens:
        return ""

    # Find plausible label tokens: "B/L", "B L", "BOL", optionally followed by "NO."
    label_idxs = []
    for i, t in enumerate(tokens):
        T = t["text"].upper().replace(" ", "")
        if T in ("B/L", "BOL", "B/LNO.", "BOLNO.", "B/LNO", "BOLNO"):
            label_idxs.append(i)
        elif T in ("B/L", "BOL") and i + 1 < len(tokens):
            T2 = tokens[i+1]["text"].upper().replace(" ", "")
            if T2 in ("NO.", "NO", "#", "NUMBER"):
                label_idxs.append(i)

    # Search rightwards (same line) then slightly below the label
    for idx in label_idxs:
        label = tokens[idx]
        y_top, y_bottom = label["top"], label["bottom"]
        y_tol = (y_bottom - y_top) * 1.8 or 8.0  # vertical tolerance
        x_right = label["x1"]

        # Same row candidates: to the right of label, within y tolerance
        row_candidates = [
            t for t in tokens
            if (t["top"] >= y_top - y_tol and t["bottom"] <= y_bottom + y_tol and t["x0"] >= x_right - 2)
        ]
        # If not on same row, search slightly below
        below_candidates = [
            t for t in tokens
            if (t["top"] > y_bottom and t["top"] <= y_bottom + 2.5 * y_tol)
        ]
        candidates = row_candidates + below_candidates

        # Typical BOL length in your docs: 5–7 digits
        for c in candidates:
            if re.fullmatch(r"[0-9]{5,7}", c["text"]):
                return c["text"].strip()

    return ""

# ---------------- Product name heuristic ----------------
def _looks_like_product(s: str) -> bool:
    S = s.upper()
    # Exclude obvious non-product lines
    blocked = (
        S.startswith("FROM:") or S.startswith("AT:") or
        "STRAIGHT BILL OF LADING" in S or
        "BARTON SOLVENTS, INC." in S or
        "CARRIER" in S or "DELIVERY" in S or "WAREHOUSE" in S or
        "QUANTITY" in S or "PACKAGING" in S or "DESCRIPTION" in S
    )
    return (not blocked) and (2 < len(s) <= 60) and re.search(r"[A-Za-z]", s)

# ---------------- OCR (page-by-page) ----------------
def try_ocr_page_texts(pdf_bytes: bytes):
    """
    Generate OCR text per page. If OCR isn't available at runtime,
    return an empty list and keep the app stable.
    """
    if not OCR_AVAILABLE:
        return []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
        texts = []
        for img in images:
            t = pytesseract.image_to_string(img, config="--psm 6")
            texts.append(normalize_text(t or ""))
        return texts
    except Exception as e:
        global OCR_REASON
        OCR_REASON = f"OCR runtime error ({e.__class__.__name__}): {e}"
        return []

# ---------------- Page parser ----------------
def parse_page(text: str, page=None, debug_label: str = ""):
    # ---- BOL # (coordinate-aware first; regex fallback) ----
    bol = ""
    if page is not None:
        bol = _find_bol_via_words(page)

    if not bol:
        m = BOL_BLOCK_RE.search(text)
        if m:
            bol = m.group(1).strip()
        else:
            m2 = re.search(r"(?:B[\/]?\s*L|BOL)[^\n]{0,60}?([0-9]{5,7})\b", text, re.IGNORECASE)
            if m2:
                bol = m2.group(1).strip()

    if not bol:
        if debug_mode:
            with st.expander(f"Debug: couldn't find BOL on {debug_label or 'page'} (showing text)"):
                st.code(text)
        return None

    # ---- Ship To (prefer last non-KS City, ST; ignore FROM/AT/header lines) ----
    ship_to = ""
    candidates = []
    for ln in text.split("\n"):
        ln_clean = ln.strip()
        for mcs in re.finditer(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", ln_clean):
            candidates.append((ln_clean, mcs.group(1)))
    filtered = [cs for (ln, cs) in candidates
                if not cs.endswith("KS")
                and not ln.upper().startswith("FROM:")
                and not ln.upper().startswith("AT:")
                and "BARTON SOLVENTS, INC." not in ln.upper()
                and "STRAIGHT BILL OF LADING" not in ln.upper()]
    if filtered:
        ship_to = filtered[-1].replace("  ", " ").strip()
    elif candidates:
        ship_to = candidates[-1][1].replace("  ", " ").strip()

    # ---- Lot Numbers ----
    lots = re.findall(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", text, flags=re.IGNORECASE)
    seen, lot_list = set(), []
    for l in lots:
        if l not in seen:
            seen.add(l)
            lot_list.append(l)
    lot = "; ".join(lot_list)

    # ---- Quantity (keep to 1–4 digits to avoid part/SKU codes) ----
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9]{1,4})\b", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9]{1,4})\b", text)

    # ---- Packaging (e.g., "435.00 lb DRUM", "2,344.00 lb TOTE", "40.00 lb PAIL", "2.00 lb CAN QT") ----
    pack = ""
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text, re.IGNORECASE,
    )
    if m:
        pack = m.group(1)

    # ---- Product (prefer parentheses after description; fallback filters header/legal lines) ----
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
            if _looks_like_product(s):
                product = s
                break

    return {
        "Unloaded By": "",
        "Ship To": ship_to,
        "Product": product,
        "Quantity": qty or "",
        "Packaging": pack,
        "Lot Numbers": lot,
        "BOL #": bol,
        "Notes": "",
    }

# ---------------- Single-PDF parser with per-file progress ----------------
def parse_pdf_with_progress(pdf_bytes: bytes, file_label: str, show_overall=None):
    rows = []

    # Open once so we can access page objects (for coordinate-aware BOL)
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages) or 1
            file_box = st.container()
            file_box.markdown(f"**Processing:** {file_label} · {total_pages} page(s)")
            pbar = file_box.progress(0, text="Reading pages…")

            native_len_sum = 0
            native_texts = []

            for i, page_obj in enumerate(pdf.pages, start=1):
                t = page_obj.extract_text() or ""
                t = normalize_text(t)
                native_texts.append(t)
                native_len_sum += len(t)

                if t:
                    row = parse_page(t, page=page_obj, debug_label=f"{file_label} · page {i}")
                    if row:
                        rows.append(row)

                pbar.progress(min(i / total_pages, 1.0), text=f"Reading pages… ({i}/{total_pages})")
                if show_overall:
                    show_overall()

    except Exception as e:
        st.error(f"Failed to read {file_label}: {e}")
        return rows

    # Auto OCR fallback (only if native text looks suspiciously small and OCR is available)
    if sum(len(t) for t in native_texts) < 80 and OCR_AVAILABLE:
        pbar.progress(0.0, text="Running OCR…")
        ocr_texts = try_ocr_page_texts(pdf_bytes)
        ocr_rows = []
        for i, t in enumerate(ocr_texts, start=1):
            if t:
                # No page object available after OCR; pass None (regex fallback will be used)
                row = parse_page(t, page=None, debug_label=f"{file_label} · OCR page {i}")
                if row:
                    ocr_rows.append(row)
            pbar.progress(min(i / (len(ocr_texts) or 1), 1.0), text=f"OCR {i}/{len(ocr_texts) or 1}")
            if show_overall:
                show_overall()
        if len(ocr_rows) > len(rows):
            rows = ocr_rows

    # finalize per-file bar (if we got here via the native block, pbar exists)
    try:
        pbar.progress(1.0, text="Done")
    except Exception:
        pass

    return rows

# ---------------- UI: multi-file upload, overall progress, export ----------------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    total_files = len(uploaded)
    done_files = 0
    overall = st.progress(0, text=f"Overall: 0/{total_files} files")

    def bump_overall():
        # reserved for per-page overall updates (optional)
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

        done_files += 1
        overall.progress(done_files / total_files, text=f"Overall: {done_files}/{total_files} files")

    if all_rows:
        columns = ["Unloaded By","Ship To","Product","Quantity","Packaging","Lot Numbers","BOL #","Notes"]
        df = pd.DataFrame(all_rows, columns=columns)

        # Sort by numeric BOL if possible (keeps stable order otherwise)
        try:
            df["__bolnum"] = pd.to_numeric(df["BOL #"], errors="coerce")
            df.sort_values("__bolnum", inplace=True, kind="stable")
            df.drop(columns=["__bolnum"], inplace=True)
        except Exception:
            pass

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Excel export (print-friendly)
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

    if not OCR_AVAILABLE:
        st.caption(
            "OCR is disabled unless Python libs + system tools are installed. "
            f"Proceeding with native text only.{(' (' + OCR_REASON + ')') if OCR_REASON else ''}"
        )

else:
    st.caption("Drop in your BOL PDFs to get started.")
