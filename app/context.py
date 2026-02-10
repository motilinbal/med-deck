"""
Context management for MedDeck Agent tools.

This module provides thread-safe and async-safe context management for the
active card ID using Python's contextvars. This ensures that:

1. Tools can access the current card ID without it being passed as a parameter
2. Multiple concurrent requests don't interfere with each other
3. The LLM never sees or needs to generate card IDs

Usage:
    # In agent.py, set the context at the start of run_agent():
    token = active_card_id.set(card_id)
    try:
        # ... agent logic ...
    finally:
        active_card_id.reset(token)
    
    # In tools, retrieve the card ID:
    card_id = get_card_id()
"""

from contextvars import ContextVar
from functools import wraps

# Context variable to hold the current card ID during request processing
# This is thread-safe and async-safe - each request sees its own value
active_card_id: ContextVar[str] = ContextVar("active_card_id", default=None)


def get_card_id() -> str:
    """
    Retrieves the current card ID from context.
    
    This function should be called from within tool functions to get the
    card ID that was set by the agent's run_agent() function.
    
    Returns:
        The current card ID string.
    
    Raises:
        ValueError: If called outside of an active card context
                   (i.e., before run_agent set the context)
    """
    val = active_card_id.get()
    if not val:
        raise ValueError(
            "Security Error: Tool called outside of an active card context. "
            "Ensure the agent is running within a valid patient session."
        )
    return val


def require_card_id(func):
    """
    Decorator that ensures a tool runs within a valid card context.
    
    This replaces repetitive try/except blocks in every tool function.
    If no card context is set, returns a user-friendly error message
    instead of crashing.
    
    The decorator:
    1. Verifies that a card context exists before executing the tool
    2. Returns a clear error message if context is missing
    3. Preserves the original function's metadata (name, docstring, etc.)
    
    Usage:
        @require_card_id
        async def tool_get_something() -> str:
            card_id = get_card_id()
            # ... tool logic
    
    Args:
        func: The async tool function to wrap.
    
    Returns:
        The wrapped function that checks context before execution.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            # Verify context exists before executing the tool
            _ = get_card_id()
        except ValueError as e:
            return f"System Error: {str(e)}"
        
        return await func(*args, **kwargs)
    return wrapper
