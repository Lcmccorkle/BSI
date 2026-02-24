
import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# ---------------- OCR availability (safe) ----------------
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
st.caption(
    "Upload one or more BOL PDFs. Outputs a printable Excel with columns: "
    "Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes."
)
debug_mode = st.checkbox("🔎 Debug Mode (show raw text only for pages that fail)", value=False)

# ---------------- Regex helpers ----------------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})")
BOL_LABELED_RE = re.compile(
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
    Use pdfplumber word coordinates to find the B/L label and then the BOL number
    to the right (same line) or just below. Return '' if not found.
    """
    try:
        words = page.extract_words() or []
    except Exception:
        return ""

    toks = [
        {
            "text": w.get("text", "").strip(),
            "x0": w.get("x0", 0.0), "x1": w.get("x1", 0.0),
            "top": w.get("top", 0.0), "bottom": w.get("bottom", 0.0)
        }
        for w in words
    ]
    if not toks:
        return ""

    label_idxs = []
    for i, t in enumerate(toks):
        T = t["text"].upper().replace(" ", "")
        if T in ("B/L", "BOL", "B/LNO.", "BOLNO.", "B/LNO", "BOLNO"):
            label_idxs.append(i)
        elif T in ("B/L", "BOL") and i + 1 < len(toks):
            T2 = toks[i+1]["text"].upper().replace(" ", "")
            if T2 in ("NO.", "NO", "#", "NUMBER"):
                label_idxs.append(i)

    for idx in label_idxs:
        label = toks[idx]
        y_top, y_bottom = label["top"], label["bottom"]
        y_tol = (y_bottom - y_top) * 1.8 or 8.0
        x_right = label["x1"]

        same_row = [
            t for t in toks
            if (t["top"] >= y_top - y_tol and t["bottom"] <= y_bottom + y_tol and t["x0"] >= x_right - 2)
        ]
        below = [
            t for t in toks
            if (t["top"] > y_bottom and t["top"] <= y_bottom + 2.5 * y_tol)
        ]
        for c in (same_row + below):
            if re.fullmatch(r"[0-9]{5,7}", c["text"]):
                return c["text"].strip()

    return ""

def _guess_bol_from_text(text: str) -> str:
    """
    Smart fallback: choose a 6-digit ID that appears near B/L or CUST ORDER NUMBER lines,
    or any 6–7 digit token repeated ≥2 times. Avoid 10-digit phone numbers automatically.
    """
    # 1) Look near explicit labels
    win = 70
    for lab in (r"B[\/]?\s*L", r"\bBOL\b", r"CUST(?:OMER)?\s+ORDER\s+NUMBER"):
        for m in re.finditer(lab, text, flags=re.IGNORECASE):
            start = max(0, m.start() - win)
            end = min(len(text), m.end() + win)
            chunk = text[start:end]
            mnum = re.search(r"\b([0-9]{5,7})\b", chunk)
            if mnum:
                return mnum.group(1)

    # 2) Frequency vote among 5–7 digit tokens
    nums = re.findall(r"\b([0-9]{5,7})\b", text)
    if nums:
        from collections import Counter
        cnt = Counter(nums)
        common = cnt.most_common(1)[0]
        if common[1] >= 2:
            return common[0]
        # if nothing repeats, pick the first (still better than empty)
        return nums[0]

    return ""

def _looks_like_product(s: str) -> bool:
    S = s.upper()
    blocked = (
        S.startswith("FROM:") or S.startswith("AT:") or
        "STRAIGHT BILL OF LADING" in S or
        "BARTON SOLVENTS, INC." in S or
        "CARRIER" in S or "DELIVERY" in S or "WAREHOUSE" in S or
        "QUANTITY" in S or "PACKAGING" in S or "DESCRIPTION" in S
    )
    return (not blocked) and (2 < len(s) <= 60) and re.search(r"[A-Za-z]", s)

def _ship_to_from(text: str) -> str:
    # Choose last City, ST that isn't KS and isn't on a FROM/AT/header line.
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
        return filtered[-1].replace("  ", " ").strip()
    if candidates:
        return candidates[-1][1].replace("  ", " ").strip()
    return ""

def _lot_numbers_from(text: str) -> str:
    lots = re.findall(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", text, flags=re.IGNORECASE)
    seen, order = set(), []
    for l in lots:
        if l not in seen:
            seen.add(l)
            order.append(l)
    return "; ".join(order)

def _quantity_from(text: str) -> str:
    # keep to 1–4 digits; avoids swallowing long part numbers like 1600088
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9]{1,4})\b", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9]{1,4})\b", text)
    return qty or ""

def _packaging_from(text: str) -> str:
    # e.g., "435.00 lb DRUM", "2,344.00 lb TOTE", "40.00 lb PAIL", "2.00 lb CAN QT"
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text, re.IGNORECASE,
    )
    return m.group(1) if m else ""

def _product_from(text: str) -> str:
    for pat in [
        r"QUANTITY,\s*\(([^)]+)\)",
        r"NOT REGULATED BY DOT,?\s*\(([^)]+)\)",
        r"UN\d{3,4}[^()\n]*\(([^)]+)\)",
    ]:
        p = extract_field(pat, text)
        if p:
            return p
    # fallback window: between 'Product' header and 'CUST. NO.'
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
            return s
    return ""

# ---------------- OCR per page ----------------
def try_ocr_page_texts(pdf_bytes: bytes):
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

# ---------------- Parse one page ----------------
def parse_page(text: str, page=None, debug_label: str = ""):
    # 1) BOL: coordinates → labeled regex → smart guess
    bol = ""
    if page is not None:
        bol = _find_bol_via_words(page)
    if not bol:
        m = BOL_LABELED_RE.search(text)
        if m:
            bol = m.group(1).strip()
    if not bol:
        bol = _guess_bol_from_text(text)

    if not bol:
        if debug_mode:
            with st.expander(f"Debug: couldn't find BOL on {debug_label or 'page'} (showing text)"):
                st.code(text)
        return None

    # 2) Remaining fields
    ship_to = _ship_to_from(text)
    lot = _lot_numbers_from(text)
    qty = _quantity_from(text)
    pack = _packaging_from(text)
    product = _product_from(text)

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

# ---------------- Parse a PDF with progress ----------------
def parse_pdf_with_progress(pdf_bytes: bytes, file_label: str, show_overall=None):
    rows = []

    # Native text (and page objects) first
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages) or 1
            box = st.container()
            box.markdown(f"**Processing:** {file_label} · {total_pages} page(s)")
            pbar = box.progress(0, text="Reading pages…")

            native_texts = []
            for i, pg in enumerate(pdf.pages, start=1):
                t = normalize_text(pg.extract_text() or "")
                native_texts.append(t)
                if t:
                    row = parse_page(t, page=pg, debug_label=f"{file_label} · page {i}")
                    if row:
                        rows.append(row)
                pbar.progress(min(i/total_pages, 1.0), text=f"Reading pages… ({i}/{total_pages})")
                if show_overall: show_overall()

    except Exception as e:
        st.error(f"Failed to read {file_label}: {e}")
        return rows

    # Auto‑OCR only if native text is suspiciously small AND OCR available
    if sum(len(t) for t in native_texts) < 80 and OCR_AVAILABLE:
        try:
            pbar.progress(0.0, text="Running OCR…")
        except Exception:
            pass
        ocr_texts = try_ocr_page_texts(pdf_bytes)
        ocr_rows = []
        for i, t in enumerate(ocr_texts, start=1):
            if t:
                row = parse_page(t, page=None, debug_label=f"{file_label} · OCR page {i}")
                if row:
                    ocr_rows.append(row)
            try:
                pbar.progress(min(i/(len(ocr_texts) or 1), 1.0), text=f"OCR {i}/{len(ocr_texts) or 1}")
            except Exception:
                pass
            if show_overall: show_overall()
        if len(ocr_rows) > len(rows):
            rows = ocr_rows

    try:
        pbar.progress(1.0, text="Done")
    except Exception:
        pass

    return rows

# ---------------- UI: upload, parse, export ----------------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    total = len(uploaded)
    done = 0
    overall = st.progress(0, text=f"Overall: 0/{total} files")

    def bump_overall():
        # reserved for future per-page updates
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
        done += 1
        overall.progress(done/total, text=f"Overall: {done}/{total} files")

    if all_rows:
        cols = ["Unloaded By","Ship To","Product","Quantity","Packaging","Lot Numbers","BOL #","Notes"]
        df = pd.DataFrame(all_rows, columns=cols)
        # Sort by numeric BOL if possible
        try:
            df["__bolnum"] = pd.to_numeric(df["BOL #"], errors="coerce")
            df.sort_values("__bolnum", inplace=True, kind="stable")
            df.drop(columns=["__bolnum"], inplace=True)
        except Exception:
            pass

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Excel export
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
            "OCR disabled unless Python libs + system tools are installed. "
            f"Proceeding with native text only.{(' (' + OCR_REASON + ')') if OCR_REASON else ''}"
        )
else:
    st.caption("Drop in your BOL PDFs to get started.")
