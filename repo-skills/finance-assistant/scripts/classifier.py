"""
Expense classification system.
"""

from typing import Dict, List, Optional
from database import Database
from establishment_repository import EstablishmentRepository
from category_repository import CategoryRepository
from models import Establishment
from establishment_matcher import EstablishmentMatcher


class ExpenseClassifier:
    """Classifies expenses into establishments and categories."""
    
    def __init__(self, database: Database):
        self.database = database
        self.establishment_repo = EstablishmentRepository(database)
        self.category_repo = CategoryRepository(database)
        self._establishments = None
        self._matcher = None
    
    def _load_establishments(self):
        """Load establishments from database if not already loaded."""
        if self._establishments is None:
            self._establishments = self.establishment_repo.get_all()
            self._matcher = EstablishmentMatcher(self._establishments)
    
    def classify_expense(self, description: str) -> Dict[str, Optional[str]]:
        """
        Classify an expense description.
        
        Args:
            description: The expense description
            
        Returns:
            Dictionary with establishment_name and category_name
        """
        self._load_establishments()
        
        establishment = self._matcher.match_establishment(description)
        
        if establishment:
            category = self.category_repo.get_by_id(establishment.category_id)
            return {
                "establishment_name": establishment.name,
                "category_name": category.name if category else "Outros"
            }
        else:
            return {
                "establishment_name": None,
                "category_name": "Outros"
            }
    
    def classify_expenses(self, expenses: List[Dict]) -> List[Dict]:
        """
        Classify a list of expenses.
        
        Args:
            expenses: List of expense dictionaries with 'description' key
            
        Returns:
            List of expense dictionaries with added 'establishment' and 'category' keys
        """
        classified_expenses = []
        
        for expense in expenses:
            classification = self.classify_expense(expense["description"])
            classified_expense = expense.copy()
            classified_expense["establishment"] = classification["establishment_name"]
            classified_expense["category"] = classification["category_name"]
            classified_expenses.append(classified_expense)
        
        return classified_expenses
    
    def refresh_establishments(self):
        """Refresh the establishments cache."""
        self._establishments = None
        self._matcher = None
        self._load_establishments()
