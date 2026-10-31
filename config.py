"""
Configuration management for SettlementResolver.
Loads environment variables and defines application constants.
"""

from dotenv import load_dotenv
from pydantic_settings import BaseSettings


# Load environment variables from .env file
load_dotenv()


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys (empty key => offline scripted mode, no Sarvam calls)
    sarvam_api_key: str = ""

    # Neon Postgres connection string; empty => local SQLite (data/local.db)
    database_url: str = ""
    sqlite_path: str = ""  # override the SQLite file (tests use a temp file)
    log_dir: str = ""      # override the logs/ folder (tests use a temp folder)

    # Call recording: merchant and AI audio saved per turn under recordings/<conversation>/
    record_calls: bool = True
    recordings_dir: str = ""  # override the recordings/ folder (tests use a temp folder)

    # Sarvam model configuration
    llm_model: str = "sarvam-105b"
    reasoning_effort: str = "off"  # off | low | medium | high. Off is 3-6x faster with the same decisions in tests
    stt_model: str = "saaras:v3"
    stt_mode: str = "codemix"
    tts_model: str = "bulbul:v4-flash"
    tts_speaker: str = "simran_enhi_customer"

    # Guardrails (enforced in code, never by the LLM)
    per_txn_credit_cap: int = 10_000
    daily_credit_cap: int = 20_000
    min_account_age_days: int = 30
    max_risk_score: float = 0.7
    merchant_tat_days: int = 5  # RBI TAT: merchant payments auto-reversal by T+5
    tat_compensation_per_day: int = 100  # RBI TAT: Rs 100/day beyond TAT

    # Agent loop
    max_agent_steps: int = 8
    llm_timeout_s: int = 20  # one Sarvam call once hung for 60s (the SDK default); fail fast and retry
    llm_retries: int = 1
    scripted_delay_s: float = 0.35  # offline planner pause per step, so animations are visible

    # Detailed step-by-step timing log (can also be toggled from the dashboard)
    detailed_log: bool = True

    # Server Configuration
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def offline(self) -> bool:
        """True when no Sarvam key is set; the scripted planner is used instead."""
        return not self.sarvam_api_key.strip()


# Global settings instance
settings = Settings()
