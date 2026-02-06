import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import motor.motor_asyncio
from bson.objectid import ObjectId
from pydantic import ValidationError

from parser import parse_lab_result, create_mongo_timestamp, remove_date_padding, quantify_text_divergence
from ai_engine import check_duplicate_documents
from models import (
    QuantitativeLabModel,
    ReferenceRangeModel,
    MicrobiologyModel,
    PathologyModel,
    ImagingModel,
    PendingIngestion,
)

DELIMITER = "^^^"

# Configure logging
logger = logging.getLogger(__name__)

# REPLACE with your Atlas string if needed
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.meddeck_db
cards_collection = db.get_collection("cards")
traces_collection = db.get_collection("agent_traces")  # NEW
labs_collection = db.get_collection("labs")  # Collection for lab results
pending_collection = db.get_collection("pending_ingestions")  # Staging area for email data

def card_helper(card) -> dict:
    return {
        "id": str(card["_id"]),
        "serial": card.get("serial"),
        "nickname": card.get("nickname"),
        "transcript": card.get("transcript", ""),
        "processed_note": card.get("processed_note", ""),
    }

async def get_all_cards():
    """Returns all cards sorted by serial number"""
    cards = []
    async for card in cards_collection.find().sort("serial", 1):
        cards.append(card_helper(card))
    return cards

async def create_empty_card():
    """Creates a new card with the next available serial number"""
    # Find highest serial
    highest = await cards_collection.find_one(sort=[("serial", -1)])
    next_serial = (highest["serial"] + 1) if highest else 1
    
    new_card = {
        "serial": next_serial,
        "nickname": "New Consultation",
        "transcript": "", # Empty bucket
    }
    result = await cards_collection.insert_one(new_card)
    return card_helper(await cards_collection.find_one({"_id": result.inserted_id}))

async def delete_card_by_id(card_id: str):
    """Deletes a card permanently"""
    try:
        await cards_collection.delete_one({"_id": ObjectId(card_id)})
        return True
    except:
        return False

async def append_transcript(card_id: str, text: str):
    """Appends new text to the specific card's transcript bucket"""
    if not text or not card_id: return
    
    try:
        await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            [
                {"$set": {"transcript": {"$concat": [{"$ifNull": ["$transcript", ""]}, " ", text]}}}
            ]
        )
    except Exception as e:
        print(f"DB Error appending to {card_id}: {e}")



async def create_trace_run(card_id: str, user_prompt: str):
    """
    Initializes a new 'Run' for the agent. 
    Returns the run_id so we can append events to it.
    """
    new_trace = {
        "card_id": str(card_id),
        "start_time": datetime.datetime.now(datetime.timezone.utc),
        "status": "running",
        "initial_prompt": user_prompt,
        "events": []  # This will store the chain of thought
    }
    result = await traces_collection.insert_one(new_trace)
    return str(result.inserted_id)

async def log_trace_event(run_id: str, role: str, content: any, tool_call_info: dict = None):
    """
    Appends a single step (event) to the run log.
    
    Args:
        role: 'user', 'model', 'tool', or 'system'
        content: The text content or result
        tool_call_info: Dictionary containing function name/args (if applicable)
    """
    event = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
        "role": role,
        "content": content,  # Can be string or dict/JSON
        "tool_info": tool_call_info # Optional: {name: "get_labs", args: {...}}
    }
    
    await traces_collection.update_one(
        {"_id": ObjectId(run_id)},
        {"$push": {"events": event}}
    )

async def complete_trace_run(run_id: str, final_answer: str, status="completed"):
    """
    Marks the run as finished and saves the final output.
    """
    await traces_collection.update_one(
        {"_id": ObjectId(run_id)},
        {
            "$set": {
                "status": status,
                "end_time": datetime.now().astimezone(),
                "final_output": final_answer
            }
        }
    )


# =============================================================================
# HISTORY CRUD OPERATIONS
# =============================================================================

async def append_history_chunks(
    card_id: str,
    text: str,
    delimiter: str = DELIMITER
) -> Dict[str, Any]:
    """
    Append new raw text chunks to a card's history list.
    
    Splits input text by delimiter, checks each chunk against existing
    history for duplicates using quantify_text_divergence(), and appends
    non-duplicate chunks with processed=null.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        text: Raw text containing one or more chunks separated by delimiter
        delimiter: String delimiter to split chunks (default: "^^^")
        
    Returns:
        Dict with operation results including:
        - total_input_chunks: Number of chunks in input
        - added: Number of new chunks added
        - skipped_duplicates: Number of chunks skipped as duplicates
        - added_indices: List of indices where chunks were inserted
        - skipped_indices: List of input indices that were duplicates
        - details: Per-chunk operation details
        
    Raises:
        ValueError: If card_id is invalid
        KeyError: If card not found
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Fetch the card to get existing history
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise KeyError(f"Card not found: {card_id}")
    
    # Get existing history (default to empty list)
    existing_history = card.get("history", [])
    existing_raw_chunks = [item.get("raw", "") for item in existing_history]
    
    # Split input text by delimiter
    candidate_chunks = text.split(delimiter)
    
    # Prepare tracking structures
    new_chunks = []
    details = []
    added_indices = []
    skipped_indices = []
    
    for input_idx, candidate in enumerate(candidate_chunks):
        # Strip whitespace from candidate
        candidate = candidate.strip()
        
        # Skip empty chunks
        if not candidate:
            details.append({
                "input_index": input_idx,
                "action": "skipped_empty",
                "matched_existing_index": None,
                "similarity_metrics": None
            })
            continue
        
        # Check for duplicates against all existing chunks
        is_duplicate = False
        matched_index = None
        similarity_metrics = None
        
        for existing_idx, existing_raw in enumerate(existing_raw_chunks):
            divergence_result = quantify_text_divergence(candidate, existing_raw)
            
            if divergence_result.get("is_same_source", False):
                is_duplicate = True
                matched_index = existing_idx
                similarity_metrics = divergence_result.get("metrics", {})
                break
        
        if is_duplicate:
            skipped_indices.append(input_idx)
            details.append({
                "input_index": input_idx,
                "action": "skipped_duplicate",
                "matched_existing_index": matched_index,
                "similarity_metrics": similarity_metrics
            })
        else:
            # Add to new chunks list
            new_chunk = {"raw": candidate, "processed": None}
            new_chunks.append(new_chunk)
            added_indices.append(input_idx)
            details.append({
                "input_index": input_idx,
                "action": "added",
                "matched_existing_index": None,
                "similarity_metrics": None
            })
            # Also add to existing_raw_chunks to avoid duplicates within the same batch
            existing_raw_chunks.append(candidate)
    
    # If we have new chunks to add, push them to the database
    if new_chunks:
        await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            {"$push": {"history": {"$each": new_chunks}}}
        )
        logger.info(f"Added {len(new_chunks)} new history chunks to card {card_id}")
    
    return {
        "card_id": card_id,
        "total_input_chunks": len(candidate_chunks),
        "added": len(new_chunks),
        "skipped_duplicates": len(skipped_indices),
        "added_indices": added_indices,
        "skipped_indices": skipped_indices,
        "details": details
    }


async def get_raw_chunk(card_id: str, index: int) -> str:
    """
    Retrieve the raw text content of a history chunk at specified index.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        index: 0-based index into the history array
        
    Returns:
        The raw text string at the specified index
        
    Raises:
        ValueError: If card_id is invalid
        KeyError: If card not found or has no history
        IndexError: If index is out of bounds
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Use aggregation to get the specific chunk and history size
    pipeline = [
        {"$match": {"_id": ObjectId(card_id)}},
        {"$project": {
            "chunk": {"$arrayElemAt": ["$history", index]},
            "history_size": {"$size": {"$ifNull": ["$history", []]}}
        }}
    ]
    
    result = await cards_collection.aggregate(pipeline).to_list(length=1)
    
    if not result:
        raise KeyError(f"Card not found: {card_id}")
    
    result = result[0]
    history_size = result.get("history_size", 0)
    
    if index < 0 or index >= history_size:
        raise IndexError(f"Index {index} out of bounds for history of size {history_size}")
    
    chunk = result.get("chunk")
    if not chunk:
        raise KeyError(f"No chunk found at index {index}")
    
    return chunk.get("raw", "")


async def get_processed_chunk(card_id: str, index: int) -> Optional[str]:
    """
    Retrieve the processed text content of a history chunk at specified index.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        index: 0-based index into the history array
        
    Returns:
        The processed text string, or None if not yet processed
        
    Raises:
        ValueError: If card_id is invalid
        KeyError: If card not found or has no history
        IndexError: If index is out of bounds
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Use aggregation to get the specific chunk and history size
    pipeline = [
        {"$match": {"_id": ObjectId(card_id)}},
        {"$project": {
            "chunk": {"$arrayElemAt": ["$history", index]},
            "history_size": {"$size": {"$ifNull": ["$history", []]}}
        }}
    ]
    
    result = await cards_collection.aggregate(pipeline).to_list(length=1)
    
    if not result:
        raise KeyError(f"Card not found: {card_id}")
    
    result = result[0]
    history_size = result.get("history_size", 0)
    
    if index < 0 or index >= history_size:
        raise IndexError(f"Index {index} out of bounds for history of size {history_size}")
    
    chunk = result.get("chunk")
    if not chunk:
        raise KeyError(f"No chunk found at index {index}")
    
    return chunk.get("processed")


async def get_history_length(card_id: str) -> int:
    """
    Get the number of history chunks for a card.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        
    Returns:
        Integer count of history items (0 if no history field or card not found)
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Use projection to get just the history size
    result = await cards_collection.find_one(
        {"_id": ObjectId(card_id)},
        {"_id": 0, "history": 1}
    )
    
    if not result:
        return 0
    
    history = result.get("history", [])
    return len(history)


async def update_processed_chunk(card_id: str, index: int, processed_text: str) -> bool:
    """
    Update the processed field of a history chunk at specified index.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        index: 0-based index into the history array
        processed_text: The processed/annotated text to store
        
    Returns:
        True if update successful
        
    Raises:
        ValueError: If card_id is invalid
        KeyError: If card not found or has no history
        IndexError: If index is out of bounds
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # First verify card exists and index is valid
    # Use aggregation to check history size
    pipeline = [
        {"$match": {"_id": ObjectId(card_id)}},
        {"$project": {
            "history_size": {"$size": {"$ifNull": ["$history", []]}}
        }}
    ]
    
    result = await cards_collection.aggregate(pipeline).to_list(length=1)
    
    if not result:
        raise KeyError(f"Card not found: {card_id}")
    
    history_size = result[0].get("history_size", 0)
    
    if index < 0 or index >= history_size:
        raise IndexError(f"Index {index} out of bounds for history of size {history_size}")
    
    # Update the processed field at the specified index
    await cards_collection.update_one(
        {"_id": ObjectId(card_id)},
        {"$set": {f"history.{index}.processed": processed_text}}
    )
    
    logger.info(f"Updated processed text for history chunk {index} in card {card_id}")
    return True


# =============================================================================
# LABS COLLECTION FUNCTIONS
# =============================================================================

async def _is_duplicate_quantitative(card_id: str, doc: Dict[str, Any]) -> bool:
    """
    Check if a quantitative lab document already exists.
    
    A quantitative document is a duplicate if it has the same:
    - timestamp
    - material
    - test_name
    - value
    
    Args:
        card_id: The patient's card ID
        doc: The document to check
        
    Returns:
        True if duplicate exists, False otherwise
    """
    existing = await labs_collection.find_one({
        "card_id": card_id,
        "category": "Quantitative",
        "timestamp": doc["timestamp"],
        "material": doc["material"],
        "test_name": doc["test_name"],
        "value": doc["value"]
    })
    return existing is not None


async def _is_duplicate_reference(card_id: str, doc: Dict[str, Any]) -> bool:
    """
    Check if a reference range document already exists.
    
    A reference document is a duplicate if it has the same:
    - test_name
    - material
    - units
    
    Args:
        card_id: The patient's card ID
        doc: The document to check
        
    Returns:
        True if duplicate exists, False otherwise
    """
    existing = await labs_collection.find_one({
        "card_id": card_id,
        "category": "Reference",
        "test_name": doc["test_name"],
        "material": doc["material"],
        "units": doc["units"]
    })
    return existing is not None


async def _is_duplicate_narrative(card_id: str, doc: Dict[str, Any]) -> bool:
    """
    Check if a narrative document (Microbiology, Pathology, Imaging) already exists.
    
    Uses two-phase detection:
    1. Find documents with same category and timestamp
    2. Use LLM to verify if they refer to the same test
    
    Args:
        card_id: The patient's card ID
        doc: The document to check
        
    Returns:
        True if duplicate exists, False otherwise
    """
    category = doc.get("category")
    timestamp = doc.get("timestamp")
    
    if not category or not timestamp:
        return False
    
    # Find potential duplicates (same category and timestamp)
    cursor = labs_collection.find({
        "card_id": card_id,
        "category": category,
        "timestamp": timestamp
    })
    
    async for existing in cursor:
        # Use LLM to verify if they are the same
        existing_str = str(existing)
        doc_str = str(doc)
        
        try:
            is_duplicate = await check_duplicate_documents(existing_str, doc_str)
            if is_duplicate:
                return True
        except Exception as e:
            logger.warning(f"LLM duplicate check failed: {e}. Assuming not duplicate.")
            continue
    
    return False


def _unpack_format_b(grouped_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Unpack a Format B (grouped panel) document into multiple Format A documents.
    
    Format B has a 'results' field containing multiple test_name: value pairs.
    This function creates separate documents for each test, copying parent metadata.
    
    Args:
        grouped_doc: Format B document with 'results' field
        
    Returns:
        List of Format A documents
    """
    results = grouped_doc.get("results", {})
    if not results:
        return []
    
    unpacked = []
    for test_name, value in results.items():
        doc = {
            "category": "Quantitative",
            "date": grouped_doc.get("date"),
            "time": grouped_doc.get("time"),
            "material": grouped_doc.get("material"),
            "test_name": test_name,
            "value": value,
            "note": grouped_doc.get("note")
        }
        unpacked.append(doc)
    
    return unpacked


def _process_quantitative_document(doc: Dict[str, Any], card_id: str) -> Optional[Dict[str, Any]]:
    """
    Process a quantitative document: parse value, create timestamp, clean date.
    
    Args:
        doc: The raw document from OCR
        card_id: The patient's card ID
        
    Returns:
        Processed document ready for validation, or None if processing fails
    """
    try:
        # Parse value into value and operator
        raw_value = doc.get("value", "")
        parsed = parse_lab_result(raw_value)
        
        # Create timestamp from date and time
        date_str = doc.get("date", "")
        time_str = doc.get("time", "")
        timestamp = create_mongo_timestamp(date_str, time_str)
        
        # Clean date by removing padding
        clean_date = remove_date_padding(date_str)
        
        processed = {
            "card_id": card_id,
            "category": "Quantitative",
            "date": clean_date,
            "time": time_str,
            "timestamp": timestamp,
            "material": doc.get("material", ""),
            "test_name": doc.get("test_name", ""),
            "value": parsed["value"],
            "operator": parsed["operator"],
            "note": doc.get("note")
        }
        
        return processed
        
    except Exception as e:
        logger.error(f"Failed to process quantitative document: {e}")
        return None


async def store_quantitative_labs(card_id: str, ocr_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Store quantitative lab results from OCR output.
    
    Handles both Format A (single tests) and Format B (grouped panels).
    Format B documents are unpacked into multiple Format A documents.
    
    Args:
        card_id: The patient's card ID
        ocr_data: List of documents from OCR engine
        
    Returns:
        Dict with counts: {"inserted": int, "duplicates_skipped": int, "errors": int}
    """
    inserted = 0
    duplicates_skipped = 0
    errors = 0
    
    for item in ocr_data:
        try:
            # Check if this is Format B (grouped panel) or Format A (single test)
            if "results" in item and isinstance(item["results"], dict):
                # Format B - unpack into multiple documents
                unpacked_docs = _unpack_format_b(item)
            else:
                # Format A - use as is
                unpacked_docs = [item]
            
            for doc in unpacked_docs:
                # Process the document (parse value, create timestamp, etc.)
                processed = _process_quantitative_document(doc, card_id)
                if not processed:
                    errors += 1
                    continue
                
                # Validate with Pydantic model
                try:
                    validated = QuantitativeLabModel(**processed)
                except ValidationError as e:
                    logger.warning(f"Validation error for quantitative document: {e}")
                    errors += 1
                    continue
                
                # Check for duplicates
                if await _is_duplicate_quantitative(card_id, validated.model_dump()):
                    logger.info(f"Skipping duplicate quantitative document: {validated.test_name}")
                    duplicates_skipped += 1
                    continue
                
                # Insert into database
                await labs_collection.insert_one(validated.model_dump())
                inserted += 1
                logger.info(f"Inserted quantitative document: {validated.test_name}")
                
        except Exception as e:
            logger.error(f"Error processing quantitative lab item: {e}")
            errors += 1
    
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "errors": errors
    }


async def store_reference_ranges(card_id: str, ocr_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Store reference range documents from OCR output.
    
    Args:
        card_id: The patient's card ID
        ocr_data: List of reference documents from OCR engine
        
    Returns:
        Dict with counts: {"inserted": int, "duplicates_skipped": int, "errors": int}
    """
    inserted = 0
    duplicates_skipped = 0
    errors = 0
    
    for item in ocr_data:
        try:
            # Prepare document
            doc = {
                "card_id": card_id,
                "category": "Reference",
                "test_name": item.get("test_name", ""),
                "material": item.get("material", ""),
                "low_value": item.get("low_value"),
                "high_value": item.get("high_value"),
                "units": item.get("units", "")
            }
            
            # Validate with Pydantic model
            try:
                validated = ReferenceRangeModel(**doc)
            except ValidationError as e:
                logger.warning(f"Validation error for reference document: {e}")
                errors += 1
                continue
            
            # Check for duplicates
            if await _is_duplicate_reference(card_id, validated.model_dump()):
                logger.info(f"Skipping duplicate reference document: {validated.test_name}")
                duplicates_skipped += 1
                continue
            
            # Insert into database
            await labs_collection.insert_one(validated.model_dump())
            inserted += 1
            logger.info(f"Inserted reference document: {validated.test_name}")
            
        except Exception as e:
            logger.error(f"Error processing reference range item: {e}")
            errors += 1
    
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "errors": errors
    }


async def store_microbiology_reports(card_id: str, ocr_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Store microbiology culture and sensitivity reports.
    
    Args:
        card_id: The patient's card ID
        ocr_data: List of microbiology documents from OCR engine
        
    Returns:
        Dict with counts: {"inserted": int, "duplicates_skipped": int, "errors": int}
    """
    inserted = 0
    duplicates_skipped = 0
    errors = 0
    
    for item in ocr_data:
        try:
            # Process date and timestamp
            date_str = item.get("date", "")
            time_str = item.get("time", "")
            timestamp = create_mongo_timestamp(date_str, time_str)
            clean_date = remove_date_padding(date_str)
            
            # Prepare document
            doc = {
                "card_id": card_id,
                "category": "Microbiology",
                "date": clean_date,
                "time": time_str,
                "timestamp": timestamp,
                "material": item.get("material", ""),
                "gram_stain": item.get("gram_stain"),
                "culture": item.get("culture", [])
            }
            
            # Validate with Pydantic model
            try:
                validated = MicrobiologyModel(**doc)
            except ValidationError as e:
                logger.warning(f"Validation error for microbiology document: {e}")
                errors += 1
                continue
            
            # Check for duplicates
            if await _is_duplicate_narrative(card_id, validated.model_dump()):
                logger.info(f"Skipping duplicate microbiology document for timestamp: {validated.timestamp}")
                duplicates_skipped += 1
                continue
            
            # Insert into database
            await labs_collection.insert_one(validated.model_dump())
            inserted += 1
            logger.info(f"Inserted microbiology document for material: {validated.material}")
            
        except Exception as e:
            logger.error(f"Error processing microbiology item: {e}")
            errors += 1
    
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "errors": errors
    }


async def store_pathology_reports(card_id: str, ocr_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Store pathology (histopathology) reports.
    
    Args:
        card_id: The patient's card ID
        ocr_data: List of pathology documents from OCR engine
        
    Returns:
        Dict with counts: {"inserted": int, "duplicates_skipped": int, "errors": int}
    """
    inserted = 0
    duplicates_skipped = 0
    errors = 0
    
    for item in ocr_data:
        try:
            # Process date and timestamp
            date_str = item.get("date", "")
            time_str = item.get("time", "")
            timestamp = create_mongo_timestamp(date_str, time_str)
            clean_date = remove_date_padding(date_str)
            
            # Prepare document
            doc = {
                "card_id": card_id,
                "category": "Pathology",
                "date": clean_date,
                "time": time_str,
                "timestamp": timestamp,
                "specimen": item.get("specimen", ""),
                "clinical_data": item.get("clinical_data"),
                "macroscopic": item.get("macroscopic"),
                "microscopic": item.get("microscopic"),
                "diagnosis": item.get("diagnosis")
            }
            
            # Validate with Pydantic model
            try:
                validated = PathologyModel(**doc)
            except ValidationError as e:
                logger.warning(f"Validation error for pathology document: {e}")
                errors += 1
                continue
            
            # Check for duplicates
            if await _is_duplicate_narrative(card_id, validated.model_dump()):
                logger.info(f"Skipping duplicate pathology document for timestamp: {validated.timestamp}")
                duplicates_skipped += 1
                continue
            
            # Insert into database
            await labs_collection.insert_one(validated.model_dump())
            inserted += 1
            logger.info(f"Inserted pathology document for specimen: {validated.specimen}")
            
        except Exception as e:
            logger.error(f"Error processing pathology item: {e}")
            errors += 1
    
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "errors": errors
    }


async def store_imaging_reports(card_id: str, ocr_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Store imaging reports (CT, MRI, PET-CT, Ultrasound, etc.).
    
    Args:
        card_id: The patient's card ID
        ocr_data: List of imaging documents from OCR engine
        
    Returns:
        Dict with counts: {"inserted": int, "duplicates_skipped": int, "errors": int}
    """
    inserted = 0
    duplicates_skipped = 0
    errors = 0
    
    for item in ocr_data:
        try:
            # Process date and timestamp
            date_str = item.get("date", "")
            time_str = item.get("time", "")
            timestamp = create_mongo_timestamp(date_str, time_str)
            clean_date = remove_date_padding(date_str)
            
            # Prepare document
            doc = {
                "card_id": card_id,
                "category": "Imaging",
                "date": clean_date,
                "time": time_str,
                "timestamp": timestamp,
                "exam_type": item.get("exam_type", ""),
                "indication": item.get("indication"),
                "comparison": item.get("comparison"),
                "findings": item.get("findings", {}),
                "summary": item.get("summary")
            }
            
            # Validate with Pydantic model
            try:
                validated = ImagingModel(**doc)
            except ValidationError as e:
                logger.warning(f"Validation error for imaging document: {e}")
                errors += 1
                continue
            
            # Check for duplicates
            if await _is_duplicate_narrative(card_id, validated.model_dump()):
                logger.info(f"Skipping duplicate imaging document for timestamp: {validated.timestamp}")
                duplicates_skipped += 1
                continue
            
            # Insert into database
            await labs_collection.insert_one(validated.model_dump())
            inserted += 1
            logger.info(f"Inserted imaging document for exam: {validated.exam_type}")
            
        except Exception as e:
            logger.error(f"Error processing imaging item: {e}")
            errors += 1
    
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "errors": errors
    }


# =============================================================================
# READ OPERATIONS FOR LABS COLLECTION
# =============================================================================

async def get_quantitative_labs(
    card_id: str,
    test_names: List[str],
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """
    Retrieve quantitative lab results with reference range enrichment for specified tests.
    
    Args:
        card_id: The patient's card ID
        test_names: List of test names to query
        start_time: Optional start of timestamp range (inclusive)
        end_time: Optional end of timestamp range (inclusive)
        
    Returns:
        List of grouped quantitative results with reference ranges
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Build timestamp filter dynamically
    timestamp_filter = {}
    if start_time is not None:
        timestamp_filter["$gte"] = start_time
    if end_time is not None:
        timestamp_filter["$lte"] = end_time
    
    # Build match stage
    match_stage = {
        "card_id": card_id,
        "category": "Quantitative",
        "test_name": {"$in": test_names}
    }
    if timestamp_filter:
        match_stage["timestamp"] = timestamp_filter
    
    # Build aggregation pipeline
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "labs",
            "let": {"test_name": "$test_name", "material": "$material"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$card_id", card_id]},
                            {"$eq": ["$category", "Reference"]},
                            {"$eq": ["$test_name", "$$test_name"]},
                            {"$eq": ["$material", "$$material"]}
                        ]
                    }
                }}
            ],
            "as": "reference"
        }},
        {"$group": {
            "_id": {"test_name": "$test_name", "material": "$material"},
            "test_name": {"$first": "$test_name"},
            "material": {"$first": "$material"},
            "units": {"$first": {"$arrayElemAt": ["$reference.units", 0]}},
            "low_value": {"$first": {"$arrayElemAt": ["$reference.low_value", 0]}},
            "high_value": {"$first": {"$arrayElemAt": ["$reference.high_value", 0]}},
            "results": {
                "$push": {
                    "timestamp": "$timestamp",
                    "value": "$value",
                    "operator": "$operator",
                    "note": "$note"
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "test_name": 1,
            "material": 1,
            "units": {"$ifNull": ["$units", ""]},
            "low_value": 1,
            "high_value": 1,
            "results": {"$sortArray": {"input": "$results", "sortBy": {"timestamp": 1}}}
        }}
    ]
    
    try:
        results = await labs_collection.aggregate(pipeline).to_list(length=None)
        return results
    except Exception as e:
        logger.error(f"Error retrieving quantitative labs for card {card_id}: {e}")
        raise


async def get_quantitative_overview(card_id: str) -> List[Dict[str, Any]]:
    """
    Provide catalog of all available quantitative tests for a patient.
    
    Args:
        card_id: The patient's card ID
        
    Returns:
        List of test summaries with test_name, material, and timestamp range
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    pipeline = [
        {"$match": {
            "card_id": card_id,
            "category": "Quantitative"
        }},
        {"$group": {
            "_id": {"test_name": "$test_name", "material": "$material"},
            "test_name": {"$first": "$test_name"},
            "material": {"$first": "$material"},
            "earliest_timestamp": {"$min": "$timestamp"},
            "latest_timestamp": {"$max": "$timestamp"}
        }},
        {"$project": {
            "_id": 0,
            "test_name": 1,
            "material": 1,
            "earliest_timestamp": 1,
            "latest_timestamp": 1
        }},
        {"$sort": {"test_name": 1}}
    ]
    
    try:
        results = await labs_collection.aggregate(pipeline).to_list(length=None)
        return results
    except Exception as e:
        logger.error(f"Error retrieving quantitative overview for card {card_id}: {e}")
        raise


async def get_microbiology_overview(card_id: str) -> List[Dict[str, Any]]:
    """
    List all available microbiology reports for a patient.
    
    Args:
        card_id: The patient's card ID
        
    Returns:
        List of microbiology report summaries (timestamp, material)
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find(
        {"card_id": card_id, "category": "Microbiology"},
        {"_id": 0, "timestamp": 1, "material": 1}
    ).sort("timestamp", -1)
    
    return await cursor.to_list(length=None)


async def get_imaging_overview(card_id: str) -> List[Dict[str, Any]]:
    """
    List all available imaging reports for a patient.
    
    Args:
        card_id: The patient's card ID
        
    Returns:
        List of imaging report summaries (timestamp, exam_type)
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find(
        {"card_id": card_id, "category": "Imaging"},
        {"_id": 0, "timestamp": 1, "exam_type": 1}
    ).sort("timestamp", -1)
    
    return await cursor.to_list(length=None)


async def get_pathology_overview(card_id: str) -> List[Dict[str, Any]]:
    """
    List all available pathology reports for a patient.
    
    Args:
        card_id: The patient's card ID
        
    Returns:
        List of pathology report summaries (timestamp, specimen)
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find(
        {"card_id": card_id, "category": "Pathology"},
        {"_id": 0, "timestamp": 1, "specimen": 1}
    ).sort("timestamp", -1)
    
    return await cursor.to_list(length=None)


async def get_microbiology_report(
    card_id: str,
    timestamp: datetime,
    material: str
) -> List[Dict[str, Any]]:
    """
    Get a single microbiology report by timestamp and material.
    
    Args:
        card_id: The patient's card ID
        timestamp: Exact timestamp from overview
        material: Specimen type from overview
        
    Returns:
        List of matching Microbiology documents
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find({
        "card_id": card_id,
        "category": "Microbiology",
        "timestamp": timestamp,
        "material": material
    })
    
    results = await cursor.to_list(length=None)
    
    # Convert ObjectId to string for serialization
    for doc in results:
        doc["_id"] = str(doc["_id"])
    
    return results


async def get_imaging_report(
    card_id: str,
    timestamp: datetime,
    exam_type: str
) -> List[Dict[str, Any]]:
    """
    Get a single imaging report by timestamp and exam_type.
    
    Args:
        card_id: The patient's card ID
        timestamp: Exact timestamp from overview
        exam_type: Exam type from overview
        
    Returns:
        List of matching Imaging documents
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find({
        "card_id": card_id,
        "category": "Imaging",
        "timestamp": timestamp,
        "exam_type": exam_type
    })
    
    results = await cursor.to_list(length=None)
    
    for doc in results:
        doc["_id"] = str(doc["_id"])
    
    return results


async def get_pathology_report(
    card_id: str,
    timestamp: datetime,
    specimen: str
) -> List[Dict[str, Any]]:
    """
    Get a single pathology report by timestamp and specimen.
    
    Args:
        card_id: The patient's card ID
        timestamp: Exact timestamp from overview
        specimen: Specimen site from overview
        
    Returns:
        List of matching Pathology documents
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = labs_collection.find({
        "card_id": card_id,
        "category": "Pathology",
        "timestamp": timestamp,
        "specimen": specimen
    })
    
    results = await cursor.to_list(length=None)
    
    for doc in results:
        doc["_id"] = str(doc["_id"])
    
    return results


async def get_microbiology_reports_by_indices(
    card_id: str,
    indices: List[int]
) -> List[Dict[str, Any]]:
    """
    Retrieve multiple microbiology reports by their indices from the overview.
    
    Args:
        card_id: The patient's card ID
        indices: List of 0-based indices from get_microbiology_overview
        
    Returns:
        List of full Microbiology documents in the order specified by indices
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    if not indices:
        return []
    
    # Get the overview to map indices to documents
    overview = await get_microbiology_overview(card_id)
    
    results = []
    for idx in indices:
        if 0 <= idx < len(overview):
            item = overview[idx]
            # Retrieve the full document
            cursor = labs_collection.find({
                "card_id": card_id,
                "category": "Microbiology",
                "timestamp": item["timestamp"],
                "material": item["material"]
            })
            docs = await cursor.to_list(length=None)
            for doc in docs:
                doc["_id"] = str(doc["_id"])
                results.append(doc)
    
    return results


async def get_imaging_reports_by_indices(
    card_id: str,
    indices: List[int]
) -> List[Dict[str, Any]]:
    """
    Retrieve multiple imaging reports by their indices from the overview.
    
    Args:
        card_id: The patient's card ID
        indices: List of 0-based indices from get_imaging_overview
        
    Returns:
        List of full Imaging documents in the order specified by indices
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    if not indices:
        return []
    
    overview = await get_imaging_overview(card_id)
    
    results = []
    for idx in indices:
        if 0 <= idx < len(overview):
            item = overview[idx]
            cursor = labs_collection.find({
                "card_id": card_id,
                "category": "Imaging",
                "timestamp": item["timestamp"],
                "exam_type": item["exam_type"]
            })
            docs = await cursor.to_list(length=None)
            for doc in docs:
                doc["_id"] = str(doc["_id"])
                results.append(doc)
    
    return results


async def get_pathology_reports_by_indices(
    card_id: str,
    indices: List[int]
) -> List[Dict[str, Any]]:
    """
    Retrieve multiple pathology reports by their indices from the overview.
    
    Args:
        card_id: The patient's card ID
        indices: List of 0-based indices from get_pathology_overview
        
    Returns:
        List of full Pathology documents in the order specified by indices
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    if not indices:
        return []
    
    overview = await get_pathology_overview(card_id)
    
    results = []
    for idx in indices:
        if 0 <= idx < len(overview):
            item = overview[idx]
            cursor = labs_collection.find({
                "card_id": card_id,
                "category": "Pathology",
                "timestamp": item["timestamp"],
                "specimen": item["specimen"]
            })
            docs = await cursor.to_list(length=None)
            for doc in docs:
                doc["_id"] = str(doc["_id"])
                results.append(doc)
    
    return results


# =============================================================================
# DELETE OPERATIONS FOR LABS COLLECTION
# =============================================================================

async def delete_labs_by_card(card_id: str) -> Dict[str, int]:
    """
    Delete all lab documents for a specific card.
    
    Args:
        card_id: The patient's card ID
        
    Returns:
        Dict with deleted_count
        
    Raises:
        ValueError: If card_id is invalid
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    result = await labs_collection.delete_many({"card_id": card_id})
    
    deleted_count = result.deleted_count
    logger.info(f"Deleted {deleted_count} lab documents for card {card_id}")
    
    return {"deleted_count": deleted_count}


async def delete_card_by_id(card_id: str):
    """
    Deletes a card permanently along with all associated lab data.
    
    Args:
        card_id: The card ID to delete
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # First delete all associated lab documents
        await delete_labs_by_card(card_id)
        
        # Then delete the card itself
        await cards_collection.delete_one({"_id": ObjectId(card_id)})
        return True
    except Exception as e:
        logger.error(f"Error deleting card {card_id}: {e}")
        return False


# =============================================================================
# PENDING INGESTION OPERATIONS (Email Staging Area)
# =============================================================================

async def get_card_by_serial(serial: int) -> Optional[dict]:
    """
    Find a card by its serial number.
    
    Args:
        serial: The card's serial number (e.g., 5 for "Patient 5")
        
    Returns:
        The card helper dict if found, None otherwise
    """
    card = await cards_collection.find_one({"serial": serial})
    if card:
        return card_helper(card)
    return None


async def create_pending_ingestion(data: PendingIngestion) -> str:
    """
    Create a new pending ingestion record in the staging area.
    
    Args:
        data: PendingIngestion Pydantic model containing all ingestion data
        
    Returns:
        The string ID of the newly created pending ingestion document
    """
    # Convert Pydantic model to dict
    doc = data.model_dump()
    
    # Insert into collection
    result = await pending_collection.insert_one(doc)
    
    logger.info(f"Created pending ingestion {result.inserted_id} for card {data.card_id}")
    return str(result.inserted_id)


async def get_pending_ingestion(pending_id: str) -> Optional[dict]:
    """
    Retrieve a pending ingestion by its ID.
    
    Args:
        pending_id: The pending ingestion document ID
        
    Returns:
        The raw document dict if found, None otherwise
    """
    try:
        doc = await pending_collection.find_one({"_id": ObjectId(pending_id)})
        return doc
    except Exception as e:
        logger.error(f"Error fetching pending ingestion {pending_id}: {e}")
        return None


async def delete_pending_ingestion(pending_id: str) -> bool:
    """
    Delete a pending ingestion record.
    
    Args:
        pending_id: The pending ingestion document ID to delete
        
    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        result = await pending_collection.delete_one({"_id": ObjectId(pending_id)})
        deleted = result.deleted_count > 0
        if deleted:
            logger.info(f"Deleted pending ingestion {pending_id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting pending ingestion {pending_id}: {e}")
        return False


async def get_pending_by_card_id(card_id: str) -> List[dict]:
    """
    Get all pending ingestions for a specific card.
    
    Args:
        card_id: The card ID to query
        
    Returns:
        List of pending ingestion documents
    """
    try:
        cursor = pending_collection.find({"card_id": card_id})
        return await cursor.to_list(length=None)
    except Exception as e:
        logger.error(f"Error fetching pending ingestions for card {card_id}: {e}")
        return []