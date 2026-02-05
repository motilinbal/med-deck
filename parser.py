import re
from typing import Dict, Union, Any
from datetime import datetime
from typing import Optional

def parse_lab_result_v3(raw_input: Union[str, int, float]) -> Dict[str, Any]:
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

