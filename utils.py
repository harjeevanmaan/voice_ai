import logging

def setup_logging(level="INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

# You might add audio conversion functions here if needed, e.g., PCMU to LPCM
# import audioop
# def pcmu_to_lpcm16(pcmu_data):
#     """Converts PCMU (G.711 u-law) audio data to 16-bit LPCM."""
#     return audioop.ulaw2lin(pcmu_data, 2)

# def resample_audio(audio_bytes, in_rate, out_rate, width=2):
#     """Resamples audio using audioop.ratecv. Crude but simple."""
#     # state = None
#     # new_audio, state = audioop.ratecv(audio_bytes, width, 1, in_rate, out_rate, state)
#     # return new_audio
#     # For better quality resampling, consider libraries like resampy.
#     pass 