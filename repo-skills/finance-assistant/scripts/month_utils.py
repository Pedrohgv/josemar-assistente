"""
Month normalization and utility functions.
Handles month name conversions and date logic.
"""

from datetime import datetime
from typing import Dict, Tuple

# Comprehensive month mapping dictionary
MONTH_MAPPING: Dict[str, int] = {
    # Portuguese full names
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # Portuguese abbreviations
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
    # English full names
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # English abbreviations
    "feb": 2, "apr": 4, "sep": 9, "oct": 10, "dec": 12,
}

# Portuguese month abbreviations for Google Sheets compatibility
MONTH_ABBREVIATIONS = {
    1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"
}


def normalize_month(month_input: str) -> int:
    """
    Normalize any month input to month number (1-12).
    
    Handles full names and abbreviations in both Portuguese and English.
    Matching is case-insensitive.
    
    Args:
        month_input: Month name in any supported format
        
    Returns:
        Month number (1-12)
        
    Raises:
        ValueError: If month_input is not a valid month name
    """
    if not month_input or not isinstance(month_input, str):
        raise ValueError(f"Invalid month input: {month_input}")
    
    month_normalized = month_input.strip().lower()
    month_num = MONTH_MAPPING.get(month_normalized)
    
    if month_num is None:
        raise ValueError(
            f"Invalid month name: '{month_input}'. "
            f"Expected a month name in Portuguese or English "
            f"(full name or 3-letter abbreviation)."
        )
    
    return month_num


def month_number_to_abbr(month_num: int) -> str:
    """
    Convert month number to Portuguese abbreviation.
    
    Args:
        month_num: Month number (1-12)
        
    Returns:
        Portuguese month abbreviation (e.g., "Jan", "Fev", "Mar")
        
    Raises:
        ValueError: If month_num is not in range 1-12
    """
    if not isinstance(month_num, int) or month_num < 1 or month_num > 12:
        raise ValueError(f"Invalid month number: {month_num}. Expected 1-12.")
    
    return MONTH_ABBREVIATIONS[month_num]


def normalize_month_to_abbr(month_input: str) -> str:
    """
    Normalize month input directly to Portuguese abbreviation.
    
    Args:
        month_input: Month name in any supported format
        
    Returns:
        Portuguese month abbreviation
    """
    month_num = normalize_month(month_input)
    return month_number_to_abbr(month_num)


def get_suggested_month() -> Tuple[int, str, bool]:
    """
    Get the suggested month for updating based on current date.
    
    Rules:
    - Day 1-10: Suggest last month (requires confirmation)
    - Day 11+: No suggestion, just ask user
    
    Returns:
        Tuple of (year, month_abbreviation, requires_confirmation)
    """
    today = datetime.now()
    current_day = today.day
    
    if current_day <= 10:
        # Suggest last month (requires confirmation)
        if today.month == 1:
            year = today.year - 1
            month_num = 12
        else:
            year = today.year
            month_num = today.month - 1
        
        month_abbr = month_number_to_abbr(month_num)
        return (year, month_abbr, True)
    else:
        # Middle/end of month - no suggestion
        year = today.year
        month_num = today.month
        month_abbr = month_number_to_abbr(month_num)
        return (year, month_abbr, False)


def get_month_suggestion_message() -> dict:
    """
    Get the appropriate message for month selection based on current date.
    
    Returns:
        Dictionary with message and suggested month info
    """
    year, month_abbr, requires_confirmation = get_suggested_month()
    today = datetime.now()
    
    if requires_confirmation:
        full_month_names = {
            "Jan": "Janeiro", "Fev": "Fevereiro", "Mar": "Março", "Abr": "Abril",
            "Mai": "Maio", "Jun": "Junho", "Jul": "Julho", "Ago": "Agosto",
            "Set": "Setembro", "Out": "Outubro", "Nov": "Novembro", "Dez": "Dezembro"
        }
        full_month = full_month_names.get(month_abbr, month_abbr)
        
        return {
            "action": "suggest",
            "message": f"Estamos no início do mês ({today.day:02d}/{today.month:02d}). Deseja atualizar a fatura de {full_month} de {year}?",
            "suggested_month": month_abbr,
            "suggested_year": year,
            "requires_confirmation": True
        }
    else:
        return {
            "action": "ask",
            "message": "Para qual mês você deseja registrar estas despesas?",
            "current_month": month_abbr,
            "current_year": year,
            "requires_confirmation": False
        }
