"""
Run:   python twilio_capture.py
Then:  ngrok http 8765   # copy the https URL, swap scheme to wss://, paste into the TwiML Bin above
Dial:  client.calls.create(url=<TwiML Bin>, ...)
Stop:  Ctrl-C, then play the file
       ffplay -f mulaw -ar 8000 -ac 1 twilio_ulaw_8k.pcm
"""
import asyncio, websockets, json, base64, logging, pathlib, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
INBOUND_QUEUE = asyncio.Queue(maxsize=200)          # back-pressure guard
OUT_PATH       = pathlib.Path("twilio_ulaw_8k.pcm")
SENTINEL = b""

# ------------------------------------------------------------------ WebSocket
async def media_handler(ws):
    async for msg in ws:                            # messages are JSON strings
        logging.info(msg)
        m = json.loads(msg)
        if m.get("event") == "media":               # Twilio 'media' frame
            frame = base64.b64decode(m["media"]["payload"])
            await INBOUND_QUEUE.put(frame)          # 160-byte chunk
        elif m.get("event") == "stop":
            await INBOUND_QUEUE.put(SENTINEL)      # poison pill
            break
    logging.info("Twilio stream ended")

# ------------------------------------------------------------------ Consumer
async def file_writer():
    with OUT_PATH.open("wb") as f:
        while True:
            frame = await INBOUND_QUEUE.get()
            if frame is SENTINEL:          # end-of-stream
                break
            f.write(frame)

# ------------------------------------------------------------------ Main
async def main():
    async with websockets.serve(media_handler, "", 8765):
        logging.info("🌐 WebSocket listening on ws://localhost:8765")
        await file_writer()                         # runs until Ctrl-C

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Wrote  %s  (%d kB)",
                     OUT_PATH, OUT_PATH.stat().st_size // 1024)
