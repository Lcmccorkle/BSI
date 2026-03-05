import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import easyocr
import io
import re
import pandas as pd

# Initialize easyocr reader (downloads model on first run; set gpu=False for cloud compatibility)
@st.cache_resource
def get_ocr_reader():
    return easyocr.Reader(['en'], gpu=False)

# Function to extract text from PDF (tries direct text, then OCR if needed)
def extract_text_from_pdf(pdf_file):
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    all_page_texts = []
    
    reader = get_ocr_reader()
    
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        
        # Try direct text extraction (faster for printed PDFs)
        page_text = page.get_text().strip()
        
        # If little/no text, use OCR
        if len(page_text) < 50:
            pix = page.get_pixmap()
            img_bytes = pix.tobytes("png")
            ocr_results = reader.readtext(img_bytes)
            page_text = ' '.join([text for _, text, _ in ocr_results])  # Concatenate detected text
        
        all_page_texts.append(page_text)
    
    doc.close()
    pdf_file.seek(0)  # Reset pointer
    return all_page_texts  # List of text per page

# Function to parse inventory from a single page's text
def parse_inventory_from_page(page_text):
    item = {
        "BL No.": "N/A",
        "Product": "N/A",
        "Quantity Shipped": "N/A",
        "Packaging": "N/A",
        "Description": "N/A",
        "Lot No.": [],
        "Net Weight (lb)": "N/A",
        "Gross Weight (lb)": "N/A"
    }
    
    lines = re.split(r'\n|\s{2,}', page_text)  # Split on newlines or multiple spaces
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # BL No.
        bl_match = re.search(r'BL NO\. (\d+)', line, re.I)
        if bl_match:
            item["BL No."] = bl_match.group(1)
        
        # Product (usually at top)
        if "Product" in line:
            product_match = re.search(r'Product\s+(.*)', line, re.I)
            if product_match:
                item["Product"] = product_match.group(1).strip()
        
        # Quantity Shipped (often handwritten)
        qty_match = re.search(r'Quantity Shipped\s+(\d+)', line, re.I)
        if qty_match:
            item["Quantity Shipped"] = qty_match.group(1)
        
        # Packaging (e.g., "445.00 lb DRUM")
        pack_match = re.search(r'(\d+\.?\d* lb\s+[A-Z]+)', line, re.I)
        if pack_match:
            item["Packaging"] = pack_match.group(1)
        
        # Description (starts with "NOT REGULATED", "UN", or "X")
        if re.search(r'(NOT REGULATED|UN|X\s)', line, re.I):
            item["Description"] = line
        
        # Lot No. (can have multiple)
        lot_match = re.search(r'Lot No\.\s*([\w-]+)', line, re.I)
        if lot_match:
            item["Lot No."].append(lot_match.group(1))
        
        # Weights (e.g., "Total 5220 Total 5700" or labeled)
        weight_match = re.search(r'(Net Weight|Total)\s*\(lb\)?\s*(\d+)\s*(Gross Weight|Total)\s*\(lb\)?\s*(\d+)', line, re.I)
        if weight_match:
            item["Net Weight (lb)"] = weight_match.group(2)
            item["Gross Weight (lb)"] = weight_match.group(4)
    
    item["Lot No."] = ', '.join(item["Lot No."]) if item["Lot No."] else "N/A"
    
    return item if item["Product"] != "N/A" else None  # Return only if product found

# Streamlit app
st.title("BOL Inventory Extractor")

uploaded_file = st.file_uploader("Drag and drop your PDF here", type=["pdf"])

if uploaded_file is not None:
    st.write("Processing PDF...")
    page_texts = extract_text_from_pdf(uploaded_file)
    
    inventory = []
    for page_num, text in enumerate(page_texts, start=1):
        if text:
            item = parse_inventory_from_page(text)
            if item:
                item["Page"] = page_num  # Add page for reference in multi-page PDFs
                inventory.append(item)
    
    if inventory:
        df = pd.DataFrame(inventory)
        st.subheader("Extracted Inventory List")
        st.dataframe(df.style.hide(axis="index"))  # Nice table without index
    else:
        st.warning("No inventory items found. The PDF may be blank, or try refining OCR/parsing for your specific format.")
