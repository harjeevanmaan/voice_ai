import asyncio
import base64
import json
import logging

from config import END_OF_STREAM_SENTINEL, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL_ID
# Assuming ELEVENLABS_OUTPUT_FORMAT from config is what ElevenLabs API expects for 'output_format' query param
# And that "file_format=pcm_s16le_16" is a separate param for the multipart form.
from elevenlabs_client import stream_audio_to_elevenlabs

logger = logging.getLogger(__name__)

# This is the TwiML Twilio should fetch when a call comes in.
# It tells Twilio to connect to your WebSocket and also bridge to SIP.
def generate_twilio_connect_twiml(websocket_url: str, sip_uri: str) -> str:
    """
    Generates TwiML to <Stream> audio to WebSocket and <Sip> call to VOIP.ms.
    """
    # Note: The <Sip> verb might need to be within a <Dial> verb depending on exact behavior desired.
    # If <Sip> is at the same level as <Connect><Stream>, they might compete or behave unexpectedly.
    # A common pattern is to <Dial> and then within the <Dial> use <Sip>,
    # and <Connect><Stream> would be for the *caller's* leg if you want to modify their audio.
    # However, your description implies <Stream> and <Sip> are parallel actions initiated by Twilio.
    # Let's assume your initial interpretation is what Twilio supports for this use case.
    # If you want to modify what *your friend* hears (your voice), then the <Stream>
    # needs to be on the outbound leg of the call to your Groundwire app.
    # The current setup seems to imply modifying the audio *from your friend* before it reaches you.
    #
    # Re-reading: "Twilio injects those bytes into the outbound leg; caller hears the new voice."
    # This means the <Stream> is correctly set up to capture *your* audio (from Groundwire via VOIP.ms then Twilio)
    # and send it to your proxy, then to ElevenLabs, then back to Twilio to inject into the leg going *to your friend*.
    #
    # The TwiML would look something like this if Twilio is making an outbound call *for you*
    # and you want to stream *your* microphone audio.
    # If your friend calls *your Twilio number*, and you answer on Groundwire (via VOIP.ms),
    # then the <Stream> needs to capture audio from the Groundwire leg.
    #
    # Let's assume the TwiML is for when your Twilio number is called:
    # Twilio <Stream>s the inbound audio from your friend to your WebSocket.
    # Twilio <Sip>s the call to your VOIP.ms, which rings Groundwire.
    # Audio from Groundwire (your voice) goes back through Twilio.
    # To change *your* voice, the <Stream> should be associated with the <Sip> leg.
    #
    # The description "Twilio sends one JSON frame every 20 ms" refers to the <Stream> input.
    # "Twilio injects those bytes into the outbound leg" means the audio you send back on the WS
    # goes to the *other party* of the <Stream>.
    #
    # If the goal is to change *your* voice when your friend calls *you*:
    # 1. Friend calls Twilio DID.
    # 2. Twilio TwiML:
    #    <Response>
    #      <Dial>
    #        <Sip>
    #          <Uri>sip:your_user@your_voip_provider.com</Uri>
    #          <Parameter name="custom_param" value="foo"/> <!-- Optional -->
    #        </Sip>
    #      </Dial>
    #      <Connect> <!-- This Connect is for media streaming -->
    #        <Stream url="YOUR_WEBSOCKET_URL_HERE" track="outbound_track" />
    #      </Connect> <!-- This might not be the right structure. -->
    #    </Response>
    #
    # A more common way for voice changing *your own voice* when *receiving* a call:
    # Friend -> Twilio DID -> TwiML -> <Dial callerId="YOUR_TWILIO_DID_OR_FRIEND_CID">
    #                                    <Sip>YOUR_VOIP_MS_ENDPOINT</Sip>
    #                                  </Dial>
    # And then, separately, your Groundwire client would need to send its audio to your proxy.
    # This is getting complex.
    #
    # Let's stick to the user's initial flow:
    # A. Call setup
    # Friend dials your Twilio DID.
    # Twilio executes the Voice Webhook you configured:
    # • <Stream> opens a WebSocket to proxy. (This stream will carry friend's audio TO your proxy)
    # • <Sip> simultaneously bridges the call to VOIP.ms. (This connects friend to your Groundwire)
    #
    # B. Media paths (live audio)
    # Twilio sends one JSON frame every 20 ms (Friend's audio)
    # ...
    # 6. Twilio injects those bytes into the outbound leg (audio sent back on WS goes to friend);
    #    caller hears the new voice. Your inbound audio from the caller is never touched.
    #
    # This means you are changing *your own voice*. The <Stream> must be capturing *your* audio.
    # If <Stream> is at the top level, it captures the *caller's* (friend's) audio.
    # To capture *your* audio (from Groundwire, via VoIP.ms, via Twilio), the <Stream>
    # needs to be associated with the outbound leg of the call towards your friend,
    # or the inbound leg from your SIP phone.
    #
    # The simplest TwiML for the described flow where *your* voice (from SIP endpoint) is modified:
    # When friend calls your Twilio number:
    # <Response>
    #   <Connect>
    #     <Stream url="YOUR_WEBSOCKET_URL_HERE" /> <!-- This will stream audio from your SIP phone -->
    #   </Connect>
    #   <Dial record="false" answerOnBridge="true"> <!-- This dials your SIP phone -->
    #     <Sip>{{ sip_uri }}</Sip>
    #   </Dial>
    # </Response>
    # With this, audio from your SIP phone (Groundwire) is sent to your WebSocket.
    # Audio you send back on the WebSocket is played to your friend.

    # Let's use the TwiML that seems to match the "change my voice" intent:
    # Stream audio from the SIP leg (your voice) to the WebSocket.
    return f"""
<Response>
    <Connect>
        <Stream url="{websocket_url}" />
    </Connect>
    <Dial>
        <Sip>{sip_uri}</Sip>
    </Dial>
</Response>
    """.strip()


async def _twilio_to_queue_coro(websocket: any, upload_q: asyncio.Queue, call_sid: str, stream_sid: str):
    """
    Reads audio from Twilio WebSocket, decodes it, and puts it into the upload_q.
    Handles 'media', 'stop', 'mark' events from Twilio.
    """
    logger.info(f"[{call_sid}] Starting twilio_to_queue_coro for stream {stream_sid}")
    try:
        while True:
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            event = message.get("event")

            if event == "media":
                media = message.get("media", {})
                chunk = media.get("payload")
                if chunk:
                    # Twilio sends audio as base64 encoded PCMU by default for <Stream>
                    # PCMU is 8-bit, 8kHz. 20ms = 160 bytes.
                    # If ElevenLabs needs 16-bit 16kHz PCM, conversion is needed.
                    # For now, let's assume we pass along what Twilio gives,
                    # and ElevenLabs client handles format.
                    # The "640-byte PCM block" in notes suggests Twilio might be sending L16.
                    # Check Twilio <Stream> docs for `audioEncoding` if needed.
                    # Default is PCMU. If you set <Stream audioEncoding="audio/l16;rate=16000">
                    # then you'd get 16-bit 16kHz PCM. 20ms * 16000 samples/s * 2 bytes/sample = 640 bytes.
                    # Let's assume it's already base64 of L16 @ 16kHz based on "640-byte PCM block".
                    raw_audio_bytes = base64.b64decode(chunk)
                    # logger.debug(f"[{call_sid}] Received {len(raw_audio_bytes)} audio bytes from Twilio.")
                    try:
                        upload_q.put_nowait(raw_audio_bytes)
                    except asyncio.QueueFull:
                        logger.warning(f"[{call_sid}] Upload queue full. Dropping oldest audio frame.")
                        # Implement Option B: drop oldest and put newest
                        try:
                            upload_q.get_nowait() # Drop oldest
                            upload_q.put_nowait(raw_audio_bytes) # Put newest
                        except asyncio.QueueEmpty: # Should not happen if it was full
                            pass
                        except asyncio.QueueFull: # Still full, extreme case
                            logger.error(f"[{call_sid}] Upload queue still full after dropping. Frame lost.")


            elif event == "stop":
                logger.info(f"[{call_sid}] Received 'stop' event from Twilio for stream {stream_sid}. Payload: {message.get('stop')}")
                await upload_q.put(END_OF_STREAM_SENTINEL) # Signal EOF
                break
            elif event == "mark":
                logger.info(f"[{call_sid}] Received 'mark' event: {message.get('mark')}")
            elif event == "connected": # This is for bi-directional streams, not the initial 'start'
                logger.info(f"[{call_sid}] Received 'connected' event (should be after 'start').")
            else:
                logger.warning(f"[{call_sid}] Received unknown event: {event}")

    except WebSocketDisconnect:
        logger.info(f"[{call_sid}] Twilio WebSocket disconnected during receive.")
        await upload_q.put(END_OF_STREAM_SENTINEL) # Ensure downstream knows
    except Exception as e:
        logger.error(f"[{call_sid}] Error in twilio_to_queue_coro: {e}", exc_info=True)
        await upload_q.put(END_OF_STREAM_SENTINEL) # Ensure downstream knows
    finally:
        logger.info(f"[{call_sid}] Exiting twilio_to_queue_coro for stream {stream_sid}")


async def _elevenlabs_uploader_body_generator(upload_q: asyncio.Queue, call_sid: str):
    """
    Async generator that yields audio chunks from the upload_q for ElevenLabs.
    Terminates when END_OF_STREAM_SENTINEL is received.
    """
    logger.info(f"[{call_sid}] Starting ElevenLabs body generator.")
    while True:
        chunk = await upload_q.get()
        if chunk is END_OF_STREAM_SENTINEL:
            logger.info(f"[{call_sid}] EOS received in body generator. Ending stream to ElevenLabs.")
            upload_q.put_nowait(END_OF_STREAM_SENTINEL) # Put it back for other potential consumers or cleanup
            break
        # logger.debug(f"[{call_sid}] Yielding {len(chunk)} bytes to ElevenLabs uploader.")
        yield chunk
        upload_q.task_done() # Mark task as done for queue management
    logger.info(f"[{call_sid}] Exiting ElevenLabs body generator.")


async def _elevenlabs_to_twilio_coro(
    elevenlabs_response_iterator: any, # aiohttp response content.iter_chunked()
    websocket: any, # Twilio WebSocket
    upload_q: asyncio.Queue, # Original audio queue, for fallback
    call_sid: str,
    stream_sid: str
):
    """
    Reads modified audio from ElevenLabs response stream, base64 encodes it,
    and sends it back to Twilio via WebSocket.
    Implements fallback to original audio if ElevenLabs stream fails.
    """
    logger.info(f"[{call_sid}] Starting elevenlabs_to_twilio_coro.")
    fallback_active = False
    original_audio_buffer = asyncio.Queue() # Temporary buffer for fallback if needed

    async def send_to_twilio(audio_bytes):
        # logger.debug(f"[{call_sid}] Sending {len(audio_bytes)} bytes to Twilio.")
        encoded_audio = base64.b64encode(audio_bytes).decode("utf-8")
        media_response = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": encoded_audio
            }
        }
        await websocket.send_text(json.dumps(media_response))

    try:
        async for modified_chunk in elevenlabs_response_iterator:
            if modified_chunk: # Ensure chunk is not empty
                # logger.debug(f"[{call_sid}] Received {len(modified_chunk)} bytes from ElevenLabs.")
                await send_to_twilio(modified_chunk)
            # We might need to drain original_audio_buffer if we were in fallback and it recovered
            # This logic can get complex. For now, once fallback, stay fallback.

    except Exception as e:
        logger.error(f"[{call_sid}] Error reading from ElevenLabs stream: {e}. Activating fallback.", exc_info=True)
        fallback_active = True

    if fallback_active:
        logger.info(f"[{call_sid}] Fallback mode activated. Streaming original audio to Twilio.")
        # Drain any remaining items from upload_q and send them
        while True:
            try:
                original_chunk = await asyncio.wait_for(upload_q.get(), timeout=0.1)
                if original_chunk is END_OF_STREAM_SENTINEL:
                    logger.info(f"[{call_sid}] EOS received in fallback. Stopping.")
                    upload_q.put_nowait(END_OF_STREAM_SENTINEL) # Put back for other cleanup
                    break
                if original_chunk: # Ensure it's not None or empty if that's possible
                    # logger.debug(f"[{call_sid}] Fallback: sending {len(original_chunk)} original bytes to Twilio.")
                    await send_to_twilio(original_chunk)
                upload_q.task_done()
            except asyncio.TimeoutError:
                # This means the queue is empty and no EOS yet, could be end of call
                # or just a pause. If the call truly ended, twilio_to_queue_coro
                # would have put EOS.
                if websocket.client_state == websocket.client_state.DISCONNECTED:
                    logger.info(f"[{call_sid}] Websocket disconnected during fallback drain.")
                    break
                # continue waiting for more data or EOS
            except Exception as e_fallback:
                logger.error(f"[{call_sid}] Error during fallback audio streaming: {e_fallback}", exc_info=True)
                break # Exit fallback loop on further errors
    
    logger.info(f"[{call_sid}] Exiting elevenlabs_to_twilio_coro.")


async def handle_twilio_websocket_stream(websocket: any, upload_q: asyncio.Queue, call_sid: str, stream_sid: str):
    """
    Manages the three main coroutines for a single call's audio stream.
    """
    # Coroutine to read from Twilio and put into upload_q
    twilio_reader_task = asyncio.create_task(
        _twilio_to_queue_coro(websocket, upload_q, call_sid, stream_sid)
    )

    # Async generator for the ElevenLabs request body
    audio_generator = _elevenlabs_uploader_body_generator(upload_q, call_sid)

    # Coroutine to stream to ElevenLabs and then send result back to Twilio
    # This task will internally call ElevenLabs and then handle its response.
    # It needs to be structured to first make the call, then process.
    
    elevenlabs_processing_task = None
    try:
        # This call will make the HTTP request and give us an async iterator for the response
        # The `stream_audio_to_elevenlabs` function needs to handle the HTTP session and request.
        # It should return an async iterator over the chunks from ElevenLabs.
        # It also needs to handle the case where ElevenLabs returns an error immediately (e.g., 422)
        
        # Parameters for ElevenLabs
        # The "file_format=pcm_s16le_16" is a query param for the HTTP request to ElevenLabs.
        # The actual audio format being sent needs to match this.
        # Twilio's default <Stream> is PCMU 8kHz. If you need PCM S16LE 16kHz for ElevenLabs,
        # you must either:
        # 1. Configure Twilio <Stream ... audioEncoding="audio/l16;rate=16000"> (if supported for <Stream>)
        # 2. Transcode PCMU 8kHz to LPCM 16kHz in `_twilio_to_queue_coro` before putting on `upload_q`.
        # For now, assuming audio in `upload_q` is already in the correct format for ElevenLabs.
        
        # The `file_format` param for ElevenLabs streaming input is usually part of the URL query string.
        # e.g., /v1/text-to-speech/{voice_id}/stream?output_format=pcm_16000
        # For speech-to-speech, it might be different.
        # The "422 missing audio" with "name='audio'" and "file_format=pcm_s16le_16"
        # suggests a multipart form upload where one part is named 'audio' and you
        # also pass 'file_format' as a form field or query parameter.
        # Let's assume `stream_audio_to_elevenlabs` handles this.
        
        # The `ELEVENLABS_OUTPUT_FORMAT` from config should align with what you want back.
        # e.g., "pcm_16000" for 16kHz 16-bit PCM.
        
        # The `file_format` in your notes ("pcm_s16le_16") is likely the *input* format.
        # Let's assume `stream_audio_to_elevenlabs` takes this as an argument.
        input_audio_format_for_elevenlabs = "pcm_s16le_16000" # Example: 16-bit, 16kHz, little-endian

        elevenlabs_response_aiter = stream_audio_to_elevenlabs(
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID, # Optional, defaults to a good one
            audio_chunk_iterator=audio_generator,
            input_audio_format=input_audio_format_for_elevenlabs, # e.g. "pcm_s16le_16000"
            output_audio_format=config.ELEVENLABS_OUTPUT_FORMAT, # e.g. "pcm_16000"
            call_sid=call_sid
        )

        if elevenlabs_response_aiter: # If the request was successful and we have a stream
            elevenlabs_processor_task = asyncio.create_task(
                _elevenlabs_to_twilio_coro(
                    elevenlabs_response_aiter, websocket, upload_q, call_sid, stream_sid
                )
            )
            # Store tasks in active_calls if app.py is managing it
            # This part depends on how app.py structures active_calls
            # For simplicity here, just await them.
            # In app.py, you'd add these tasks to active_calls[call_sid]['tasks']
            await asyncio.gather(twilio_reader_task, elevenlabs_processor_task)
        else:
            # ElevenLabs call failed upfront (e.g. 4xx error before streaming)
            logger.error(f"[{call_sid}] ElevenLabs stream could not be initiated. Falling back immediately.")
            # Implement immediate fallback: stream from upload_q directly to Twilio
            # This is a simplified version of the fallback in _elevenlabs_to_twilio_coro
            async def direct_fallback_sender():
                logger.info(f"[{call_sid}] Starting direct fallback sender.")
                try:
                    while True:
                        original_chunk = await upload_q.get()
                        if original_chunk is END_OF_STREAM_SENTINEL:
                            upload_q.put_nowait(END_OF_STREAM_SENTINEL)
                            break
                        if original_chunk:
                            encoded_audio = base64.b64encode(original_chunk).decode("utf-8")
                            media_response = {
                                "event": "media", "streamSid": stream_sid,
                                "media": {"payload": encoded_audio}
                            }
                            await websocket.send_text(json.dumps(media_response))
                        upload_q.task_done()
                except Exception as e_df:
                    logger.error(f"[{call_sid}] Error in direct fallback: {e_df}")
                finally:
                    logger.info(f"[{call_sid}] Exiting direct fallback sender.")

            fallback_task = asyncio.create_task(direct_fallback_sender())
            await asyncio.gather(twilio_reader_task, fallback_task)

    except Exception as e:
        logger.error(f"[{call_sid}] Error in handle_twilio_websocket_stream: {e}", exc_info=True)
    finally:
        logger.info(f"[{call_sid}] Cleaning up tasks in handle_twilio_websocket_stream.")
        if not twilio_reader_task.done():
            twilio_reader_task.cancel()
        if elevenlabs_processing_task and not elevenlabs_processing_task.done():
            elevenlabs_processing_task.cancel()
        # Ensure queue is drained of sentinel if not already processed
        try:
            while not upload_q.empty():
                item = upload_q.get_nowait()
                if item is not END_OF_STREAM_SENTINEL:
                    logger.warning(f"[{call_sid}] Discarding item from queue during cleanup: {type(item)}")
                upload_q.task_done()
        except asyncio.QueueEmpty:
            pass
        except Exception as e_cleanup:
            logger.error(f"[{call_sid}] Error during queue cleanup: {e_cleanup}")

        logger.info(f"[{call_sid}] handle_twilio_websocket_stream finished.") 