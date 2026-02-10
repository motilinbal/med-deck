import os
import logging
from datetime import datetime, timezone
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
    ChatMessage,
    MessageRole,
    HistoryChunk,
    ProcessedHistoryDocument,
)
from app.services.notification_hub import notification_hub

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
history_collection = db.get_collection("history")  # Processed clinical history documents

def card_helper(card) -> dict:
    """
    Transform a MongoDB card document for API responses.
    
    Includes the chat array for frontend rendering.
    """
    # Get the chat array and serialize datetime objects to ISO strings
    chat = card.get("chat", [])
    serialized_chat = []
    for msg in chat:
        msg_dict = msg.copy() if isinstance(msg, dict) else msg
        # Convert datetime to ISO string if present
        if isinstance(msg_dict.get("timestamp"), datetime):
            msg_dict["timestamp"] = msg_dict["timestamp"].isoformat()
        serialized_chat.append(msg_dict)
    
    return {
        "id": str(card["_id"]),
        "serial": card.get("serial"),
        "nickname": card.get("nickname"),
        "transcript": card.get("transcript", ""),
        "chat": serialized_chat,
        "processed_note": card.get("processed_note", ""),  # Deprecated but kept for migration
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

async def append_transcript(card_id: str, text: str) -> bool:
    """
    Appends new text to the specific card's transcript bucket.
    
    Uses MongoDB $concat with $ifNull to safely append text with a space separator.
    This ensures words don't get glued together when multiple chunks are appended.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        text: The text to append to the transcript
        
    Returns:
        True if the append was successful, False otherwise
    """
    if not text or not card_id:
        return False
    
    try:
        result = await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            [
                {"$set": {"transcript": {"$concat": [{"$ifNull": ["$transcript", ""]}, " ", text]}}}
            ]
        )
        return result.acknowledged
    except Exception as e:
        logger.error(f"DB Error appending transcript to {card_id}: {e}")
        return False


async def append_chat_message(card_id: str, role: MessageRole, content: str) -> ChatMessage:
    """
    Append a new chat message to a card's chat array and notify connected clients.
    
    This function implements the "Write + Notify" pattern:
    1. Creates a new ChatMessage object
    2. Persists it to MongoDB using $push
    3. Immediately broadcasts the message via WebSocket to the frontend
    
    Args:
        card_id: The card's MongoDB ObjectId string
        role: The message role (user, assistant, log, info, or error)
        content: The text content of the message
        
    Returns:
        The created ChatMessage object
        
    Raises:
        ValueError: If card_id is invalid
        Exception: If database write fails
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Create the new chat message
    message = ChatMessage(
        role=role,
        content=content
    )
    
    # Convert to dict for MongoDB storage (keeps datetime as datetime objects)
    mongo_dict = message.model_dump()
    
    # Convert timestamp to datetime for MongoDB (Pydantic may serialize it)
    if isinstance(mongo_dict.get("timestamp"), str):
        mongo_dict["timestamp"] = datetime.fromisoformat(mongo_dict["timestamp"].replace("Z", "+00:00"))
    
    # Push to the chat array in MongoDB
    try:
        result = await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            {"$push": {"chat": mongo_dict}}
        )
        
        if not result.acknowledged:
            raise Exception("Database write not acknowledged")
            
    except Exception as e:
        logger.error(f"Failed to append chat message to card {card_id}: {e}")
        raise
    
    # Create a separate payload for WebSocket (datetime as ISO strings for JSON)
    socket_payload = message.model_dump(mode='json')
    
    # Notify connected clients via WebSocket
    await notification_hub.emit_system_event(
        card_id=card_id,
        category="chat_update",
        payload=socket_payload
    )
    
    logger.info(f"Appended {role.value} message to card {card_id}")
    
    return message


async def create_trace_run(card_id: str, user_prompt: str):
    """
    Initializes a new 'Run' for the agent.
    Returns the run_id so we can append events to it.
    """
    new_trace = {
        "card_id": str(card_id),
        "start_time": datetime.now(timezone.utc),
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
        "timestamp": datetime.now(timezone.utc),
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

async def append_raw_chunks(
    card_id: str,
    text: str,
    delimiter: str = DELIMITER
) -> Dict[str, Any]:
    """
    Append new raw text chunks to a card's chunks ledger.
    
    Splits input text by delimiter, checks each chunk against existing
    chunks for duplicates using quantify_text_divergence(), and appends
    non-duplicate chunks with processed_id=None.
    
    This function serves as the entry point for the ledger architecture,
    storing raw text that will later be processed by the Scribe pipeline.
    
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
    
    # Fetch the card to get existing chunks
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise KeyError(f"Card not found: {card_id}")
    
    # Get existing chunks (default to empty list)
    existing_chunks = card.get("chunks", [])
    existing_raw_texts = [item.get("text", "") for item in existing_chunks]
    
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
        
        for existing_idx, existing_raw in enumerate(existing_raw_texts):
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
            # Create new chunk matching HistoryChunk model
            new_chunk = {
                "text": candidate,
                "processed_id": None,
                "ingested_at": datetime.utcnow()
            }
            new_chunks.append(new_chunk)
            added_indices.append(input_idx)
            details.append({
                "input_index": input_idx,
                "action": "added",
                "matched_existing_index": None,
                "similarity_metrics": None
            })
            # Also add to existing_raw_texts to avoid duplicates within the same batch
            existing_raw_texts.append(candidate)
    
    # If we have new chunks to add, push them to the database
    if new_chunks:
        await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            {"$push": {"chunks": {"$each": new_chunks}}}
        )
        logger.info(f"Added {len(new_chunks)} new raw chunks to card {card_id}")
    
    return {
        "card_id": card_id,
        "total_input_chunks": len(candidate_chunks),
        "added": len(new_chunks),
        "skipped_duplicates": len(skipped_indices),
        "added_indices": added_indices,
        "skipped_indices": skipped_indices,
        "details": details
    }


async def create_history_document(doc: ProcessedHistoryDocument) -> str:
    """
    Create a new processed history document in the history collection.
    
    This stores the Scribe-processed clinical narrative extracted from a raw chunk.
    
    Args:
        doc: A ProcessedHistoryDocument Pydantic model containing:
            - card_id: Reference to the parent card
            - timestamp: The clinical date of the event
            - date_estimated: Whether the date was inferred
            - title: One-line summary
            - content: Full Markdown narrative
            - original_chunk_index: Index of the source chunk
            
    Returns:
        The string ID of the newly created history document
        
    Raises:
        ValueError: If document validation fails
    """
    # Convert Pydantic model to dict
    doc_dict = doc.model_dump()
    
    # Handle the id field - if present and not None, use it as _id
    if doc.id is not None:
        doc_dict["_id"] = ObjectId(doc.id)
        del doc_dict["id"]
    else:
        del doc_dict["id"]
    
    # Insert into history collection
    result = await history_collection.insert_one(doc_dict)
    
    logger.info(f"Created history document {result.inserted_id} for card {doc.card_id}")
    return str(result.inserted_id)


async def update_chunk_processed_id(card_id: str, index: int, history_id: str) -> bool:
    """
    Update the processed_id field of a specific chunk in a card's chunks array.
    
    This links the raw chunk to its processed document in the history collection,
    marking it as "processed" and preventing re-processing by the Scribe pipeline.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        index: 0-based index into the chunks array
        history_id: The MongoDB ObjectId string of the processed history document
        
    Returns:
        True if update was successful, False otherwise
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    try:
        result = await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            {"$set": {f"chunks.{index}.processed_id": history_id}}
        )
        
        if result.acknowledged:
            logger.info(f"Updated chunk {index} processed_id to {history_id} for card {card_id}")
            return True
        else:
            logger.warning(f"Update not acknowledged for chunk {index} in card {card_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating chunk processed_id: {e}")
        return False


async def get_unprocessed_chunks(card_id: str) -> Optional[Dict[str, Any]]:
    """
    Find the first unprocessed chunk in a card's chunks array.
    
    This is critical for the Scribe pipeline's resumability - it identifies
    where processing should resume by finding chunks where processed_id is None.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        
    Returns:
        Dict with 'index' (int) and 'text' (str) of the first unprocessed chunk,
        or None if all chunks are processed or card/chunks don't exist
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Fetch the card's chunks array
    card = await cards_collection.find_one(
        {"_id": ObjectId(card_id)},
        {"chunks": 1}
    )
    
    if not card:
        return None
    
    chunks = card.get("chunks", [])
    
    # Find the first chunk where processed_id is None
    for index, chunk in enumerate(chunks):
        if chunk.get("processed_id") is None:
            return {
                "index": index,
                "text": chunk.get("text", "")
            }
    
    # All chunks are processed
    return None


async def get_processed_history_context(card_id: str, limit_index: int) -> List[tuple]:
    """
    Retrieve processed history documents for chunks 0 to limit_index (exclusive).
    
    This builds the LLM context window by fetching all processed documents
    that came before the current chunk being processed. Results are returned
    in the original chunk order (not random $in order).
    
    Args:
        card_id: The card's MongoDB ObjectId string
        limit_index: Exclusive upper bound - get context for chunks 0 to limit_index-1
        
    Returns:
        List of tuples: (raw_text, processed_doc_dict) sorted by original chunk order.
        The raw_text is the original input, processed_doc_dict is the LLM output.
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    if limit_index <= 0:
        return []
    
    # Fetch the card's chunks array
    card = await cards_collection.find_one(
        {"_id": ObjectId(card_id)},
        {"chunks": 1}
    )
    
    if not card:
        return []
    
    chunks = card.get("chunks", [])
    
    # Slice chunks from 0 to limit_index (exclusive) and collect processed_ids
    sliced_chunks = chunks[:limit_index]
    processed_ids = []
    id_to_order = {}  # Map ObjectId string to original chunk index
    
    for idx, chunk in enumerate(sliced_chunks):
        processed_id = chunk.get("processed_id")
        if processed_id:
            processed_ids.append(ObjectId(processed_id))
            id_to_order[processed_id] = idx
    
    if not processed_ids:
        return []
    
    # Query history_collection for these IDs
    cursor = history_collection.find({"_id": {"$in": processed_ids}})
    raw_results = await cursor.to_list(length=None)
    
    # Sort results by original chunk order and pair with raw text
    sorted_results = []
    for doc in raw_results:
        doc_id = str(doc["_id"])
        if doc_id in id_to_order:
            # Convert ObjectId to string for serialization
            doc["_id"] = doc_id
            chunk_index = id_to_order[doc_id]
            raw_text = sliced_chunks[chunk_index].get("text", "")
            sorted_results.append((chunk_index, raw_text, doc))
    
    # Sort by chunk index and return (raw_text, doc) tuples
    sorted_results.sort(key=lambda x: x[0])
    return [(raw_text, doc) for _, raw_text, doc in sorted_results]


async def get_history_overview(card_id: str) -> List[Dict[str, Any]]:
    """
    Get a chronological catalog of processed history documents for a patient.
    
    This provides a lightweight "table of contents" for the Agent to browse
    available history entries. Results are sorted by clinical timestamp descending.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        
    Returns:
        List of summary dicts with '_id', 'timestamp', 'title', 'date_estimated' fields,
        sorted by timestamp descending
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    cursor = history_collection.find(
        {"card_id": card_id},
        {"_id": 1, "timestamp": 1, "title": 1, "date_estimated": 1}
    ).sort("timestamp", -1)
    
    results = await cursor.to_list(length=None)
    
    # Convert ObjectId to string for serialization
    for doc in results:
        doc["_id"] = str(doc["_id"])
    
    return results


async def get_history_documents_by_indices(card_id: str, indices: List[int]) -> List[Dict[str, Any]]:
    """
    Retrieve full history documents by their indices from the overview.
    
    This allows the Agent to fetch specific history entries by index number
    (as shown in the overview). The function first gets the sorted overview
    to establish deterministic ordering, then fetches full documents.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        indices: List of 0-based indices from get_history_overview
        
    Returns:
        List of full history documents in the requested order
        
    Raises:
        ValueError: If card_id is invalid
    """
    # Validate card_id
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    if not indices:
        return []
    
    # Get the overview to establish deterministic order
    overview = await get_history_overview(card_id)
    
    # Map indices to document IDs
    requested_ids = []
    for idx in indices:
        if 0 <= idx < len(overview):
            doc_id = overview[idx]["_id"]
            requested_ids.append(ObjectId(doc_id))
    
    if not requested_ids:
        return []
    
    # Fetch full documents
    cursor = history_collection.find({"_id": {"$in": requested_ids}})
    raw_results = await cursor.to_list(length=None)
    
    # Create a map of ID to document for ordering
    id_to_doc = {}
    for doc in raw_results:
        doc_id = str(doc["_id"])
        doc["_id"] = doc_id
        id_to_doc[doc_id] = doc
    
    # Return documents in the requested order
    results = []
    for idx in indices:
        if 0 <= idx < len(overview):
            doc_id = overview[idx]["_id"]
            if doc_id in id_to_doc:
                results.append(id_to_doc[doc_id])
    
    return results


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



async def get_abnormal_labs(
    card_id: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100  # <--- FIXED: Added limit
) -> Dict[str, Any]:  # <--- FIXED: Returns Dict
    """
    Retrieve abnormal lab results with a safety-first approach.
    """
    try:
        ObjectId(card_id)
    except Exception:
        raise ValueError(f"Invalid card_id: {card_id}")
    
    # Build timestamp filter
    date_filter = {}
    if start_time is not None:
        date_filter["$gte"] = start_time
    if end_time is not None:
        date_filter["$lte"] = end_time
    
    # Build match stage
    match_stage = {
        "card_id": card_id,
        "category": "Quantitative"
    }
    if date_filter:
        match_stage["timestamp"] = date_filter
    
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "labs",
            "let": {"t_name": "$test_name", "mat": "$material"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$card_id", card_id]},
                            {"$eq": ["$category", "Reference"]},
                            {"$eq": ["$test_name", "$$t_name"]},
                            {"$eq": ["$material", "$$mat"]}
                        ]
                    }
                }}
            ],
            "as": "ref_data"
        }},
        {"$unwind": {"path": "$ref_data", "preserveNullAndEmptyArrays": True}},
        {"$match": {
            "$expr": {
                "$or": [
                    {"$eq": ["$ref_data", None]},
                    {"$and": [
                        {"$isNumber": "$value"},
                        {"$ne": ["$ref_data", None]},
                        {"$isNumber": "$ref_data.low_value"},
                        {"$lt": ["$value", "$ref_data.low_value"]}
                    ]},
                    {"$and": [
                        {"$isNumber": "$value"},
                        {"$ne": ["$ref_data", None]},
                        {"$isNumber": "$ref_data.high_value"},
                        {"$gt": ["$value", "$ref_data.high_value"]}
                    ]}
                ]
            }
        }},
        {"$project": {
            "_id": 0, "test_name": 1, "material": 1, "value": 1, "operator": 1, "timestamp": 1,
            "unit": {"$ifNull": ["$ref_data.units", ""]},
            "ref_low": {"$ifNull": ["$ref_data.low_value", None]},
            "ref_high": {"$ifNull": ["$ref_data.high_value", None]},
            "status": {
                "$switch": {
                    "branches": [
                        {"case": {"$eq": ["$ref_data", None]}, "then": "UNKNOWN_REF"},
                        {"case": {"$and": [{"$isNumber": "$value"}, {"$ne": ["$ref_data", None]}, {"$lt": ["$value", "$ref_data.low_value"]}]}, "then": "LOW"},
                        {"case": {"$and": [{"$isNumber": "$value"}, {"$ne": ["$ref_data", None]}, {"$gt": ["$value", "$ref_data.high_value"]}]}, "then": "HIGH"}
                    ],
                    "default": "ABNORMAL"
                }
            }
        }},
        {"$sort": {"timestamp": -1}},
        {"$limit": limit + 1} # Fetch one extra to check for truncation
    ]
    
    try:
        results = await labs_collection.aggregate(pipeline).to_list(length=None)
        
        # Check for truncation
        truncated = False
        if len(results) > limit:
            truncated = True
            results = results[:limit]
            
        # FIXED: Return DICTIONARY to match tools.py expectation
        return {
            "results": results,
            "truncated": truncated,
            "total_available": len(results) + (1 if truncated else 0)
        }
    except Exception as e:
        logger.error(f"Error retrieving abnormal labs for card {card_id}: {e}")
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
        The document dict with ObjectId converted to string, None if not found
    """
    try:
        doc = await pending_collection.find_one({"_id": ObjectId(pending_id)})
        if doc:
            # Convert ObjectId to string for JSON serialization
            doc["_id"] = str(doc["_id"])
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
        List of pending ingestion documents with ObjectIds converted to strings.
        EXCLUDES the heavy 'pdf_data' binary field for performance and JSON safety.
    """
    try:
        # Exclude 'pdf_data' from the result set (0 = exclude in MongoDB projection)
        cursor = pending_collection.find(
            {"card_id": card_id},
            {"pdf_data": 0}
        )
        results = await cursor.to_list(length=None)
        
        # Convert ObjectId to string for JSON serialization
        for doc in results:
            doc["_id"] = str(doc["_id"])
        
        return results
    except Exception as e:
        logger.error(f"Error fetching pending ingestions for card {card_id}: {e}")
        return []


async def ingestion_exists_by_uid(email_uid: str) -> bool:
    """
    Check if a pending ingestion already exists for the given email UID.
    
    This is used for idempotency - preventing duplicate processing of the same
    email when it's fetched multiple times from the IMAP server.
    
    Args:
        email_uid: The unique identifier assigned by the mail server
        
    Returns:
        True if a pending ingestion with this UID exists, False otherwise
    """
    try:
        count = await pending_collection.count_documents({"email_uid": email_uid})
        return count > 0
    except Exception as e:
        logger.error(f"Error checking ingestion existence for UID {email_uid}: {e}")
        # On error, return False to allow processing (fail-open)
        return False