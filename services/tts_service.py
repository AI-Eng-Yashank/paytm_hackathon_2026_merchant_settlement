"""
Text-to-Speech Service using Sarvam Bulbul.
Returns WAV audio for the agent's reply to the merchant.
"""

import base64
import re

from sarvamai import AsyncSarvamAI

from config import settings


TTS_CHARACTER_LIMIT = 2500  # bulbul:v4-flash limit per request


def clean_text_for_tts(text: str) -> str:
    """Strip markdown and extra whitespace so Bulbul reads plain sentences."""
    text = re.sub(r"[*_#`>]+", "", text or "")
    return re.sub(r"\s+", " ", text).strip()[:TTS_CHARACTER_LIMIT]


class SarvamTTSService:
    """
    Text-to-Speech service using Sarvam Bulbul v4 Flash.
    Falls back to the model's default speaker if the configured persona is rejected
    (Sarvam renames personas occasionally).
    """

    def __init__(self):
        """Initialize Sarvam client with API key from config."""
        self.client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)

    async def generate_audio(self, text: str, language_code: str = "hi-IN") -> bytes:
        """
        Convert text to speech.

        Args:
            text: Text to speak
            language_code: BCP-47 code from STT (e.g. 'hi-IN', 'en-IN')

        Returns:
            WAV audio bytes
        """
        params = dict(
            text=clean_text_for_tts(text),
            language_code=language_code if language_code in ("hi-IN", "en-IN") else "hi-IN",
            model=settings.tts_model,
            enable_preprocessing=True,
        )
        try:
            response = await self.client.text_to_speech.convert(speaker=settings.tts_speaker, **params)
        except Exception as e:
            print(f"[TTS] Speaker '{settings.tts_speaker}' failed ({e}); retrying with default speaker")
            response = await self.client.text_to_speech.convert(**params)

        return base64.b64decode(response.audios[0])
