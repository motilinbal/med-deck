"""
Ingestion Orchestrator Service for MedDeck Server.

This module coordinates the ingestion of pending email data (text chunks and PDFs)
into the permanent patient record. It handles:
- Retrieving staged data from pending_ingestions collection
- Appending text chunks to patient history
- Processing PDFs through the OCR pipeline with live progress updates
- Cleaning up staging records after successful ingestion

Usage:
    from app.services.ingestion import process_ingestion
    
    # Called from API endpoint with BackgroundTasks
    await process_ingestion(card_id, pending_id)
"""

import asyncio
import logging
import tempfile
import os
from typing import Optional

from database import (
    get_pending_ingestion,
    append_raw_chunks,
    delete_pending_ingestion,
    delete_card_by_id,
    DELIMITER,
)
from ingest_pdf import process_pdf as ingest_pdf_process
from app.services.notification_hub import notification_hub
from app.services.scribe import trigger_processing

logger = logging.getLogger(__name__)


async def process_ingestion(card_id: str, pending_id: str):
    """
    Process a pending ingestion: import text chunks and PDF into the patient's record.
    
    This function orchestrates the heavy lifting of ingestion:
    1. Retrieves staged data from pending_ingestions
    2. Appends text chunks to patient history
    3. Processes PDF through OCR pipeline (in thread to avoid blocking)
    4. Sends live progress updates via WebSocket
    5. Cleans up the pending record
    
    Args:
        card_id: The patient card ID to ingest data into
        pending_id: The pending ingestion document ID
    """
    logger.info(f"Starting ingestion for card {card_id}, pending {pending_id}")
    
    # Step 1: Retrieve staged data
    pending_data = await get_pending_ingestion(pending_id)
    if not pending_data:
        logger.error(f"Pending ingestion {pending_id} not found")
        await notification_hub.notify_progress(
            card_id, 
            "Error: Ingestion data not found", 
            "error"
        )
        return
    
    try:
        # Step 2: Process text chunks
        chunks = pending_data.get("clean_body_chunks", [])
        if chunks:
            await notification_hub.notify_progress(
                card_id, 
                f"Importing {len(chunks)} text chunks...", 
                "processing"
            )
            
            # Join chunks with delimiter for append_raw_chunks
            text_to_append = DELIMITER.join(chunks)
            
            await append_raw_chunks(card_id, text_to_append)
            logger.info(f"Appended {len(chunks)} raw chunks to card {card_id}")
        
        # Step 2.5: Trigger Scribe processing for history ingestion
        # This runs the stateful LLM pipeline to process raw chunks into clinical narratives
        await trigger_processing(card_id)
        logger.info(f"Scribe processing triggered for card {card_id}")
        
        # Step 3: Process PDF if present
        if pending_data.get("has_pdf") and pending_data.get("pdf_data"):
            await notification_hub.notify_progress(
                card_id, 
                "Processing PDF attachment...", 
                "processing"
            )
            
            # Create temp file for PDF
            tmp_file_path: Optional[str] = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(pending_data["pdf_data"])
                    tmp_file_path = tmp.name
                
                logger.info(f"Wrote PDF to temp file: {tmp_file_path}")
                
                # Get the current event loop for callback bridge
                loop = asyncio.get_running_loop()
                
                # Define sync callback that schedules async notification on main loop
                def sync_callback(msg: str, state: str):
                    """Sync callback that bridges to async notification hub."""
                    try:
                        asyncio.run_coroutine_threadsafe(
                            notification_hub.notify_progress(card_id, msg, state),
                            loop
                        )
                    except Exception as e:
                        # Don't let callback errors break the pipeline
                        logger.warning(f"Failed to send progress notification: {e}")
                
                # Run PDF processing in thread to avoid blocking
                await notification_hub.notify_progress(
                    card_id, 
                    "Starting PDF OCR analysis...", 
                    "processing"
                )
                
                await asyncio.to_thread(
                    ingest_pdf_process,
                    tmp_file_path,
                    "output",  # output_base_dir
                    sync_callback
                )
                
                logger.info(f"PDF processing complete for card {card_id}")
                
            finally:
                # Cleanup temp file
                if tmp_file_path and os.path.exists(tmp_file_path):
                    try:
                        os.remove(tmp_file_path)
                        logger.debug(f"Cleaned up temp file: {tmp_file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to cleanup temp file {tmp_file_path}: {e}")
        
        # Step 4: Cleanup - delete pending ingestion record
        deleted = await delete_pending_ingestion(pending_id)
        if deleted:
            logger.info(f"Deleted pending ingestion {pending_id}")
        else:
            logger.warning(f"Failed to delete pending ingestion {pending_id}")
        
        # Step 5: Finalize with success message
        await notification_hub.notify_progress(
            card_id, 
            "Ingestion complete", 
            "success"
        )
        logger.info(f"Ingestion complete for card {card_id}")
        
    except Exception as e:
        logger.error(f"Ingestion failed for card {card_id}: {e}")
        await notification_hub.notify_progress(
            card_id, 
            f"Ingestion failed: {str(e)}", 
            "error"
        )
        raise


async def discard_ingestion(card_id: str, pending_id: str):
    """
    Discard a pending ingestion. If it created a new card, delete the card too.
    
    Args:
        card_id: The patient card ID
        pending_id: The pending ingestion document ID
    """
    logger.info(f"Discarding ingestion for card {card_id}, pending {pending_id}")
    
    # Retrieve pending data to check if it created a new card
    pending_data = await get_pending_ingestion(pending_id)
    
    if pending_data and pending_data.get("created_new_card"):
        # This was a "Patient X" email that created a provisional card
        # Delete the card entirely
        deleted = await delete_card_by_id(card_id)
        if deleted:
            logger.info(f"Deleted provisional card {card_id}")
        else:
            logger.error(f"Failed to delete provisional card {card_id}")
        
        # Trigger sync so all clients refresh their card lists
        await notification_hub.trigger_sync()
    
    # Delete the pending ingestion record
    if pending_data:
        await delete_pending_ingestion(pending_id)
        logger.info(f"Deleted pending ingestion {pending_id}")
    
    logger.info(f"Discard complete for card {card_id}")
