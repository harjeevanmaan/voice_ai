import os
from dotenv import load_dotenv

load_dotenv()

# Twilio Credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
YOUR_TWILIO_PHONE_NUMBER = os.getenv("YOUR_TWILIO_PHONE_NUMBER") # Your Twilio DID
VOIP_MS_SIP_URI = os.getenv("VOIP_MS_SIP_URI") # e.g., username@sip.voip.ms

# ElevenLabs Credentials & Settings
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "XXhALD8N7SAICWXSC1Km") # Specify your target voice
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_sts_v2")
ELEVENLABS_OUTPUT_FORMAT = "pcm_16000" # Corresponds to pcm_s16le_16000 for 16kHz
# Note: ElevenLabs API might expect slightly different format strings than Twilio.
# For raw PCM, it's often like output_format=pcm_16000 (for 16kHz) or pcm_24000 etc.
# The "file_format=pcm_s16le_16" mentioned in your notes is likely a query param for the HTTP request.

# Application Settings
WEBSOCKET_SERVER_HOST = "0.0.0.0"
WEBSOCKET_SERVER_PORT = int(os.getenv("WEBSOCKET_PORT", 8080))
UPLOAD_QUEUE_MAX_SIZE = int(os.getenv("UPLOAD_QUEUE_MAX_SIZE", 50)) # Approx 1 second of audio (50 * 20ms)
PCM_FRAME_DURATION_MS = 20
TWILIO_EXPECTED_SAMPLE_RATE = 8000 # Twilio <Stream> typically sends 8kHz PCMU
# If ElevenLabs requires 16kHz, resampling will be needed.
# Twilio can send PCMU (G.711 μ-law) - 8000 Hz, 1 byte per sample. 20ms = 160 bytes.
# Or LPCM (Linear PCM) if you configure it.
# The 640-byte PCM block implies 16-bit stereo 8kHz, or 16-bit mono 16kHz for 20ms.
# Clarify Twilio's output format vs ElevenLabs input. If Twilio sends 8kHz PCMU,
# it needs to be decoded to LPCM and potentially upsampled for ElevenLabs if it requires 16kHz.

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Sentinel for queues
END_OF_STREAM_SENTINEL = object() 