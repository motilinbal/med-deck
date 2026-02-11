"""
Time utilities for MedDeck Server.

This module provides timezone-aware datetime functions,
centralizing Israel time handling for consistent behavior
regardless of the server's physical location.
"""

from datetime import datetime
import pytz

# Israel timezone constant
ISRAEL_TZ = pytz.timezone('Asia/Jerusalem')


def get_israel_now() -> datetime:
    """
    Get the current date and time in Israel timezone.

    Returns:
        A timezone-aware datetime object for the current time in Israel.
    """
    return datetime.now(ISRAEL_TZ)


def format_israel_datetime(dt: datetime | None = None, fmt: str = '%Y-%m-%d %H:%M') -> str:
    """
    Format a datetime as a string in Israel timezone.

    Args:
        dt: The datetime to format. If None, uses current Israel time.
        fmt: The strftime format string. Default is '%Y-%m-%d %H:%M'.

    Returns:
        The formatted datetime string.
    """
    if dt is None:
        dt = get_israel_now()
    # Ensure the datetime is timezone-aware in Israel time
    if dt.tzinfo is None:
        dt = ISRAEL_TZ.localize(dt)
    else:
        dt = dt.astimezone(ISRAEL_TZ)
    return dt.strftime(fmt)


def get_israel_date_str() -> str:
    """
    Get today's date in Israel as a formatted string.

    Returns:
        Date string in format 'YYYY-MM-DD'.
    """
    return get_israel_now().strftime('%Y-%m-%d')


def get_israel_time_str() -> str:
    """
    Get the current time in Israel as a formatted string.

    Returns:
        Time string in format 'HH:MM'.
    """
    return get_israel_now().strftime('%H:%M')
