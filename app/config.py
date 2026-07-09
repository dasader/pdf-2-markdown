import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PDF2MD_DATA", "data"))
DB_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"
RESULTS_DIR = DATA_DIR / "results"

ADMIN_KEY = os.environ.get("PDF2MD_ADMIN_KEY", "")
PORT = int(os.environ.get("PDF2MD_PORT", "8001"))

MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 500
MAX_QUEUED_PER_SESSION = 20
SEC_PER_PAGE = float(os.environ.get("PDF2MD_SEC_PER_PAGE", "1.5"))
RETENTION_SEC = 24 * 3600


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOADS_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
