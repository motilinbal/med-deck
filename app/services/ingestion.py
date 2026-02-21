"""
Ingestion Orchestrator Service for MedDeck Server.

This module coordinates the ingestion of pending email data (text chunks and PDFs)
into the permanent patient record. It handles:
- Retrieving staged data from pending_ingestions collection
- Appending text chunks to patient history
- Processing PDFs through the OCR pipeline with live progress updates
- Persisting extracted data to MongoDB
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
from typing import Optional, Dict, Any

from database import (
    get_pending_ingestion,
    append_raw_chunks,
    delete_pending_ingestion,
    delete_card_by_id,
    card_has_data,
    card_has_other_pending,
    DELIMITER,
    store_quantitative_labs,
    store_reference_ranges,
    store_microbiology_reports,
    store_pathology_reports,
    store_imaging_reports,
    append_chat_message,
    update_pending_ingestion_status,
    get_card_metadata,
)
from models import MessageRole
from ingest_pdf import process_pdf as ingest_pdf_process
from app.services.notification_hub import notification_hub
from app.services.scribe import trigger_processing
from app.services.email_listener import email_listener
from app.utils.transient import TransientLog

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
    
    # Step 0: Immediately update status and mark email as seen
    # This prevents the listener from re-processing if Scribe fails
    await update_pending_ingestion_status(pending_id, "processing")
    
    # Mark email as seen immediately so listener stops checking it
    # This gives immediate feedback to user in Gmail even if processing fails
    email_uid = pending_data.get("email_uid")
    if email_uid:
        await asyncio.to_thread(email_listener.mark_as_seen, email_uid)
        logger.info(f"Marked email {email_uid} as seen (Ingestion Started)")
    
    try:
        # Step 2: Process text chunks
        chunks = pending_data.get("clean_body_chunks", [])
        if chunks:
            async with TransientLog(card_id, f"Importing {len(chunks)} text chunks..."):
                # Join chunks with delimiter for append_raw_chunks
                text_to_append = DELIMITER.join(chunks)
                
                await append_raw_chunks(card_id, text_to_append)
                logger.info(f"Appended {len(chunks)} raw chunks to card {card_id}")
        
        # Step 2.5: Trigger Scribe processing for history ingestion
        # This runs the stateful LLM pipeline to process raw chunks into clinical narratives
        # Skip for capture source (already structured data from OCR)
        source_type = pending_data.get("source_type", "email")

        if source_type != "capture":
            async with TransientLog(card_id, "Processing clinical narratives..."):
                await trigger_processing(card_id)
            logger.info(f"Scribe processing triggered for card {card_id}")
        else:
            logger.info(f"Skipping scribe processing for capture source card {card_id}")

        # Step 3: Process capture data OR PDF
        # Handle capture source (data already OCR'd)
        if source_type == "capture" and pending_data.get("captured_data"):
            await notification_hub.notify_progress(
                card_id,
                "Processing captured lab data...",
                "processing"
            )

            # Directly store captured data using the same functions
            async with TransientLog(card_id, "Saving captured data to database..."):
                total_stats: Dict[str, Any] = {
                    "quant_labs_inserted": 0,
                    "quant_labs_duplicates": 0,
                    "ref_ranges_inserted": 0,
                    "ref_ranges_duplicates": 0,
                    "microbiology_inserted": 0,
                    "microbiology_duplicates": 0,
                    "pathology_inserted": 0,
                    "pathology_duplicates": 0,
                    "imaging_inserted": 0,
                    "imaging_duplicates": 0,
                }

                captured = pending_data.get("captured_data", {})

                # Split quantitative data into labs and reference ranges
                quantitative_data = captured.get("quantitative", [])
                quant_labs = []
                ref_ranges = []

                for item in quantitative_data:
                    if isinstance(item, dict) and item.get("category") == "Reference":
                        ref_ranges.append(item)
                    else:
                        quant_labs.append(item)

                # Store quantitative labs
                if quant_labs:
                    lab_stats = await store_quantitative_labs(card_id, quant_labs)
                    total_stats["quant_labs_inserted"] += lab_stats.get("inserted", 0)
                    total_stats["quant_labs_duplicates"] += lab_stats.get("duplicates_skipped", 0)
                    logger.info(f"Stored {lab_stats.get('inserted', 0)} quantitative labs for card {card_id}")

                # Store reference ranges
                if ref_ranges:
                    ref_stats = await store_reference_ranges(card_id, ref_ranges)
                    total_stats["ref_ranges_inserted"] += ref_stats.get("inserted", 0)
                    total_stats["ref_ranges_duplicates"] += ref_stats.get("duplicates_skipped", 0)
                    logger.info(f"Stored {ref_stats.get('inserted', 0)} reference ranges for card {card_id}")

                # Store microbiology reports
                microbiology_data = captured.get("microbiology", [])
                if microbiology_data:
                    micro_stats = await store_microbiology_reports(card_id, microbiology_data)
                    total_stats["microbiology_inserted"] += micro_stats.get("inserted", 0)
                    total_stats["microbiology_duplicates"] += micro_stats.get("duplicates_skipped", 0)
                    logger.info(f"Stored {micro_stats.get('inserted', 0)} microbiology reports for card {card_id}")

                # Store pathology reports
                pathology_data = captured.get("pathology", [])
                if pathology_data:
                    path_stats = await store_pathology_reports(card_id, pathology_data)
                    total_stats["pathology_inserted"] += path_stats.get("inserted", 0)
                    total_stats["pathology_duplicates"] += path_stats.get("duplicates_skipped", 0)
                    logger.info(f"Stored {path_stats.get('inserted', 0)} pathology reports for card {card_id}")

                # Store imaging reports
                imaging_data = captured.get("imaging", [])
                if imaging_data:
                    imaging_stats = await store_imaging_reports(card_id, imaging_data)
                    total_stats["imaging_inserted"] += imaging_stats.get("inserted", 0)
                    total_stats["imaging_duplicates"] += imaging_stats.get("duplicates_skipped", 0)
                    logger.info(f"Stored {imaging_stats.get('inserted', 0)} imaging reports for card {card_id}")

                logger.info(f"Database persistence complete for card {card_id}: {total_stats}")

                # Send ingestion summary as chat message
                quant_total = total_stats["quant_labs_inserted"] + total_stats["ref_ranges_inserted"]
                quant_dups = total_stats["quant_labs_duplicates"] + total_stats["ref_ranges_duplicates"]
                micro_total = total_stats["microbiology_inserted"]
                path_total = total_stats["pathology_inserted"]
                imaging_total = total_stats["imaging_inserted"]

                dup_info = f" ({quant_dups} duplicates)" if quant_dups > 0 else ""

                summary_message = (
                    f"**Capture Ingestion Complete**\n"
                    f"• Quantitative: {total_stats['quant_labs_inserted']} labs, "
                    f"{total_stats['ref_ranges_inserted']} ranges{dup_info}\n"
                    f"• Microbiology: {micro_total} reports\n"
                    f"• Pathology: {path_total} reports\n"
                    f"• Imaging: {imaging_total} reports"
                )

                await append_chat_message(card_id, MessageRole.INFO, summary_message)
            logger.info(f"Sent capture ingestion summary to chat for card {card_id}")

        # Step 3b: Process PDF if present (skip for capture - data already OCR'd)
        elif pending_data.get("has_pdf") and pending_data.get("pdf_data"):
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

                async def async_progress_callback(msg: str, state: str):
                    """Send progress update via WebSocket for real-time UI updates."""
                    await notification_hub.notify_progress(card_id, msg, state)
                
                # Define sync callback that bridges to async on main loop
                def sync_callback(msg: str, state: str):
                    """Sync callback that bridges to async notification hub."""
                    try:
                        asyncio.run_coroutine_threadsafe(
                            async_progress_callback(msg, state),
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
                
                # Capture extraction results
                extraction_result = await asyncio.to_thread(
                    ingest_pdf_process,
                    tmp_file_path,
                    "output",  # output_base_dir
                    sync_callback
                )
                
                logger.info(f"PDF processing complete for card {card_id}")

                # Step 3.5: Persist extracted data to database
                async with TransientLog(card_id, "Saving extracted data to database..."):
                    # Initialize stats accumulator
                    total_stats: Dict[str, Any] = {
                        "quant_labs_inserted": 0,
                        "quant_labs_duplicates": 0,
                        "ref_ranges_inserted": 0,
                        "ref_ranges_duplicates": 0,
                        "microbiology_inserted": 0,
                        "microbiology_duplicates": 0,
                        "pathology_inserted": 0,
                        "pathology_duplicates": 0,
                        "imaging_inserted": 0,
                        "imaging_duplicates": 0,
                    }
                    
                    # Split quantitative data into labs and reference ranges
                    quantitative_data = extraction_result.get("quantitative", [])
                    quant_labs = []
                    ref_ranges = []
                    
                    for item in quantitative_data:
                        if isinstance(item, dict) and item.get("category") == "Reference":
                            ref_ranges.append(item)
                        else:
                            quant_labs.append(item)
                    
                    # Store quantitative labs
                    if quant_labs:
                        lab_stats = await store_quantitative_labs(card_id, quant_labs)
                        total_stats["quant_labs_inserted"] += lab_stats.get("inserted", 0)
                        total_stats["quant_labs_duplicates"] += lab_stats.get("duplicates_skipped", 0)
                        logger.info(f"Stored {lab_stats.get('inserted', 0)} quantitative labs for card {card_id}")
                    
                    # Store reference ranges
                    if ref_ranges:
                        ref_stats = await store_reference_ranges(card_id, ref_ranges)
                        total_stats["ref_ranges_inserted"] += ref_stats.get("inserted", 0)
                        total_stats["ref_ranges_duplicates"] += ref_stats.get("duplicates_skipped", 0)
                        logger.info(f"Stored {ref_stats.get('inserted', 0)} reference ranges for card {card_id}")
                    
                    # Store microbiology reports
                    microbiology_data = extraction_result.get("microbiology", [])
                    if microbiology_data:
                        micro_stats = await store_microbiology_reports(card_id, microbiology_data)
                        total_stats["microbiology_inserted"] += micro_stats.get("inserted", 0)
                        total_stats["microbiology_duplicates"] += micro_stats.get("duplicates_skipped", 0)
                        logger.info(f"Stored {micro_stats.get('inserted', 0)} microbiology reports for card {card_id}")
                    
                    # Store pathology reports
                    pathology_data = extraction_result.get("pathology", [])
                    if pathology_data:
                        path_stats = await store_pathology_reports(card_id, pathology_data)
                        total_stats["pathology_inserted"] += path_stats.get("inserted", 0)
                        total_stats["pathology_duplicates"] += path_stats.get("duplicates_skipped", 0)
                        logger.info(f"Stored {path_stats.get('inserted', 0)} pathology reports for card {card_id}")
                    
                    # Store imaging reports
                    imaging_data = extraction_result.get("imaging", [])
                    if imaging_data:
                        imaging_stats = await store_imaging_reports(card_id, imaging_data)
                        total_stats["imaging_inserted"] += imaging_stats.get("inserted", 0)
                        total_stats["imaging_duplicates"] += imaging_stats.get("duplicates_skipped", 0)
                        logger.info(f"Stored {imaging_stats.get('inserted', 0)} imaging reports for card {card_id}")
                    
                    logger.info(f"Database persistence complete for card {card_id}: {total_stats}")
                    
                    # Step 3.6: Send ingestion summary as chat message
                    # Build the summary message with stats
                    quant_total = total_stats["quant_labs_inserted"] + total_stats["ref_ranges_inserted"]
                    quant_dups = total_stats["quant_labs_duplicates"] + total_stats["ref_ranges_duplicates"]
                    micro_total = total_stats["microbiology_inserted"]
                    path_total = total_stats["pathology_inserted"]
                    imaging_total = total_stats["imaging_inserted"]
                    
                    # Build duplicate info string (only show if there are duplicates)
                    dup_info = f" ({quant_dups} duplicates)" if quant_dups > 0 else ""
                    
                    summary_message = (
                        f"**Ingestion Complete**\n"
                        f"• Quantitative: {total_stats['quant_labs_inserted']} labs, "
                        f"{total_stats['ref_ranges_inserted']} ranges{dup_info}\n"
                        f"• Microbiology: {micro_total} reports\n"
                        f"• Pathology: {path_total} reports\n"
                        f"• Imaging: {imaging_total} reports"
                    )
                    
                    await append_chat_message(card_id, MessageRole.INFO, summary_message)
                logger.info(f"Sent ingestion summary to chat for card {card_id}")
                
                # Step 3.7: Fetch and broadcast updated metadata
                # This ensures the frontend immediately shows the latest timestamps
                try:
                    metadata = await get_card_metadata(card_id)
                    
                    # Broadcast metadata update to all connected clients
                    await notification_hub.emit_system_event(
                        card_id=card_id,
                        category="card_update",
                        payload=metadata
                    )
                    
                    logger.info(f"Broadcasted updated metadata for card {card_id}: {metadata}")
                except Exception as e:
                    # Non-critical error - don't fail ingestion if metadata broadcast fails
                    logger.warning(f"Failed to broadcast metadata update for card {card_id}: {e}")
                
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
        
        # Step 5: Confirm email is marked as read (already done at start, but ensure it's marked)
        email_uid = pending_data.get("email_uid")
        if email_uid:
            await asyncio.to_thread(email_listener.mark_as_seen, email_uid)
            logger.info(f"Confirmed email {email_uid} is marked as seen")
        
        # Step 6: Finalize with success message
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
    Discard a pending ingestion.
    
    If the card was created specifically for this ingestion and has no actual data
    (history, labs, chat), and there are no other pending ingestions for this card,
    then the card will be deleted.
    
    Args:
        card_id: The patient card ID
        pending_id: The pending ingestion document ID
    """
    logger.info(f"Discarding ingestion for card {card_id}, pending {pending_id}")
    
    # Retrieve pending data to check conditions
    pending_data = await get_pending_ingestion(pending_id)
    
    # Capture email_uid before deletion
    email_uid = pending_data.get("email_uid") if pending_data else None
    
    # Check if we should delete the card - ALL conditions must be true:
    # 1. The card was created for this ingestion (created_new_card=True)
    # 2. The card has no actual data (history, labs, chat)
    # 3. There are no other pending ingestions for this card
    should_delete_card = False
    
    if pending_data and pending_data.get("created_new_card"):
        # Check if card has any actual data
        has_data = await card_has_data(card_id)
        
        # Check if there are other pending ingestions
        has_other_pending = await card_has_other_pending(card_id, pending_id)
        
        # Delete only if all conditions are met
        should_delete_card = not has_data and not has_other_pending
        
        if should_delete_card:
            logger.info(f"Card {card_id} is empty and has no other pending - safe to delete")
        else:
            if has_data:
                logger.info(f"Card {card_id} has data - will NOT delete")
            if has_other_pending:
                logger.info(f"Card {card_id} has other pending ingestions - will NOT delete")
    
    # Delete the card if safe
    if should_delete_card:
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
    
    # Mark email as read on the server (even for discarded ingestions)
    if email_uid:
        await asyncio.to_thread(email_listener.mark_as_seen, email_uid)
        logger.info(f"Marked email {email_uid} as seen after discard")
    
    logger.info(f"Discard complete for card {card_id}")
