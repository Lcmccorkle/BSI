import streamlit as st
import pandas as pd
import re
from io import StringIO
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image

# Set path to Tesseract executable (update if necessary)
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'  # Windows example; adjust for your OS

# ────────────────────────────────────────────────
# Helper function to parse one BOL text block (unchanged, but tested on sample BOLs)
# ────────────────────────────────────────────────
def parse_bol_text(text_block: str) -> dict | None:
    text = text_block.strip()
    if not text:
        return None

    data = {}

    # BOL number (look for 6-digit near top like 636845, 637042, etc.)
    bol_match = re.search(r'\b(63[0-9]{4})\b', text)
    if bol_match:
        data['BOL #'] = bol_match.group(1)

    # Date (BIL.DATE / 02/19/2026 or similar)
    date_match = re.search(r'(?:BIL\.DATE|BL\.DATE|DELIVERY\s*DATE).*?(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE | re.DOTALL)
    if date_match:
        data['Date'] = date_match.group(1)
    else:
        date_match2 = re.search(r'(\d{2}/\d{2}/2026)', text)
        if date_match2:
            data['Date'] = date_match2.group(1)

    # Product name (very common patterns)
    product_patterns = [
        r'Product\s*[\r\n]+.*?([A-Za-z0-9\- /()]+?)\s*(?:\bQuantity|\bTransporter|\bDM|\bEL|\bKC|\b(?:Barsol|BARSOL|Showtime|D-Limonene|PMX|Mineral))',
        r'(Barsol\s+[A-Z0-9\-/]+)\b',
        r'(D-Limonene\s+Industrial)',
        r'(Showtime\s+Tire\s+Dressing\s*\(S-[0-9]+\))',
        r'(Mineral\s+Spirits\s+63%)',
    ]
    for pat in product_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            prod = m.group(1).strip().replace('\n', ' ')
            data['Product'] = prod
            break

    # Quantity shipped (look near Quantity Shipped / TOTAL / Qty Open)
    qty_patterns = [
        r'(?:Quantity\s+Shipped|QUANTITY\s+OPEN|ORDERED|Shipped)\s*[:\s]*(\d+)\s',
        r'Total\s+[:\s]*(\d{1,5})\s',
        r'\b(\d+)\s+(?:DRUM|TOTE|CAN|PAIL|DRUMS|TOTES)\b',
    ]
    for pat in qty_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            data['Quantity'] = int(m.group(1))
            break

    # Packaging
    pkg_match = re.search(r'(DRUM|TOTE|CAN|PAIL|QT|GALLON|DRUMS|TOTES|CANS)\b.*?(?:lb|\()?\s*([\d,.]+)?\s*(lb|kg|GAL|QT)?', text, re.IGNORECASE)
    if pkg_match:
        data['Packaging'] = f"{pkg_match.group(1)} ({pkg_match.group(2) or ''} {pkg_match.group(3) or ''})".strip()

    # Lot / Lot No. / Whse Lot / KC- / EL- / 6xx
    lot_match = re.search(r'(?:Lot|LOT|Wha|Whe|Lot No\.?|No\.?|KC-|EL-)\s*(?:No\.?|#:?)?\s*([A-Za-z0-9\-; ,]+?)(?:\s|$|\n)', text, re.IGNORECASE)
    if lot_match:
        data['Lot Numbers'] = lot_match.group(1).strip().replace('\n', '; ')

    # Destination / Ship To
    dest_match = re.search(r'(Bettendorf|Des Moines|West Bend|El Dorado|(?:IA|WI))\b.*?(?:,|\s)(IA|WI|KS)?', text, re.IGNORECASE)
    if dest_match:
        city = dest_match.group(1).strip()
        state = dest_match.group(2) or ""
        data['Ship To'] = f"{city}, {state}".strip(", ")

    # Fallback: weight based destination clues
    if 'West Bend' in text:
        data['Ship To'] = "West Bend, WI"
    elif 'Des Moines' in text or 'Broadway' in text:
        data['Ship To'] = "Des Moines, IA"
    elif 'Bettendorf' in text:
        data['Ship To'] = "Bettendorf, IA"

    if len(data) < 4:  # too little info → skip
        return None

    # Fill missing with defaults/placeholders
    data.setdefault('Unloaded By', '')
    data.setdefault('Notes', '')
    return data


# ────────────────────────────────────────────────
# Function to extract text from PDF using OCR
# ────────────────────────────────────────────────
def extract_text_from_pdf(pdf_bytes):
    try:
        images = convert_from_bytes(pdf_bytes)
        full_text = ""
        for page_num, img in enumerate(images, start=1):
            text = pytesseract.image_to_string(img)
            full_text += f"--- Page {page_num} ---\n{text}\n"
        return full_text
    except Exception as e:
        st.error(f"Error extracting text from PDF: {e}")
        return ""


# ────────────────────────────────────────────────
# Main Streamlit App
# ────────────────────────────────────────────────
st.title("Inventory Dashboard – Barton Solvents Shipments")

st.markdown("""
Upload your current inventory Excel and drag/drop or upload PDF Bills of Lading.  
The app will OCR the PDFs, parse shipments, and append to the inventory.
""")

# 1. Load existing sorted inventory Excel
DEFAULT_EXCEL = "formatted_inventory_sorted.xlsx"

uploaded_excel = st.file_uploader("Upload your current inventory Excel (xlsx)", type=["xlsx"])
if uploaded_excel is not None:
    df_inventory = pd.read_excel(uploaded_excel)
else:
    try:
        df_inventory = pd.read_excel(DEFAULT_EXCEL)
        st.info(f"Using default file: {DEFAULT_EXCEL}")
    except FileNotFoundError:
        st.warning("Default Excel not found. Starting with empty inventory.")
        df_inventory = pd.DataFrame(columns=["Unloaded By", "Ship To", "Product", "Quantity", "Packaging", "Lot Numbers", "BOL #", "Notes"])

# Show current inventory
st.subheader("Current Inventory")
st.dataframe(df_inventory, use_container_width=True)

# 2. Upload PDFs for new BOLs
uploaded_pdfs = st.file_uploader("Upload PDF Bill(s) of Lading (supports multiple files)", type=["pdf"], accept_multiple_files=True)

if uploaded_pdfs:
    new_rows = []
    for pdf in uploaded_pdfs:
        st.info(f"Processing {pdf.name}...")
        pdf_bytes = pdf.read()
        bol_text = extract_text_from_pdf(pdf_bytes)
        if not bol_text:
            continue

        # Split into pages (assuming OCR added --- Page X --- separators)
        blocks = re.split(r'--- Page \d+ ---', bol_text)
        for block in blocks:
            if len(block.strip()) < 100:
                continue
            parsed = parse_bol_text(block)
            if parsed:
                new_rows.append(parsed)

    if new_rows:
        df_new = pd.DataFrame(new_rows)
        # Reorder columns to match example
        cols_order = ["Unloaded By", "Ship To", "Product", "Quantity", "Packaging", "Lot Numbers", "BOL #", "Notes"]
        for c in cols_order:
            if c not in df_new.columns:
                df_new[c] = ""
        df_new = df_new[cols_order]

        # Append to existing
        df_updated = pd.concat([df_inventory, df_new], ignore_index=True)

        st.success(f"Added {len(new_rows)} new shipment record(s) from PDFs!")

        st.subheader("Updated Inventory Preview")
        st.dataframe(df_updated, use_container_width=True)

        # Download option
        csv = df_updated.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="Download Updated Inventory as CSV",
            data=csv,
            file_name="updated_inventory.csv",
            mime="text/csv"
        )

        # Optional: save back to Excel
        # df_updated.to_excel("updated_inventory.xlsx", index=False)
    else:
        st.warning("Could not extract usable shipment data from the PDFs. Check if OCR worked or try pasting text manually.")

# Fallback: Paste text if PDF OCR fails
st.subheader("Or Paste Bill(s) of Lading Text (Fallback)")
bol_text = st.text_area(
    "Paste full text from one or multiple BOLs here (OCR output or copied text)",
    height=300
)

if st.button("Parse & Add Pasted Text") and bol_text.strip():
    # Similar parsing logic as before
    blocks = re.split(r'(?=FROM\s*:|Straight\s+Bill\s+of\s+Lading)', bol_text, flags=re.IGNORECASE)
    new_rows = []
    for block in blocks:
        if len(block.strip()) < 100:
            continue
        parsed = parse_bol_text(block)
        if parsed:
            new_rows.append(parsed)

    if new_rows:
        df_new = pd.DataFrame(new_rows)
        cols_order = ["Unloaded By", "Ship To", "Product", "Quantity", "Packaging", "Lot Numbers", "BOL #", "Notes"]
        for c in cols_order:
            if c not in df_new.columns:
                df_new[c] = ""
        df_new = df_new[cols_order]

        df_updated = pd.concat([df_inventory, df_new], ignore_index=True)

        st.success(f"Added {len(new_rows)} new shipment record(s)!")

        st.subheader("Updated Inventory Preview")
        st.dataframe(df_updated, use_container_width=True)

        csv = df_updated.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="Download Updated Inventory as CSV",
            data=csv,
            file_name="updated_inventory.csv",
            mime="text/csv"
        )
    else:
        st.warning("Could not extract usable shipment data from the text. Check formatting.")
