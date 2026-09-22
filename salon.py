"""Данные салона: услуги, мастера, график и база знаний для ИИ."""
import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Service:
    id: str
    title: str
    category: str
    price: int
    duration: int  # минуты
    description: str


@dataclass(frozen=True)
class Master:
    id: str
    name: str
    role: str
    services: tuple[str, ...]


class Salon:
    def __init__(self, raw: dict, faq: str):
        self.name: str = raw["name"]
        self.address: str = raw["address"]
        self.phone: str = raw["phone"]
        self.open_time = time.fromisoformat(raw["hours"]["open"])
        self.close_time = time.fromisoformat(raw["hours"]["close"])
        self.days_off: set[int] = set(raw["hours"].get("days_off", []))
        self.services = {s["id"]: Service(**s) for s in raw["services"]}
        self.masters = {
            m["id"]: Master(m["id"], m["name"], m["role"], tuple(m["services"]))
            for m in raw["masters"]
        }
        self.faq = faq

    @classmethod
    def load(cls) -> "Salon":
        raw = json.loads((DATA_DIR / "salon.json").read_text("utf-8"))
        faq = (DATA_DIR / "faq.md").read_text("utf-8")
        return cls(raw, faq)

    def masters_for(self, service_id: str) -> list[Master]:
        return [m for m in self.masters.values() if service_id in m.services]

    @property
    def hours_text(self) -> str:
        return f"ежедневно с {self.open_time:%H:%M} до {self.close_time:%H:%M}"

    def price_list(self) -> str:
        lines: list[str] = []
        category = None
        for s in self.services.values():
            if s.category != category:
                category = s.category
                lines.append(f"\n<b>{category}</b>")
            lines.append(f"• {s.title} — {s.price} ₽ ({s.duration} мин)")
        return "\n".join(lines).strip()

    def knowledge_base(self) -> str:
        services = "\n".join(
            f"- {s.title}: {s.price} руб., {s.duration} мин. {s.description} "
            f"Мастера: {', '.join(m.name for m in self.masters_for(s.id))}."
            for s in self.services.values()
        )
        masters = "\n".join(f"- {m.name} — {m.role}" for m in self.masters.values())
        return (
            f"Салон: {self.name}\nАдрес: {self.address}\nТелефон: {self.phone}\n"
            f"Часы работы: {self.hours_text}\n\nУслуги и цены:\n{services}\n\n"
            f"Мастера:\n{masters}\n\n{self.faq}"
        )
