import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO

st.title("📦 Bill of Lading → Inventory Extractor")

uploaded_file = st.file_uploader("Upload a Bill of Lading PDF", type=["pdf"])

def extract_text(pdf_file):
    text = ""
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text

def extract_field(pattern, text, default=""):
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else default

if uploaded_file:
    text = extract_text(uploaded_file)

    # Extract fields using regex patterns
    bol = extract_field(r"B/L\s*NO\.?\s*(\d+)", text)
    product = extract_field(r"Product\s*\n(.+)", text)
    quantity = extract_field(r"QUANTITY ORDERED\s*\n(\d+)", text)
    packaging = extract_field(r"PACKAGING\s*\n(.+)", text)
    lot = "; ".join(re.findall(r"Lot No\.?\s*([A-Za-z0-9\-]+)", text))
    ship_to = extract_field(r"([A-Za-z ]+, [A-Z]{2})\s*\d{5}", text)

    st.subheader("Extracted Data")
    st.write({
        "BOL #": bol,
        "Ship To": ship_to,
        "Product": product,
        "Quantity": quantity,
        "Packaging": packaging,
        "Lot Numbers": lot
    })

    # Create Excel row
    df = pd.DataFrame([{
        "Unloaded By": "",
        "Ship To": ship_to,
        "Product": product,
        "Quantity": quantity,
        "Packaging": packaging,
        "Lot Numbers": lot,
        "BOL #": bol,
        "Notes": ""
    }])

    # Download button
    output = BytesIO()
    df.to_excel(output, index=False)
    st.download_button(
        label="⬇️ Download Excel Row",
        data=output.getvalue(),
        file_name="inventory_row.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )