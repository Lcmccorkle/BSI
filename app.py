
import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --------------- OCR availability (safe) ---------------
OCR_AVAILABLE = False
OCR_REASON = ""
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except Exception as e:
    OCR_AVAILABLE = False
    OCR_REASON = f"python libs missing ({e.__class__.__name__})"

# --------------- Streamlit page config ---------------
st.set_page_config(page_title="BOL → Inventory Extractor", page_icon="📦", layout="wide")
st.title("📦 Bill of Lading → Inventory Extractor")
st.caption(
    "Upload one or more BOL PDFs. Outputs a printable Excel with columns: "
    "Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes."
)
debug_mode = st.checkbox("🔎 Debug Mode (show raw text for pages that fail BOL detection)", value=False)

# --------------- Regex helpers (single backslashes) ---------------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b")
# Only allow exactly 6 digits for BOL to avoid grabbing ZIP codes like 11207
BOL_LABELED_RE = re.compile(
    r"(?:B[\/]?\s*L|BOL)\s*(?:NO\.?|#|NUMBER)?\s*[:\-]?\s*([0-9]{6})\b",
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
def find_bol_via_words(page) -> str:
    """
    Use pdfplumber word boxes to find the B/L label then the BOL number
    to the right (same row) or slightly below. Return '' if not found.
    """
    try:
        words = page.extract_words() or []
    except Exception:
        return ""

    toks = [
        {
            "text": w.get("text", "").strip(),
            "x0": float(w.get("x0", 0.0)), "x1": float(w.get("x1", 0.0)),
            "top": float(w.get("top", 0.0)), "bottom": float(w.get("bottom", 0.0))
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

        # Only accept exactly 6 digits
        for c in (same_row + below):
            if re.fullmatch(r"[0-9]{6}", c["text"]):
                return c["text"].strip()

    return ""

def guess_bol_from_text(text: str) -> str:
    """
    Fallback: look in a tight window around B/L or CUST ORDER NUMBER for 6 digits only.
    If none, pick the most common 6-digit token on the page. Never 5 digits.
    """
    win = 60
    for lab in (r"B[\/]?\s*L", r"\bBOL\b", r"CUST(?:OMER)?\s+ORDER\s+NUMBER"):
        for m in re.finditer(lab, text, flags=re.IGNORECASE):
            start = max(0, m.start() - win)
            end = min(len(text), m.end() + win)
            chunk = text[start:end]
            mnum = re.search(r"\b([0-9]{6})\b", chunk)
            if mnum:
                return mnum.group(1)

    nums = re.findall(r"\b([0-9]{6})\b", text)
    if nums:
        from collections import Counter
        cnt = Counter(nums).most_common(1)[0]
        return cnt[0]
    return ""

def looks_like_product(s: str) -> bool:
    S = s.upper()
    blocked = (
        S.startswith("FROM:") or S.startswith("AT:") or
        "STRAIGHT BILL OF LADING" in S or
        "BARTON SOLVENTS, INC." in S or
        "CARRIER" in S or "DELIVERY" in S or "WAREHOUSE" in S or
        "QUANTITY" in S or "PACKAGING" in S or "DESCRIPTION" in S
    )
    return (not blocked) and (2 < len(s) <= 60) and re.search(r"[A-Za-z]", s)

# --------- Ship To: score candidates & prefer rightmost non-KS destination ---------
def ship_to_from(page, text: str) -> str:
    """
    Score City, ST candidates. Prefer non-KS, not on FROM/AT/header lines.
    If several tie, choose the rightmost by x0 (dest block tends to be right/upper right).
    """
    # Gather candidates by line
    candidates = []  # (line_text, cityst, line_index)
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        ln_clean = ln.strip()
        for m in re.finditer(CITY_STATE_RE, ln_clean):
            candidates.append((ln_clean, m.group(1), i))

    def is_bad_line(ln: str) -> bool:
        L = ln.upper()
        return (
            L.startswith("FROM:") or L.startswith("AT:") or
            "BARTON SOLVENTS, INC." in L or "STRAIGHT BILL OF LADING" in L
        )

    # Assign a score to each candidate
    scored = []
    for (ln, cs, li) in candidates:
        score = 0
        if not cs.endswith("KS"):
            score += 3  # prefer non-KS
        if not is_bad_line(ln):
            score += 2  # avoid origin/header
        # +1 if line contains "USA" or a ZIP (5 digits), typical of full address blocks
        if re.search(r"\bUSA\b", ln, re.IGNORECASE) or re.search(r"\b\d{5}(?:-\d{4})?\b", ln):
            score += 1
        scored.append((score, ln, cs, li))

    if not scored:
        return ""

    # Among ties, prefer rightmost (use coordinates if available)
    best = max(scored, key=lambda x: x[0])
    best_score = best[0]
    best_group = [t for t in scored if t[0] == best_score]

    if page is not None and len(best_group) > 1:
        # Try to disambiguate by x0 position: choose the rightmost on the page
        try:
            words = page.extract_words() or []
        except Exception:
            words = []
        def line_x0(ln_text: str) -> float:
            # approximate: min x0 of all tokens from the line that appear in words
            xs = []
            ln_upper = ln_text.upper()
            for w in words:
                wt = (w.get("text") or "").upper()
                if wt and wt in ln_upper:
                    xs.append(float(w.get("x0", 0.0)))
            return max(xs) if xs else 0.0  # choose rightmost presence
        best_group.sort(key=lambda t: line_x0(t[1]), reverse=True)
        return best_group[0][2].replace("  ", " ").strip()

    # Otherwise, just take the first best candidate
    return best_group[0][2].replace("  ", " ").strip()

def lot_numbers_from(text: str) -> str:
    lots = re.findall(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", text, flags=re.IGNORECASE)
    seen, order = set(), []
    for l in lots:
        if l not in seen:
            seen.add(l)
            order.append(l)
    return "; ".join(order)

def quantity_from(text: str) -> str:
    # BOL pages show small counts like 1, 2, 6, 8, 12, 30, etc.
    qty = extract_field(r"QUANTITY\s*ORDERED\s*([0-9]{1,3})\b", text)
    if not qty:
        qty = extract_field(r"Quantity\s*Shipped\s*([0-9]{1,3})\b", text)
    return qty or ""

def packaging_from(text: str) -> str:
    m = re.search(
        r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))",
        text, re.IGNORECASE,
    )
    return m.group(1) if m else ""

def product_from(text: str) -> str:
    for pat in [
        r"QUANTITY,\s*\(([^)]+)\)",
        r"NOT REGULATED BY DOT,?\s*\(([^)]+)\)",
        r"UN\d{3,4}[^()\n]*\(([^)]+)\)",
    ]:
        p = extract_field(pat, text)
        if p:
            return p
    # fallback: between 'Product' and 'CUST. NO.' with filters
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
        if looks_like_product(s):
            return s
    return ""

# ---------------- OCR per page ----------------
def ocr_page_texts(pdf_bytes: bytes):
    if not OCR_AVAILABLE:
        return []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
        out = []
        for img in images:
            t = pytesseract.image_to_string(img, config="--psm 6")
            out.append(normalize_text(t or ""))
        return out
    except Exception as e:
        global OCR_REASON
        OCR_REASON = f"OCR runtime error ({e.__class__.__name__}): {e}"
        return []

# ---------------- Parse one page ----------------
def parse_page(text: str, page=None, debug_label: str = ""):
    # 1) BOL: coordinates → labeled regex (6 digits) → smart guess (6 digits)
    bol = ""
    if page is not None:
        bol = find_bol_via_words(page)
    if not bol:
        m = BOL_LABELED_RE.search(text)
        if m:
            bol = m.group(1).strip()
    if not bol:
        bol = guess_bol_from_text(text)

    if not bol:
        if debug_mode:
            with st.expander(f"Debug: couldn't find BOL on {debug_label or 'page'} (showing text)"):
                st.code(text)
        return None

    # 2) Remaining fields (tightened)
    ship_to = ship_to_from(page, text)
    lot = lot_numbers_from(text)
    qty = quantity_from(text)
    pack = packaging_from(text)
    product = product_from(text)

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
        ocr_texts = ocr_page_texts(pdf_bytes)
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
