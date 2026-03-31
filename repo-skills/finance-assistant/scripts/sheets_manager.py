"""
Google Sheets manager using gogcli commands.
Handles all sheet operations for credit card and general expenses.
"""

import subprocess
import json
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


def run_gog_command(args: List[str]) -> Dict[str, Any]:
    """
    Execute a gogcli command and return the result.
    
    Args:
        args: List of command arguments
        
    Returns:
        Dictionary with success status and result or error
    """
    try:
        cmd = ["gog"] + args + ["--json"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            try:
                output = json.loads(result.stdout)
                return {"success": True, "data": output}
            except json.JSONDecodeError:
                return {"success": True, "data": result.stdout}
        else:
            return {
                "success": False,
                "error": result.stderr or "Unknown error",
                "stdout": result.stdout
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_spreadsheet_id_by_year(year: int) -> Optional[str]:
    """
    Find spreadsheet ID by year name.
    
    Args:
        year: The year (e.g., 2025)
        
    Returns:
        Spreadsheet ID or None if not found
    """
    # Use drive search to find spreadsheets (sheets list command doesn't exist)
    result = run_gog_command(["drive", "search", f"type:spreadsheet '{year}'"])
    
    if not result["success"]:
        logger.error(f"Failed to search for spreadsheet: {result.get('error')}")
        return None
    
    files = result["data"].get("files", [])
    year_str = str(year)
    
    for file in files:
        if file.get("name") == year_str:
            return file.get("id")
    
    return None


def get_table_id(spreadsheet_id: str, table_name: str) -> Optional[str]:
    """
    Find table ID by name within a spreadsheet.
    
    Args:
        spreadsheet_id: The spreadsheet ID
        table_name: Name of the table (e.g., "Cartão Jan")
        
    Returns:
        Table ID or None if not found
    """
    result = run_gog_command(["sheets", "table", "list", spreadsheet_id])
    
    if not result["success"]:
        logger.error(f"Failed to list tables: {result.get('error')}")
        return None
    
    tables = result["data"].get("tables", [])
    
    for table in tables:
        if table.get("tableName") == table_name:
            return table.get("tableId")
    
    return None


def append_credit_card_expenses(
    year: int,
    month_abbr: str,
    expenses_data: Dict[str, List]
) -> Dict[str, Any]:
    """
    Append credit card expenses to the Cartão table.
    
    Args:
        year: The year (e.g., 2025)
        month_abbr: Month abbreviation (e.g., "Jan")
        expenses_data: Dictionary with column data
        
    Returns:
        Result dictionary with status and message
    """
    # Find spreadsheet
    spreadsheet_id = get_spreadsheet_id_by_year(year)
    if not spreadsheet_id:
        return {
            "success": False,
            "error": f"Spreadsheet for year {year} not found"
        }
    
    # Find table
    table_name = f"Cartão {month_abbr}"
    table_id = get_table_id(spreadsheet_id, table_name)
    if not table_id:
        return {
            "success": False,
            "error": f"Table '{table_name}' not found in spreadsheet {year}"
        }
    
    # Convert data to row format for gogcli
    # expenses_data has columns as keys, we need to transpose to rows
    num_rows = len(expenses_data.get("Data", []))
    if num_rows == 0:
        return {
            "success": True,
            "message": "No expenses to add",
            "rows_added": 0
        }
    
    # Build rows as JSON array
    rows = []
    for i in range(num_rows):
        row = [
            expenses_data["Data"][i],
            expenses_data["Estabelecimento"][i],
            expenses_data["Categoria"][i],
            expenses_data["Valor"][i],
            expenses_data["Texto Compra"][i]
        ]
        rows.append(row)
    
    # Build gogcli command using --values-json flag (required for proper parsing)
    rows_json = json.dumps(rows)
    result = run_gog_command([
        "sheets", "table", "append",
        spreadsheet_id, table_id,
        f"--values-json={rows_json}"
    ])
    
    if result["success"]:
        return {
            "success": True,
            "message": f"Successfully added {num_rows} rows to {table_name}",
            "rows_added": num_rows,
            "spreadsheet": str(year),
            "worksheet": month_abbr,
            "table": table_name
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Failed to append rows")
        }


def update_general_expense(
    year: int,
    month_abbr: str,
    description: str,
    amount: float
) -> Dict[str, Any]:
    """
    Update a general expense in the Despesas table.
    
    Args:
        year: The year (e.g., 2025)
        month_abbr: Month abbreviation (e.g., "Jan")
        description: Expense description (e.g., "Aluguel")
        amount: The new amount
        
    Returns:
        Result dictionary with status and message
    """
    # Find spreadsheet
    spreadsheet_id = get_spreadsheet_id_by_year(year)
    if not spreadsheet_id:
        return {
            "success": False,
            "error": f"Spreadsheet for year {year} not found"
        }
    
    # Find table
    table_name = f"Despesas {month_abbr}"
    table_id = get_table_id(spreadsheet_id, table_name)
    if not table_id:
        return {
            "success": False,
            "error": f"Table '{table_name}' not found in spreadsheet {year}"
        }
    
    # For gogcli update, we need to match by description column and update value column
    # This is a simplified approach - gogcli's update command syntax may vary
    result = run_gog_command([
        "sheets", "table", "update",
        spreadsheet_id, table_id,
        "--match-column", "Despesa",
        "--match-value", description,
        "--update-column", "Valor",
        "--update-value", str(amount)
    ])
    
    if result["success"]:
        return {
            "success": True,
            "message": f"Successfully updated '{description}' to R$ {amount:.2f} in {table_name}",
            "spreadsheet": str(year),
            "worksheet": month_abbr,
            "table": table_name,
            "description": description,
            "amount": amount
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Failed to update expense")
        }


def query_historical_expenses(
    start_year: int,
    start_month: str,
    end_year: Optional[int] = None,
    end_month: Optional[str] = None,
    establishments: Optional[List[str]] = None,
    categories: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Query historical credit card expenses.
    
    Note: This is a simplified version. Full implementation would need to:
    1. Query multiple months
    2. Filter by establishments/categories
    3. Calculate aggregations
    
    For now, this returns instructions on how to do it with gogcli.
    """
    # This would require iterating through multiple tables and aggregating
    # For the skill, we'll document the gogcli commands in SKILL.md
    return {
        "success": True,
        "note": "Use gogcli commands directly for complex historical queries",
        "example_commands": [
            f"gog sheets table get <spreadsheet-id> <table-id> --json",
            f"gog sheets export <spreadsheet-id> --format csv"
        ]
    }
