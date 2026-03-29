"""
PDF processing module for finance-assistant skill.
Orchestrates text extraction, parsing, and classification.
"""

import pymupdf
from typing import Dict, Any, List
from data_extractor import extract_expense_data
from data_parser import parse_and_classify_expenses, format_expenses_for_sheets
from database import Database


def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Extract text from PDF file using pymupdf.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Extracted text from all pages
        
    Raises:
        Exception: If PDF extraction fails
    """
    try:
        doc = pymupdf.open(pdf_path)
        text = ''
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        raise Exception(f"Error extracting text from PDF: {str(e)}")


def process_pdf(pdf_path: str, database: Database) -> Dict[str, Any]:
    """
    Process a PDF file and extract classified expenses.
    
    Args:
        pdf_path: Path to the PDF file
        database: Database instance for classification
        
    Returns:
        Dictionary with extraction results and classified expenses
    """
    # Extract text from PDF
    pdf_text = extract_text_from_pdf(pdf_path)
    
    # Extract expense data
    extracted_data = extract_expense_data(pdf_text)
    
    if not extracted_data['total_expense']:
        return {
            "success": False,
            "error": "Could not extract total expense from PDF",
            "source": "pdf_file",
            "file_path": pdf_path
        }
    
    if not extracted_data['expense_details']:
        return {
            "success": False,
            "error": "Could not extract expense details from PDF",
            "source": "pdf_file",
            "file_path": pdf_path
        }
    
    # Parse and classify expenses
    classified_expenses = parse_and_classify_expenses(
        extracted_data['expense_details'],
        extracted_data['total_expense'],
        database
    )
    
    # Format for sheets
    sheets_data = format_expenses_for_sheets(classified_expenses)
    
    return {
        "success": True,
        "source": "pdf_file",
        "file_path": pdf_path,
        "extraction": {
            "total_expense": extracted_data['total_expense'],
            "line_count": extracted_data['line_count'],
            "text_preview": pdf_text[:500] + '...' if len(pdf_text) > 500 else pdf_text
        },
        "classified_expenses": classified_expenses,
        "sheets_data": sheets_data,
        "summary": {
            "total_items": len(classified_expenses),
            "total_amount": sum(exp['amount'] for exp in classified_expenses)
        }
    }


def process_text(raw_text: str, database: Database) -> Dict[str, Any]:
    """
    Process raw text and extract classified expenses.
    
    Args:
        raw_text: Raw text from PDF invoice
        database: Database instance for classification
        
    Returns:
        Dictionary with extraction results and classified expenses
    """
    # Extract expense data
    extracted_data = extract_expense_data(raw_text)
    
    if not extracted_data['total_expense']:
        return {
            "success": False,
            "error": "Could not extract total expense from text",
            "source": "raw_text"
        }
    
    if not extracted_data['expense_details']:
        return {
            "success": False,
            "error": "Could not extract expense details from text",
            "source": "raw_text"
        }
    
    # Parse and classify expenses
    classified_expenses = parse_and_classify_expenses(
        extracted_data['expense_details'],
        extracted_data['total_expense'],
        database
    )
    
    # Format for sheets
    sheets_data = format_expenses_for_sheets(classified_expenses)
    
    return {
        "success": True,
        "source": "raw_text",
        "extraction": {
            "total_expense": extracted_data['total_expense'],
            "line_count": extracted_data['line_count']
        },
        "classified_expenses": classified_expenses,
        "sheets_data": sheets_data,
        "summary": {
            "total_items": len(classified_expenses),
            "total_amount": sum(exp['amount'] for exp in classified_expenses)
        }
    }
