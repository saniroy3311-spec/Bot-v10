"""
Patches infra/gsheet.py to also accept GSHEET_CREDENTIALS_PATH
(a path to a JSON key file). Keeps GSHEET_CREDENTIALS_JSON
working for backward compat.
"""
import shutil, sys, pathlib

TARGET = pathlib.Path("/root/Bot-v10/infra/gsheet.py")
BACKUP = pathlib.Path("/root/Bot-v10/infra/gsheet.py.prepatch")

OLD_LOAD = '''def _load_creds():
    """Load Google service account credentials from env var."""
    raw = os.environ.get("GSHEET_CREDENTIALS_JSON", "")
    if not raw:
        raise ValueError(
            "GSHEET_CREDENTIALS_JSON env var is not set. "
            "See infra/gsheet.py header for setup instructions."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GSHEET_CREDENTIALS_JSON is not valid JSON: {e}")'''

NEW_LOAD = '''def _load_creds():
    """Load Google service account credentials from env.

    Two ways to provide credentials (path wins if both are set):
      1. GSHEET_CREDENTIALS_PATH = /path/to/key.json    (recommended)
      2. GSHEET_CREDENTIALS_JSON = '<full JSON as one line>'
    """
    path = os.environ.get("GSHEET_CREDENTIALS_PATH", "").strip()
    if path:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise ValueError(f"GSHEET_CREDENTIALS_PATH file not found: {path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"GSHEET_CREDENTIALS_PATH file is not valid JSON: {e}")

    raw = os.environ.get("GSHEET_CREDENTIALS_JSON", "")
    if not raw:
        raise ValueError(
            "Neither GSHEET_CREDENTIALS_PATH nor GSHEET_CREDENTIALS_JSON is set. "
            "See infra/gsheet.py header for setup instructions."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GSHEET_CREDENTIALS_JSON is not valid JSON: {e}")'''

OLD_ENABLED = '''        self._enabled = bool(
            os.environ.get("GSHEET_CREDENTIALS_JSON") and
            os.environ.get("GSHEET_SPREADSHEET_ID")
        )
        if not self._enabled:
            logger.info(
                "GSheet disabled — set GSHEET_CREDENTIALS_JSON + GSHEET_SPREADSHEET_ID to enable."
            )'''

NEW_ENABLED = '''        self._enabled = bool(
            (os.environ.get("GSHEET_CREDENTIALS_PATH") or
             os.environ.get("GSHEET_CREDENTIALS_JSON"))
            and os.environ.get("GSHEET_SPREADSHEET_ID")
        )
        if not self._enabled:
            logger.info(
                "GSheet disabled — set GSHEET_CREDENTIALS_PATH (or _JSON) "
                "+ GSHEET_SPREADSHEET_ID to enable."
            )'''

src = TARGET.read_text()
if OLD_LOAD not in src:
    print("ERROR: _load_creds block not found verbatim. Aborting.")
    sys.exit(1)
if OLD_ENABLED not in src:
    print("ERROR: self._enabled block not found verbatim. Aborting.")
    sys.exit(1)

shutil.copy2(TARGET, BACKUP)
src = src.replace(OLD_LOAD, NEW_LOAD, 1)
src = src.replace(OLD_ENABLED, NEW_ENABLED, 1)
TARGET.write_text(src)
print(f"OK — patched {TARGET}")
print(f"     backup at {BACKUP}")
