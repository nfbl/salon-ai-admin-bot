"""Форматирование дат и записей — общее для ботов в Telegram и MAX."""
from datetime import date, datetime

from db import Booking
from salon import Salon

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def fmt_dt(dt: datetime) -> str:
    return f"{fmt_day(dt.date())} в {dt:%H:%M}"


def booking_text(salon: Salon, b: Booking) -> str:
    service = salon.services[b.service_id]
    master = salon.masters[b.master_id]
    return f"{service.title} — {master.name}, {fmt_dt(b.starts_at)}"
