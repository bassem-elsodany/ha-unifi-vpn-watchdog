"""When a rotation job runs. Pure functions: the clock comes in as a timestamp, so they are easy to test.

Hours count from when the job started (or last ran). Days and weeks run at a time of day, in the add-on's own time zone."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import JobCfg


def summary(job: JobCfg) -> str:
    unit = job.unit[:-1] if job.every == 1 else job.unit
    return f"Every {job.every} {unit}" + (f" at {job.at}" if job.unit != "hours" else "")


def signature(job: JobCfg) -> str:
    return f"{job.every}/{job.unit}/{job.at}"


def next_run(job: JobCfg, last_ts: float, now: float) -> float:
    """The next time the job is due. `last_ts` is when it last ran (0 = never)."""
    if job.unit == "hours":
        return (last_ts or now) + job.every * 3600
    hh, mm = (int(x) for x in job.at.split(":"))
    step = job.every * (7 if job.unit == "weeks" else 1)
    if last_ts:
        base = datetime.fromtimestamp(last_ts).replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=step)
        return base.timestamp()
    first = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if first.timestamp() <= now:
        first += timedelta(days=1)
    return (first + timedelta(days=step - 1)).timestamp()
