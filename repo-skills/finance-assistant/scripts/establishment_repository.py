"""
Repository for establishment operations.
"""

from typing import List, Optional
from database import Database
from models import Establishment


class EstablishmentRepository:
    """Repository for establishment CRUD operations."""
    
    def __init__(self, database: Database):
        self.db = database
    
    def get_all(self) -> List[Establishment]:
        """Get all establishments with their category names."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT e.*, c.name as category_name
            FROM establishments e
            JOIN categories c ON e.category_id = c.id
            ORDER BY e.name
        """)
        rows = cursor.fetchall()
        
        conn.close()
        
        return [Establishment.from_dict(dict(row)) for row in rows]
    
    def get_by_id(self, establishment_id: int) -> Optional[Establishment]:
        """Get establishment by ID."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT e.*, c.name as category_name
            FROM establishments e
            JOIN categories c ON e.category_id = c.id
            WHERE e.id = ?
        """, (establishment_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            return Establishment.from_dict(dict(row))
        return None
    
    def create(self, establishment: Establishment) -> Establishment:
        """Create a new establishment."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO establishments (name, match_pattern, exclude_pattern, category_id)
            VALUES (?, ?, ?, ?)
        """, (establishment.name, establishment.match_pattern, 
              establishment.exclude_pattern, establishment.category_id))
        
        establishment.id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return establishment
    
    def update(self, establishment: Establishment) -> Establishment:
        """Update an existing establishment."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE establishments
            SET name = ?, match_pattern = ?, exclude_pattern = ?, category_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (establishment.name, establishment.match_pattern,
              establishment.exclude_pattern, establishment.category_id, establishment.id))
        
        conn.commit()
        conn.close()
        
        return establishment
    
    def delete(self, establishment_id: int) -> bool:
        """Delete an establishment by ID."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM establishments WHERE id = ?", (establishment_id,))
        deleted = cursor.rowcount > 0
        
        conn.commit()
        conn.close()
        
        return deleted
