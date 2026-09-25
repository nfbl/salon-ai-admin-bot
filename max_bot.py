"""ИИ-администратор салона в мессенджере MAX.

Умеет то же, что Telegram-бот (bot.py): отвечает на вопросы по базе знаний, записывает
к мастеру, напоминает о визите и передаёт сложные вопросы живому администратору.
Записи из MAX и Telegram лежат в одной базе, поэтому расписание у мастеров общее:
время, занятое через Telegram, в MAX тоже показано занятым."""
import asyncio
import html
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import aiohttp

import fmt
from config import load_settings
from db import Booking, Bookings
from fmt import WEEKDAYS, fmt_day, fmt_dt
from llm import Assistant
from max_api import MaxAPI, MaxError, button, contact_button
from salon import Salon
from slots import day_grid, free_slots

log = logging.getLogger(__name__)

settings = load_settings()
salon = Salon.load()
db = Bookings(settings.db_path)
assistant = Assistant(settings, salon)
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))

CHANNEL = "max"
BOOKING_DAYS = 7
REMIND_BEFORE = timedelta(hours=3)

BTN_PRICES = "💅 Услуги и цены"
BTN_BOOK = "📅 Записаться"
BTN_MASTERS = "👩‍🎨 Мастера"
BTN_MY = "📋 Мои записи"
BTN_CONTACTS = "📍 Контакты"
BTN_ASK = "💬 Спросить администратора"
BTN_MENU = "☰ Меню"
BTN_CANCEL = "✖️ Отмена"

# Пункты меню можно не только нажать, но и написать словами: «записаться», «мастера»
MENU = {BTN_PRICES: "prices", BTN_BOOK: "book", BTN_MASTERS: "masters",
        BTN_MY: "my", BTN_CONTACTS: "contacts", BTN_ASK: "ask_admin"}


@dataclass
class Form:
    """На каком шаге сценария клиент и что уже выбрал (то же, что FSM в aiogram)."""
    step: str | None = None
    data: dict = field(default_factory=dict)


forms: dict[int, Form] = {}
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
tasks: set[asyncio.Task] = set()


@dataclass
class Ctx:
    """Собеседник и способ ответить ему."""
    api: MaxAPI
    user_id: int
    name: str
    username: str | None = None
    chat_id: int | None = None
    callback_id: str | None = None

    async def show(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        """Показать экран: после нажатия кнопки заменить сообщение с кнопками, иначе отправить новое."""
        if self.callback_id:
            callback_id, self.callback_id = self.callback_id, None
            try:
                await self.api.answer(callback_id, text=text, buttons=buttons)
                return
            except MaxError:  # сообщение с кнопкой уже нельзя изменить — пишем новое
                log.warning("Не удалось обновить сообщение по нажатию кнопки", exc_info=True)
        await self.api.send(self.user_id, text, buttons)

    async def send(self, text: str, buttons: list[list[dict]] | None = None, plain: bool = False) -> None:
        await self.api.send(self.user_id, text, buttons, html=not plain)

    async def toast(self, text: str) -> None:
        if self.callback_id:
            callback_id, self.callback_id = self.callback_id, None
            await self.api.answer(callback_id, notification=text)


def now() -> datetime:
    return datetime.now(settings.tz)


def booking_text(b: Booking) -> str:
    return fmt.booking_text(salon, b)


def rows(buttons: list[dict], per_row: int) -> list[list[dict]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def menu_row() -> list[dict]:
    return [button(BTN_MENU, "menu")]


def main_menu() -> list[list[dict]]:
    return [
        [button(BTN_PRICES, "prices"), button(BTN_BOOK, "book")],
        [button(BTN_MASTERS, "masters"), button(BTN_MY, "my")],
        [button(BTN_CONTACTS, "contacts"), button(BTN_ASK, "ask_admin")],
    ]


def book_or_ask() -> list[list[dict]]:
    kb = [[button(BTN_BOOK, "book")]]
    if settings.max_admin_id:
        kb.append([button(BTN_ASK, "ask_admin")])
    return kb + [menu_row()]


def menu_action(text: str) -> str | None:
    t = text.strip().lower()
    for label, action in MENU.items():
        if t in (label.lower(), label.split(" ", 1)[1].lower()):
            return action
    return None


def phone_from_contact(attachment: dict) -> str:
    """Номер из карточки контакта: MAX присылает её в формате vCard."""
    vcf = (attachment.get("payload") or {}).get("vcf_info") or ""
    m = re.search(r"^TEL[^:\n]*:(.+)$", vcf, re.M)
    return m.group(1).strip() if m else ""


def slots_for(master_id: str, service_id: str, day: date) -> list[datetime]:
    duration = salon.services[service_id].duration
    return free_slots(salon, db.busy(master_id, day), day, duration, now())


def grid_for(master_id: str, service_id: str, day: date) -> list[tuple[datetime, bool]]:
    duration = salon.services[service_id].duration
    return day_grid(salon, db.busy(master_id, day), day, duration, now())


async def notify_admin(api: MaxAPI, text: str) -> None:
    """Уведомление администратору в MAX, а если подключён Telegram-бот — ещё и в Telegram."""
    if settings.max_admin_id:
        try:
            await api.send(settings.max_admin_id, text)
        except Exception:
            log.exception("Не удалось уведомить администратора в MAX")
    if settings.bot_token and settings.admin_chat_id:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
                    json={"chat_id": settings.admin_chat_id, "text": text, "parse_mode": "HTML"},
                ) as resp:
                    resp.raise_for_status()
        except Exception:
            log.exception("Не удалось уведомить администратора в Telegram")


# ---------- Меню ----------

async def start(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    await ctx.send(
        f"Здравствуйте, {html.escape(ctx.name)}! 👋\n\n"
        f"Я виртуальный администратор салона «{salon.name}».\n"
        "Отвечу на вопросы об услугах, ценах и акциях, запишу к мастеру и напомню о визите.\n\n"
        "Выберите пункт меню или просто напишите вопрос 🙂",
        main_menu(),
    )


async def show_menu(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    await ctx.show("Чем могу помочь? Выберите пункт меню или напишите вопрос.", main_menu())


async def show_prices(ctx: Ctx) -> None:
    await ctx.show(salon.price_list(), [[button(BTN_BOOK, "book")], menu_row()])


async def show_contacts(ctx: Ctx) -> None:
    await ctx.show(
        f"<b>{salon.name}</b>\n📍 {salon.address}\n📞 {salon.phone}\n🕙 {salon.hours_text}",
        book_or_ask(),
    )


async def my_bookings(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    items = db.upcoming(now(), user_id=ctx.user_id, channel=CHANNEL)
    if not items:
        await ctx.show("У вас пока нет предстоящих записей.", [[button(BTN_BOOK, "book")], menu_row()])
        return
    lines = ["<b>Ваши записи:</b>", *(f"• {booking_text(b)}" for b in items)]
    kb = [[button(f"Отменить {b.starts_at:%d.%m %H:%M}", f"ucancel:{b.id}")] for b in items]
    await ctx.show("\n".join(lines), kb + [menu_row()])


async def user_cancel(ctx: Ctx, booking_id: int) -> None:
    booking = db.cancel(booking_id, ctx.user_id, channel=CHANNEL)
    if not booking:
        await ctx.toast("Запись уже отменена")
        return
    await ctx.show(f"Запись отменена: {booking_text(booking)}", [menu_row()])
    await notify_admin(ctx.api, f"❌ Клиент отменил запись (MAX)\n{booking_text(booking)}\n"
                                f"{html.escape(booking.client_name)}, {html.escape(booking.phone)}")


# ---------- Запись ----------

def services_kb(master_id: str | None = None) -> list[list[dict]]:
    kb = [[button(f"{s.title} · {s.price} ₽", f"svc:{s.id}")]
          for s in salon.services.values()
          if master_id is None or s.id in salon.masters[master_id].services]
    if master_id is None:
        kb.append([button("👩‍🎨 Сначала выбрать мастера", "masters")])
    return kb + [[button(BTN_CANCEL, "abort")]]


async def book_start(ctx: Ctx) -> None:
    forms[ctx.user_id] = Form("service")
    await ctx.show("Выберите услугу:", services_kb())


async def show_masters(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    lines = ["<b>Наши мастера</b>"]
    for m in salon.masters.values():
        services = ", ".join(salon.services[s].title for s in m.services)
        lines.append(f"\n<b>{m.name}</b> — {m.role}\n{services}")
    kb = rows([button(f"Записаться: {m.name}", f"bymst:{m.id}") for m in salon.masters.values()], 2)
    await ctx.show("\n".join(lines), kb + [[button(BTN_CANCEL, "abort")]])


async def book_with_master(ctx: Ctx, master_id: str) -> None:
    master = salon.masters.get(master_id)
    if not master:
        await stale(ctx)
        return
    forms[ctx.user_id] = Form("service", {"master_id": master.id})
    await ctx.show(f"Мастер: <b>{master.name}</b>\nВыберите услугу:", services_kb(master.id))


async def pick_service(ctx: Ctx, service_id: str) -> None:
    form = forms.get(ctx.user_id)
    if not form or form.step != "service" or service_id not in salon.services:
        await stale(ctx)
        return
    form.data["service_id"] = service_id
    masters = salon.masters_for(service_id)
    if form.data.get("master_id"):  # мастер выбран заранее
        await ask_day(ctx)
    elif len(masters) == 1:
        form.data["master_id"] = masters[0].id
        await ask_day(ctx)
    else:
        form.step = "master"
        kb = [[button(f"{m.name} — {m.role}", f"mst:{m.id}")] for m in masters]
        await ctx.show("Выберите мастера:", kb + [[button(BTN_CANCEL, "abort")]])


async def pick_master(ctx: Ctx, master_id: str) -> None:
    form = forms.get(ctx.user_id)
    if not form or form.step != "master" or master_id not in salon.masters:
        await stale(ctx)
        return
    form.data["master_id"] = master_id
    await ask_day(ctx)


async def ask_day(ctx: Ctx, prefix: str = "") -> None:
    form = forms[ctx.user_id]
    service = salon.services[form.data["service_id"]]
    master = salon.masters[form.data["master_id"]]
    today = now().date()
    days, has_free, has_full = [], False, False
    for d in (today + timedelta(days=i) for i in range(BOOKING_DAYS)):
        grid = grid_for(master.id, service.id, d)
        label = f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"
        if any(free for _, free in grid):
            has_free = True
            days.append(button(label, f"day:{d.isoformat()}"))
        elif grid:  # все окна дня заняты реальными записями
            has_full = True
            days.append(button(f"✕ {label}", "full"))
    if not has_free:
        forms.pop(ctx.user_id, None)
        await ctx.show(
            f"{prefix}К сожалению, у мастера {master.name} нет свободных окон на ближайшую неделю. "
            f"Позвоните нам: {salon.phone} — постараемся найти время.",
            [menu_row()],
        )
        return
    form.step = "day"
    legend = "\n✕ — всё занято" if has_full else ""
    await ctx.show(
        f"{prefix}<b>{service.title}</b> · {service.price} ₽ · мастер {master.name}\n\n"
        f"Выберите день:{legend}",
        rows(days, 3) + [[button(BTN_CANCEL, "abort")]],
    )


async def pick_day(ctx: Ctx, iso: str) -> None:
    form = forms.get(ctx.user_id)
    if not form or form.step != "day":
        await stale(ctx)
        return
    day = date.fromisoformat(iso)
    grid = grid_for(form.data["master_id"], form.data["service_id"], day)
    if not any(free for _, free in grid):
        await ctx.toast("На этот день окна уже заняли, выберите другой")
        await ask_day(ctx)
        return
    times = [button(f"{t:%H:%M}", f"time:{t.isoformat()}") if free else button(f"✕ {t:%H:%M}", "busy")
             for t, free in grid]
    legend = "\n✕ — время занято" if not all(free for _, free in grid) else ""
    form.step = "time"
    await ctx.show(f"{fmt_day(day)}. Выберите время:{legend}", rows(times, 4) + [[button(BTN_CANCEL, "abort")]])


async def pick_time(ctx: Ctx, iso: str) -> None:
    form = forms.get(ctx.user_id)
    if not form or form.step != "time":
        await stale(ctx)
        return
    starts_at = datetime.fromisoformat(iso)
    form.data["starts_at"] = starts_at.isoformat()
    form.step = "name"
    await ctx.show(f"Отлично, {fmt_dt(starts_at)} 👍")
    await ctx.send("Как к вам обращаться?")


async def enter_name(ctx: Ctx, text: str) -> None:
    name = text.strip()[:50]
    if len(name) < 2:
        await ctx.send("Напишите, пожалуйста, ваше имя.")
        return
    form = forms[ctx.user_id]
    form.data["client_name"] = name
    form.step = "phone"
    await ctx.send(
        "Оставьте номер телефона — нажмите кнопку ниже или напишите вручную.",
        [[contact_button("📱 Отправить мой номер")], [button(BTN_CANCEL, "abort")]],
    )


async def enter_phone(ctx: Ctx, phone: str) -> None:
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 12:
        await ctx.send("Похоже, в номере ошибка. Напишите его в формате +7 900 000-00-00.")
        return
    form = forms[ctx.user_id]
    form.data["phone"] = phone
    form.step = "confirm"
    service = salon.services[form.data["service_id"]]
    master = salon.masters[form.data["master_id"]]
    await ctx.send(
        "<b>Проверьте запись:</b>\n"
        f"Услуга: {service.title} — {service.price} ₽\n"
        f"Мастер: {master.name}\n"
        f"Когда: {fmt_dt(datetime.fromisoformat(form.data['starts_at']))}\n"
        f"Имя: {html.escape(form.data['client_name'])}\n"
        f"Телефон: {html.escape(phone)}",
        [[button("✅ Подтвердить", "confirm"), button(BTN_CANCEL, "abort")]],
    )


async def confirm(ctx: Ctx) -> None:
    form = forms.get(ctx.user_id)
    if not form or form.step != "confirm":
        await stale(ctx)
        return
    data = form.data
    service = salon.services[data["service_id"]]
    starts_at = datetime.fromisoformat(data["starts_at"])
    # Время могли занять, пока клиент вводил данные, — в том числе через Telegram
    if starts_at not in slots_for(data["master_id"], service.id, starts_at.date()):
        await ask_day(ctx, prefix="😔 Это время только что заняли, выберите другое.\n\n")
        return
    booking = db.add(
        user_id=ctx.user_id, username=ctx.username,
        client_name=data["client_name"], phone=data["phone"],
        service_id=service.id, master_id=data["master_id"],
        starts_at=starts_at, ends_at=starts_at + timedelta(minutes=service.duration),
        now=now(), channel=CHANNEL,
    )
    forms.pop(ctx.user_id, None)
    await ctx.show(
        f"✅ Вы записаны!\n\n{booking_text(booking)}\n📍 {salon.address}\n\n"
        f"Напомню о визите за {REMIND_BEFORE.seconds // 3600} часа. "
        "Отменить или посмотреть запись можно в разделе «Мои записи».",
        [menu_row()],
    )
    await notify_admin(ctx.api, f"🆕 Новая запись (MAX)\n{booking_text(booking)}\n"
                                f"{html.escape(booking.client_name)}, {html.escape(booking.phone)}")


async def abort(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    await ctx.show("Запись отменена. Если появятся вопросы — пишите, я на связи 🙂", [menu_row()])


async def stale(ctx: Ctx) -> None:
    await ctx.toast("Эта кнопка устарела — начните заново через меню.")


# ---------- Живой администратор ----------

async def ask_admin_start(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    if not settings.max_admin_id:
        await ctx.show(f"Позвоните администратору: {salon.phone} — с радостью ответим.", [menu_row()])
        return
    forms[ctx.user_id] = Form("question")
    await ctx.show("Напишите ваш вопрос одним сообщением — передам администратору. Ответ придёт сюда же.",
                   [[button(BTN_CANCEL, "ask_cancel")]])


async def ask_admin_cancel(ctx: Ctx) -> None:
    forms.pop(ctx.user_id, None)
    await ctx.show("Хорошо! Если появятся вопросы — пишите 🙂", [menu_row()])


async def ask_admin_send(ctx: Ctx, text: str) -> None:
    forms.pop(ctx.user_id, None)
    question = text[:2000]
    who = html.escape(ctx.name) + (f" (@{html.escape(ctx.username)})" if ctx.username else "")
    try:
        sent = await ctx.api.send(
            settings.max_admin_id,
            f"💬 <b>Вопрос от клиента</b>\n{who}\n\n{html.escape(question)}\n\n"
            "<i>Ответьте на это сообщение — ответ уйдёт клиенту.</i>",
        )
    except Exception:
        log.exception("Не удалось переслать вопрос администратору")
        await ctx.send(f"Не получилось передать вопрос 🙏 Позвоните нам: {salon.phone}")
        return
    db.add_question(ctx.user_id, sent["body"]["mid"], question, now(), channel=CHANNEL)
    await ctx.send("✅ Передал вопрос администратору. Ответ придёт в этот чат.", [menu_row()])


async def admin_reply(api: MaxAPI, message: dict, text: str) -> bool:
    """Администратор ответил на пересланный вопрос — отправляем ответ клиенту."""
    link = message.get("link") or {}
    mid = (link.get("message") or {}).get("mid")
    client_id = db.question_user(mid, channel=CHANNEL) if link.get("type") == "reply" and mid else None
    if not client_id:
        return False
    await api.send(client_id, f"💬 <b>Ответ администратора:</b>\n{html.escape(text)}", [[button(BTN_BOOK, "book")]])
    db.mark_answered(mid, now(), channel=CHANNEL)
    await api.send(settings.max_admin_id, "✅ Ответ отправлен клиенту.")
    return True


async def admin_bookings(ctx: Ctx) -> None:
    if ctx.user_id != settings.max_admin_id:
        await ctx.send("Команда доступна только администратору.")
        return
    items = db.upcoming(now(), until=now() + timedelta(days=BOOKING_DAYS))
    if not items:
        await ctx.send("На ближайшую неделю записей нет.")
        return
    lines, current = [], None
    for b in items:
        if b.starts_at.date() != current:
            current = b.starts_at.date()
            lines.append(f"\n<b>{fmt_day(current)}</b>")
        lines.append(f"{b.starts_at:%H:%M} {salon.services[b.service_id].title} — "
                     f"{salon.masters[b.master_id].name}; "
                     f"{html.escape(b.client_name)}, {html.escape(b.phone)} · "
                     + ("MAX" if b.channel == "max" else "Telegram"))
    await ctx.send("\n".join(lines).strip())


# ---------- ИИ ----------

async def ask_ai(ctx: Ctx, text: str) -> None:
    if ctx.chat_id:
        try:
            await ctx.api.typing(ctx.chat_id)
        except Exception:
            log.debug("Не удалось показать «печатает»", exc_info=True)
    dialog = history[ctx.user_id]
    dialog.append({"role": "user", "content": text[:1000]})
    answer = await assistant.reply(list(dialog))
    dialog.append({"role": "assistant", "content": answer})
    await ctx.send(answer, book_or_ask(), plain=True)


# ---------- Разбор событий ----------

async def on_callback(ctx: Ctx, payload: str) -> None:
    action, _, arg = payload.partition(":")
    screens = {
        "menu": show_menu, "prices": show_prices, "contacts": show_contacts, "my": my_bookings,
        "book": book_start, "masters": show_masters, "ask_admin": ask_admin_start,
        "ask_cancel": ask_admin_cancel, "abort": abort, "confirm": confirm,
    }
    with_arg = {"svc": pick_service, "mst": pick_master, "bymst": book_with_master,
                "day": pick_day, "time": pick_time}
    if action in screens:
        await screens[action](ctx)
    elif action in with_arg:
        await with_arg[action](ctx, arg)
    elif action == "ucancel" and arg.isdigit():
        await user_cancel(ctx, int(arg))
    elif action == "busy":
        await ctx.toast("Это время уже занято — выберите другое 🙏")
    elif action == "full":
        await ctx.toast("На этот день всё занято — выберите другой 🙏")
    else:
        await stale(ctx)


async def on_message(ctx: Ctx, text: str, contact: dict | None) -> None:
    form = forms.get(ctx.user_id)
    step = form.step if form else None
    if text in ("/start", "/menu"):
        await start(ctx)
    elif text == "/myid":
        await ctx.send(f"Ваш ID в MAX: <code>{ctx.user_id}</code>\n"
                       "Укажите его в .env как MAX_ADMIN_ID, чтобы получать записи и вопросы клиентов.")
    elif text == "/bookings":
        await admin_bookings(ctx)
    elif step == "question" and text:
        await ask_admin_send(ctx, text)
    elif action := menu_action(text):
        await on_callback(ctx, action)
    elif step == "phone" and (contact or text):
        await enter_phone(ctx, phone_from_contact(contact) if contact else text)
    elif step == "name" and text:
        await enter_name(ctx, text)
    elif step in ("service", "master", "day", "time", "confirm"):
        await ctx.send("Пожалуйста, выберите вариант кнопкой выше или нажмите «Отмена».")
    elif text:
        await ask_ai(ctx, text)


def event_user(update: dict) -> dict:
    kind = update.get("update_type")
    if kind == "bot_started":
        return update.get("user") or {}
    if kind == "message_callback":
        return (update.get("callback") or {}).get("user") or {}
    return (update.get("message") or {}).get("sender") or {}


async def handle(api: MaxAPI, update: dict) -> None:
    kind = update.get("update_type")
    user = event_user(update)
    if not user.get("user_id") or user.get("is_bot"):
        return
    ctx = Ctx(api, user["user_id"], user.get("first_name") or "", user.get("username"))
    if kind == "bot_started":
        ctx.chat_id = update.get("chat_id")
        await start(ctx)
    elif kind == "message_callback":
        ctx.callback_id = update["callback"]["callback_id"]
        await on_callback(ctx, update["callback"].get("payload") or "")
    elif kind == "message_created":
        message = update["message"]
        recipient = message.get("recipient") or {}
        if recipient.get("chat_type", "dialog") != "dialog":
            return  # в групповых чатах бот молчит
        ctx.chat_id = recipient.get("chat_id")
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        if ctx.user_id == settings.max_admin_id and text and await admin_reply(api, message, text):
            return
        contact = next((a for a in body.get("attachments") or [] if a.get("type") == "contact"), None)
        await on_message(ctx, text, contact)


async def process(api: MaxAPI, update: dict) -> None:
    # События одного клиента обрабатываем по порядку, разных клиентов — параллельно
    async with locks[event_user(update).get("user_id", 0)]:
        try:
            await handle(api, update)
        except Exception:
            log.exception("Ошибка при обработке события %s", update.get("update_type"))


# ---------- Напоминания и запуск ----------

async def reminders_loop(api: MaxAPI) -> None:
    while True:
        try:
            for b in db.due_reminders(now(), now() + REMIND_BEFORE, channel=CHANNEL):
                await api.send(
                    b.user_id,
                    f"⏰ Напоминаем о записи: {booking_text(b)}\n📍 {salon.address}\n"
                    "Если планы изменились — отмените запись в разделе «Мои записи».",
                    [[button(BTN_MY, "my")]],
                )
                db.mark_reminded(b.id)
        except Exception:
            log.exception("Ошибка при отправке напоминаний")
        await asyncio.sleep(60)


async def polling(api: MaxAPI) -> None:
    marker = None
    while True:
        try:
            updates, new_marker = await api.updates(marker)
        except Exception:
            log.exception("Не удалось получить события MAX, повтор через 5 секунд")
            await asyncio.sleep(5)
            continue
        if new_marker is not None:
            marker = new_marker
        for update in updates:
            task = asyncio.create_task(process(api, update))
            tasks.add(task)
            task.add_done_callback(tasks.discard)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.max_token:
        raise SystemExit("Не задан MAX_BOT_TOKEN в .env — см. README.md")
    api = MaxAPI(settings.max_token, settings.max_api_url)
    try:
        me = await api.me()
        try:
            await api.set_commands({"start": "Главное меню"})
        except Exception:
            log.warning("Не удалось обновить команды бота", exc_info=True)
        log.info("Бот MAX запущен: %s, ИИ-провайдер: %s", me.get("username") or me.get("first_name"),
                 settings.llm_provider)
        tasks.add(asyncio.create_task(reminders_loop(api)))
        await polling(api)
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
