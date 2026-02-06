import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import motor.motor_asyncio
from bson.objectid import ObjectId
from pydantic import ValidationError

from parser import parse_lab_result, create_mongo_timestamp, remove_date_padding
from ai_engine import check_duplicate_documents
from models import (
    QuantitativeLabModel,
    ReferenceRangeModel,
    MicrobiologyModel,
    PathologyModel,
    ImagingModel,
)

# Configure logging
logger = logging.getLogger(__name__)

# REPLACE with your Atlas string if needed
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.meddeck_db
cards_collection = db.get_collection("cards")
traces_collection = db.get_collection("agent_traces")  # NEW
labs_collection = db.get_collection("labs")  # Collection for lab results

def card_helper(card) -> dict:
    return {
        "id": str(card["_id"]),
        "serial": card.get("serial"),
        "nickname": card.get("nickname"),
        "transcript": card.get("transcript", ""),
        "processed_note": card.get("processed_note", ""),
        "analysis": card.get("analysis", None),
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
        "analysis": None
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