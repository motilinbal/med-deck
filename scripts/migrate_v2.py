#!/usr/bin/env python3
"""
Migration Script: V1 to V2 Chat Architecture

This one-time script migrates existing cards from the deprecated `processed_note`
field to the new `chat` array format. Each existing note is converted to a 
ChatMessage with role=USER, treating it as the first refined input.

Usage:
    python scripts/migrate_v2.py
    
Environment:
    Requires MONGO_URL environment variable (defaults to mongodb://localhost:27017)
"""

import asyncio
import os
import sys
from datetime import datetime

# Add parent directory to path to allow imports from the main app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import motor.motor_asyncio
from bson.objectid import ObjectId

from models import ChatMessage, MessageRole
from config import Config


async def migrate():
    """
    Execute the V1 to V2 migration.
    
    Finds all cards with a non-null `processed_note` field and an empty `chat`
    array, then migrates the note to a chat message and removes the old field.
    """
    mongo_url = Config.MONGO_URL
    print(f"Connecting to MongoDB at: {mongo_url}")
    
    # Initialize Motor client
    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_url)
    db = client.meddeck_db
    cards_collection = db.get_collection("cards")
    
    # Query for cards with processed_note that need migration
    # We look for cards where:
    # 1. processed_note exists and is not null/empty
    # 2. chat array is empty or doesn't exist (to avoid double migration)
    query = {
        "processed_note": {"$exists": True, "$ne": None, "$ne": ""},
        "$or": [
            {"chat": {"$exists": False}},
            {"chat": {"$size": 0}},
            {"chat": None}
        ]
    }
    
    # Count cards to migrate
    total_count = await cards_collection.count_documents(query)
    print(f"Found {total_count} cards to migrate")
    
    if total_count == 0:
        print("No cards need migration. Exiting.")
        client.close()
        return
    
    # Track migration stats
    migrated_count = 0
    skipped_count = 0
    error_count = 0
    
    # Process each card
    cursor = cards_collection.find(query)
    
    async for card in cursor:
        card_id = str(card["_id"])
        serial = card.get("serial", "unknown")
        processed_note = card.get("processed_note", "")
        
        # Double-check: skip if chat already has content
        existing_chat = card.get("chat", [])
        if existing_chat and len(existing_chat) > 0:
            print(f"  [SKIP] Card {card_id} (Serial {serial}): Chat already populated")
            skipped_count += 1
            continue
        
        # Skip empty notes
        if not processed_note or not processed_note.strip():
            print(f"  [SKIP] Card {card_id} (Serial {serial}): Empty processed_note")
            skipped_count += 1
            continue
        
        try:
            # Create a new ChatMessage from the processed_note
            message = ChatMessage(
                role=MessageRole.USER,
                content=processed_note.strip()
            )
            
            # Convert to dict for MongoDB
            message_dict = message.model_dump()
            
            # Ensure timestamp is a datetime object for MongoDB
            if isinstance(message_dict.get("timestamp"), str):
                message_dict["timestamp"] = datetime.fromisoformat(
                    message_dict["timestamp"].replace("Z", "+00:00")
                )
            
            # Perform atomic update: push message and unset processed_note
            result = await cards_collection.update_one(
                {"_id": ObjectId(card_id)},
                {
                    "$push": {"chat": message_dict},
                    "$unset": {"processed_note": ""}
                }
            )
            
            if result.modified_count > 0:
                print(f"  [OK] Card {card_id} (Serial {serial}): Migrated processed_note -> chat")
                migrated_count += 1
            else:
                print(f"  [WARN] Card {card_id} (Serial {serial}): No documents modified")
                skipped_count += 1
                
        except Exception as e:
            print(f"  [ERROR] Card {card_id} (Serial {serial}): {e}")
            error_count += 1
    
    # Print summary
    print("\n" + "=" * 50)
    print("MIGRATION SUMMARY")
    print("=" * 50)
    print(f"Total cards found:    {total_count}")
    print(f"Successfully migrated: {migrated_count}")
    print(f"Skipped:              {skipped_count}")
    print(f"Errors:               {error_count}")
    print("=" * 50)
    
    # Close database connection
    client.close()
    print("\nMigration complete. Database connection closed.")


if __name__ == "__main__":
    print("=" * 50)
    print("MedDeck V1 -> V2 Migration Script")
    print("Converting processed_note to chat array")
    print("=" * 50 + "\n")
    
    asyncio.run(migrate())
