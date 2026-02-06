import re
from typing import Dict, Union, Any
from datetime import datetime
from typing import Optional


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

