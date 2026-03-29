"""
Expense classification using regex pattern matching.
"""

import re
from typing import Dict, List, Optional
from models import Establishment


class EstablishmentMatcher:
    """Matches expense descriptions against establishment patterns."""
    
    def __init__(self, establishments: List[Establishment]):
        self.establishments = establishments
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Pre-compile regex patterns for performance."""
        self._compiled = []
        for est in self.establishments:
            try:
                match_re = re.compile(est.match_pattern, re.IGNORECASE)
                exclude_re = None
                if est.exclude_pattern:
                    exclude_re = re.compile(est.exclude_pattern, re.IGNORECASE)
                self._compiled.append((est, match_re, exclude_re))
            except re.error:
                # Skip invalid patterns
                continue
    
    def match_establishment(self, description: str) -> Optional[Establishment]:
        """
        Match a description against establishment patterns.
        
        Args:
            description: The expense description to match
            
        Returns:
            Matched Establishment or None
        """
        for est, match_re, exclude_re in self._compiled:
            # Check if it matches the match pattern
            if match_re.search(description):
                # Check if it doesn't match the exclude pattern (if any)
                if exclude_re is None or not exclude_re.search(description):
                    return est
        
        return None
