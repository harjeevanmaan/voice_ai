import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse # For TwiML

from config import (
    WEBSOCKET_SERVER_HOST,
    WEBSOCKET_SERVER_PORT,
    YOUR_TWILIO_PHONE_NUMBER,
    VOIP_MS_SIP_URI,
    END_OF_STREAM_SENTINEL,
    UPLOAD_QUEUE_MAX_SIZE
)
from twilio_handler import generate_twilio_connect_twiml, handle_twilio_websocket_stream
# audio_processor.py might be implicitly used by handle_twilio_websocket_stream

# Basic logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Store active calls and their resources (e.g., queues)
active_calls = {}

@app.post("/twiml/voice")
async def provide_twiml_for_call(request: Request):
    """
    Twilio will hit this endpoint when your Twilio number receives a call.
    It expects TwiML in response.
    """
    # The base URL for your WebSocket endpoint
    # If using a tunneling service like ngrok, ensure this is the public URL
    # For production, this would be your server's public address/domain.
    # Example: wss://your-app-domain.com/ws/voice
    # During local dev with ngrok: wss://<ngrok_id>.ngrok.io/ws/voice
    # You might need to get this from request headers if behind a proxy or use a configured base URL
    host = request.headers.get("host", f"localhost:{WEBSOCKET_SERVER_PORT}")
    # Determine scheme based on environment or headers (X-Forwarded-Proto)
    scheme = "wss" if "ngrok" in host or os.getenv("PRODUCTION") else "ws" # Simplified logic
    websocket_url = f"{scheme}://{host}/ws/voice"

    logger.info(f"Generating TwiML for call, WebSocket URL: {websocket_url}")
    twiml_response = generate_twilio_connect_twiml(
        websocket_url=websocket_url,
        sip_uri=VOIP_MS_SIP_URI
    )
    return PlainTextResponse(twiml_response, media_type="application/xml")

@app.websocket("/ws/voice")
async def websocket_voice_endpoint(websocket: WebSocket):
    await websocket.accept()
    call_sid = None # Will be extracted from the 'start' message
    upload_q = asyncio.Queue(maxsize=UPLOAD_QUEUE_MAX_SIZE)
    
    try:
        # The first message from Twilio will be a 'start' event
        start_event = await websocket.receive_json()
        if start_event.get("event") == "start":
            call_sid = start_event.get("start", {}).get("callSid")
            stream_sid = start_event.get("start", {}).get("streamSid")
            logger.info(f"WebSocket connected for CallSid: {call_sid}, StreamSid: {stream_sid}")
            active_calls[call_sid] = {"queue": upload_q, "websocket": websocket, "tasks": []}

            # Start the processing pipeline
            # handle_twilio_websocket_stream will internally create and manage tasks
            await handle_twilio_websocket_stream(websocket, upload_q, call_sid, stream_sid)
        else:
            logger.error(f"Expected 'start' event, got: {start_event}")
            await websocket.close(code=1008) # Policy Violation
            return

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for CallSid: {call_sid}")
    except Exception as e:
        logger.error(f"Error in WebSocket connection for CallSid {call_sid}: {e}", exc_info=True)
        if not websocket.client_state == websocket.client_state.DISCONNECTED: # Check if already closed
            await websocket.close(code=1011) # Internal Error
    finally:
        logger.info(f"Cleaning up resources for CallSid: {call_sid}")
        if call_sid and call_sid in active_calls:
            # Signal EOF to tasks consuming from the queue
            if 'queue' in active_calls[call_sid]:
                try:
                    active_calls[call_sid]['queue'].put_nowait(END_OF_STREAM_SENTINEL)
                except asyncio.QueueFull:
                    logger.warning(f"Queue full when trying to send EOS for {call_sid}")
            # Cancel any running tasks associated with this call
            for task in active_calls[call_sid].get("tasks", []):
                if not task.done():
                    task.cancel()
            del active_calls[call_sid]
        logger.info(f"Call {call_sid} ended and cleaned up.")

if __name__ == "__main__":
    import uvicorn
    # Make sure to configure your Twilio phone number's voice webhook
    # to point to YOUR_SERVER_URL/twiml/voice (HTTP POST)
    logger.info(f"Starting server on {WEBSOCKET_SERVER_HOST}:{WEBSOCKET_SERVER_PORT}")
    logger.info(f"Configure Twilio Voice Webhook to: http://<your_public_url>/twiml/voice")
    logger.info(f"WebSocket endpoint will be available at: ws://<your_public_url>/ws/voice (or wss://)")
    uvicorn.run(app, host=WEBSOCKET_SERVER_HOST, port=WEBSOCKET_SERVER_PORT) 