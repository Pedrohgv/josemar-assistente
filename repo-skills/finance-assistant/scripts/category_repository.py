"""
Repository for category operations.
"""

from typing import List, Optional
from database import Database
from models import Category


class CategoryRepository:
    """Repository for category CRUD operations."""
    
    def __init__(self, database: Database):
        self.db = database
    
    def get_all(self) -> List[Category]:
        """Get all categories."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM categories ORDER BY name")
        rows = cursor.fetchall()
        
        conn.close()
        
        return [Category.from_dict(dict(row)) for row in rows]
    
    def get_by_id(self, category_id: int) -> Optional[Category]:
        """Get category by ID."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            return Category.from_dict(dict(row))
        return None
    
    def get_by_name(self, name: str) -> Optional[Category]:
        """Get category by name."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM categories WHERE name = ?", (name,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            return Category.from_dict(dict(row))
        return None
