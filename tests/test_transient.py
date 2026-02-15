"""
Tests for the TransientLog class, specifically for minimum duration functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.utils.transient import TransientLog


class TestTransientLogMinDuration:
    """Tests for the min_duration parameter of TransientLog."""

    @pytest.mark.asyncio
    async def test_min_duration_zero_no_wait(self):
        """Test that min_duration=0 (default) doesn't wait."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.return_value = 0.0
            
            # Test with default min_duration (0.0)
            async with TransientLog("card123", "Test message"):
                pass
            
            # Should NOT have slept
            mock_sleep.assert_not_called()
            mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_duration_fast_task_waits_remaining(self):
        """Test that fast task waits for remaining time to meet min_duration."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock - time progresses from 0.0 to 0.5 (0.5 seconds elapsed)
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.side_effect = [0.0, 0.5]  # enter, exit
            
            # min_duration=2.0, elapsed=0.5, remaining=1.5
            async with TransientLog("card123", "Test message", min_duration=2.0):
                pass
            
            # Should have slept for 1.5 seconds
            mock_sleep.assert_called_once_with(1.5)
            mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_duration_slow_task_no_extra_wait(self):
        """Test that slow task (longer than min_duration) doesn't wait extra."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock - time progresses from 0.0 to 3.0 (3 seconds elapsed)
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.side_effect = [0.0, 3.0]  # enter, exit
            
            # min_duration=2.0, elapsed=3.0, remaining=-1.0 (negative = no wait)
            async with TransientLog("card123", "Test message", min_duration=2.0):
                pass
            
            # Should NOT have slept (remaining was negative)
            mock_sleep.assert_not_called()
            mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_duration_with_exception(self):
        """Test that min_duration works even when exception is raised."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock - task fails after 0.3 seconds
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.side_effect = [0.0, 0.3]  # enter, exit
            
            # min_duration=2.0, elapsed=0.3, remaining=1.7
            with pytest.raises(ValueError):
                async with TransientLog("card123", "Test message", min_duration=2.0):
                    raise ValueError("Task failed")
            
            # Should have slept for remaining time
            mock_sleep.assert_called_once_with(1.7)
            # Should still cleanup
            mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_min_duration_exact_match(self):
        """Test that exact match of min_duration doesn't wait."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock - elapsed time exactly equals min_duration
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.side_effect = [0.0, 2.0]  # enter, exit
            
            # min_duration=2.0, elapsed=2.0, remaining=0.0
            async with TransientLog("card123", "Test message", min_duration=2.0):
                pass
            
            # Should NOT have slept (remaining <= 0)
            mock_sleep.assert_not_called()
            mock_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_backward_compatibility(self):
        """Test that existing code without min_duration still works."""
        with patch('app.utils.transient.db.append_chat_message') as mock_append, \
             patch('app.utils.transient.db.remove_chat_message') as mock_remove, \
             patch('app.utils.transient.time.monotonic') as mock_time, \
             patch('app.utils.transient.asyncio.sleep') as mock_sleep:
            
            # Setup mock
            mock_msg = MagicMock()
            mock_msg.id = "test-msg-id"
            mock_append.return_value = mock_msg
            mock_time.return_value = 0.0
            
            # Old usage without min_duration parameter
            async with TransientLog("card123", "Test message"):
                pass
            
            # Should work exactly as before
            mock_sleep.assert_not_called()
            mock_remove.assert_called_once()
            assert mock_append.call_count == 1
