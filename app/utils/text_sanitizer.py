"""
Text sanitizer for LLM-generated medical correspondence.

This module sanitizes LLM-generated text to appear as human-written
Hebrew medical letters by removing Markdown artifacts, fixing date formats,
and normalizing bullet points.

Transforms:
1. Dates: ISO (YYYY-MM-DD) and Zero-padded (05/07/2025) -> Compact Israeli (5/7/25)
2. Formatting: Removes Markdown bold (**text**) and headers (#)
3. Punctuation: Replaces Em-dashes with standard hyphens
4. Lists: Converts asterisk bullets (*) to standard dashes (-) and fixes indentation
"""

import re
from typing import Optional


class MedicalLetterSanitizer:
    """
    Sanitizes LLM-generated medical text to look like a human-written 
    Hebrew letter.
    
    This class is stateless and thread-safe. Regex patterns are compiled
    once during initialization for optimal performance.
    
    Usage:
        sanitizer = MedicalLetterSanitizer()
        clean_text = sanitizer.process(llm_generated_text)
    """
    
    def __init__(self):
        """Initialize and compile all regex patterns."""
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """Compile regex patterns for text transformations."""
        # 1. ISO Date Pattern: YYYY-MM-DD (e.g., 2025-12-05)
        self.date_iso_pattern = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b')

        # 2. Slashed Date Pattern: DD/MM/YYYY or D/M/YY (e.g., 08/12/2025)
        # We catch 2 or 4 digit years to ensure we normalize everything.
        self.date_slash_pattern = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b')
        
        # 3. Markdown Bold/Italic (**text** or __text__)
        self.bold_pattern = re.compile(r'\*\*(.*?)\*\*|__(.*?)__')
        
        # 4. Headers (Lines starting with #)
        self.header_pattern = re.compile(r'^#{1,6}\s*', re.MULTILINE)
        
        # 5. Em-Dashes (—)
        self.em_dash_pattern = re.compile(r'—')
        
        # 6. Bullets (Lines starting with *)
        self.bullet_pattern = re.compile(r'^(\s*)\*\s+(.*)', re.MULTILINE)
        
        # 7. Excessive newlines (3 or more)
        self.excessive_newlines_pattern = re.compile(r'\n{3,}')

    def _format_date_standard(self, day: str, month: str, year: str) -> str:
        """
        Format date components to compact Israeli format (D/M/YY).
        
        Removes leading zeros from day and month, and uses 2-digit year.
        
        Args:
            day: Day component (may have leading zero)
            month: Month component (may have leading zero)
            year: Year component (2 or 4 digits)
            
        Returns:
            Formatted date string (e.g., "5/7/25")
        """
        # Strip leading zeros via int()
        d = int(day)
        m = int(month)
        # Ensure year is 2 digits
        y = year[-2:] 
        return f"{d}/{m}/{y}"

    def _callback_iso_date(self, match: re.Match) -> str:
        """
        Regex callback: Converts YYYY-MM-DD -> D/M/YY.
        
        Args:
            match: Regex match object with groups (year, month, day)
            
        Returns:
            Formatted date string
        """
        return self._format_date_standard(
            day=match.group(3), 
            month=match.group(2), 
            year=match.group(1)
        )

    def _callback_slash_date(self, match: re.Match) -> str:
        """
        Regex callback: Converts 08/12/2025 -> 8/12/25.
        
        Args:
            match: Regex match object with groups (day, month, year)
            
        Returns:
            Formatted date string
        """
        return self._format_date_standard(
            day=match.group(1), 
            month=match.group(2), 
            year=match.group(3)
        )

    def _normalize_bullets(self, match: re.Match) -> str:
        """
        Regex callback: Converts '* Item' to '- Item' and fixes indentation.
        
        For nested bullets, uses single space indentation.
        For top-level bullets, no indentation.
        
        Args:
            match: Regex match object with groups (indentation, content)
            
        Returns:
            Formatted bullet line
        """
        indentation = match.group(1)
        content = match.group(2)
        
        # Standardize indentation: if there was indentation, use single space
        if len(indentation) > 0:
            return f" - {content}"
        return f"- {content}"

    def process(self, text: Optional[str]) -> str:
        """
        Apply all sanitization transformations to the input text.
        
        Processing order:
        1. Fix ISO Dates (2025-12-05 -> 5/12/25)
        2. Fix Existing Slashed Dates (08/12/2025 -> 8/12/25)
        3. Replace Em-dashes with hyphens
        4. Remove Markdown bolding (keep text content)
        5. Remove Markdown headers
        6. Normalize bullets and indentation
        7. Collapse excessive newlines
        
        Args:
            text: The input text to sanitize (may be None or empty)
            
        Returns:
            Sanitized text string, or empty string if input was None/empty
        """
        if not text:
            return ""

        # 1. Fix ISO Dates (2025-12-05 -> 5/12/25)
        text = self.date_iso_pattern.sub(self._callback_iso_date, text)

        # 2. Fix Existing Slashed Dates (08/12/2025 -> 8/12/25)
        text = self.date_slash_pattern.sub(self._callback_slash_date, text)

        # 3. Replace Em-dashes with standard hyphens
        text = self.em_dash_pattern.sub('-', text)
        
        # 4. Remove bolding but keep the text inside
        text = self.bold_pattern.sub(lambda m: m.group(1) or m.group(2), text)
        
        # 5. Remove headers hashtags
        text = self.header_pattern.sub('', text)
        
        # 6. Normalize bullets
        text = self.bullet_pattern.sub(self._normalize_bullets, text)
        
        # 7. Normalize excessive newlines (max 2 consecutive)
        text = self.excessive_newlines_pattern.sub('\n\n', text)

        return text.strip()
