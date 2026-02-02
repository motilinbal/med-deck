import os
import motor.motor_asyncio
from bson.objectid import ObjectId

# REPLACE with your Atlas string if needed
MONGO_URL = "mongodb://localhost:27017" 

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.meddeck_db
cards_collection = db.get_collection("cards")

async def get_or_create_card(serial: int, nickname: str):
    """
    Finds the most recent card for this user. 
    If it exists, return it (Resume Session).
    If not, create a new one.
    """
    # 1. Try to find the latest card for this serial number
    # We sort by _id descending (newest first)
    existing_card = await cards_collection.find_one(
        {"serial": serial},
        sort=[("_id", -1)]
    )

    if existing_card:
        # Found one! Return its ID to resume writing to it.
        return str(existing_card["_id"])
    
    # 2. No card found? Create a new one.
    new_card = {
        "serial": serial,
        "nickname": nickname,
        "history": "",     # Starts empty
        "analysis": None
    }
    result = await cards_collection.insert_one(new_card)
    return str(result.inserted_id)

async def append_transcript(card_id: str, text: str):
    """
    Appends new text to the existing history field.
    Uses MongoDB aggregation pipeline to append efficiently.
    """
    if not text: return
    
    # This magic command tells Mongo: "Take the current history, add a space, add the new text."
    await cards_collection.update_one(
        {"_id": ObjectId(card_id)},
        [
            {"$set": {"history": {"$concat": ["$history", " ", text]}}}
        ]
    )