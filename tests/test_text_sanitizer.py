"""
Unit tests for the MedicalLetterSanitizer class.

Tests all text sanitization transformations including date formatting,
Markdown removal, bullet normalization, and edge cases.
"""

import pytest
from app.utils.text_sanitizer import MedicalLetterSanitizer


class TestISODates:
    """Tests for ISO date format conversion (YYYY-MM-DD -> D/M/YY)."""
    
    def test_basic_iso_date(self):
        """ISO date should convert to Israeli format."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 2025-12-15")
        assert result == "Date: 15/12/25"
    
    def test_iso_date_with_leading_zeros(self):
        """ISO date with leading zeros should strip them."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 2025-07-05")
        assert result == "Date: 5/7/25"
    
    def test_iso_date_single_digit_month_day(self):
        """ISO date with single digit month/day should work."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("2025-1-5")
        assert result == "5/1/25"
    
    def test_multiple_iso_dates(self):
        """Multiple ISO dates in text should all be converted."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("From 2025-01-01 to 2025-12-31")
        assert result == "From 1/1/25 to 31/12/25"
    
    def test_iso_date_in_hebrew_context(self):
        """ISO date in Hebrew text should convert correctly."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("תאריך: 2025-03-15")
        assert result == "תאריך: 15/3/25"


class TestSlashedDates:
    """Tests for slashed date format conversion (DD/MM/YYYY -> D/M/YY)."""
    
    def test_slashed_date_four_digit_year(self):
        """Slashed date with 4-digit year should convert."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 15/12/2025")
        assert result == "Date: 15/12/25"
    
    def test_slashed_date_with_leading_zeros(self):
        """Slashed date with leading zeros should strip them."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 05/07/2025")
        assert result == "Date: 5/7/25"
    
    def test_slashed_date_two_digit_year(self):
        """Slashed date with 2-digit year should normalize zeros."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 08/12/25")
        assert result == "Date: 8/12/25"
    
    def test_slashed_date_already_compact(self):
        """Already compact slashed date should remain unchanged."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Date: 5/7/25")
        assert result == "Date: 5/7/25"


class TestEmDashes:
    """Tests for em-dash replacement."""
    
    def test_em_dash_replacement(self):
        """Em-dash should be replaced with hyphen."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Text—more text")
        assert result == "Text-more text"
    
    def test_multiple_em_dashes(self):
        """Multiple em-dashes should all be replaced."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("One—two—three")
        assert result == "One-two-three"
    
    def test_em_dash_in_hebrew(self):
        """Em-dash in Hebrew text should be replaced."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("טקסט—עוד טקסט")
        assert result == "טקסט-עוד טקסט"


class TestMarkdownBold:
    """Tests for Markdown bold removal."""
    
    def test_asterisk_bold_removal(self):
        """Markdown bold with asterisks should be removed."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("**bold text**")
        assert result == "bold text"
    
    def test_underscore_bold_removal(self):
        """Markdown bold with underscores should be removed."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("__bold text__")
        assert result == "bold text"
    
    def test_bold_in_sentence(self):
        """Bold within a sentence should be unwrapped."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("This is **important** text")
        assert result == "This is important text"
    
    def test_multiple_bold_sections(self):
        """Multiple bold sections should all be unwrapped."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("**First** and **Second**")
        assert result == "First and Second"
    
    def test_bold_hebrew_text(self):
        """Bold Hebrew text should be unwrapped."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("**כותרת חשובה**")
        assert result == "כותרת חשובה"


class TestMarkdownHeaders:
    """Tests for Markdown header removal."""
    
    def test_h1_header_removal(self):
        """H1 header markers should be removed."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("# Title")
        assert result == "Title"
    
    def test_h2_header_removal(self):
        """H2 header markers should be removed."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("## Section Title")
        assert result == "Section Title"
    
    def test_h3_header_removal(self):
        """H3 header markers should be removed."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("### Subsection")
        assert result == "Subsection"
    
    def test_multiple_headers(self):
        """Multiple headers should all be cleaned."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("# Main\n## Sub\n### Detail")
        assert result == "Main\nSub\nDetail"
    
    def test_header_with_hebrew(self):
        """Hebrew header should be cleaned."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("## סיכום")
        assert result == "סיכום"


class TestBullets:
    """Tests for bullet point normalization."""
    
    def test_asterisk_to_dash(self):
        """Asterisk bullet should become dash."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("* Item")
        assert result == "- Item"
    
    def test_multiple_bullets(self):
        """Multiple bullets should all be converted."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("* First\n* Second\n* Third")
        assert result == "- First\n- Second\n- Third"
    
    def test_nested_bullet_indentation(self):
        """Nested bullets should have single space indentation."""
        sanitizer = MedicalLetterSanitizer()
        # Note: leading whitespace at start of text is stripped by final strip()
        # So we test nested bullets in context
        result = sanitizer.process("Top item\n  * Nested item")
        assert result == "Top item\n - Nested item"
    
    def test_deeply_nested_bullet(self):
        """Deeply nested bullets should have single space."""
        sanitizer = MedicalLetterSanitizer()
        # Note: leading whitespace at start of text is stripped by final strip()
        # So we test nested bullets in context
        result = sanitizer.process("Top item\n    * Deep nested")
        assert result == "Top item\n - Deep nested"
    
    def test_mixed_level_bullets(self):
        """Mixed level bullets should be normalized."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("* Top\n  * Nested\n* Another top")
        assert result == "- Top\n - Nested\n- Another top"


class TestExcessiveNewlines:
    """Tests for excessive newline normalization."""
    
    def test_three_newlines_to_two(self):
        """Three consecutive newlines should become two."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Para 1\n\n\nPara 2")
        assert result == "Para 1\n\nPara 2"
    
    def test_many_newlines_to_two(self):
        """Many consecutive newlines should become two."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Para 1\n\n\n\n\n\nPara 2")
        assert result == "Para 1\n\nPara 2"
    
    def test_two_newlines_unchanged(self):
        """Two newlines should remain unchanged."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("Para 1\n\nPara 2")
        assert result == "Para 1\n\nPara 2"


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""
    
    def test_empty_string(self):
        """Empty string should return empty."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("")
        assert result == ""
    
    def test_none_input(self):
        """None should return empty string."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process(None)
        assert result == ""
    
    def test_whitespace_only(self):
        """Whitespace-only string should return empty."""
        sanitizer = MedicalLetterSanitizer()
        result = sanitizer.process("   \n\n   ")
        assert result == ""
    
    def test_already_clean_text(self):
        """Already clean text should remain unchanged."""
        sanitizer = MedicalLetterSanitizer()
        clean_text = "This is clean text with no issues."
        result = sanitizer.process(clean_text)
        assert result == clean_text
    
    def test_plain_text_unchanged(self):
        """Plain text without LLM artifacts should pass through."""
        sanitizer = MedicalLetterSanitizer()
        text = "Patient presented with fever and cough."
        result = sanitizer.process(text)
        assert result == text


class TestCombinedTransformations:
    """Tests for combined transformations in realistic text."""
    
    def test_full_medical_letter(self):
        """Full medical letter with multiple issues should be cleaned."""
        sanitizer = MedicalLetterSanitizer()
        raw_text = """
**סיכום פגישה**

## פרטי המטופל
* שם: ישראל ישראלי
* תאריך לידה: 1985-03-15
* תאריך הפניה: 05/07/2025

## רקע רפואי
המטופל הגיע עם תלונות על—כאבים בחזה.

* סיפור מחלה נוכחי
  * תחילת התסמינים: 2025-01-10
  * משך התסמינים: שבועיים

**המלצות:**
בדיקות נוספות נדרשות.
"""
        result = sanitizer.process(raw_text)
        
        # Check no Markdown bold
        assert "**" not in result
        assert "__" not in result
        
        # Check no Markdown headers
        assert "##" not in result
        assert "#" not in result
        
        # Check dates are Israeli format
        assert "1985-03-15" not in result
        assert "15/3/85" in result
        assert "05/07/2025" not in result
        assert "5/7/25" in result
        
        # Check bullets are dashes
        assert "* " not in result
        assert "- " in result
        
        # Check em-dash replaced
        assert "—" not in result
        assert "-" in result
    
    def test_llm_styled_output(self):
        """Typical LLM-styled output should be humanized."""
        sanitizer = MedicalLetterSanitizer()
        raw = "**חשוב**: התור נקבע לתאריך 2025-12-15.\n\n## הערות\n* הבא מסמכים\n* הגע בזמן"
        result = sanitizer.process(raw)
        
        assert result == "חשוב: התור נקבע לתאריך 15/12/25.\n\nהערות\n- הבא מסמכים\n- הגע בזמן"
