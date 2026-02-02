import os
import motor.motor_asyncio
from bson.objectid import ObjectId

# REPLACE with your Atlas string if needed
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.meddeck_db
cards_collection = db.get_collection("cards")

def card_helper(card) -> dict:
    return {
        "id": str(card["_id"]),
        "serial": card.get("serial"),
        "nickname": card.get("nickname"),
        "transcript": card.get("transcript", ""), # Raw accumulation bucket
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