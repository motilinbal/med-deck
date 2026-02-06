"""
Text processing utilities for MedDeck Server.

This module centralizes the logic for:
- Fixing encoding issues (Mojibake from Windows-1255 Hebrew text)
- Cleaning email bodies (removing hospital footers)
- Extracting text chunks using the standard delimiter
"""

import re
from typing import List

# Import DELIMITER from database.py for single source of truth
from database import DELIMITER


# Regex to detect if the string already contains valid Hebrew characters
HEBREW_PATTERN = re.compile(r'[\u0590-\u05FF]')

# The specific footer appended by the hospital's email server
# Defined exactly as it appears in the raw emails (CRLF line endings)
HOSPITAL_FOOTER = (
    "This Message confirms that this email message has been scanned by\r\n"
    "Hadassah for the presence of malicious code, vandals & computer viruses.\r\n\r\n"
    "WARNING - CONFIDENTIAL INFORMATION\r\n"
    "The information contained in the email may contain confidential or privileged information. "
    "If you are not the intended recipient please contact the sender."
)


def fix_encoding_issues(text: str) -> str:
    """
    Detects and fixes 'Mojibake' (garbled text) commonly caused by 
    Windows-1255 (Hebrew) text being misinterpreted as Latin-1.
    
    Logic:
    1. If the text already contains Hebrew characters, assume it is correct UTF-8.
    2. If not, try to encode as 'latin-1' (reverting to raw bytes) and decode as 'windows-1255'.
    3. If that fails (e.g. chars outside latin-1 range), return original text.
    
    Args:
        text: The input text that may contain encoding issues.
        
    Returns:
        The text with encoding issues fixed, or the original text if fixing fails.
    """
    if not text:
        return ""

    # 1. Verification: If we see Hebrew, the string is likely already fine.
    if HEBREW_PATTERN.search(text):
        return text

    try:
        # 2. Heuristic: The text is likely "Gibberish" from CP1255 -> Latin1 mismatch.
        # We reverse the damage by encoding back to bytes using Latin-1, 
        # then decoding with the correct Hebrew codepage.
        fixed_text = text.encode('latin-1').decode('windows-1255')
        return fixed_text
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 3. Fallback: The text contained characters that don't fit the Mojibake pattern 
        # (e.g., emojis or other scripts). Return original.
        return text


def clean_email_body(text: str) -> str:
    """
    Orchestrates the cleaning pipeline:
    1. Fix encoding.
    2. Remove the automated security footer.
    3. Strip whitespace.
    
    Args:
        text: The raw email body text.
        
    Returns:
        The cleaned email body text.
    """
    if not text:
        return ""
    
    # Step 1: Fix Encoding
    processed_text = fix_encoding_issues(text)
    
    # Step 2: Remove Footer
    # Attempt 1: Strict replacement (preserving CRLF if present)
    if HOSPITAL_FOOTER in processed_text:
        processed_text = processed_text.replace(HOSPITAL_FOOTER, "")
    else:
        # Attempt 2: Normalized replacement (Try \n instead of \r\n)
        # This helps if the email library auto-converted line endings.
        normalized_footer = HOSPITAL_FOOTER.replace("\r\n", "\n")
        if normalized_footer in processed_text:
            processed_text = processed_text.replace(normalized_footer, "")
            
    return processed_text.strip()


def extract_chunks(text: str) -> List[str]:
    """
    Extract text chunks from cleaned email body using the standard delimiter.
    
    Splits the text by the DELIMITER constant imported from database.py,
    strips whitespace from each chunk, and filters out empty strings.
    
    Args:
        text: The cleaned email body text.
        
    Returns:
        A list of non-empty text chunks.
    """
    if not text:
        return []
    
    # Split by delimiter and process each chunk
    raw_chunks = text.split(DELIMITER)
    
    # Strip whitespace and filter out empty chunks
    chunks = []
    for chunk in raw_chunks:
        stripped = chunk.strip()
        if stripped:  # Only include non-empty chunks
            chunks.append(stripped)
    
    return chunks
