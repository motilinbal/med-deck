import os
import json
import asyncio
import logging
import websockets
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from bson.objectid import ObjectId

from database import (
    append_transcript,
    append_chat_message,
    get_all_cards,
    create_empty_card,
    delete_card_by_id,
    cards_collection,
    get_pending_by_card_id,
)
from ai_service import refine_input_transcript
from models import MessageRole
import agent
from app.services.notification_hub import notification_hub
from app.services.email_listener import email_listener
from app.services.ingestion import process_ingestion, discard_ingestion

logger = logging.getLogger(__name__)


# --- FORENSIC DEBUG LOGGER ---
def debug_log(tag: str, message: str):
    """
    High-precision debug logger for tracking race conditions.
    
    Prints timestamp with microsecond precision along with the
    current asyncio task name to identify which coroutine is logging.
    
    Format: [HH:MM:SS.mmmmmm] [TASK_NAME] [TAG] MESSAGE
    """
    timestamp = datetime.now().strftime("%H:%M:%S.%f")
    try:
        task_name = asyncio.current_task().get_name()
    except RuntimeError:
        task_name = "MAIN"
    print(f"[{timestamp}] [{task_name}] [{tag}] {message}")

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


# --- AGENT PIPELINE (Shared Logic) ---
async def run_agent_pipeline(card_id: str, raw_text: str) -> dict:
    """
    Execute the Scribe -> Agent pipeline for a given card and raw text.
    
    This function:
    1. Fetches the chat history from the DB
    2. Runs the Scribe to refine raw text into professional medical text
    3. Saves the refined user message to chat
    4. Runs the Agent to reason and generate a response
    5. Saves the Agent's response (or error) to chat
    
    Note: Caller is responsible for validation and transcript cleanup.
    
    Args:
        card_id: The ObjectId string of the card
        raw_text: The raw transcript text to process
        
    Returns:
        dict with "status" key ("completed" or "error")
    """
    # Fetch the card to get chat history
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []
    
    # =========================================================================
    # STEP 1: THE SCRIBE (Input Refinement)
    # =========================================================================
    # Refine the raw transcript into professional medical English
    refined_text = await refine_input_transcript(raw_text, chat_history)
    
    # Save the refined user message to the chat
    # IMPORTANT: We save BEFORE running the Agent to prevent data loss
    await append_chat_message(card_id, MessageRole.USER, refined_text)
    
    logger.info(f"Scribe complete - User message saved for card {card_id}")
    
    # =========================================================================
    # STEP 2: THE AGENT (Reasoning & Tools)
    # =========================================================================
    # Refetch the card to get the updated chat (including the message we just added)
    # This is safer than manually appending to the local list
    updated_card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    updated_chat_history = updated_card.get("chat", [])
    
    # Run the Agent with the full chat history
    # The Agent will:
    # - Filter to only user/assistant messages
    # - Use tools as needed (emitting "info" messages to chat)
    # - Return a final response OR raise an exception on error
    try:
        agent_response = await agent.run_agent(card_id, updated_chat_history)
        
        # Save the Agent's response to the chat as "assistant"
        await append_chat_message(card_id, MessageRole.ASSISTANT, agent_response)
        
        logger.info(f"Agent complete - Assistant response saved for card {card_id}")
        
    except Exception as e:
        # Agent failed - save as "error" role so UI can display it appropriately
        logger.error(f"Agent failed for card {card_id}: {e}")
        
        error_message = f"System Error during AI reasoning: {str(e)}"
        await append_chat_message(card_id, MessageRole.ERROR, error_message)
        
        return {"status": "error", "message": str(e)}
    
    return {"status": "completed"}


# --- HTTP ENDPOINTS ---
@app.get("/cards")
async def get_cards(): return await get_all_cards()

@app.post("/cards")
async def create_card(): return await create_empty_card()

@app.delete("/cards/{card_id}")
async def delete_card(card_id: str): return {"success": await delete_card_by_id(card_id)}


# --- PENDING INGESTION ENDPOINTS ---
@app.get("/cards/{card_id}/pending")
async def get_pending_for_card(card_id: str):
    """
    Get all pending ingestions for a specific card.
    
    Returns a list of pending email ingestions waiting for user approval.
    """
    pending = await get_pending_by_card_id(card_id)
    return {"pending": pending}


@app.post("/cards/{card_id}/ingest/{pending_id}/approve")
async def approve_ingestion(
    card_id: str,
    pending_id: str,
    background_tasks: BackgroundTasks
):
    """
    Approve and process a pending ingestion.
    
    Starts the ingestion process in the background:
    - Appends text chunks to patient history
    - Processes PDF through OCR pipeline
    - Sends live progress updates via WebSocket
    
    Returns immediately with 202 Accepted style response.
    """
    background_tasks.add_task(process_ingestion, card_id, pending_id)
    return {
        "status": "accepted",
        "message": "Ingestion started",
        "card_id": card_id,
        "pending_id": pending_id
    }


@app.post("/cards/{card_id}/ingest/{pending_id}/discard")
async def discard_pending_ingestion(card_id: str, pending_id: str):
    """
    Discard a pending ingestion.
    
    If the ingestion created a new card (Patient X workflow),
    the card will be deleted entirely.
    
    This operation is fast (DB deletes only) so it runs synchronously.
    """
    await discard_ingestion(card_id, pending_id)
    return {
        "status": "success",
        "message": "Ingestion discarded",
        "card_id": card_id,
        "pending_id": pending_id
    }


# --- WEBSOCKET HANDLER ---
@app.websocket("/ws/audio")
async def audio_websocket(app_socket: WebSocket):
    await app_socket.accept()
    
    # Shared State
    state = {
        "current_card_id": None,
        "should_stop": False,
        "audio_queue": asyncio.Queue(),
        "session_history": "",  # Shared between read_soniox_text and processing_loop
        "last_audio_activity": asyncio.get_event_loop().time()  # Timestamp of last ASR activity
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
                        elif cmd.get("type") == "COMMIT_AND_RESET":
                            print("Queueing Commit...")
                            await state["audio_queue"].put({"type": "COMMIT"})
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
                        try:
                            async for msg in soniox_socket:
                                # PROBE 1: Raw arrival timestamp
                                debug_log("ASR_RAW", "Received message from Soniox")
                                
                                # Update activity timestamp - Soniox sent us something
                                state["last_audio_activity"] = asyncio.get_event_loop().time()
                                
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
                                
                                # PROBE 2: Content analysis - log what we received
                                all_text = final_text + draft_text
                                has_final = bool(final_text)
                                debug_log("ASR_CONTENT", f"is_final={has_final} text='{all_text}'")

                                # 1. SAVE TO DB (Blocking Write with Verification)
                                if final_text:
                                    state["session_history"] += final_text
                                    
                                    # PROBE 3: State mutation - log the buffer update
                                    debug_log("BUFFER_UPDATE", f"Appended text. History Len: {len(state['session_history'])}")
                                    
                                    # Blocking call - capture the result
                                    write_success = await append_transcript(state["current_card_id"], final_text)
                                    
                                    if write_success:
                                        # Send ACK heartbeat to keep the "red dot" alive
                                        ack_message = {
                                            "type": "ACK",
                                            "timestamp": datetime.utcnow().isoformat()
                                        }
                                        await app_socket.send_text(json.dumps(ack_message))
                                    else:
                                        # CRITICAL: Database write failed - must stop recording
                                        logger.critical(f"Database write failed for card {state['current_card_id']}")
                                        
                                        # Send error notification to client
                                        error_message = {
                                            "type": "ERROR",
                                            "code": "WRITE_FAIL",
                                            "message": "Database persistence failed"
                                        }
                                        await app_socket.send_text(json.dumps(error_message))
                                        
                                        # Signal to stop the recording session immediately
                                        # We cannot allow the user to continue speaking if we can't save
                                        state["should_stop"] = True
                                        fin_received_event.set()
                                        raise Exception("Database write failure - stopping recording")

                                # 2. UPDATE UI
                                full_text = state["session_history"] + draft_text
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
                        
                        elif isinstance(item, dict) and item.get("type") == "COMMIT":
                            # PROBE 1: Commit signal received
                            debug_log("COMMIT_START", "Received Commit Signal")
                            print("Commit Signal. Executing Hot Submit...")
                            
                            # 1. Send silence to flush Soniox buffer (1 second of audio)
                            silence_bytes = b'\x00' * 32000  # ~1s at 16kHz 16-bit mono
                            await soniox_socket.send(silence_bytes)
                            
                            # PROBE 2: Silence sent
                            debug_log("COMMIT_ACTION", "Sent Silence bytes")
                            
                            # 2. Smart Wait Loop - wait for ASR to go quiet
                            silence_threshold = 0.5  # seconds of silence to consider done
                            max_wait = 3.0  # safety timeout
                            start_wait = asyncio.get_event_loop().time()
                            
                            while True:
                                now = asyncio.get_event_loop().time()
                                time_since_last_msg = now - state["last_audio_activity"]
                                total_wait_time = now - start_wait
                                
                                # Condition A: Silence detected (success)
                                if time_since_last_msg > silence_threshold:
                                    # PROBE 3A: Wait success
                                    debug_log("WAIT_SUCCESS", f"Silence detected. Quiet for {time_since_last_msg:.3f}s")
                                    print(f"Silence detected after {total_wait_time:.2f}s, cutting.")
                                    break
                                
                                # Condition B: Timeout (safety valve)
                                if total_wait_time > max_wait:
                                    # PROBE 3B: Wait timeout
                                    debug_log("WAIT_TIMEOUT", "Timeout reached")
                                    print("Timeout reached, forcing cut.")
                                    break
                                
                                # Tick - don't eat CPU
                                await asyncio.sleep(0.1)
                            
                            # 3. Capture and reset session history
                            text_payload = state["session_history"]
                            
                            # PROBE 4: The Cut - log what we captured
                            debug_log("CUT_ACTION", f"CAPTURED: '{text_payload}'")
                            
                            state["session_history"] = ""
                            
                            # PROBE 5: The Reset
                            debug_log("RESET_ACTION", "History cleared")
                            
                            # 4. Reset DB transcript field
                            await cards_collection.update_one(
                                {"_id": ObjectId(state["current_card_id"])},
                                {"$set": {"transcript": ""}}
                            )
                            
                            # 5. Update UI to show cleared transcript
                            await app_socket.send_text(json.dumps({
                                "type": "transcript_update",
                                "cardId": state["current_card_id"],
                                "text": ""
                            }))
                            
                            # 6. Execute the Agent pipeline (non-blocking to allow continued recording)
                            print(f"Running agent pipeline for card {state['current_card_id']} with {len(text_payload)} chars")
                            asyncio.create_task(run_agent_pipeline(state["current_card_id"], text_payload))
                            
                            print("Hot Submit complete. Continuing recording...")
                            # Note: We do NOT break - recording continues for next utterance

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
    """
    Process endpoint - The Orchestrator.
    
    This endpoint coordinates the Scribe -> Agent pipeline:
    1. Scribe: Refines raw transcript into professional medical text
    2. Agent: Reasons about the input and generates a response
    
    The user's refined input is saved BEFORE running the Agent,
    ensuring no data loss even if the Agent crashes.
    """
    
    # =========================================================================
    # STEP 0: VALIDATION
    # =========================================================================
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    
    if not card:
        return {"error": "Card not found"}
    
    raw_transcript = card.get("transcript", "")
    
    if not raw_transcript.strip():
        return {"status": "no_content", "message": "Transcript is empty"}
    
    logger.info(f"Processing card {card_id} - Starting Scribe -> Agent pipeline")
    
    # =========================================================================
    # STEP 1: RUN THE PIPELINE
    # =========================================================================
    result = await run_agent_pipeline(card_id, raw_transcript)
    
    # Clear the transcript field (it's now safely in the chat)
    # Only clear on success to allow retry on error
    if result.get("status") == "completed":
        await cards_collection.update_one(
            {"_id": ObjectId(card_id)},
            {"$set": {"transcript": ""}}
        )
    
    return result


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
