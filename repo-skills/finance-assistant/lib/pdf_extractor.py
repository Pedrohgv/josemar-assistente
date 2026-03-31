import pymupdf


def extract_text(pdf_path):
    doc = pymupdf.open(pdf_path)
    text = ''
    for page in doc:
        text += page.get_text()
    doc.close()
    return text
