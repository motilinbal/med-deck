import re
import difflib
from typing import Dict, Union, Any
from datetime import datetime
from typing import Optional
import math
from collections import Counter


def parse_lab_result(raw_input: Union[str, int, float]) -> Dict[str, Any]:
    """
    Parses lab results with robust directional logic and non-greedy regex
    to correctly handle multi-character suffix operators (e.g., '5.5 >=').
    """
    
    if isinstance(raw_input, (int, float)):
        return {"value": raw_input, "operator": "="}
    
    if raw_input is None:
        return {"value": None, "operator": "="}

    clean_str = str(raw_input).strip()
    
    # Flip logic for Suffix operators
    flip_map = {
        "<": ">",
        ">": "<",
        "<=": ">=",
        ">=": "<=",
        "=": "=" 
    }

    # Dynamic Regex Construction
    # We sort by length (descending) so longer operators (<=) are checked before shorter ones (<)
    ops = ["<=", ">=", "<", ">", "="]
    ops_regex = "|".join(map(re.escape, ops))  # Result: "<=|>=|<|>|="

    # 1. PREFIX Pattern: Operator at start (e.g. "< 20")
    #    ^ matches start, (ops) matches operator, (.*) matches the rest
    prefix_pattern = re.compile(rf"^({ops_regex})\s*(.*)$")
    
    # 2. SUFFIX Pattern: Operator at end (e.g. "20 <")
    #    We use (.*?) for the value. The '?' makes it NON-GREEDY.
    #    It stops as soon as it hits a valid operator match at the end.
    suffix_pattern = re.compile(rf"^(.*?)\s*({ops_regex})$")

    detected_operator = "="
    value_str = clean_str

    prefix_match = prefix_pattern.match(clean_str)
    suffix_match = suffix_pattern.match(clean_str)

    if prefix_match:
        # Case: "< 2.4"
        detected_operator = prefix_match.group(1)
        value_str = prefix_match.group(2).strip()
        
    elif suffix_match:
        # Case: "5.5 >="
        # logic: 5.5 >= x  -->  x <= 5.5
        raw_op = suffix_match.group(2)
        detected_operator = flip_map.get(raw_op, raw_op)
        value_str = suffix_match.group(1).strip()

    # Type Conversion
    final_value = value_str
    try:
        final_value = int(value_str)
    except ValueError:
        try:
            final_value = float(value_str)
        except ValueError:
            final_value = value_str

    if final_value == "":
        final_value = None

    return {
        "value": final_value,
        "operator": detected_operator
    }


def create_mongo_timestamp(date_str: str, time_str: str) -> Optional[datetime]:
    """
    Combines DD/MM/YY date and HH:MM time into a MongoDB-compatible datetime object.
    
    Args:
        date_str: Date string in "DD/MM/YY" format (e.g., "31/01/24")
        time_str: Time string in "HH:MM" format (e.g., "14:30")
        
    Returns:
        datetime object (UTC) ready for MongoDB insertion, or None if invalid.
    """
    if not date_str or not time_str:
        return None

    try:
        # 1. Concatenate for single-pass parsing
        full_string = f"{date_str} {time_str}"
        
        # 2. Parse using the specific format
        # %d = Day, %m = Month, %y = 2-digit Year, %H = 24-hour, %M = Minute
        dt_object = datetime.strptime(full_string, "%d/%m/%y %H:%M")
        
        # 3. Best Practice: Ensure no timezone confusion.
        # Native datetime objects are "naive". If your server is UTC (standard), this is fine.
        # If you need specific timezones, simple naive datetime is usually safest for storage
        # provided you treat everything as local or everything as UTC.
        return dt_object

    except ValueError:
        # Handle cases where data is malformed (e.g. "32/01/24" or "25:00")
        return None


def remove_date_padding(text: str) -> str:
    """
    Scans a text for dates in DD/MM/YY or DD/MM formats and removes 
    leading 'space-filling' zeros from the Day and Month parts only.
    
    Preserves:
    - Trailing zeros (10, 20, 30)
    - Year parts (08 in 2008 stays 08)
    - Valid date structures only
    """
    
    # REGEX EXPLANATION:
    # \b          -> Word boundary (ensures we don't cut inside "100/200")
    # (?P<d>...)  -> Named group 'd' for Day
    # 0?          -> Optional leading zero
    # [1-9]       -> Digits 1-9
    # |           -> OR
    # [12]\d      -> 10-29
    # |           -> OR
    # 3[01]       -> 30-31
    #
    # The Month pattern is similar but limited to 1-12.
    # The Year pattern is optional (?:...)? and expects 2 or 4 digits.
    
    date_pattern = re.compile(
        r'\b(?P<d>0?[1-9]|[12]\d|3[01])/'   # Day Part + Separator
        r'(?P<m>0?[1-9]|1[0-2])'            # Month Part
        r'(?:/(?P<y>\d{4}|\d{2}))?\b'       # Optional Year Part (2 or 4 digits)
    )

    def replacement_logic(match):
        # Extract the parts identified by the regex
        day_str = match.group('d')
        month_str = match.group('m')
        year_str = match.group('y')

        # LOGIC: Convert to int and back to str.
        # This naturally strips leading zeros (int("05") -> 5 -> "5")
        # while keeping valid zeros (int("10") -> 10 -> "10").
        clean_day = str(int(day_str))
        clean_month = str(int(month_str))

        if year_str:
            # If year exists, reconstruct full date. 
            # Note: We do NOT touch year_str to preserve "08" or "09".
            return f"{clean_day}/{clean_month}/{year_str}"
        else:
            # Reconstruct partial date
            return f"{clean_day}/{clean_month}"

    # re.sub can accept a function as the second argument!
    return date_pattern.sub(replacement_logic, text)


def quantify_text_divergence(text1: str, text2: str, shingle_size: int = 5):
    """
    Quantifies whether text2 contains significant NEW information not present in text1.
    
    This is an "Incremental Delta" approach designed for medical notes where physicians
    often copy-paste previous notes and add small updates. Instead of checking for
    similarity (which fails on copy-paste), we check for NOVELTY - what new content
    was added.
    
    Args:
        text1: The reference/existing text (usually from database)
        text2: The new/candidate text (usually from incoming email)
        shingle_size: Ignored in this implementation (kept for backward compatibility)
                    
    Returns:
        dict: Analysis results including:
              - 'is_same_source': Boolean - True if text2 is a duplicate/redundant
              - 'prob_different_sources': p-value (1.0 - novelty_score)
              - 'novelty_score': Fraction of text2 that is new content
              - 'metrics': Detailed metrics including inserted_char_count
    """
    
    # --- 0. Exact Match Check ---
    if text1.strip() == text2.strip():
        return {
            "is_same_source": True,
            "prob_different_sources": 0.0,
            "novelty_score": 0.0,
            "metrics": {
                "jaccard_similarity": 1.0,
                "containment_score": 1.0,
                "inserted_char_count": 0
            },
            "interpretation": "Exact duplicate"
        }
    
    # --- 1. Normalize for comparison ---
    def normalize(text):
        """Normalize whitespace and lowercase for comparison."""
        return re.sub(r'\s+', ' ', text.lower()).strip()
    
    norm_old = normalize(text1)
    norm_new = normalize(text2)
    
    # Handle empty cases
    if not norm_old or not norm_new:
        return {
            "error": "One or both texts are empty.",
            "prob_different_sources": 1.0,
            "is_same_source": False,
            "novelty_score": 1.0 if norm_new and not norm_old else 0.0
        }
    
    # --- 2. Sequence Alignment (Diff) ---
    # Use difflib to find inserted blocks - this is the "delta"
    matcher = difflib.SequenceMatcher(None, norm_old, norm_new)
    opcodes = matcher.get_opcodes()
    
    inserted_texts = []
    inserted_length = 0
    
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ('insert', 'replace'):
            # This is new content in text2 that wasn't in text1
            segment = norm_new[j1:j2]
            inserted_texts.append(segment)
            inserted_length += len(segment)
    
    # --- 3. Calculate Novelty Score ---
    # Fraction of the NEW text that is actually new content
    total_new_length = len(norm_new) if len(norm_new) > 0 else 1
    novelty_score = inserted_length / total_new_length
    
    # --- 4. Critical Keywords Detection ---
    # High-value medical terms that indicate important updates
    # If these appear in the NEW content, it's definitely not a duplicate
    critical_keywords = [
        # English medical terms
        "plan", "recommendation", "assessment", "diagnosis", "referral", 
        "treatment", "medication", "procedure", "surgery", "follow-up",
        # Hebrew terms
        "המלצה", "המלצות",  # Recommendations
        "לסיכום", "סיכום",     # Summary
        "תוכנית", "טיפול",     # Treatment plan
        "הפניה", "דחוף",       # Referral, urgent
        "להתחיל", "להפסיק",   # Start, stop (medications)
        "שינוי", "בדיקה",     # Change, test
        "תוצאות", "ממצא",     # Results, finding
        "תאריך", "יום",        # Date, day (new dates indicate updates)
    ]
    
    full_inserted = " ".join(inserted_texts).lower()
    critical_hits = [kw for kw in critical_keywords if kw in full_inserted]
    
    # --- 5. Decision Logic ---
    # NOT a duplicate (is_same_source = False) means: ACCEPT as new chunk
    # IS a duplicate (is_same_source = True) means: SKIP as redundant
    
    is_duplicate = True  # Default to duplicate
    reason = "Insufficient novelty"
    
    # Even small new content blocks (>5 chars) might be important
    # This catches incremental medical note updates (e.g., date changes)
    if inserted_length > 5:
        is_duplicate = False
        reason = f"New content block ({inserted_length} chars)"
    # Significant new content (>0.5% of document) - lowered from 2%
    elif novelty_score > 0.005:
        is_duplicate = False
        reason = f"Significant new content ({novelty_score*100:.1f}%)"
    # Critical keywords override everything - if there's a new plan/recommendation, accept
    elif len(critical_hits) > 0:
        is_duplicate = False
        reason = f"Critical updates: {', '.join(critical_hits[:3])}"
    
    # The function returns is_same_source = is_duplicate (True = skip)
    return {
        "is_same_source": is_duplicate,
        "prob_different_sources": float(f"{1.0 - novelty_score:.6f}"),
        "novelty_score": float(f"{novelty_score:.4f}"),
        "metrics": {
            "jaccard_similarity": float(f"{1.0 - novelty_score:.4f}"),
            "containment_score": float(f"{1.0 - novelty_score:.4f}"),
            "inserted_char_count": inserted_length,
            "critical_keywords_found": critical_hits
        },
        "interpretation": reason
    }

