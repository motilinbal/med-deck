"""
Transient Log Utility - Async Context Manager for Self-Cleaning Status Messages.

This module provides the TransientLog context manager for creating status messages
that automatically disappear when an operation completes, succeeds, or fails.

Usage:
    from app.utils.transient import TransientLog
    from models import MessageRole
    
    async with TransientLog(card_id, "Scanning PDF..."):
        await process_pdf()
        # Message automatically removed when exiting context
"""

import logging
from typing import Optional

import database as db
from models import MessageRole

logger = logging.getLogger(__name__)


class TransientLog:
    """
    Async Context Manager for transient user-facing logs.
    
    Creates a LOG message on entry and guarantees its removal on exit,
    regardless of whether the operation succeeded or raised an exception.
    
    This prevents "ghost logs" from cluttering the UI when errors occur
    and eliminates the need for manual cleanup in every code path.
    
    Args:
        card_id: The MongoDB ObjectId string of the patient card
        text: The status message text to display to the user
        
    Example:
        # Basic usage
        async with TransientLog(card_id, "Fetching lab results..."):
            results = await fetch_labs()
        
        # With exception handling (cleanup still guaranteed)
        async with TransientLog(card_id, "Processing document..."):
            try:
                await risky_operation()
            except Exception:
                # Log is still cleaned up even if we re-raise
                raise
    """
    
    def __init__(self, card_id: str, text: str):
        self.card_id = card_id
        self.text = text
        self.msg_id: Optional[str] = None
        self._created = False

    async def __aenter__(self) -> "TransientLog":
        """
        Enter the context and create the transient log message.
        
        Returns:
            The TransientLog instance (allows access to msg_id if needed)
        """
        try:
            # Create the transient message with LOG role
            msg = await db.append_chat_message(
                self.card_id, 
                MessageRole.LOG, 
                self.text
            )
            self.msg_id = msg.id
            self._created = True
            logger.debug(f"Created transient log {self.msg_id}: {self.text}")
        except Exception as e:
            # Log creation failure shouldn't break the operation
            logger.warning(f"Failed to create transient log: {e}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """
        Exit the context and guarantee cleanup of the log message.
        
        This method is called even if an exception was raised in the
        context block, ensuring the transient message is always removed.
        
        Args:
            exc_type: Type of exception (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)
            
        Returns:
            False to allow any exception to propagate
        """
        if self._created and self.msg_id:
            try:
                await db.remove_chat_message(self.card_id, self.msg_id)
                logger.debug(f"Cleaned up transient log {self.msg_id}")
            except Exception as e:
                # Cleanup failure shouldn't hide the original error
                logger.warning(f"Failed to cleanup transient log {self.msg_id}: {e}")
        
        # Return False to allow exceptions to propagate normally
        return False
    
    @property
    def message_id(self) -> Optional[str]:
        """
        Get the ID of the created message (available after __aenter__).
        
        Useful if you need to reference the message ID for other purposes.
        
        Returns:
            The message ID string, or None if creation failed
        """
        return self.msg_id
