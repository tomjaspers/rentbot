"""When scans are due. cron-job.org triggers them (`scripts/sync_schedule.py` pushes the schedule in
config.yaml to it); the dashboard and watchdog read the same schedule to know when a run is due."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")


def next_slot(schedule: list[dict], after: float) -> float:
    """First scheduled minute strictly after `after`. Each entry is a cron-job.org style
    {hours: [...], minutes: [...], wdays: [...]} matched in UK time; wdays (0 = Sunday) is optional."""
    t = datetime.fromtimestamp(after, TZ).replace(second=0, microsecond=0)
    for _ in range(2 * 24 * 60):
        t = (t + timedelta(minutes=1)).astimezone(TZ)  # astimezone keeps the clock right across DST changes
        wday = (t.weekday() + 1) % 7
        if any(t.hour in s["hours"] and t.minute in s["minutes"] and wday in s.get("wdays", range(7))
               for s in schedule):
            return t.timestamp()
    raise ValueError("schedule has no slots")


def last_boundary(hours: list[int], now: float) -> float:
    """Most recent time at or before `now` that the UK clock struck one of `hours`."""
    t = datetime.fromtimestamp(now, TZ).replace(minute=0, second=0, microsecond=0)
    for _ in range(48):
        if t.hour in hours:
            return t.timestamp()
        t = (t - timedelta(hours=1)).astimezone(TZ)
    raise ValueError("no heartbeat hours")
