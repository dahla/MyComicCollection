import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "comicvault.sqlite"
COVER_DIR = Path(os.environ.get("COVER_DIR", str(DATA_DIR / "covers"))).resolve()
COVER_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / "inducks_session.txt"

# Default split threshold for the year-tabs UI. Stored as a row in the
# app_setting table (editable via the Settings page); this is the seed value.
DEFAULT_YEAR_PAGINATION_THRESHOLD = 500

