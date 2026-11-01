"""Service clock: simulated time of the service, running at 0x (paused), 1x, 10x or 60x real time."""
from __future__ import annotations

from datetime import date, datetime, timedelta

SPEEDS = (0, 1, 10, 60)
PERIOD_HOURS = {"lunch": ("11:30", "14:30"), "dinner": ("17:00", "22:30"), "late": ("22:00", "23:59")}


def period_bounds(service_date: date, period: str) -> tuple[datetime, datetime]:
    a, b = PERIOD_HOURS[period]
    start = datetime.combine(service_date, datetime.strptime(a, "%H:%M").time())
    end = datetime.combine(service_date, datetime.strptime(b, "%H:%M").time())
    return start, end


class ServiceClock:
    def __init__(self, service_date: date, period: str = "dinner", speed: int = 60):
        self.start, self.end = period_bounds(service_date, period)
        self.now: datetime = self.start
        self.speed = speed

    def set_speed(self, speed: int) -> int:
        if int(speed) not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS}")
        self.speed = int(speed)
        return self.speed

    def advance(self, sim_seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=sim_seconds)
        return self.now

    @property
    def t(self) -> float:
        """Sim seconds since service start."""
        return (self.now - self.start).total_seconds()

    def at(self, t: float) -> datetime:
        return self.start + timedelta(seconds=t)

    def iso(self, t: float | None = None) -> str:
        return (self.now if t is None else self.at(t)).isoformat(timespec="seconds")

    def hhmm(self, t: float | None = None) -> str:
        return (self.now if t is None else self.at(t)).strftime("%H:%M")

    @property
    def minutes_left(self) -> float:
        return max(0.0, (self.end - self.now).total_seconds() / 60)

    @property
    def finished(self) -> bool:
        return self.now >= self.end
