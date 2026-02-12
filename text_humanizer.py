import re
from typing import Callable, List

class MedicalLetterSanitizer:
    def __init__(self):
        self._compile_patterns()

    def _compile_patterns(self):
        # 1. ISO Date Pattern: YYYY-MM-DD (e.g., 2025-12-05)
        self.date_iso_pattern = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b')

        # 2. NEW: Slashed Date Pattern: DD/MM/YYYY or D/M/YY (e.g., 08/12/2025)
        # We catch 2 or 4 digit years to ensure we normalize everything.
        self.date_slash_pattern = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b')
        
        # 3. Markdown Bold/Italic
        self.bold_pattern = re.compile(r'\*\*(.*?)\*\*|__(.*?)__')
        
        # 4. Headers
        self.header_pattern = re.compile(r'^#{1,6}\s*', re.MULTILINE)
        
        # 5. Em-Dashes
        self.em_dash_pattern = re.compile(r'—')
        
        # 6. Bullets
        self.bullet_pattern = re.compile(r'^(\s*)\*\s+(.*)', re.MULTILINE)

    def _format_date_standard(self, day, month, year):
        """Helper to formatting strict D/M/YY"""
        # Strip leading zeros via int()
        d = int(day)
        m = int(month)
        # Ensure year is 2 digits
        y = year[-2:] 
        return f"{d}/{m}/{y}"

    def _callback_iso_date(self, match: re.Match) -> str:
        """Converts YYYY-MM-DD -> D/M/YY"""
        return self._format_date_standard(
            day=match.group(3), 
            month=match.group(2), 
            year=match.group(1)
        )

    def _callback_slash_date(self, match: re.Match) -> str:
        """Converts 08/12/2025 -> 8/12/25"""
        return self._format_date_standard(
            day=match.group(1), 
            month=match.group(2), 
            year=match.group(3)
        )

    def _normalize_bullets(self, match: re.Match) -> str:
        indentation = match.group(1)
        content = match.group(2)
        if len(indentation) > 0:
            return f" - {content}"
        return f"- {content}"

    def process(self, text: str) -> str:
        if not text: return ""

        # 1. Fix ISO Dates (2025-12-05 -> 5/12/25)
        text = self.date_iso_pattern.sub(self._callback_iso_date, text)

        # 2. Fix Existing Slashed Dates (08/12/2025 -> 8/12/25)
        text = self.date_slash_pattern.sub(self._callback_slash_date, text)

        # 3. Standard cleanups
        text = self.em_dash_pattern.sub('-', text)
        text = self.bold_pattern.sub(lambda m: m.group(1) or m.group(2), text)
        text = self.header_pattern.sub('', text)
        text = self.bullet_pattern.sub(self._normalize_bullets, text)
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

# --- Execution Example ---

if __name__ == "__main__":
    # The raw input provided in your prompt
    raw_llm_output = """

    """

    sanitizer = MedicalLetterSanitizer()
    clean_text = sanitizer.process(raw_llm_output)

    print("--- Processed Output ---")
    print(clean_text)
