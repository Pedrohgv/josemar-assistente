"""
Data models for finance-assistant skill.
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class Category:
    """Represents an expense category."""
    id: Optional[int]
    name: str
    description: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert category to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Category":
        """Create category from dictionary."""
        return cls(
            id=data.get("id"),
            name=data["name"],
            description=data.get("description")
        )


@dataclass
class Establishment:
    """Represents an establishment for expense classification."""
    id: Optional[int]
    name: str
    match_pattern: str
    exclude_pattern: Optional[str]
    category_id: int
    category_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> dict:
        """Convert establishment to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "match_pattern": self.match_pattern,
            "exclude_pattern": self.exclude_pattern,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Establishment":
        """Create establishment from dictionary."""
        created_at = None
        updated_at = None
        
        if data.get("created_at"):
            created_at = datetime.fromisoformat(data["created_at"]) if isinstance(data["created_at"], str) else data["created_at"]
        if data.get("updated_at"):
            updated_at = datetime.fromisoformat(data["updated_at"]) if isinstance(data["updated_at"], str) else data["updated_at"]
        
        return cls(
            id=data.get("id"),
            name=data["name"],
            match_pattern=data["match_pattern"],
            exclude_pattern=data.get("exclude_pattern"),
            category_id=data["category_id"],
            category_name=data.get("category_name"),
            created_at=created_at,
            updated_at=updated_at
        )
