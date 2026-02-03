import os
import motor.motor_asyncio
from bson.objectid import ObjectId

# REPLACE with your Atlas string if needed
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.meddeck_db
cards_collection = db.get_collection("cards")
traces_collection = db.get_collection("agent_traces") # NEW

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
                "end_time": datetime.datetime.now(datetime.timezone.utc),
                "final_output": final_answer
            }
        }
    )