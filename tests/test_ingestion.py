"""
Integration tests for the ingestion orchestration service.

Tests the approve/discard workflows while mocking expensive operations
like PDF processing and database calls.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio


class TestProcessIngestion:
    """Tests for the approve/ingestion workflow."""
    
    @pytest.fixture
    def mock_pending_data(self):
        """Sample pending ingestion data."""
        return {
            "_id": "pending_123",
            "card_id": "card_456",
            "email_uid": "email_789",
            "sender": "lab@hospital.com",
            "created_new_card": False,
            "clean_body_chunks": ["Patient has fever", "Blood pressure normal"],
            "has_pdf": True,
            "pdf_filename": "lab_results.pdf",
            "pdf_data": b"fake_pdf_bytes",
            "status": "waiting_approval"
        }
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.append_history_chunks')
    @patch('app.services.ingestion.delete_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    @patch('app.services.ingestion.ingest_pdf_process')
    async def test_process_ingestion_success_with_pdf(
        self, 
        mock_pdf_process, 
        mock_hub, 
        mock_delete, 
        mock_append, 
        mock_get_pending,
        mock_pending_data
    ):
        """
        Test successful ingestion with both text chunks and PDF.
        
        Verifies:
        - Pending data is retrieved
        - Text chunks are appended to history
        - PDF is processed
        - Pending record is deleted
        - Progress notifications are sent
        - Success notification is sent
        """
        # Configure mock to be awaitable
        mock_hub.notify_progress = AsyncMock()
        mock_hub.trigger_sync = AsyncMock()
        
        # Setup mocks
        mock_get_pending.return_value = mock_pending_data
        mock_delete.return_value = True
        mock_pdf_process.return_value = {"status": "success"}
        
        # Import after patching
        from app.services.ingestion import process_ingestion
        
        # Execute
        await process_ingestion("card_456", "pending_123")
        
        # Verify
        mock_get_pending.assert_called_once_with("pending_123")
        
        # Text chunks should be appended (joined with delimiter)
        mock_append.assert_called_once()
        call_args = mock_append.call_args
        assert call_args[0][0] == "card_456"  # card_id
        assert "Patient has fever" in call_args[0][1]  # text contains chunks
        assert "Blood pressure normal" in call_args[0][1]
        
        # PDF should be processed
        mock_pdf_process.assert_called_once()
        pdf_call_args = mock_pdf_process.call_args
        
        # Check Positional Arguments (args[0])
        # Arg 0: Temp file path
        assert pdf_call_args[0][0].endswith('.pdf')
        # Arg 1: Output directory
        assert pdf_call_args[0][1] == "output"
        # Arg 2: The callback function
        assert callable(pdf_call_args[0][2])
        
        # Pending should be deleted
        mock_delete.assert_called_once_with("pending_123")
        
        # Progress notifications should be sent
        assert mock_hub.notify_progress.call_count >= 3  # Multiple progress updates
        
        # Final success notification
        success_calls = [call for call in mock_hub.notify_progress.call_args_list 
                        if call[1].get('state') == 'success' or 
                        (len(call[0]) >= 3 and call[0][2] == 'success')]
        assert len(success_calls) >= 1
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.append_history_chunks')
    @patch('app.services.ingestion.delete_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    @patch('app.services.ingestion.ingest_pdf_process')
    async def test_process_ingestion_text_only(
        self, 
        mock_pdf_process, 
        mock_hub, 
        mock_delete, 
        mock_append, 
        mock_get_pending
    ):
        """
        Test ingestion with text chunks only (no PDF).
        
        Verifies PDF processing is skipped when has_pdf is False.
        """
        # Configure mock to be awaitable
        mock_hub.notify_progress = AsyncMock()
        
        # Setup mock data without PDF
        pending_data = {
            "card_id": "card_456",
            "clean_body_chunks": ["Text chunk 1", "Text chunk 2"],
            "has_pdf": False,
            "pdf_data": None,
        }
        mock_get_pending.return_value = pending_data
        mock_delete.return_value = True
        
        # Import after patching
        from app.services.ingestion import process_ingestion
        
        # Execute
        await process_ingestion("card_456", "pending_123")
        
        # Verify
        mock_append.assert_called_once()  # Text should still be appended
        mock_pdf_process.assert_not_called()  # PDF should NOT be processed
        mock_delete.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    async def test_process_ingestion_not_found(
        self, 
        mock_hub, 
        mock_get_pending
    ):
        """
        Test handling when pending ingestion doesn't exist.
        
        Verifies error notification is sent.
        """
        # Configure mock to be awaitable
        mock_hub.notify_progress = AsyncMock()
        
        mock_get_pending.return_value = None
        
        # Import after patching
        from app.services.ingestion import process_ingestion
        
        # Execute
        await process_ingestion("card_456", "nonexistent")
        
        # Verify error notification
        error_calls = [call for call in mock_hub.notify_progress.call_args_list 
                      if call[1].get('state') == 'error' or 
                      (len(call[0]) >= 3 and call[0][2] == 'error')]
        assert len(error_calls) >= 1
        assert "not found" in error_calls[0][0][1].lower()


class TestDiscardIngestion:
    """Tests for the discard workflow."""
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.delete_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    @patch('app.services.ingestion.delete_card_by_id')
    async def test_discard_provisional_card(
        self, 
        mock_delete_card, 
        mock_hub, 
        mock_delete_pending, 
        mock_get_pending
    ):
        """
        Test discarding a 'Patient X' ingestion that created a new card.
        
        Verifies:
        - The card is deleted
        - Sync is triggered for all clients
        - Pending record is deleted
        """
        # Configure mock to be awaitable
        mock_hub.trigger_sync = AsyncMock()
        
        # Setup mock for Patient X workflow
        mock_get_pending.return_value = {
            "created_new_card": True,
            "card_id": "new_card_123"
        }
        mock_delete_card.return_value = True
        mock_delete_pending.return_value = True
        
        # Import after patching
        from app.services.ingestion import discard_ingestion
        
        # Execute
        await discard_ingestion("new_card_123", "pending_123")
        
        # Verify card deletion
        mock_delete_card.assert_called_once_with("new_card_123")
        
        # Verify sync triggered
        mock_hub.trigger_sync.assert_called_once()
        
        # Verify pending deleted
        mock_delete_pending.assert_called_once_with("pending_123")
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.delete_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    @patch('app.services.ingestion.delete_card_by_id')
    async def test_discard_existing_card(
        self, 
        mock_delete_card, 
        mock_hub, 
        mock_delete_pending, 
        mock_get_pending
    ):
        """
        Test discarding an ingestion for an existing card (Patient 5).
        
        Verifies:
        - The card is NOT deleted
        - Sync is NOT triggered
        - Pending record is deleted
        """
        # Setup mock for existing card workflow
        mock_get_pending.return_value = {
            "created_new_card": False,
            "card_id": "existing_card_456"
        }
        mock_delete_pending.return_value = True
        
        # Import after patching
        from app.services.ingestion import discard_ingestion
        
        # Execute
        await discard_ingestion("existing_card_456", "pending_456")
        
        # Verify card NOT deleted
        mock_delete_card.assert_not_called()
        
        # Verify sync NOT triggered
        mock_hub.trigger_sync.assert_not_called()
        
        # Verify pending deleted
        mock_delete_pending.assert_called_once_with("pending_456")
    
    @pytest.mark.asyncio
    @patch('app.services.ingestion.get_pending_ingestion')
    @patch('app.services.ingestion.delete_pending_ingestion')
    @patch('app.services.ingestion.notification_hub')
    async def test_discard_already_deleted(
        self, 
        mock_hub, 
        mock_delete_pending, 
        mock_get_pending
    ):
        """
        Test discarding when pending record is already gone.
        
        Should handle gracefully without errors.
        """
        mock_get_pending.return_value = None
        
        # Import after patching
        from app.services.ingestion import discard_ingestion
        
        # Execute - should not raise
        await discard_ingestion("card_123", "pending_missing")
        
        # Verify no deletion attempted
        mock_delete_pending.assert_not_called()
