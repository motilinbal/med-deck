import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from database import append_transcript, get_all_cards, create_empty_card, delete_card_by_id
import os

SONIOX_API_KEY = os.getenv("SONIOX_API_KEY")
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HTTP ENDPOINTS ---
@app.get("/cards")
async def get_cards(): return await get_all_cards()

@app.post("/cards")
async def create_card(): return await create_empty_card()

@app.delete("/cards/{card_id}")
async def delete_card(card_id: str): return {"success": await delete_card_by_id(card_id)}

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
                        "enable_endpoint_detection": True 
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
                                clean_tokens = []
                                for t in tokens:
                                    if t.get("text") == "<fin>":
                                        print(">> Received <fin> token.")
                                        has_fin = True
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

                                # 3. SIGNAL FINISH AFTER PROCESSING
                                if has_fin:
                                    fin_received_event.set()
                                    break 

                        except Exception as e:
                            print(f"Reader Loop Error: {e}")
                            fin_received_event.set()

                    reader_task = asyncio.create_task(read_soniox_text())

                    # C. Stream Audio Loop
                    switch_signal = False
                    
                    while True:
                        item = await state["audio_queue"].get()
                        
                        if isinstance(item, bytes):
                            await soniox_socket.send(item)
                        
                        elif isinstance(item, dict) and item.get("type") == "SWITCH":
                            print("Switch Signal. Finalizing...")
                            switch_signal = True 
                            next_card_id = item.get("id") 
                            break 
                            
                        elif isinstance(item, dict) and item.get("type") == "STOP":
                            print("Stop Signal. Finalizing...")
                            state["should_stop"] = True
                            switch_signal = True
                            break

                    # D. THE FIX: SILENCE INJECTION + FINALIZE
                    
                    # 1. Send ~500ms of Silence (Zeros) to flush the buffer
                    # 16000 samples/sec * 2 bytes/sample * 0.5 sec = 16000 bytes
                    silence_bytes = b'\x00' * 16000
                    await soniox_socket.send(silence_bytes)
                    
                    # 2. Give the engine a split second to digest the silence
                    await asyncio.sleep(0.2)

                    # 3. Send Finalize
                    await soniox_socket.send(json.dumps({"type": "finalize"}))
                    
                    # 4. Wait for <fin>
                    try:
                        await asyncio.wait_for(fin_received_event.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        print("Warning: <fin> wait timed out.")
                    
                    await soniox_socket.close()
                    await reader_task 
                    
                    if switch_signal:
                        if state["should_stop"]:
                            break
                        else:
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