"""
Scribe Service: Stateful Serial Processing Pipeline

This module implements the core logic for processing patient history documents
in a serial, stateful manner. It uses the Gemini LLM to transform raw text
chunks into structured clinical narratives, maintaining context across
all previous documents for narrative continuity.
"""

import os
import logging
import json
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any

from google import genai
from google.genai import types

# Import app modules
import database as db
from models import ProcessedHistoryDocument, MessageRole

# Configure logger
logger = logging.getLogger("ScribeService")

# =============================================================================
# CONSTANTS
# =============================================================================

MODEL_ID = "gemini-2.5-flash"
TEMPERATURE = 0.1
MAX_RETRIES = 3

# Initialize Gemini client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

def _get_scribe_system_instruction() -> str:
    """
    Returns the system prompt for the Clinical Data Harmonizer.
    
    This prompt instructs the LLM to:
    1. Process raw clinical text into structured narratives
    2. Output strict JSON matching ProcessedHistoryDocument schema
    3. Extract or estimate dates in ISO 8601 format
    4. Integrate new information with previous context
    """
    return """You are a Clinical Data Harmonizer, an expert medical AI assistant specializing in transforming raw clinical text into structured, chronological narratives.

## Your Task
Process the provided raw clinical text and output a structured JSON object representing a distinct clinical event (e.g., admission, consult, discharge, clinic visit).

## Output Schema (Strict JSON)
You must output ONLY a valid JSON object with these exact fields:
{
    "timestamp": "YYYY-MM-DD",
    "title": "One-line summary (e.g., 'Internal Medicine Discharge Summary')",
    "content": "Full Markdown-formatted narrative",
    "date_estimated": true/false
}

## Date Extraction Rules
1. Extract the clinical date from the text (e.g., date of admission, visit, or procedure).
2. Use strict ISO 8601 format: YYYY-MM-DD.
3. If the text contains absolute dates ("12/05/2025"), convert to YYYY-MM-DD.
4. If the date is relative ("yesterday", "last week") or missing:
   - Use today's date
   - Set "date_estimated": true
5. If the date is explicitly stated, set "date_estimated": false.

## Content Guidelines
1. Write the "content" field in clean Markdown format.
2. Include all clinically relevant information from the raw text.
3. Maintain narrative flow and medical accuracy.
4. Preserve specific values, measurements, and findings.
5. Remove redundant headers but keep the clinical substance.

## Input Format
Input chunks may be prefixed with labels like "Processing Chunk #X". Ignore these labels and process only the clinical text that follows.

## Context Integration
You will receive previous clinical events as context. Integrate new information with this history:
1. Avoid duplicating information already covered in previous events.
2. Reference previous context when relevant (e.g., "Patient continues on antibiotics started on [previous date]").
3. Maintain chronological coherence across the narrative.

## CRITICAL
- Output ONLY the JSON object. No markdown code blocks, no explanations.
- Ensure the JSON is valid and parseable.
- The "content" field should be a complete, self-contained narrative."""


# =============================================================================
# CORE PROCESSING FUNCTION
# =============================================================================

async def process_patient_history(card_id: str) -> None:
    """
    Process all unprocessed history chunks for a patient in serial order.
    
    This is the main entry point for the Scribe pipeline. It:
    1. Finds the first unprocessed chunk (resumability)
    2. Builds context from all previously processed chunks
    3. Processes each chunk sequentially using Gemini
    4. Updates the ledger with processed document references
    5. Notifies the UI of progress via chat messages
    
    Args:
        card_id: The MongoDB ObjectId string of the patient card
        
    Note:
        This function is idempotent - it can be safely called multiple times.
        It will resume from where it left off if interrupted.
    """
    logger.info(f"Starting Scribe pipeline for card {card_id}")
    
    # ==========================================================================
    # Step A: Check for work (Resumability)
    # ==========================================================================
    unprocessed = await db.get_unprocessed_chunks(card_id)
    
    if unprocessed is None:
        logger.info(f"No unprocessed chunks found for card {card_id}")
        await db.append_chat_message(
            card_id, 
            MessageRole.LOG, 
            "History processing complete. No pending documents."
        )
        return
    
    start_index = unprocessed["index"]
    logger.info(f"Found unprocessed chunk at index {start_index}")
    
    # ==========================================================================
    # Step B: Build Context
    # ==========================================================================
    # Get all processed history up to this point
    context = await db.get_processed_history_context(card_id, start_index)
    
    # Initialize Gemini history as a list of Content objects
    gemini_history: List[types.Content] = []
    
    # Add previous turns to history (User = raw text, Model = processed JSON)
    for raw_text, processed_doc in context:
        # User turn: raw text input
        gemini_history.append(types.Content(
            role="user",
            parts=[types.Part(text=raw_text)]
        ))
        
        # Model turn: processed JSON output
        # IMPORTANT: Only include fields the LLM is expected to generate
        # Exclude _id, card_id, original_chunk_index to prevent hallucination
        model_response = json.dumps({
            "timestamp": processed_doc.get("timestamp", "").isoformat() if isinstance(processed_doc.get("timestamp"), datetime) else processed_doc.get("timestamp", ""),
            "title": processed_doc.get("title", ""),
            "content": processed_doc.get("content", ""),
            "date_estimated": processed_doc.get("date_estimated", False)
        }, ensure_ascii=False)
        
        gemini_history.append(types.Content(
            role="model",
            parts=[types.Part(text=model_response)]
        ))
    
    logger.info(f"Built context with {len(context)} previous documents")
    
    # ==========================================================================
    # Step C: The Processing Loop
    # ==========================================================================
    current_index = start_index
    
    while True:
        # Get the current chunk's raw text
        # We need to fetch the card to get the chunk at current_index
        card = await db.cards_collection.find_one(
            {"_id": db.ObjectId(card_id)},
            {"chunks": 1}
        )
        
        if not card or current_index >= len(card.get("chunks", [])):
            logger.info(f"Reached end of chunks at index {current_index}")
            break
        
        current_chunk = card["chunks"][current_index]
        raw_text = current_chunk.get("text", "")

        # --- DEBUG PATCH START ---
        logger.info(f"[DEBUG] Scribe processing Chunk Index: {current_index}")
        logger.info(f"[DEBUG] Chunk Content Snippet: {raw_text[:100]}...")
        logger.info(f"[DEBUG] Chunk 'processed_id' status: {current_chunk.get('processed_id')}")
        # --- DEBUG PATCH END ---
        
        # Notify UI of progress
        total_chunks = len(card.get("chunks", []))
        progress_msg = f"Processing chunk {current_index + 1} of {total_chunks}..."
        logger.info(progress_msg)
        await db.append_chat_message(card_id, MessageRole.LOG, progress_msg)
        
        # Prepare input: add current raw text as user message
        current_input = gemini_history.copy()
        current_input.append(types.Content(
            role="user",
            parts=[types.Part(text=raw_text)]
        ))
        
        # ======================================================================
        # LLM Execution with Retry
        # ======================================================================
        processed_documents: List[ProcessedHistoryDocument] = []
        success = False
        
        for attempt in range(MAX_RETRIES):
            try:
                # Call Gemini with JSON output mode
                response = client.models.generate_content(
                    model=MODEL_ID,
                    contents=current_input,
                    config=types.GenerateContentConfig(
                        system_instruction=_get_scribe_system_instruction(),
                        temperature=TEMPERATURE,
                        response_mime_type="application/json"
                    )
                )
                
                # Parse JSON response with cleanup for markdown code blocks
                response_text = response.text.strip()

                # --- DEBUG PATCH START ---
                logger.info(f"[DEBUG] RAW LLM RESPONSE:\n{response_text}\n[DEBUG] END RAW RESPONSE")
                # --- DEBUG PATCH END ---

                if response_text.startswith("```"):
                    # Strip markdown code block wrapper (```json and ```)
                    lines = response_text.split("\n")
                    response_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                
                parsed_data = json.loads(response_text)
                
                # FIX: Normalize input to always be a list
                # The LLM sometimes returns a single object, sometimes a list of objects
                if isinstance(parsed_data, dict):
                    items_to_process = [parsed_data]
                elif isinstance(parsed_data, list):
                    items_to_process = parsed_data
                else:
                    raise ValueError(f"Unexpected JSON format: {type(parsed_data)}")
                
                # Process all items in the response
                processed_documents = []
                for item in items_to_process:
                    # Validate with Pydantic model
                    # Note: We need to add card_id and original_chunk_index manually
                    # since the LLM doesn't know these
                    processed_doc = ProcessedHistoryDocument(
                        id=None,  # Will be assigned by MongoDB
                        card_id=card_id,
                        timestamp=item["timestamp"],
                        date_estimated=item.get("date_estimated", False),
                        title=item["title"],
                        content=item["content"],
                        original_chunk_index=current_index
                    )
                    processed_documents.append(processed_doc)
                
                success = True
                logger.info(f"Successfully parsed chunk {current_index} ({len(items_to_process)} document(s))")
                break
                
            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}: Invalid JSON from LLM: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    logger.error(f"Failed to parse JSON after {MAX_RETRIES} attempts")
                    
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}: LLM call failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    logger.error(f"LLM call failed after {MAX_RETRIES} attempts: {e}")
        
        # ======================================================================
        # Handle Success or Failure
        # ======================================================================
        if not success or not processed_documents:
            # Critical failure - halt the loop
            error_msg = f"Scribe Error: Failed to process chunk #{current_index + 1}. System execution paused."
            logger.error(error_msg)
            await db.append_chat_message(card_id, MessageRole.ERROR, error_msg)
            break
        
        # Success - save all documents to database
        try:
            history_ids = []
            for processed_doc in processed_documents:
                # Create history document
                history_id = await db.create_history_document(processed_doc)
                history_ids.append(history_id)
                
                # Add model response to history for next iteration
                model_response = json.dumps({
                    "timestamp": processed_doc.timestamp.isoformat() if isinstance(processed_doc.timestamp, datetime) else processed_doc.timestamp,
                    "title": processed_doc.title,
                    "content": processed_doc.content,
                    "date_estimated": processed_doc.date_estimated
                }, ensure_ascii=False)
                
                gemini_history.append(types.Content(
                    role="user",
                    parts=[types.Part(text=raw_text)]
                ))
                gemini_history.append(types.Content(
                    role="model",
                    parts=[types.Part(text=model_response)]
                ))
            
            # Update ledger to link chunk to processed documents (use first ID as reference)
            # The chunk is now considered "processed" even if it generated multiple documents
            if history_ids:
                await db.update_chunk_processed_id(card_id, current_index, history_ids[0])
                logger.info(f"Saved {len(history_ids)} history document(s) for chunk {current_index}: {history_ids}")
            
        except Exception as e:
            # Database error - halt the loop
            error_msg = f"Scribe Error: Database failure on chunk #{current_index + 1}: {e}"
            logger.error(error_msg)
            await db.append_chat_message(card_id, MessageRole.ERROR, error_msg)
            break
        
        # Move to next chunk
        current_index += 1
    
    # ==========================================================================
    # Completion
    # ==========================================================================
    if current_index > start_index:
        completion_msg = f"History update complete. Processed {current_index - start_index} document(s)."
        logger.info(completion_msg)
        await db.append_chat_message(card_id, MessageRole.LOG, completion_msg)
    else:
        logger.info("No chunks were processed")


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

async def trigger_processing(card_id: str) -> bool:
    """
    Convenience wrapper to trigger history processing.
    
    This can be called from ingestion flow or manual triggers.
    
    Args:
        card_id: The card ID to process
        
    Returns:
        True if processing was initiated (even if no work needed), False on error
    """
    try:
        await process_patient_history(card_id)
        return True
    except Exception as e:
        logger.error(f"Failed to trigger processing for card {card_id}: {e}")
        return False
