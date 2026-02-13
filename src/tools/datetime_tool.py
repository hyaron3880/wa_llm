from datetime import datetime
from zoneinfo import ZoneInfo


def get_current_datetime() -> str:
    """Return the current date and time in Israel timezone."""
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    return now.strftime("%Y-%m-%d %H:%M:%S (%A)")
