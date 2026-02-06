import re
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
    Quantifies the probability that two texts originated from different sources
    using n-gram shingling and containment analysis.
    
    Args:
        text1 (str): First text.
        text2 (str): Second text.
        shingle_size (int): Number of tokens per shingle (n-gram length).
                            5-9 is standard for prose.
                            
    Returns:
        dict: Analysis results including:
              - 'is_same_source': Boolean decision based on alpha 0.05.
              - 'prob_different_sources': The p-value (probability they are unrelated).
              - 'containment_score': How much of the smaller text is found in the larger one.
              - 'matching_shingles': Count of common unique patterns.
    """
    
    # --- 0. Exact Match Check ---
    # Before any processing, check for exact duplicates (case-insensitive)
    if text1.lower().strip() == text2.lower().strip():
        return {
            "is_same_source": True,
            "prob_different_sources": 0.0,
            "metrics": {
                "jaccard_similarity": 1.0,
                "containment_score": 1.0,
                "matching_shingles": -1,  # Indicates exact match
                "total_shingles_min": -1
            },
            "interpretation": "Exact duplicate"
        }
    
    # --- 1. Preprocessing (Lightweight & Efficient) ---
    def tokenizer(text):
        # Lowercase and split by non-alphanumeric characters
        return re.findall(r'\w+', text.lower())

    tokens1 = tokenizer(text1)
    tokens2 = tokenizer(text2)

    if not tokens1 or not tokens2:
        return {
            "error": "One or both texts are empty.",
            "prob_different_sources": 1.0,
            "is_same_source": False
        }

    # --- 2. Shingling (Sliding Window) ---
    # We use a set for O(1) lookups. This removes duplicate phrases within the same text,
    # treating the text as a "bag of phrases" rather than a sequence.
    def get_shingles(tokens, size):
        if len(tokens) < size:
            return set([tuple(tokens)])
        return set(tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1))

    shingles1 = get_shingles(tokens1, shingle_size)
    shingles2 = get_shingles(tokens2, shingle_size)

    # --- 3. Intersection & Metrics ---
    intersection = shingles1.intersection(shingles2)
    match_count = len(intersection)
    
    min_set_size = min(len(shingles1), len(shingles2))
    union_size = len(shingles1.union(shingles2))
    
    # Jaccard: |A ∩ B| / |A ∪ B| (Good for overall similarity)
    jaccard_index = match_count / union_size if union_size > 0 else 0.0
    
    # Containment: |A ∩ B| / min(|A|, |B|) (Crucial for "missing chunks" scenario)
    # If Text B is a sub-chapter of Text A, Jaccard is low, but Containment is 1.0.
    containment = match_count / min_set_size if min_set_size > 0 else 0.0

    # --- 4. Complete Containment Check ---
    # If one text is entirely contained within the other, they share the same source
    if containment >= 0.95:  # Allow for minor differences (whitespace, punctuation)
        return {
            "is_same_source": True,
            "prob_different_sources": 0.0,
            "metrics": {
                "jaccard_similarity": float(f"{jaccard_index:.4f}"),
                "containment_score": float(f"{containment:.4f}"),
                "matching_shingles": match_count,
                "total_shingles_min": min_set_size
            },
            "interpretation": "Complete containment - same source"
        }

    # --- 5. Probability Estimation (The "Git" Logic) ---
    # Hypothesis Testing:
    # H0: The texts are independent (from different sources).
    # H1: The texts share a common source.
    
    # We estimate the probability of H0 being true given the observed overlap.
    # In natural language, a specific 5-gram (e.g. "the quick brown fox jumped")
    # is extremely rare. The probability of randomly matching N unique 5-grams
    # drops exponentially.
    
    # We model this roughly as: P(Different) ~= (Chance_of_Random_Collision) ^ Match_Count
    # A conservative collision rate for 5-grams in English is ~1e-5.
    
    # However, to be robust against "common idioms" (like "in the end"), we apply a
    # "significance threshold". We ignore the first few matches as potential noise.
    
    # Scale noise threshold based on text length (max 10% of shingles)
    noise_threshold = min(2, int(min_set_size * 0.1))
    effective_matches = max(0, match_count - noise_threshold)
    
    # If containment is high, p-value crashes to 0 immediately.
    # We use an exponential decay function.
    # 0.5 is an aggressive decay factor; it assumes every new matching shingle
    # halves the probability that they are unrelated.
    if effective_matches == 0:
        p_value = 1.0
    else:
        # P-value calculation using a simplified Poisson approximation logic
        # High containment = Low P-value (High confidence they are same source)
        p_value = math.exp(-0.5 * effective_matches)

        # Correction for extremely short texts where chance collisions are higher
        if min_set_size < 10:
            p_value = min(1.0, p_value * 2)

    return {
        "is_same_source": p_value < 0.05,
        "prob_different_sources": float(f"{p_value:.6f}"),
        "metrics": {
            "jaccard_similarity": float(f"{jaccard_index:.4f}"),
            "containment_score": float(f"{containment:.4f}"),
            "matching_shingles": match_count,
            "total_shingles_min": min_set_size
        },
        "interpretation": "High likelihood of common origin" if p_value < 0.05 else "Likely independent sources"
    }

