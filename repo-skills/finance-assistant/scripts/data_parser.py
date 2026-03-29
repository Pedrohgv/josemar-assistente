"""
Data parsing module for expense processing.
Parses and validates expense details from extracted text.
"""

import re
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from database import Database
from classifier import ExpenseClassifier

# Set up logger
logger = logging.getLogger(__name__)


def parse_brazilian_currency(amount_str: str) -> float:
    """Convert Brazilian currency format to float.
    
    Handles formats like:
    - "1.234,56" -> 1234.56
    - "1234,56" -> 1234.56
    """
    # Remove dots (thousands separator) and replace comma (decimal separator) with dot
    cleaned = amount_str.replace('.', '').replace(',', '.')
    return float(cleaned)


def parse_and_classify_expenses(expense_details: str, total_expense: str, database: Database) -> List[Dict[str, Any]]:
    """
    Parse expense details from text, classify them, and return a list of structured expenses.
    
    Args:
        expense_details: Text with expense entries
        total_expense: Total expense amount string
        database: Database instance for classification
        
    Returns:
        List of dictionaries, each representing a classified expense
    """
    # Initialize classifier
    classifier = ExpenseClassifier(database)
    
    # Parse total expense
    total_amount = parse_brazilian_currency(total_expense)
    
    # Process expense details
    lines = expense_details.strip().split('\n')
    parsed_expenses = []
    sum_expenses = 0.0
    
    for line in lines:
        if not line.strip():
            continue
        
        # Skip payment entries from previous months
        if "PAGAMENTO DB DIRETO CONTA" in line:
            continue
        
        # Extract date, description, and amount
        date_match = re.match(r'^(\d{2}/\d{2})', line)
        amount_match = re.search(r'(-?[\d\.]+,\d{2})$', line)
        
        if date_match and amount_match:
            date_str = date_match.group(1)
            amount_str = amount_match.group(1)
            amount = parse_brazilian_currency(amount_str)
            
            # Extract description (everything between date and amount)
            description_start = len(date_str) + 1
            description_end = line.rfind(amount_str)
            description = line[description_start:description_end].strip()
            
            parsed_expenses.append({
                'date': date_str,
                'description': description,
                'amount': amount,
                'amount_str': amount_str
            })
            
            sum_expenses += amount
    
    # Sanity check
    tolerance = 0.01  # Allow small floating point differences
    if abs(sum_expenses - total_amount) <= tolerance:
        logger.info(f"✓ SUCCESS: Sum of expenses ({sum_expenses:.2f}) matches total expense ({total_amount:.2f})")
    else:
        logger.warning(f"⚠ WARNING: Sum of expenses ({sum_expenses:.2f}) does NOT match total expense ({total_amount:.2f})")
        logger.warning(f"   Difference: {abs(sum_expenses - total_amount):.2f}")
    
    # Classify expenses
    classified_expenses = classifier.classify_expenses(parsed_expenses)
    
    # Add full date with year logic
    current_year = datetime.now().year
    for expense in classified_expenses:
        day, month = map(int, expense['date'].split('/'))
        # If month is December, assume previous year
        year = current_year - 1 if month == 12 else current_year
        expense['full_date'] = f"{day:02d}/{month:02d}/{year}"
    
    return classified_expenses


def format_expenses_for_sheets(classified_expenses: List[Dict[str, Any]]) -> Dict[str, List]:
    """
    Format classified expenses for Google Sheets table insertion.
    
    Args:
        classified_expenses: List of classified expense dictionaries
        
    Returns:
        Dictionary with columns as keys and lists of values
    """
    return {
        "Data": [expense['full_date'] for expense in classified_expenses],
        "Estabelecimento": [expense.get('establishment', '') or '' for expense in classified_expenses],
        "Categoria": [expense.get('category', 'Outros') or 'Outros' for expense in classified_expenses],
        "Valor": [expense['amount'] for expense in classified_expenses],
        "Texto Compra": [expense['description'] for expense in classified_expenses]
    }
