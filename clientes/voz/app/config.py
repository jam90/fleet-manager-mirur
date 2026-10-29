from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic import field_validator
from typing import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    whisper_url: str = "http://localhost:8080/inference"
    whisper_health_url: str = "http://localhost:8080/health"
    whisper_timeout: float = 60.0
    whisper_language: str = "es"
    whisper_prompt: str = "tonto"
    missions_file: str = "missions.json"
    fm_url: str = "http://localhost:8050"  # real fleet
    fm_sim_url: str = "http://localhost:8051"  # simulated fleet
    fm_wait: float = 5.0

    # Hands-free voice: wake word, confirmation word and time windows
    wake_word: str = "tonto"
    wake_variants: Annotated[list[str], NoDecode] = ["tonta", "tanto", "tono"]
    confirm_word: str = "adelante"
    arm_window_s: float = 8.0
    confirm_window_s: float = 10.0

    # Voice log: every phrase, order and cancel in <dir>/voice.jsonl (empty dir = off), audio in <dir>/audio
    voice_log_dir: str = "logs"
    voice_log_audio: bool = True

    @field_validator("wake_variants", mode="before")
    @classmethod
    def split_csv(cls, value):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    def fleet_url(self, fleet: str) -> str:
        return self.fm_url if fleet == "real" else self.fm_sim_url


settings = Settings()
