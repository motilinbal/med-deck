import os
import json
import asyncio
import logging
import traceback
import websockets
from datetime import datetime
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from bson.objectid import ObjectId
from pydantic import BaseModel, Field

from database import (
    append_transcript,
    append_chat_message,
    get_all_cards,
    create_empty_card,
    delete_card_by_id,
    cards_collection,
    get_pending_by_card_id,
    update_card_nickname,
    get_card_metadata,
    get_all_pending_images,
)
from ai_service import refine_input_transcript
from models import MessageRole
import agent
from app.services.notification_hub import notification_hub
from app.services.email_listener import email_listener
from app.services.ingestion import process_ingestion, discard_ingestion
from app.services.email_sender import send_email_broadcast
from app.services.image_processor import process_image, process_decision

logger = logging.getLogger(__name__)

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class NicknameUpdateRequest(BaseModel):
    """Request model for nickname update endpoint."""
    nickname: str = Field(..., min_length=1, description="New nickname for the card")


class CardMetadataResponse(BaseModel):
    """Response model for card metadata endpoint."""
    last_history: Optional[str] = Field(None, description="ISO timestamp of latest history entry")
    last_lab: Optional[str] = Field(None, description="ISO timestamp of latest lab entry")


class ImageDecisionRequest(BaseModel):
    """Request model for image decision endpoint."""
    decision: str = Field(..., description="Decision: 'approve' or 'decline'")
    card_id: Optional[str] = Field(None, description="Card ID (required if decision is 'approve')")


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
        logger.error(f"Full traceback: {traceback.format_exc()}")
        
        error_message = f"System Error during AI reasoning: {str(e)}"
        await append_chat_message(card_id, MessageRole.ERROR, error_message)
        
        return {"status": "error", "message": str(e)}
    
    return {"status": "completed"}


# --- ADMISSION AGENT PIPELINE (Background Task) ---
async def _run_admission_pipeline(card_id: str) -> None:
    """
    Background task that generates an admission note and sends it via email.
    
    This function is triggered by the "Admission" button on the card back.
    It runs the Admission Agent (Phantom Agent) which:
    1. Reads the full patient context (Labs, History, Chat)
    2. Generates an English admission note using gemini-2.5-flash
    3. Translates to Hebrew using gemini-2.5-flash
    4. Sanitizes the Hebrew text
    5. Sends the note via email
    6. Posts an info message to the chat (not the full note)
    
    The generated note is NOT saved to the chat history to keep the
    conversation clean.
    
    Args:
        card_id: The ObjectId string of the card
    """
    logger.info(f"Admission Agent started for card {card_id}")
    
    # Fetch the card to get chat history
    try:
        card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    except Exception as e:
        logger.error(f"Card lookup failed for {card_id}: {e}")
        return
    
    if not card:
        logger.error(f"Card {card_id} not found for admission agent")
        return
    
    chat_history = card.get("chat", [])
    
    try:
        # Run the Admission Agent
        # This will:
        # - Use TransientLog to show "Checking Labs..." etc. in real-time
        # - Generate an English admission note using gemini-2.5-flash
        # - Return the English note text (NOT saved to chat)
        logger.info(f"Running admission agent for card {card_id}...")
        admission_note_en = await agent.run_admission_agent(card_id)
        logger.info(f"Admission agent completed for card {card_id}, note length: {len(admission_note_en)}")
        
    except Exception as e:
        logger.error(f"Admission agent execution failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Admission Note generation failed: {str(e)}"
        )
        return
    
    try:
        # Process the output: translate to Hebrew, sanitize, and send email
        serial = card.get("serial", "Unknown")
        subject = f"Admission Note - Patient #{serial}"
        
        # Step 1: Translate to Hebrew - emit INFO message
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "🌐 Translating admission note to Hebrew..."
        )
        
        # Use the output pipeline to translate, sanitize, and email
        logger.info(f"Translating and sending email for card {card_id}...")
        final_result = await agent.process_agent_output(
            output_dest=agent.OutputDestination.EMAIL_WITH_TRANSLATION,
            content=admission_note_en,
            card_id=card_id,
            subject=subject
        )
        logger.info(f"Email sent successfully for card {card_id}")
        
    except Exception as e:
        logger.error(f"Output pipeline failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Translation/Email failed: {str(e)}"
        )
        return
    
    try:
        # Step 2: Send email - emit INFO message
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "📧 Sending email to clinical team..."
        )
        
        # Log success
        logger.info(f"Admission note processed and sent via email for card {card_id}")
        
        # Post an info message to the chat (not the full note)
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "Admission Note generated, translated to Hebrew, and sent to email."
        )
        
    except Exception as e:
        logger.error(f"Final info message failed for card {card_id}: {e}", exc_info=True)


# --- HTTP ENDPOINTS ---
@app.get("/cards")
async def get_cards(): return await get_all_cards()

@app.post("/cards")
async def create_card(): return await create_empty_card()

@app.delete("/cards/{card_id}")
async def delete_card(card_id: str): return {"success": await delete_card_by_id(card_id)}


@app.patch("/cards/{card_id}/nickname")
async def update_card_nickname_endpoint(
    card_id: str,
    payload: NicknameUpdateRequest
) -> Dict[str, str]:
    """
    Update the nickname of a card.
    
    Validates the nickname is non-empty, updates the database,
    and broadcasts the change to all connected clients.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        payload: Request body containing the new nickname
        
    Returns:
        Dict with status and the updated nickname
        
    Raises:
        HTTPException 400: If nickname is empty
        HTTPException 404: If card not found
    """
    # Validate nickname (strip whitespace)
    cleaned_nickname = payload.nickname.strip()
    if not cleaned_nickname:
        raise HTTPException(status_code=400, detail="Nickname cannot be empty")
    
    # Update in database
    try:
        success = await update_card_nickname(card_id, cleaned_nickname)
        if not success:
            raise HTTPException(status_code=404, detail="Card not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Broadcast update to all connected clients
    await notification_hub.emit_system_event(
        card_id=card_id,
        category="card_update",
        payload={"nickname": cleaned_nickname}
    )
    
    logger.info(f"Updated nickname for card {card_id} to '{cleaned_nickname}'")
    
    return {"status": "success", "nickname": cleaned_nickname}


@app.get("/cards/{card_id}/metadata", response_model=CardMetadataResponse)
async def get_card_metadata_endpoint(card_id: str) -> CardMetadataResponse:
    """
    Get the latest clinical timestamps for a card.
    
    Returns the most recent timestamps from the history and labs
    collections for the given card.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        
    Returns:
        CardMetadataResponse with last_history and last_lab timestamps (ISO format)
        
    Raises:
        HTTPException 400: If card_id is invalid
    """
    try:
        metadata = await get_card_metadata(card_id)
        return CardMetadataResponse(**metadata)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


# --- IMAGE INGESTION ENDPOINTS (for shared images) ---

@app.post("/images/pending")
async def upload_pending_image(file: UploadFile = File(...)):
    """
    Upload a single image for OCR processing.

    The image is processed immediately (streaming - no waiting for other images).
    Returns the OCR result for frontend to display for user approval.

    This endpoint is for images shared from outside the app (gallery, camera).
    """
    # Read image bytes
    image_bytes = await file.read()

    # Check file size (max 10MB)
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10MB)")

    # Check file type
    content_type = file.content_type
    if content_type not in ["image/jpeg", "image/png", "image/jpg"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Use JPEG or PNG.")

    try:
        # Process image - OCR and create pending record
        result = await process_image(image_bytes, file.filename or "capture.jpg")

        return {
            "status": "success",
            "image_id": result["image_id"],
            "preview": result["preview"],
            "extracted_data": result["extracted_data"]
        }
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")


@app.post("/images/pending/{image_id}/decide")
async def decide_pending_image(image_id: str, request: ImageDecisionRequest):
    """
    Submit decision for a pending image.

    If approve: requires card_id, stores data to database
    If decline: simply removes the pending record
    """
    try:
        result = await process_decision(
            image_id=image_id,
            decision=request.decision,
            card_id=request.card_id
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Decision processing failed for {image_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")


@app.get("/images/pending")
async def get_pending_images():
    """
    Get all pending images waiting for decision.

    Used when user returns to app and wants to see pending images.
    """
    pending = await get_all_pending_images()
    return {"pending": pending}


@app.delete("/images/pending/{image_id}")
async def delete_pending_image(image_id: str):
    """
    Delete a pending image without making a decision.
    """
    from database import delete_pending_image as db_delete_pending_image

    deleted = await db_delete_pending_image(image_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Pending image not found")

    return {
        "status": "success",
        "message": "Pending image deleted"
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
        "last_audio_activity": asyncio.get_event_loop().time(),  # Timestamp of last ASR activity
        "finalization_complete": asyncio.Event()  # Signal when Soniox confirms finalize is done
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

                                # 1. SAVE TO DB (Blocking Write with Verification)
                                if final_text:
                                    state["session_history"] += final_text
                                    
                                    # CONTROL-PLANE: Signal that final transcript was received
                                    # This unblocks the COMMIT handler's wait for finalization
                                    state["finalization_complete"].set()
                                    
                                    # Blocking call - capture the result
                                    write_success = await append_transcript(state["current_card_id"], final_text)
                                    
                                    if write_success:
                                        # logger.info(f"DB Write Success for {state['current_card_id']} - Sending ACK")
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
                            # CONTROL-PLANE SYNCHRONIZATION: Hot Submit
                            logger.info("Hot Submit: Sending finalize command to Soniox")
                            
                            # 1. Reset the flag (ensure traffic light is Red)
                            state["finalization_complete"].clear()
                            
                            # 2. Send the Finalize command to Soniox
                            finalize_command = {"type": "finalize", "trailing_silence_ms": 300}
                            await soniox_socket.send(json.dumps(finalize_command))
                            
                            # 3. Wait for Soniox to confirm finalization (flag turns Green)
                            try:
                                await asyncio.wait_for(
                                    state["finalization_complete"].wait(),
                                    timeout=5.0
                                )
                                logger.info("Hot Submit: Finalization confirmed by Soniox")
                            except asyncio.TimeoutError:
                                # Safety valve: proceed anyway if Soniox doesn't respond
                                logger.warning("Hot Submit: Finalization timeout - proceeding with partial transcript")
                            
                            # 4. Capture and reset session history
                            text_payload = state["session_history"]
                            state["session_history"] = ""
                            
                            # 5. Reset DB transcript field
                            await cards_collection.update_one(
                                {"_id": ObjectId(state["current_card_id"])},
                                {"$set": {"transcript": ""}}
                            )
                            
                            # 6. Update UI to show cleared transcript
                            await app_socket.send_text(json.dumps({
                                "type": "transcript_update",
                                "cardId": state["current_card_id"],
                                "text": ""
                            }))
                            
                            # 7. Execute the Agent pipeline (non-blocking to allow continued recording)
                            logger.info(f"Hot Submit: Running agent pipeline with {len(text_payload)} chars")
                            asyncio.create_task(run_agent_pipeline(state["current_card_id"], text_payload))
                            
                            logger.info("Hot Submit complete. Continuing recording...")
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


@app.post("/cards/{card_id}/actions/admission")
async def trigger_admission_agent(
    card_id: str,
    background_tasks: BackgroundTasks
):
    """
    Trigger the Admission Agent to generate a Hebrew admission note.
    
    This endpoint is called when the user clicks the "Admission" button
    on the back of the card. It starts a background task that:
    
    1. Reads the full patient context (Labs, History, Chat)
    2. Generates a Hebrew admission note
    3. Sends the note via email
    4. Posts an info message to the chat (not the full note)
    
    The generated note is NOT saved to the chat history to keep the
    conversation clean.
    
    Returns immediately with 202 Accepted style response.
    
    Args:
        card_id: The card's MongoDB ObjectId string
        background_tasks: FastAPI background tasks manager
        
    Returns:
        dict with status "accepted" and card_id
        
    Raises:
        HTTPException 404: If card not found
    """
    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    
    # Start the admission pipeline in the background
    background_tasks.add_task(_run_admission_pipeline, card_id)
    
    logger.info(f"Admission Agent triggered for card {card_id}")
    
    return {
        "status": "accepted",
        "message": "Admission note generation started",
        "card_id": card_id
    }


async def _run_ddx_pipeline(card_id: str):
    """
    Background task that runs the DDx agent and adds output to chat.

    The DDx agent:
    - Is an observer of the chat (ANALYTIC framing)
    - Does NOT translate, sanitize, or email output
    - Adds output directly to chat as assistant message
    """
    import agent as agent_module
    from models import MessageRole

    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        logger.error(f"Card {card_id} not found for DDx agent")
        return

    chat_history = card.get("chat", [])

    try:
        # Run the DDx Agent
        # This will:
        # - Use TransientLog to show "Checking Labs..." etc. in real-time
        # - Generate a DDx report using gemini-2.5-flash
        # - Return the DDx report text (NOT saved to chat yet)
        logger.info(f"Running DDx agent for card {card_id}...")

        # Emit info message to chat
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "🧠 Running Differential Diagnosis analysis..."
        )

        ddx_report = await agent_module.run_ddx_agent(card_id)
        logger.info(f"DDx agent completed for card {card_id}, report length: {len(ddx_report)}")

    except Exception as e:
        logger.error(f"DDx agent execution failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Differential Diagnosis generation failed: {str(e)}"
        )
        return

    try:
        # Add the DDx report to chat as assistant message
        # NO translation, NO sanitization, NO email - just save to chat
        logger.info(f"Adding DDx report to chat for card {card_id}...")

        await append_chat_message(
            card_id,
            MessageRole.ASSISTANT,
            ddx_report
        )

        # Send notification to client
        await notification_hub.emit_system_event(
            card_id=card_id,
            category="ddx_generated",
            payload={"message": "DDx Report Generated"}
        )

        # Emit success info message
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "Differential Diagnosis report generated and added to chat."
        )

        logger.info(f"DDx report added to chat for card {card_id}")

    except Exception as e:
        logger.error(f"DDx output pipeline failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Failed to save DDx report to chat: {str(e)}"
        )


@app.post("/cards/{card_id}/actions/ddx")
async def trigger_ddx_agent(
    card_id: str,
    background_tasks: BackgroundTasks
):
    """
    Trigger the DDx Agent to generate a Differential Diagnosis report.

    This endpoint is called when the user clicks the "DDx" button
    on the back of the card. It starts a background task that:

    1. Reads the full patient context (Labs, History, Chat)
    2. Generates a Probabilistic Differential Diagnosis
    3. Adds the report to the chat as an assistant message

    The output is:
    - NOT translated to Hebrew
    - NOT sanitized
    - NOT sent via email
    - Added directly to chat as markdown

    Returns:
        dict with status "accepted" and card_id

    Raises:
        HTTPException 404: If card not found
    """
    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Start the DDx pipeline in the background
    background_tasks.add_task(_run_ddx_pipeline, card_id)

    logger.info(f"DDx Agent triggered for card {card_id}")

    return {
        "status": "accepted",
        "message": "Differential Diagnosis analysis started",
        "card_id": card_id
    }


async def _run_morning_report_pipeline(card_id: str):
    """
    Background task that runs the Morning Report agent and adds output to chat.

    The Morning Report agent:
    - Is an observer of the chat (ANALYTIC framing)
    - Uses simulated date injection (treats last clinical event as "yesterday")
    - Does NOT translate, sanitize, or email output
    - Adds output directly to chat as assistant message
    """
    import agent as agent_module
    from models import MessageRole

    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        logger.error(f"Card {card_id} not found for Morning Report agent")
        return

    chat_history = card.get("chat", [])

    try:
        # Run the Morning Report Agent
        # This will:
        # - Use TransientLog to show progress in real-time
        # - Generate a Morning Report using gemini-2.5-flash
        # - Return the report text (NOT saved to chat yet)
        logger.info(f"Running Morning Report agent for card {card_id}...")

        # Emit info message to chat
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "📋 Generating Morning Report..."
        )

        morning_report = await agent_module.run_morning_report_agent(card_id)
        logger.info(f"Morning Report agent completed for card {card_id}, report length: {len(morning_report)}")

    except Exception as e:
        logger.error(f"Morning Report agent execution failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Morning Report generation failed: {str(e)}"
        )
        return

    try:
        # Add the Morning Report to chat as assistant message
        # NO translation, NO sanitization, NO email - just save to chat
        logger.info(f"Adding Morning Report to chat for card {card_id}...")

        await append_chat_message(
            card_id,
            MessageRole.ASSISTANT,
            morning_report
        )

        logger.info(f"Morning Report successfully added to chat for card {card_id}")

    except Exception as e:
        logger.error(f"Failed to save Morning Report to chat: {str(e)}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Failed to save Morning Report to chat: {str(e)}"
        )


@app.post("/cards/{card_id}/actions/morning-report")
async def trigger_morning_report_agent(
    card_id: str,
    background_tasks: BackgroundTasks
):
    """
    Trigger the Morning Report Agent to generate a sign-out report.

    This endpoint is called when the user clicks the "Morning Report" button
    on the card. It starts a background task that:

    1. Reads the full patient context (Labs, History, Chat)
    2. Uses simulated date injection (treats last data as "yesterday")
    3. Generates a concise morning report for the incoming team
    4. Adds the report to the chat as an assistant message

    The output is:
    - NOT translated to Hebrew
    - NOT sanitized
    - NOT sent via email
    - Added directly to chat as markdown

    Returns:
        dict with status "accepted" and card_id

    Raises:
        HTTPException 404: If card not found
    """
    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Start the Morning Report pipeline in the background
    background_tasks.add_task(_run_morning_report_pipeline, card_id)

    logger.info(f"Morning Report Agent triggered for card {card_id}")

    return {
        "status": "accepted",
        "message": "Morning Report generation started",
        "card_id": card_id
    }


async def _run_rx_pipeline(card_id: str):
    """
    Background task that runs the Rx Agent and adds output to chat.

    The Rx Agent (Master Therapeutist):
    - Is an observer of the chat (ANALYTIC framing)
    - Uses simulated date injection (treats last clinical event as "today")
    - Generates detailed, executable treatment orders with safety audit
    - Does NOT translate, sanitize, or email output
    - Adds output directly to chat as assistant message
    """
    import agent as agent_module
    from models import MessageRole

    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        logger.error(f"Card {card_id} not found for Rx agent")
        return

    chat_history = card.get("chat", [])

    try:
        # Run the Rx Agent
        # This will:
        # - Use TransientLog to show progress in real-time
        # - Generate a detailed treatment plan using gemini-2.5-flash
        # - Return the treatment plan text (NOT saved to chat yet)
        logger.info(f"Running Rx agent for card {card_id}...")

        # Emit info message to chat
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "💊 Generating Treatment Plan..."
        )

        rx_plan = await agent_module.run_rx_agent(card_id)
        logger.info(f"Rx agent completed for card {card_id}, plan length: {len(rx_plan)}")

    except Exception as e:
        logger.error(f"Rx agent execution failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Treatment Plan generation failed: {str(e)}"
        )
        return

    try:
        # Add the Rx plan to chat as assistant message
        # NO translation, NO sanitization, NO email - just save to chat
        logger.info(f"Adding Rx plan to chat for card {card_id}...")

        await append_chat_message(
            card_id,
            MessageRole.ASSISTANT,
            rx_plan
        )

        logger.info(f"Rx plan successfully added to chat for card {card_id}")

    except Exception as e:
        logger.error(f"Failed to save Rx plan to chat: {str(e)}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Failed to save Treatment Plan to chat: {str(e)}"
        )


@app.post("/cards/{card_id}/actions/rx")
async def trigger_rx_agent(
    card_id: str,
    background_tasks: BackgroundTasks
):
    """
    Trigger the Rx Agent to generate a detailed Treatment Plan.

    This endpoint is called when the user clicks the "Rx" button
    on the card. It starts a background task that:

    1. Reads the full patient context (Labs, History, Chat)
    2. Generates a detailed, executable treatment plan with safety audit
    3. Adds the plan to the chat as an assistant message

    The output is:
    - NOT translated to Hebrew
    - NOT sanitized
    - NOT sent via email
    - Added directly to chat as markdown

    Returns:
        dict with status "accepted" and card_id

    Raises:
        HTTPException 404: If card not found
    """
    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Start the Rx pipeline in the background
    background_tasks.add_task(_run_rx_pipeline, card_id)

    logger.info(f"Rx Agent triggered for card {card_id}")

    return {
        "status": "accepted",
        "message": "Treatment Plan generation started",
        "card_id": card_id
    }


async def _run_discharge_pipeline(card_id: str) -> None:
    """
    Background task that generates a discharge summary and sends it via email.

    This function is triggered by the "Discharge" button on the card back.
    It runs the Discharge Agent (Phantom Agent) which:
    1. Reads the full patient context (Labs, History, Chat)
    2. Generates an English discharge summary using gemini-2.5-flash
    3. Translates to Hebrew using gemini-2.5-flash
    4. Sanitizes the Hebrew text
    5. Sends the note via email
    6. Posts an info message to the chat (not the full note)

    The generated summary is NOT saved to the chat history to keep the
    conversation clean.

    Args:
        card_id: The ObjectId string of the card
    """
    logger.info(f"Discharge Agent started for card {card_id}")

    # Fetch the card to get chat history
    try:
        card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    except Exception as e:
        logger.error(f"Card lookup failed for {card_id}: {e}")
        return

    if not card:
        logger.error(f"Card {card_id} not found for discharge agent")
        return

    chat_history = card.get("chat", [])

    try:
        # Run the Discharge Agent
        # This will:
        # - Use TransientLog to show progress in real-time
        # - Generate an English discharge summary using gemini-2.5-flash
        # - Return the English summary text (NOT saved to chat)
        logger.info(f"Running discharge agent for card {card_id}...")
        discharge_summary_en = await agent.run_discharge_agent(card_id)
        logger.info(f"Discharge agent completed for card {card_id}, summary length: {len(discharge_summary_en)}")

    except Exception as e:
        logger.error(f"Discharge agent execution failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Discharge Summary generation failed: {str(e)}"
        )
        return

    try:
        # Process the output: translate to Hebrew, sanitize, and send email
        serial = card.get("serial", "Unknown")
        subject = f"Discharge Summary - Patient #{serial}"

        # Step 1: Translate to Hebrew - emit INFO message
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "🌐 Translating discharge summary to Hebrew..."
        )

        # Use the output pipeline to translate, sanitize, and email
        logger.info(f"Translating and sending email for card {card_id}...")
        final_result = await agent.process_agent_output(
            output_dest=agent.OutputDestination.EMAIL_WITH_TRANSLATION,
            content=discharge_summary_en,
            card_id=card_id,
            subject=subject
        )
        logger.info(f"Email sent successfully for card {card_id}")

    except Exception as e:
        logger.error(f"Output pipeline failed for card {card_id}: {e}", exc_info=True)
        await append_chat_message(
            card_id,
            MessageRole.ERROR,
            f"Translation/Email failed: {str(e)}"
        )
        return

    try:
        # Step 2: Send email - emit INFO message
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "📧 Sending email to clinical team..."
        )

        # Log success
        logger.info(f"Discharge summary processed and sent via email for card {card_id}")

        # Post an info message to the chat (not the full summary)
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            "Discharge Summary generated, translated to Hebrew, and sent to email."
        )

    except Exception as e:
        logger.error(f"Final info message failed for card {card_id}: {e}", exc_info=True)


@app.post("/cards/{card_id}/actions/discharge")
async def trigger_discharge_agent(
    card_id: str,
    background_tasks: BackgroundTasks
):
    """
    Trigger the Discharge Agent to generate a Hospital Discharge Summary.

    This endpoint is called when the user clicks the "Discharge" button
    on the card. It starts a background task that:

    1. Reads the full patient context (Labs, History, Chat)
    2. Generates a gold-standard discharge summary with problem-based hospital course
    3. Translates the summary to Hebrew
    4. Sanitizes the Hebrew text
    5. Sends the summary via email to the clinical team
    6. Posts an info message to the chat (NOT the full summary)

    Returns:
        dict with status "accepted" and card_id

    Raises:
        HTTPException 404: If card not found
    """
    # Validate card exists
    card = await cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Start the Discharge pipeline in the background
    background_tasks.add_task(_run_discharge_pipeline, card_id)

    logger.info(f"Discharge Agent triggered for card {card_id}")

    return {
        "status": "accepted",
        "message": "Discharge Summary generation started",
        "card_id": card_id
    }


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
