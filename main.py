import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from database import append_transcript, get_or_create_card
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# 1. API CONFIGURATION
SONIOX_API_KEY = os.getenv("SONIOX_API_KEY", "YOUR_API_KEY_HERE")

# CRITICAL FIX: Use the V3 Real-time Endpoint
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

@app.websocket("/ws/audio/{client_id}")
async def audio_websocket(app_socket: WebSocket, client_id: str):
    print(f"Client #{client_id} Connected.")
    await app_socket.accept()

    # Create a session card in MongoDB
    current_card_id = await get_or_create_card(serial=101, nickname="Live Session")
    print(f"Resuming Session Card: {current_card_id}")

    try:
        # 2. CONNECT TO SONIOX
        async with websockets.connect(SONIOX_URL) as soniox_socket:
            print(f"Connected to Soniox for #{client_id}")

            # 3. SEND CONFIGURATION (V3 Standard)
            config = {
                "api_key": SONIOX_API_KEY,
                "model": "stt-rt-v3",         # Updated to V3 model
                "audio_format": "pcm_s16le",  # Raw PCM 16-bit
                "sample_rate": 16000,         # Hertz
                "num_channels": 1,            # Mono
                "enable_endpoint_detection": True # Helps finalize sentences faster
            }
            await soniox_socket.send(json.dumps(config))

            # --- LOOP 1: Phone Mic -> Soniox ---
            async def receive_audio_from_app():
                try:
                    while True:
                        data = await app_socket.receive_bytes()
                        # print(f"Received {len(data)} bytes from phone")

                        if len(data) > 0:
                            await soniox_socket.send(data)
                except Exception as e:
                    print(f"Upstream (Audio) Error: {e}")

            # --- LOOP 2: Soniox -> Phone Screen & DB ---
            async def receive_text_from_soniox():
                try:
                    async for message in soniox_socket:
                        response = json.loads(message)
                        
                        # 4. PARSE TOKENS (V3 Logic)
                        tokens = response.get("tokens", [])
                        if not tokens:
                            continue

                        # Construct full text for the Phone UI (includes partials)
                        full_sentence = "".join([t["text"] for t in tokens])
                        
                        # Construct only FINAL text for the Database
                        final_text = "".join([t["text"] for t in tokens if t.get("is_final")])

                        # A. Send to Phone (Real-time view)
                        if full_sentence.strip():
                            print(f"Soniox: {full_sentence}")
                            await app_socket.send_text(full_sentence)

                        # B. Save to DB (Only confirmed text)
                        if final_text.strip():
                             await append_transcript(current_card_id, final_text)

                except Exception as e:
                    print(f"Downstream (Text) Error: {e}")

            # Run both loops simultaneously
            await asyncio.gather(receive_audio_from_app(), receive_text_from_soniox())

    except WebSocketDisconnect:
        print(f"Client #{client_id} disconnected")
    except Exception as e:
        print(f"Connection Closed: {e}")