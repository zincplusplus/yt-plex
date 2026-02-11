from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    app_name: str = "YT-Plex"
    data_dir: Path = Path("/app/data")
    downloads_dir: Path = Path("/app/downloads")
    database_url: str = "sqlite+aiosqlite:///app/data/yt-plex.db"

    default_scan_interval_hours: int = 6
    default_download_format: str = "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    default_sponsorblock_categories: str = "sponsor,selfpromo,interaction"
    default_subtitle_langs: str = "en"
    default_delete_after_days: int = 30

    gemini_api_key: str = ""

    download_queue_interval_seconds: int = 60
    cleanup_interval_hours: int = 24

    class Config:
        env_file = ".env"


settings = Settings()
