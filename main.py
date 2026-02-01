from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

@app.websocket("/ws/audio/{client_id}")
async def audio_websocket(websocket: WebSocket, client_id: str):
    print(f"Connection request from Client #{client_id}")
    await websocket.accept()
    print(f"Client #{client_id} Connected.")
    
    try:
        while True:
            # Receive data (Simulating Audio Packets)
            data = await websocket.receive_text()
            
            # Logic: In the future, this is where we send data to Soniox
            print(f"Received packet from {client_id}: {data}")
            
            # Send ACK (Heartbeat)
            # The app needs this to keep the Red Button ON
            await websocket.send_text("ACK")
            
    except WebSocketDisconnect:
        print(f"Client #{client_id} disconnected")
    except Exception as e:
        print(f"Error: {e}")