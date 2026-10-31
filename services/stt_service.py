"""
Speech-to-Text Service using Sarvam Saaras.
Handles Hinglish / Hindi / English audio with automatic language detection.
"""

from sarvamai import AsyncSarvamAI

from config import settings


class SarvamSTTService:
    """
    Speech-to-Text service using Sarvam Saaras.
    Uses codemix mode so English words stay in English and Hindi in Devanagari.
    """

    def __init__(self):
        """Initialize Sarvam client with API key from config."""
        self.client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)

    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.webm") -> tuple[str, str]:
        """
        Transcribe audio bytes to text.

        Args:
            audio_bytes: Raw audio (WebM/Opus from the browser, or WAV)
            filename: Filename hint so Sarvam can infer the codec

        Returns:
            Tuple of (transcript_text, language_code), e.g. ("mera settlement...", "hi-IN")
        """
        response = await self.client.speech_to_text.transcribe(
            file=(filename, audio_bytes),
            model=settings.stt_model,
            mode=settings.stt_mode,
            language_code="unknown",
        )
        transcript = (response.transcript or "").strip()
        language_code = response.language_code or "hi-IN"
        print(f"[STT] ({language_code}) {transcript}")
        return transcript, language_code
