import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "comicvault.sqlite"
COVER_DIR = Path(os.environ.get("COVER_DIR", str(DATA_DIR / "covers"))).resolve()
COVER_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / "inducks_session.txt"

