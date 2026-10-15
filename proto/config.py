from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    assemblyai_api_key: str = ""
    assemblyai_model: str = "universal-3-6-pro"

    yt_cookies_file: str = ""
    ytdlp_js_runtime: str = "node"
    ytdlp_format: str = "worstaudio/worst"
    ytdlp_extra_args: str = ""

    # off | always | fallback
    proxy_mode: str = "off"
    webshare_user_base: str = ""
    webshare_pass: str = ""
    webshare_host: str = "p.webshare.io"
    webshare_port: int = 80
    webshare_country: str = "ar"
    webshare_sessions: int = 100

    stall_seconds: float = 30
    slow_ratio: float = 0.9
    slow_window_seconds: float = 120
    data_dir: Path = Path("data")
    save_audio: bool = True


settings = Settings()
