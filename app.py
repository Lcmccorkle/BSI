import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io
import re
import pandas as pd

# Function to extract text from PDF (tries direct text, then OCR if needed)
def extract_text_from_pdf(pdf_file):
    text = ""
    pdf = pdfplumber.open(pdf_file)
    
    # Try direct text extraction (for printed PDFs)
    for page in pdf.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"
    
    # If little/no text found, assume scanned and use OCR
    if len(text.strip()) < 50:  # Arbitrary threshold; adjust as needed
        text = ""
        doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap()
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            page_text = pytesseract.image_to_string(img)
            text += page_text + "\n"
        doc.close()
    
    pdf_file.seek(0)  # Reset file pointer
    return text

# Function to parse inventory from extracted text
def parse_inventory(text):
    # Simple regex patterns for common BOL inventory lines
    # Assumes lines like: "Item Description: Widget Qty: 10 Weight: 50 lbs"
    # Customize patterns based on your BOL format
    pattern = re.compile(r'(?i)(description|item|goods).*?(qty|quantity|no\.?|pieces).*?(\d+).*?(weight|wt|lbs|kg).*?(\d+\.?\d*)', re.DOTALL)
    matches = pattern.findall(text)
    
    inventory = []
    for match in matches:
        desc = match[0].strip() if match[0] else "Unknown"
        qty = match[2].strip() if match[2] else "N/A"
        weight = f"{match[4].strip()} {match[3].lower()}" if match[4] else "N/A"
        inventory.append({"Description": desc, "Quantity": qty, "Weight": weight})
    
    # Fallback: If no matches, split lines and look for tabular data
    if not inventory:
        lines = text.split("\n")
        for line in lines:
            if re.search(r'\d+', line):  # Lines with numbers likely inventory
                parts = re.split(r'\s{2,}', line.strip())  # Split on multiple spaces
                if len(parts) >= 3:
                    inventory.append({"Description": parts[0], "Quantity": parts[1], "Weight": parts[2]})
    
    return inventory

# Streamlit app
st.title("BOL Inventory Extractor")

uploaded_file = st.file_uploader("Drag and drop your PDF here", type=["pdf"])

if uploaded_file is not None:
    st.write("Processing PDF...")
    text = extract_text_from_pdf(uploaded_file)
    
    if text:
        inventory = parse_inventory(text)
        
        if inventory:
            df = pd.DataFrame(inventory)
            st.subheader("Extracted Inventory List")
            st.dataframe(df)  # Displays as a table
        else:
            st.warning("No inventory items found. Check the PDF format or refine parsing logic.")
    else:
        st.error("Could not extract text from PDF. Ensure it's a valid BOL.")

# Run with: streamlit run app.py
