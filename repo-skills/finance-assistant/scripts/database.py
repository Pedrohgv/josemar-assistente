"""
Database module for finance-assistant skill.
Manages SQLite database for categories and establishments.
Database is stored in workspace for persistence.
"""

import sqlite3
import os
from typing import Optional


class Database:
    """SQLite database connection and initialization."""
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize database connection.
        
        Args:
            db_path: Optional custom path. Defaults to workspace directory.
        """
        if db_path is None:
            # Store in workspace for persistence across container restarts
            workspace = os.environ.get('OPENCLAW_WORKSPACE', '/root/.openclaw/workspace')
            db_path = os.path.join(workspace, 'finance_assistant.db')
        
        self.db_path = db_path
        self._ensure_directory()
        self._init_database()
    
    def _ensure_directory(self):
        """Ensure the database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    
    def _init_database(self):
        """Initialize the database with required tables and initial data."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Create categories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT
            )
        """)
        
        # Create establishments table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS establishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                match_pattern TEXT NOT NULL,
                exclude_pattern TEXT,
                category_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        """)
        
        # Insert initial categories if they don't exist
        categories = [
            ('Supermercado', 'Grocery expenses'),
            ('Transporte', 'Uber, 99 Pop, transportation services'),
            ('Marmitas', 'Day-to-day, healthy and frozen meals'),
            ('Ifood', 'Take-out and delivered food'),
            ('Assinaturas', 'Subscription services'),
            ('Outros', 'Miscellaneous expenses')
        ]
        
        for name, description in categories:
            cursor.execute("""
                INSERT OR IGNORE INTO categories (name, description)
                VALUES (?, ?)
            """, (name, description))
        
        conn.commit()
        conn.close()
    
    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
