"""Расчёт свободных и занятых окон мастера."""
from datetime import date, datetime, timedelta

from salon import Salon

STEP_MINUTES = 30
LEAD_MINUTES = 60  # нельзя записаться менее чем за час


def day_grid(
    salon: Salon,
    busy: list[tuple[datetime, datetime]],
    day: date,
    duration: int,
    now: datetime,
) -> list[tuple[datetime, bool]]:
    """Будущие окна дня для услуги: (время, свободно ли).

    Занятым (False) считается только окно, на которое у мастера уже есть запись.
    Окна, где услуга не успевает закончиться до следующей записи, не показываются.
    """
    if day.weekday() in salon.days_off:
        return []
    tz = now.tzinfo
    t = datetime.combine(day, salon.open_time, tzinfo=tz)
    close = datetime.combine(day, salon.close_time, tzinfo=tz)
    earliest = now + timedelta(minutes=LEAD_MINUTES)
    length = timedelta(minutes=duration)

    grid = []
    while t + length <= close:
        if t >= earliest:
            end = t + length
            if any(b_start <= t < b_end for b_start, b_end in busy):
                grid.append((t, False))
            elif all(end <= b_start or t >= b_end for b_start, b_end in busy):
                grid.append((t, True))
        t += timedelta(minutes=STEP_MINUTES)
    return grid


def free_slots(
    salon: Salon,
    busy: list[tuple[datetime, datetime]],
    day: date,
    duration: int,
    now: datetime,
) -> list[datetime]:
    return [t for t, free in day_grid(salon, busy, day, duration, now) if free]
