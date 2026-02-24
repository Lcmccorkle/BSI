import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# ---------- OCR availability ----------
OCR_AVAILABLE = False
OCR_REASON = ""
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_AVAILABLE = True
except Exception as e:
    OCR_AVAILABLE = False
    OCR_REASON = f"python libs missing ({e.__class__.__name__})"

# ---------- UI ----------
st.set_page_config(page_title="BOL → Inventory Extractor", page_icon="📦", layout="wide")
st.title("📦 Bill of Lading → Inventory Extractor")
st.caption(
    "Upload one or more BOL PDFs. Outputs a printable Excel with columns: "
    "Unloaded By, Ship To, Product, Quantity, Packaging, Lot Numbers, BOL #, Notes."
)
debug_mode = st.checkbox("🔎 Debug Mode (show raw text for pages that fail BOL detection)", value=False)

# ---------- Regex helpers ----------
CITY_STATE_RE = re.compile(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b")
# BOL must be exactly 6 digits (avoid ZIPs etc.)
BOL_LABELED_RE = re.compile(r"(?:B[\/]?\s*L|BOL)\s*(?:NO\.?|#|NUMBER)?\s*[:\-]?\s*([0-9]{6})\b", re.IGNORECASE)

def normalize_text(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def extract_field(pattern, text, default=""):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else default

# ---------- Coordinate-aware BOL ----------
def find_bol_via_words(page) -> str:
    try:
        words = page.extract_words() or []
    except Exception:
        return ""
    toks = [{
        "text": (w.get("text") or "").strip(),
        "x0": float(w.get("x0", 0.0)), "x1": float(w.get("x1", 0.0)),
        "top": float(w.get("top", 0.0)), "bottom": float(w.get("bottom", 0.0))
    } for w in words]
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

        same_row = [t for t in toks if (t["top"] >= y_top - y_tol and t["bottom"] <= y_bottom + y_tol and t["x0"] >= x_right - 2)]
        below = [t for t in toks if (t["top"] > y_bottom and t["top"] <= y_bottom + 2.5 * y_tol)]

        for c in (same_row + below):
            if re.fullmatch(r"[0-9]{6}", c["text"]):
                return c["text"].strip()
    return ""

def guess_bol_from_text(text: str) -> str:
    win = 60
    for lab in (r"B[\/]?\s*L", r"\bBOL\b", r"CUST(?:OMER)?\s+ORDER\s+NUMBER"):
        for m in re.finditer(lab, text, flags=re.IGNORECASE):
            start, end = max(0, m.start()-win), min(len(text), m.end()+win)
            chunk = text[start:end]
            mnum = re.search(r"\b([0-9]{6})\b", chunk)
            if mnum:
                return mnum.group(1)
    nums = re.findall(r"\b([0-9]{6})\b", text)
    if nums:
        from collections import Counter
        return Counter(nums).most_common(1)[0][0]
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

def ship_to_from(page, text: str) -> str:
    lines = text.split("\n")
    candidates = []
    for i, ln in enumerate(lines):
        ln_clean = ln.strip()
        for m in re.finditer(CITY_STATE_RE, ln_clean):
            candidates.append((ln_clean, m.group(1), i))

    def bad_line(ln: str) -> bool:
        L = ln.upper()
        return (L.startswith("FROM:") or L.startswith("AT:")
                or "BARTON SOLVENTS, INC." in L
                or "STRAIGHT BILL OF LADING" in L)

    scored = []
    for (ln, cs, li) in candidates:
        score = 0
        if not cs.endswith("KS"): score += 3
        if not bad_line(ln): score += 2
        if re.search(r"\bUSA\b", ln, re.IGNORECASE) or re.search(r"\b\d{5}(?:-\d{4})?\b", ln): score += 1
        scored.append((score, ln, cs, li))

    if not scored:
        return ""

    best_score = max(s[0] for s in scored)
    best_group = [t for t in scored if t[0] == best_score]

    if page is not None and len(best_group) > 1:
        try:
            words = page.extract_words() or []
        except Exception:
            words = []

        def approx_rightness(ln_text: str) -> float:
            xs = []
            LU = ln_text.upper()
            for w in words:
                wt = (w.get("text") or "").upper()
                if wt and wt in LU:
                    xs.append(float(w.get("x0", 0.0)))
            return max(xs) if xs else 0.0

        best_group.sort(key=lambda t: approx_rightness(t[1]), reverse=True)
        return best_group[0][2].replace("  ", " ").strip()

    return best_group[0][2].replace("  ", " ").strip()

def lot_numbers_from(text: str) -> str:
    lots = re.findall(r"Lot\s*No\.?\s*([A-Za-z0-9\-]+)", text, flags=re.IGNORECASE)
    seen, out = set(), []
    for l in lots:
        if l not in seen:
            seen.add(l); out.append(l)
    return "; ".join(out)

def quantity_from(text: str) -> str:
    # Quantities are small (1–3 digits)
    q = extract_field(r"QUANTITY\s*ORDERED\s*([0-9]{1,3})\b", text)
    if not q:
        q = extract_field(r"Quantity\s*Shipped\s*([0-9]{1,3})\b", text)
    return q or ""

def packaging_from(text: str) -> str:
    m = re.search(r"(\d{1,3}(?:,\d{3})*\.\d+\s*lb\s*(?:DRUM|TOTE|PAIL|CAN(?:\s+QT|\s+Gallon)?))", text, re.IGNORECASE)
    return m.group(1) if m else ""

def product_from(text: str) -> str:
    for pat in [
        r"QUANTITY,\s*\(([^)]+)\)",
        r"NOT REGULATED BY DOT,?\s*\(([^)]+)\)",
        r"UN\d{3,4}[^()\n]*\(([^)]+)\)",
    ]:
        p = extract_field(pat, text)
        if p: return p
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

def ocr_page_texts(pdf_bytes: bytes):
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

def parse_page(text: str, page=None, debug_label: str = ""):
    # BOL: coords → labeled regex (6 digits) → guess (6 digits)
    bol = find_bol_via_words(page) if page is not None else ""
    if not bol:
        m = BOL_LABELED_RE.search(text)
        if m: bol = m.group(1).strip()
    if not bol:
        bol = guess_bol_from_text(text)

    if not bol:
        if debug_mode:
            with st.expander(f"Debug: couldn't find BOL on {debug_label or 'page'} (text)"):
                st.code(text)
        return None

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

    # Auto-OCR only when native text is tiny
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

# ---------- Top-level UI ----------
uploaded = st.file_uploader("Upload BOL PDF(s)", type=["pdf"], accept_multiple_files=True)

if uploaded:
    total = len(uploaded)
    done = 0
    overall = st.progress(0, text=f"Overall: 0/{total} files")

    def bump_overall(): pass

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
        try:
            df["__bolnum"] = pd.to_numeric(df["BOL #"], errors="coerce")
            df.sort_values("__bolnum", inplace=True, kind="stable")
            df.drop(columns=["__bolnum"], inplace=True)
        except Exception:
            pass

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True, hide_index=True)

        out = BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Inventory", index=False)
            ws = writer.book["Inventory"]; ws.freeze_panes = "A2"
            widths = {"A":14,"B":22,"C":36,"D":10,"E":22,"F":26,"G":12,"H":22}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w

        st.download_button("⬇️ Download Excel", out.getvalue(), "inventory_rows.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if not OCR_AVAILABLE:
        st.caption("OCR disabled unless Python libs + system tools are installed. "
                   f"Proceeding with native text only.{(' (' + OCR_REASON + ')') if OCR_REASON else ''}")
else:
    st.caption("Drop in your BOL PDFs to get started.")
