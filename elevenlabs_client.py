import asyncio
import logging
import aiohttp

from config import ELEVENLABS_API_KEY # Or pass as arg

logger = logging.getLogger(__name__)

# Example input_audio_format: "pcm_s16le_16000" (for 16-bit Signed Little Endian PCM at 16kHz)
# Example output_audio_format: "pcm_16000" (ElevenLabs specific format string for output)
async def stream_audio_to_elevenlabs(
    api_key: str,
    voice_id: str,
    model_id: str, # e.g., "eleven_turbo_v2" or "eleven_multilingual_v2"
    audio_chunk_iterator: any, # Async iterator yielding audio byte chunks
    input_audio_format: str, # e.g. "pcm_s16le_16000" - this is for the 'file_format' param
    output_audio_format: str, # e.g. "pcm_16000" - this is for the 'output_format' query param
    call_sid: str # For logging
):
    """
    Streams audio chunks to ElevenLabs Speech-to-Speech endpoint and
    returns an async iterator for the response audio chunks.

    The "file_format=pcm_s16le_16" from user notes likely refers to a parameter
    that describes the raw PCM stream. ElevenLabs API docs should clarify exact names.
    For streaming input, often it's not a named 'file' but a raw stream.
    The /v1/speech-to-speech/{voice_id}/stream endpoint expects multipart/form-data.
    One part is 'audio' (the stream), other parts are metadata.
    """
    # Construct the URL
    # Note: The API endpoint for speech-to-speech streaming might differ.
    # This is based on typical ElevenLabs streaming patterns.
    # The user's note about "name='audio'" and "file_format=pcm_s16le_16"
    # strongly suggests a multipart form upload for the streaming S2S.
    url = f"https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}/stream"
    
    headers = {
        "Accept": "audio/mpeg", # Or "application/json" for status, then audio
        "Xi-Api-Key": api_key
    }
    # For output_format, it's usually a query parameter
    # For input format, it's often part of the multipart form data if not inferred.
    params = {
        "model_id": model_id,
        "output_format": output_audio_format # e.g. pcm_16000, mp3_44100_128
    }

    # The body needs to be a multipart/form-data stream
    # with one part for audio and potentially others for config.
    # aiohttp's FormData can handle async generators for parts.
    
    data = aiohttp.FormData()
    # Part 1: The audio stream
    # The 'filename' for the audio part might not be strictly necessary but often included.
    # The 'Content-Type' for the audio part should be 'application/octet-stream' or specific PCM type.
    data.add_field(
        name='audio', # As per user's note "name='audio'"
        value=audio_chunk_iterator, # This should be an async generator
        filename='audio.pcm', # Arbitrary filename
        content_type='application/octet-stream' # Or specific PCM type if API requires
    )
    
    # Part 2: The input file format (as per user's note "file_format=pcm_s16le_16")
    # This might be what `input_audio_format` is for.
    # The exact parameter name 'file_format' needs to be confirmed from ElevenLabs S2S streaming docs.
    # Let's assume it's a form field.
    data.add_field(name='file_format', value=input_audio_format)


    logger.info(f"[{call_sid}] Connecting to ElevenLabs S2S: {url} with voice {voice_id}, model {model_id}")
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, params=params, data=data) as response:
                logger.info(f"[{call_sid}] ElevenLabs response status: {response.status}")
                if response.status == 200:
                    logger.info(f"[{call_sid}] Successfully connected to ElevenLabs stream.")
                    # Return an async iterator for the response content chunks
                    async def generate_response_chunks():
                        try:
                            async for chunk in response.content.iter_any(): # iter_any or iter_chunked(None)
                                if chunk: # filter out keep-alive new lines
                                    yield chunk
                            logger.info(f"[{call_sid}] ElevenLabs response stream finished.")
                        except Exception as e_iter:
                            logger.error(f"[{call_sid}] Error iterating ElevenLabs response: {e_iter}", exc_info=True)
                            # This exception needs to propagate to _elevenlabs_to_twilio_coro
                            raise 
                    return generate_response_chunks()
                else:
                    error_text = await response.text()
                    logger.error(f"[{call_sid}] ElevenLabs API error: {response.status} - {error_text}")
                    # If it's a 422, the user's note about "missing audio" or "file_format" is key.
                    # This function should return None or raise an exception to signal failure.
                    return None # Or raise an exception
    except aiohttp.ClientError as e:
        logger.error(f"[{call_sid}] aiohttp client error connecting to ElevenLabs: {e}", exc_info=True)
        return None # Or raise an exception
    except Exception as e:
        logger.error(f"[{call_sid}] Generic error in stream_audio_to_elevenlabs: {e}", exc_info=True)
        return None # Or raise an exception 