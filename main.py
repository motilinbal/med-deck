import os
import json
import asyncio
import logging
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from bson.objectid import ObjectId

from database import (
    append_transcript,
    get_all_cards,
    create_empty_card,
    delete_card_by_id,
    cards_collection,
    get_pending_ingestion,
    delete_pending_ingestion,
    delete_card_by_id as delete_card_and_labs,
    append_history_chunks,
)
from ai_service import process_transcript_with_gemini
from app.services.notification_hub import notification_hub
from app.services.email_listener import email_listener
from app.services.ingestion import process_ingestion

logger = logging.getLogger(__name__)

SONIOX_API_KEY = os.getenv("SONIOX_API_KEY")
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- STARTUP EVENT ---
@app.on_event("startup")
async def startup_event():
    """Start background services on application startup."""
    logger.info("Starting MedDeck Server...")
    
    # Start email listener in background
    asyncio.create_task(email_listener.start())
    logger.info("Email listener started")


# --- HTTP ENDPOINTS ---
@app.get("/cards")
async def get_cards(): return await get_all_cards()

@app.post("/cards")
async def create_card(): return await create_empty_card()

@app.delete("/cards/{card_id}")
async def delete_card(card_id: str): return {"success": await delete_card_by_id(card_id)}

# --- WEBSOCKET ENDPOINTS ---
@app.websocket("/ws/{card_id}")
async def websocket_endpoint(websocket: WebSocket, card_id: str):
    """
    WebSocket endpoint for real-time notifications to clients.
    
    Clients connect to /ws/{card_id} to receive system events
    (new_mail, process_status) for a specific card.
    """
    await notification_hub.connect(websocket, card_id)
    try:
        while True:
            # Keep connection alive, listen for client messages
            # Clients can send pings or other commands if needed
            data = await websocket.receive_text()
            
            # Echo back for ping/pong keepalive
            if data == "ping":
                await websocket.send_text("pong")
                
    except WebSocketDisconnect:
        notification_hub.disconnect(websocket, card_id)
        logger.info(f"WebSocket disconnected for card {card_id}")


# --- WEBSOCKET HANDLER ---
@app.websocket("/ws/audio")
async def audio_websocket(app_socket: WebSocket):
    await app_socket.accept()
    
    # Shared State
    state = {
        "current_card_id": None,
        "should_stop": False,
        "audio_queue": asyncio.Queue() 
    }
    
    print("Client Connected.")

    # --- 1. READER LOOP (Phone -> Buffer) ---
    async def app_reader():
        try:
            while True:
                message = await app_socket.receive()
                if "bytes" in message:
                    await state["audio_queue"].put(message["bytes"])
                elif "text" in message:
                    try:
                        cmd = json.loads(message["text"])
                        if cmd.get("type") == "switch_card":
                            print(f"Queueing Switch to {cmd.get('cardId')}")
                            await state["audio_queue"].put({"type": "SWITCH", "id": cmd.get("cardId")})
                        elif cmd.get("type") == "stop_recording":
                            print("Queueing Stop")
                            await state["audio_queue"].put({"type": "STOP"})
                    except Exception as e:
                        print(f"Cmd Error: {e}")
        except:
            await state["audio_queue"].put({"type": "STOP"})

    # --- 2. PROCESSOR LOOP (Buffer -> Soniox) ---
    async def processing_loop():
        while True:
            # A. Wait for valid card ID
            if not state["current_card_id"]:
                item = await state["audio_queue"].get()
                if isinstance(item, dict) and item.get("type") == "SWITCH":
                    state["current_card_id"] = item.get("id")
                elif isinstance(item, dict) and item.get("type") == "STOP":
                    break 
                continue

            print(f"Starting Session for Card {state['current_card_id']}")
            try:
                async with websockets.connect(SONIOX_URL) as soniox_socket:
                    await soniox_socket.send(json.dumps({
                        "api_key": SONIOX_API_KEY,
                        "model": "stt-rt-v3",
                        "audio_format": "pcm_s16le",
                        "sample_rate": 16000,
                        "num_channels": 1,
                        "enable_endpoint_detection": False  # Disabled for manual dictation
                    }))

                    fin_received_event = asyncio.Event()

                    # B. Background Reader (Soniox -> Server)
                    async def read_soniox_text():
                        session_history = ""
                        try:
                            async for msg in soniox_socket:
                                resp = json.loads(msg)
                                tokens = resp.get("tokens", [])
                                if not tokens: continue

                                has_fin = False
                                has_end = False
                                clean_tokens = []
                                for t in tokens:
                                    if t.get("text") == "<fin>":
                                        print(">> Received <fin> token.")
                                        has_fin = True
                                    elif t.get("text") == "<end>":
                                        print(">> Received <end> token (endpoint detected).")
                                        has_end = True
                                    else:
                                        clean_tokens.append(t)

                                final_text = "".join([t["text"] for t in clean_tokens if t.get("is_final")])
                                draft_text = "".join([t["text"] for t in clean_tokens if not t.get("is_final")])

                                # 1. SAVE TO DB
                                if final_text:
                                    session_history += final_text
                                    await append_transcript(state["current_card_id"], final_text)

                                # 2. UPDATE UI
                                full_text = session_history + draft_text
                                if full_text.strip():
                                    await app_socket.send_text(json.dumps({
                                        "type": "transcript_update",
                                        "cardId": state["current_card_id"],
                                        "text": full_text
                                    }))

                                # 3. SIGNAL FIN RECEIVED (but don't break yet!)
                                if has_fin:
                                    fin_received_event.set()
                                    # Continue processing - there might be more tokens!

                        except websockets.exceptions.ConnectionClosed:
                            # Normal closure - expected behavior
                            print("Soniox socket closed normally")
                        except Exception as e:
                            print(f"Reader Loop Error: {e}")
                        finally:
                            # Always signal that reader is done
                            fin_received_event.set()

                    reader_task = asyncio.create_task(read_soniox_text())

                    # C. Stream Audio Loop
                    switch_signal = False
                    
                    while True:
                        # Use a small timeout to allow checking for "STOP" while keeping stream active
                        try:
                            item = await asyncio.wait_for(state["audio_queue"].get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue  # Keep waiting if connection is open

                        if isinstance(item, bytes):
                            await soniox_socket.send(item)
                        
                        elif isinstance(item, dict) and item.get("type") == "SWITCH":
                            print("Switch Signal. Finalizing...")
                            switch_signal = True
                            next_card_id = item.get("id")
                            
                            # CRITICAL: Drain remaining audio bytes from queue before breaking
                            print("Draining remaining audio from queue...")
                            drained_count = 0
                            while not state["audio_queue"].empty():
                                try:
                                    next_item = state["audio_queue"].get_nowait()
                                    if isinstance(next_item, bytes):
                                        await soniox_socket.send(next_item)
                                        drained_count += 1
                                except asyncio.QueueEmpty:
                                    break
                            print(f"Drained {drained_count} audio chunks on SWITCH")
                            break
                            
                        elif isinstance(item, dict) and item.get("type") == "STOP":
                            print("Stop Signal. Finalizing...")
                            state["should_stop"] = True
                            switch_signal = True
                            
                            # CRITICAL: Drain remaining audio bytes
                            print("Draining remaining audio from queue...")
                            drained_count = 0
                            while not state["audio_queue"].empty():
                                try:
                                    next_item = state["audio_queue"].get_nowait()
                                    if isinstance(next_item, bytes):
                                        print(f"Draining pending audio chunk: {len(next_item)} bytes")
                                        await soniox_socket.send(next_item)
                                        drained_count += 1
                                except asyncio.QueueEmpty:
                                    break
                            print(f"Drained {drained_count} audio chunks on STOP")
                            break

                    # D. FINALIZATION SEQUENCE
                    
                    # 1. Send ~500ms of Silence to flush the buffer
                    # 16000 samples/sec * 2 bytes/sample * 0.5 sec = 16000 bytes
                    silence_bytes = b'\x00' * 16000
                    await soniox_socket.send(silence_bytes)
                    
                    # 2. Short sleep to ensure order
                    await asyncio.sleep(0.2)

                    # 3. Send Finalize
                    await soniox_socket.send(json.dumps({"type": "finalize"}))
                    
                    # 4. Wait for <fin>
                    try:
                        await asyncio.wait_for(fin_received_event.wait(), timeout=5.0)
                        print("<fin> received successfully")
                    except asyncio.TimeoutError:
                        print("Warning: <fin> wait timed out after 5 seconds")

                    # 5. Flush UI before closing
                    # Give a tiny moment for the last 'app_socket.send_text' (from reader_task)
                    # to actually hit the network before we close the socket.
                    await asyncio.sleep(0.1)

                    # 6. Close the socket
                    await soniox_socket.close()

                    # 7. Wait for reader task to complete (with timeout)
                    try:
                        await asyncio.wait_for(reader_task, timeout=3.0)
                        print("Reader task completed")
                    except asyncio.TimeoutError:
                        print("Warning: Reader task did not complete gracefully")
                        try:
                            reader_task.cancel()
                        except asyncio.CancelledError:
                            pass
                    
                    # 8. Proceed with switch or stop
                    if switch_signal:
                        if state["should_stop"]:
                            print("Stopping processing loop")
                            break
                        else:
                            print(f"Switching to card {next_card_id}")
                            state["current_card_id"] = next_card_id

            except Exception as e:
                print(f"Soniox Connection Error: {e}")
                await asyncio.sleep(1)

        print("Processing finished. Closing socket.")
        try:
            await app_socket.close()
        except RuntimeError:
            pass

    await asyncio.gather(app_reader(), processing_loop())


@app.post("/cards/{card_id}/process")
async def process_card_transcript(card_id: str):
    # 1. Fetch the card (Now cards_collection is defined!)
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    
    if not card:
        return {"error": "Card not found"}
    
    raw_transcript = card.get("transcript", "")
    current_note = card.get("processed_note", "")

    if not raw_transcript.strip():
        return {"status": "no_change", "message": "Transcript is empty"}

    print(f"Processing card {card_id} with Gemini...")

    # 2. Call Gemini
    updated_note = await process_transcript_with_gemini(current_note, raw_transcript)

    # 3. Update DB
    await cards_collection.update_one(
        {"_id": ObjectId(card_id)},
        {
            "$set": {
                "processed_note": updated_note,
                "transcript": "" 
            }
        }
    )

    return {"status": "success", "processed_note": updated_note}