import asyncio
import aiohttp
import os
import logging
from pydub import AudioSegment
from typing import AsyncGenerator, Optional
import config as app_configs
import audioop
# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global Configuration ---

# Input µ-law file details (simulating Twilio's output)
INPUT_MULAW_FILE_PATH: str = "sound_files/sample_8khz_mono_TRUE_RAW_clipped.ulaw" # Your raw 8kHz µ-law file
INPUT_MULAW_FRAMERATE: int = 8000
INPUT_MULAW_CHANNELS: int = 1
INPUT_MULAW_SAMPLE_WIDTH: int = 1 # 1 byte for µ-law

# Target PCM format for ElevenLabs S2S API
TARGET_PCM_FRAMERATE: int = 16000
TARGET_PCM_CHANNELS: int = 1
TARGET_PCM_SAMPLE_WIDTH: int = 2 # 2 bytes = 16-bit

# Chunking parameters (based on the TARGET_PCM format)
CHUNK_DURATION_MS: int = 20
BYTES_PER_CHUNK: int = int(TARGET_PCM_FRAMERATE * TARGET_PCM_SAMPLE_WIDTH * TARGET_PCM_CHANNELS * (CHUNK_DURATION_MS / 1000.0)) # 640 bytes

# Output file from ElevenLabs
OUTPUT_EL_PCM_FILE: str = "sound_files/el_output_from_converted_mulaw.pcm"
INTERMEDIATE_PCM_FILE_PATH: str = "sound_files/intermediate_converted_s16le.pcm" # For saving converted data

# ElevenLabs API Details
API_KEY: Optional[str] = getattr(app_configs, 'ELEVENLABS_API_KEY', None)
VOICE_ID: str = getattr(app_configs, 'ELEVENLABS_VOICE_ID', "XXhALD8N7SAICWXSC1Km")
MODEL_ID: str = getattr(app_configs, 'ELEVENLABS_MODEL_ID', "eleven_multilingual_sts_v2")
EL_OUTPUT_FORMAT_QUERY_PARAM: str = getattr(app_configs, 'ELEVENLABS_OUTPUT_FORMAT', "pcm_16000")
EL_INPUT_FILE_FORMAT_FORM_PARAM: str = "pcm_s16le_16" # We will send this format
EL_OPTIMIZE_LATENCY_QUERY_PARAM: int = 0

URL = f"https://api.elevenlabs.io/v1/speech-to-speech/{VOICE_ID}/stream"


async def generate_s16le_chunks_from_mulaw() -> AsyncGenerator[bytes, None]:
    """
    Reads a raw µ-law file, converts it to 16kHz 16-bit mono s16le PCM,
    and yields it in chunks. Uses global constants for configuration.
    """

    try:
        with open(INPUT_MULAW_FILE_PATH, "rb") as f_mulaw:
            raw_mulaw_data = f_mulaw.read()
        
        logger.info(f"Read {len(raw_mulaw_data)} bytes from µ-law file: {INPUT_MULAW_FILE_PATH}")

        # --- 2. µ-law ➜ 16-bit linear PCM, still at 8 kHz ---
        pcm16_8k = audioop.ulaw2lin(raw_mulaw_data, 2)   # 2 = 16-bit samples
        logger.info(f"Decoded to {len(pcm16_8k)} bytes of 16-bit PCM @8 kHz")
        # --- 3. Resample 8 kHz ➜ 16 kHz using audioop.ratecv ---
        pcm16_16k, _state = audioop.ratecv(
            pcm16_8k,               # input bytes
            2,                      # sample-width (bytes per sample)
            1,                      # channels
            8000,                   # from rate
            16000,                  # to   rate
            None                    # initial state
        )
        logger.info(f"Upsampled to {len(pcm16_16k)} bytes of 16-bit PCM @16 kHz")
        
        # Save intermediate converted data for inspection
        with open(INTERMEDIATE_PCM_FILE_PATH, "wb") as f_intermediate:
            f_intermediate.write(pcm16_16k)

        # --- 5. Yield 20 ms (640-byte) chunks in “real time” ---
        for i in range(0, len(pcm16_16k), BYTES_PER_CHUNK):
            chunk = pcm16_16k[i : i + BYTES_PER_CHUNK]
            if not chunk:
                break
            yield chunk
            await asyncio.sleep(CHUNK_DURATION_MS / 1000.0)  # simulate live feed
        logger.info("Finished yielding all converted s16le PCM chunks.")
    except Exception as e:
        logger.error(f"Error in generate_s16le_chunks_from_mulaw: {e}", exc_info=True)


async def run_elevenlabs_streaming_test() -> bool:
    """
    Streams audio chunks (from generate_s16le_chunks_from_mulaw) to ElevenLabs S2S.
    Uses global constants for configuration.
    """
    url = URL
    headers = {"Xi-Api-Key": API_KEY}
    
    query_params = {
        "output_format": EL_OUTPUT_FORMAT_QUERY_PARAM,
        "optimize_streaming_latency": EL_OPTIMIZE_LATENCY_QUERY_PARAM
    }

    s16le_chunk_generator = generate_s16le_chunks_from_mulaw()

    form_data = aiohttp.FormData()
    form_data.add_field(
        name='audio',
        value=s16le_chunk_generator,
        filename='audio.pcm',
        content_type='application/octet-stream'
    )
    form_data.add_field(name='file_format', value=EL_INPUT_FILE_FORMAT_FORM_PARAM)
    form_data.add_field(name='model_id', value=MODEL_ID)

    logger.info(f"Streaming to ElevenLabs: URL={url}, Voice={VOICE_ID}, Model={MODEL_ID}")
    logger.info(f"Query Params: {query_params}, Input Format Form Param: {EL_INPUT_FILE_FORMAT_FORM_PARAM}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, params=query_params, data=form_data) as response:
                logger.info(f"ElevenLabs response status: {response.status}")
                if response.status == 200:
                    received_bytes = 0
                    os.makedirs(os.path.dirname(OUTPUT_EL_PCM_FILE), exist_ok=True)
                    with open(OUTPUT_EL_PCM_FILE, "wb") as f_out:
                        async for chunk in response.content.iter_any():
                            if chunk:
                                f_out.write(chunk)
                                received_bytes += len(chunk)
                    logger.info(f"Successfully received {received_bytes} bytes from ElevenLabs.")
                    logger.info(f"Output saved to: {OUTPUT_EL_PCM_FILE}")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"ElevenLabs API error: {response.status} - {error_text}")
                    return False
    except Exception as e:
        logger.error(f"Error during ElevenLabs API call: {e}", exc_info=True)
        return False


async def main():
    logger.info(f"--- Starting Test: µ-law file -> Convert to s16le -> Stream to ElevenLabs ---")
    logger.info(f"Input µ-law file: {INPUT_MULAW_FILE_PATH} ({INPUT_MULAW_FRAMERATE}Hz, {INPUT_MULAW_SAMPLE_WIDTH*8}-bit, {INPUT_MULAW_CHANNELS}-ch)")
    logger.info(f"Targeting {TARGET_PCM_FRAMERATE}Hz, {TARGET_PCM_SAMPLE_WIDTH*8}-bit, {TARGET_PCM_CHANNELS}-ch PCM for ElevenLabs input ({EL_INPUT_FILE_FORMAT_FORM_PARAM}).")
    logger.info(f"Chunk size for streaming: {BYTES_PER_CHUNK} bytes ({CHUNK_DURATION_MS}ms)")

    success = await run_elevenlabs_streaming_test()

    if success:
        logger.info(f"--- Test (µ-law -> s16le -> ElevenLabs) Completed Successfully ---")
        logger.info(f"Play output: ffplay -f s16le -ar {TARGET_PCM_FRAMERATE} -ac {TARGET_PCM_CHANNELS} {OUTPUT_EL_PCM_FILE}")
    else:
        logger.error(f"--- Test (µ-law -> s16le -> ElevenLabs) Failed ---")


if __name__ == "__main__":
    asyncio.run(main()) 