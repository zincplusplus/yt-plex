import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from .api import channels, downloads, logs, settings
from .core.database import init_db
from .core.scheduler import init_scheduler, shutdown_scheduler
from .core.config import settings as app_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting YT-Plex...")

    # Ensure directories exist
    app_settings.data_dir.mkdir(parents=True, exist_ok=True)
    app_settings.downloads_dir.mkdir(parents=True, exist_ok=True)

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Start scheduler
    init_scheduler()

    yield

    # Shutdown
    shutdown_scheduler()
    logger.info("YT-Plex stopped")


app = FastAPI(
    title="YT-Plex",
    description="YouTube downloader with per-channel settings for Plex",
    version="1.0.0",
    lifespan=lifespan
)

# Include API routers
app.include_router(channels.router)
app.include_router(downloads.router)
app.include_router(logs.router)
app.include_router(settings.router)

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the web UI."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "YT-Plex API is running. Static files not found."}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}
