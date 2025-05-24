"""
Run:   python twilio_capture.py
Then:  ngrok http 8765   # copy the https URL, swap scheme to wss://, paste into the TwiML Bin above
Dial:  client.calls.create(url=<TwiML Bin>, ...)
Stop:  Ctrl-C, then play the file
       ffplay -f mulaw -ar 8000 -ac 1 twilio_ulaw_8k.pcm
"""
import asyncio, websockets, json, base64, logging, pathlib, datetime, aiohttp, struct, config as cfg
from aiohttp.payload import AsyncIterablePayload

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
INBOUND_QUEUE = asyncio.Queue(maxsize=40000)          # Twilio → EL
WS_HANDLE: websockets.WebSocketServerProtocol | None = None
STREAM_SID = ""                                    # filled on "start"
SENTINEL   = b""

# 300 ms bucket: 15 frames × 160 B  (Twilio sends 20 ms per frame)
CHUNK_FRAMES = 90

# --- ElevenLabs settings (env-driven; fall back to hard-coded IDs) -----------
VOICE_ID = cfg.ELEVENLABS_VOICE_ID or "XXhALD8N7SAICWXSC1Km"
MODEL_ID = cfg.ELEVENLABS_MODEL_ID  or "eleven_multilingual_sts_v2"
API_KEY  = cfg.ELEVENLABS_API_KEY
OUT_PCM  = pathlib.Path("sound_files/el_output_wav_stream.pcm")

# ------------------------------------------------------------------ µ-law WAV header helper
"""
Just a simple header for the 8khz ulaw audio that we send to ElevenLabs. For some reason ElevenLabs API does not work without a header despite API saying it should.
"""
def mulaw_wav_header_unknown() -> bytes:
    """
    44-byte WAV header for 8-kHz mono μ-law (fmt-tag 7).  Total length is
    left 0xFFFFFFFF so we can stream forever.
    """
    UNKNOWN = 0xFFFFFFFF
    byte_rate   = 8000 * 1 * 1      # sampleRate * blockAlign
    block_align = 1                 # mono * 1 byte/sample
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", UNKNOWN, b"WAVE",
        b"fmt ", 16, 7, 1, 8000, byte_rate, block_align, 8,
        b"data", UNKNOWN
    )

# ------------------------------------------------------------------ WebSocket
"""
This is the main coroutine that handles the WebSocket connection. Responsible for receiving Twilio JSON messages, parsing them, and placing audio frames
into the inbound queue.
"""
async def media_handler(ws):
    global WS_HANDLE, STREAM_SID
    async for raw in ws:
        logging.info(raw)                           # keep full frame log
        msg = json.loads(raw)
        if msg.get("event") == "start":
            STREAM_SID = msg["streamSid"]
            WS_HANDLE = ws
        elif msg.get("event") == "media":
            frame = base64.b64decode(msg["media"]["payload"])

            # 1) feed the live frame to ElevenLabs
            await INBOUND_QUEUE.put(frame)

            # # 2) instantly echo it back so the WebSocket stays alive
            # await _send_frame_to_twilio(frame)
        elif msg.get("event") == "stop":
            await INBOUND_QUEUE.put(SENTINEL)
            break
    WS_HANDLE = None                              # mark closed

# ------------------------------------------------------------------ NEW: full upload coroutine
"""
Coroutine responsible for handling aynscronous HTTP connection to ElevenLabs. Uses a generator created by _body_generator() to asynchronously send the audio,
and a coroutine _recv_and_forward() to asyncronously receive/process the audio.
"""
async def _elevenlabs_upload():
    if not API_KEY:
        logging.error("ELEVENLABS_API_KEY not configured")
        return

    url     = f"https://api.elevenlabs.io/v1/speech-to-speech/{VOICE_ID}/stream"
    params  = {"output_format": "ulaw_8000", "optimize_streaming_latency": 0}
    headers = {"Xi-Api-Key": API_KEY}

    async with aiohttp.ClientSession() as s:
        iteration = 1
        while (blob := await _collect_chunk()):
            form = aiohttp.FormData()
            form.add_field("audio", blob,
                           filename="chunk.wav",
                           content_type="audio/wav")
            form.add_field("model_id", MODEL_ID)

            logging.info("POST %d B chunk → EL  (iteration %d)", len(blob), iteration)
            r = await s.post(url, params=params, data=form, headers=headers)
            if r.status != 200:
                logging.error("EL %s %s", r.status, await r.text())
                continue
            # relay reply now; next chunk waits until this one finishes
            await _recv_and_forward(r, iteration)
            iteration += 1

# ------------------------------------------------------------------ NEW: body-generator sent to ElevenLabs
"""
Asyncronously iterates over the queue and yields ulaw frames as they arrive.
"""
# async def _body_generator():
#     """Async-iterable that yields one WAV header then live 160-B μ-law frames."""
#     yield mulaw_wav_header_unknown()            # header first
#     while True:
#         frame = await INBOUND_QUEUE.get()
#         if frame is SENTINEL:                   # poison pill → EOF
#             break
#         yield frame
#         INBOUND_QUEUE.task_done()

# ------------------------------------------------------------------ EL reply reader
"""
Coroutine to handle asyncronous receiving of transformed audio, from ElevenLabs. Currently saves a local copy and relays it back to Twilio over the websocket.
"""
async def _recv_and_forward(resp: aiohttp.ClientResponse, iteration: int = 0):
    """Mirror EL reply back to Twilio *and* save a local copy."""
    OUT_PCM.parent.mkdir(parents=True, exist_ok=True)
    buf, written = bytearray(), 0
    with OUT_PCM.open("wb") as out:
        async for chunk in resp.content.iter_chunked(160):
            logging.info("Inside _recv_and_forward: Received chunk")
            buf.extend(chunk)
            while len(buf) >= 160:
                frame, buf = bytes(buf[:160]), buf[160:]
                out.write(frame)                 # save to disk
                written += 160
                logging.info("Inside _recv_and_forward: Sending frame to Twilio")
                await _send_frame_to_twilio(frame)
    logging.info("✓ relayed %d B ElevenLabs → Twilio & %s", written, OUT_PCM)
    logging.info("Completed iteration %d", iteration)

# ------------------------------------------------------------------ NEW: save ElevenLabs streaming reply
# async def _recv_and_save(resp: aiohttp.ClientResponse):
#     OUT_PCM.parent.mkdir(parents=True, exist_ok=True)
#     received = 0
#     with OUT_PCM.open("wb") as out:
#         async for chunk in resp.content.iter_chunked(4096):
#             out.write(chunk)
#             received += len(chunk)
#     logging.info("✓ wrote %d B ElevenLabs output → %s", received, OUT_PCM)



# ------------------------------------------------------------------ helper to push one frame
"""
Helper coroutine to send transformed audio back over to Twilio. 
"""

async def _send_frame_to_twilio(frame: bytes):
    global WS_HANDLE                              # we assign to it below
    if WS_HANDLE is None or getattr(WS_HANDLE, "closed", False):
        logging.info("socket not ready / gone inside _send_frame_to_twilio")
        return                                   # socket not ready / gone
    try:
        logging.info("Attempting to send frame to twilio")
        payload = base64.b64encode(frame).decode()
        await WS_HANDLE.send(json.dumps({
            "event": "media",
            "streamSid": STREAM_SID,
            "media": {"payload": payload, "track": "outbound"}
        }))
        logging.info("Frame sent to Twilio")
    except websockets.exceptions.ConnectionClosedOK:
        logging.info("Connection closed inside _send_frame_to_twilio")
        WS_HANDLE = None                         # Twilio hung up



# ------------------------------------------------------------------ Main
async def main():
    async with websockets.serve(media_handler, "", 8765):
        logging.info("🌐 WebSocket listening on ws://localhost:8765")
        await _elevenlabs_upload()               # returns when call ends

# ------------------------------------------------------------------ helper
async def _collect_chunk() -> bytes | None:
    """
    Pull ≤15 frames from the queue, prepend WAV header.
    Return bytes to upload, or None when stream is over.
    """
    buf = bytearray(mulaw_wav_header_unknown())
    for _ in range(CHUNK_FRAMES):
        frame = await INBOUND_QUEUE.get()
        if frame is SENTINEL:
            return None if len(buf) == 44 else bytes(buf)
        buf += frame
    return bytes(buf)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("EL output file size: %d kB", OUT_PCM.stat().st_size // 1024)
