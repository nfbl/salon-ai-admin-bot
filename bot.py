"""ИИ-администратор салона красоты: отвечает на вопросы, записывает клиентов,
напоминает о визите, уведомляет администратора и передаёт ему сложные вопросы."""
import asyncio
import html
import logging
import re
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import load_settings
from db import Booking, Bookings
from llm import Assistant
from salon import Salon
from slots import day_grid, free_slots

log = logging.getLogger(__name__)

settings = load_settings()
salon = Salon.load()
db = Bookings(settings.db_path)
assistant = Assistant(settings, salon)
router = Router()
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))

BOOKING_DAYS = 7
REMIND_BEFORE = timedelta(hours=3)

BTN_PRICES = "Услуги и цены"
BTN_BOOK = "Записаться"
BTN_MASTERS = "Мастера"
BTN_MY = "Мои записи"
BTN_CONTACTS = "Контакты"
BTN_ASK = "Спросить администратора"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PRICES), KeyboardButton(text=BTN_BOOK)],
        [KeyboardButton(text=BTN_MASTERS), KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_CONTACTS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Задайте вопрос или выберите пункт меню",
)
PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True,
)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


class BookingForm(StatesGroup):
    service = State()
    master = State()
    day = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


class AskAdmin(StatesGroup):
    question = State()


def now() -> datetime:
    return datetime.now(settings.tz)


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def fmt_dt(dt: datetime) -> str:
    return f"{fmt_day(dt.date())} в {dt:%H:%M}"


def booking_text(b: Booking) -> str:
    service = salon.services[b.service_id]
    master = salon.masters[b.master_id]
    return f"{service.title} — {master.name}, {fmt_dt(b.starts_at)}"


def book_button() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_BOOK, callback_data="book")
    return kb.as_markup()


def book_or_ask_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_BOOK, callback_data="book")
    if settings.admin_chat_id:
        kb.button(text=BTN_ASK, callback_data="ask_admin")
    kb.adjust(1)
    return kb.as_markup()


def with_cancel(kb: InlineKeyboardBuilder, *sizes: int) -> InlineKeyboardMarkup:
    kb.adjust(*sizes)
    kb.row(InlineKeyboardButton(text="Отмена", callback_data="abort"))
    return kb.as_markup()


async def notify_admin(bot: Bot, text: str) -> None:
    if settings.admin_chat_id:
        try:
            await bot.send_message(settings.admin_chat_id, text)
        except Exception:
            log.exception("Не удалось уведомить администратора")


def slots_for(master_id: str, service_id: str, day: date) -> list[datetime]:
    duration = salon.services[service_id].duration
    return free_slots(salon, db.busy(master_id, day), day, duration, now())


def grid_for(master_id: str, service_id: str, day: date) -> list[tuple[datetime, bool]]:
    duration = salon.services[service_id].duration
    return day_grid(salon, db.busy(master_id, day), day, duration, now())


# ---------- Старт ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = html.escape(message.from_user.first_name or "")
    await message.answer(
        f"Здравствуйте, {name}!\n\n"
        f"Я виртуальный администратор салона «{salon.name}».\n"
        "Отвечу на вопросы об услугах, ценах и акциях, запишу к мастеру и напомню о визите.\n\n"
        "Выберите пункт меню или просто напишите вопрос.",
        reply_markup=MAIN_KB,
    )


# ---------- Живой администратор ----------

async def reply_to_question(message: Message) -> dict | bool:
    """Фильтр: администратор ответил (Reply) на пересланный вопрос клиента."""
    if message.chat.id != settings.admin_chat_id or not message.reply_to_message:
        return False
    client_id = db.question_user(message.reply_to_message.message_id)
    return {"client_id": client_id} if client_id else False


@router.message(F.text, reply_to_question)
async def admin_reply(message: Message, bot: Bot, client_id: int):
    await bot.send_message(
        client_id,
        f"<b>Ответ администратора:</b>\n{html.escape(message.text)}",
        reply_markup=book_button(),
    )
    db.mark_answered(message.reply_to_message.message_id, now())
    await message.answer("Ответ отправлен клиенту.")


@router.callback_query(F.data == "ask_admin")
async def ask_admin_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    if not settings.admin_chat_id:
        await cb.message.answer(f"Позвоните администратору: {salon.phone} — с радостью ответим.")
        return
    await state.set_state(AskAdmin.question)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="ask_cancel")
    await cb.message.answer(
        "Напишите ваш вопрос одним сообщением — передам администратору. Ответ придёт сюда же.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "ask_cancel")
async def ask_admin_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Хорошо. Если появятся вопросы — пишите.")
    await cb.answer()


@router.message(AskAdmin.question, F.text)
async def ask_admin_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user = message.from_user
    question = message.text[:2000]
    username = f" (@{user.username})" if user.username else ""
    try:
        sent = await bot.send_message(
            settings.admin_chat_id,
            f"<b>Вопрос от клиента</b>\n{html.escape(user.full_name)}{username}\n\n"
            f"{html.escape(question)}\n\n"
            "<i>Ответьте на это сообщение (Reply) — ответ уйдёт клиенту.</i>",
        )
    except Exception:
        log.exception("Не удалось переслать вопрос администратору")
        await message.answer(f"Не получилось передать вопрос. Позвоните нам: {salon.phone}")
        return
    db.add_question(user.id, sent.message_id, question, now())
    await message.answer("Передал вопрос администратору. Ответ придёт в этот чат.",
                         reply_markup=MAIN_KB)


# ---------- Меню ----------

@router.message(F.text == BTN_PRICES)
async def show_prices(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(salon.price_list(), reply_markup=book_button())


@router.message(F.text == BTN_CONTACTS)
async def show_contacts(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"<b>{salon.name}</b>\n{salon.address}\nТелефон: {salon.phone}\nЧасы работы: {salon.hours_text}",
        reply_markup=book_or_ask_kb(),
    )


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my_bookings(message: Message, state: FSMContext):
    await state.clear()
    items = db.upcoming(now(), user_id=message.from_user.id)
    if not items:
        await message.answer("У вас пока нет предстоящих записей.", reply_markup=book_button())
        return
    kb = InlineKeyboardBuilder()
    lines = ["<b>Ваши записи:</b>"]
    for b in items:
        lines.append(f"• {booking_text(b)}")
        kb.button(text=f"Отменить {b.starts_at:%d.%m %H:%M}", callback_data=f"ucancel:{b.id}")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ucancel:"))
async def user_cancel(cb: CallbackQuery, bot: Bot):
    booking = db.cancel(int(cb.data.split(":")[1]), cb.from_user.id)
    if not booking:
        await cb.answer("Запись уже отменена", show_alert=True)
        return
    await cb.message.edit_text(f"Запись отменена: {booking_text(booking)}")
    await cb.answer()
    await notify_admin(bot, f"Клиент отменил запись\n{booking_text(booking)}\n"
                            f"{html.escape(booking.client_name)}, {html.escape(booking.phone)}")


# ---------- Запись ----------

@router.message(F.text == BTN_BOOK)
@router.message(Command("book"))
async def book_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BookingForm.service)
    await message.answer("Выберите услугу:", reply_markup=services_kb())


@router.callback_query(F.data == "book")
async def book_start_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(BookingForm.service)
    await cb.message.answer("Выберите услугу:", reply_markup=services_kb())
    await cb.answer()


@router.callback_query(F.data == "abort")
async def abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Запись отменена. Если появятся вопросы — пишите.")
    await cb.answer()


@router.message(F.text == BTN_MASTERS)
async def show_masters(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(masters_text(), reply_markup=masters_kb())


@router.callback_query(F.data == "masters")
async def show_masters_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(masters_text(), reply_markup=masters_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("bymst:"))
async def book_with_master(cb: CallbackQuery, state: FSMContext):
    master = salon.masters[cb.data.split(":")[1]]
    await state.clear()
    await state.set_state(BookingForm.service)
    await state.update_data(master_id=master.id)
    await cb.message.edit_text(f"Мастер: <b>{master.name}</b>\nВыберите услугу:",
                               reply_markup=services_kb(master.id))
    await cb.answer()


def masters_text() -> str:
    lines = ["<b>Наши мастера</b>"]
    for m in salon.masters.values():
        services = ", ".join(salon.services[s].title for s in m.services)
        lines.append(f"\n<b>{m.name}</b> — {m.role}\n{services}")
    return "\n".join(lines)


def masters_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in salon.masters.values():
        kb.button(text=f"Записаться: {m.name}", callback_data=f"bymst:{m.id}")
    return with_cancel(kb, 2)


def services_kb(master_id: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in salon.services.values():
        if master_id is None or s.id in salon.masters[master_id].services:
            kb.button(text=f"{s.title} · {s.price} ₽", callback_data=f"svc:{s.id}")
    if master_id is None:
        kb.button(text="Сначала выбрать мастера", callback_data="masters")
    return with_cancel(kb, 1)


@router.callback_query(BookingForm.service, F.data.startswith("svc:"))
async def pick_service(cb: CallbackQuery, state: FSMContext):
    service_id = cb.data.split(":")[1]
    await state.update_data(service_id=service_id)
    masters = salon.masters_for(service_id)
    if (await state.get_data()).get("master_id"):  # мастер выбран заранее
        await ask_day(cb.message, state)
    elif len(masters) == 1:
        await state.update_data(master_id=masters[0].id)
        await ask_day(cb.message, state)
    else:
        kb = InlineKeyboardBuilder()
        for m in masters:
            kb.button(text=f"{m.name} — {m.role}", callback_data=f"mst:{m.id}")
        await state.set_state(BookingForm.master)
        await cb.message.edit_text("Выберите мастера:", reply_markup=with_cancel(kb, 1))
    await cb.answer()


@router.callback_query(BookingForm.master, F.data.startswith("mst:"))
async def pick_master(cb: CallbackQuery, state: FSMContext):
    await state.update_data(master_id=cb.data.split(":")[1])
    await ask_day(cb.message, state)
    await cb.answer()


async def ask_day(message: Message, state: FSMContext, prefix: str = ""):
    data = await state.get_data()
    service = salon.services[data["service_id"]]
    master = salon.masters[data["master_id"]]
    today = now().date()
    kb = InlineKeyboardBuilder()
    has_free = has_full = False
    for d in (today + timedelta(days=i) for i in range(BOOKING_DAYS)):
        grid = grid_for(master.id, service.id, d)
        label = f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"
        if any(free for _, free in grid):
            has_free = True
            kb.button(text=label, callback_data=f"day:{d.isoformat()}")
        elif grid:  # все окна дня заняты реальными записями
            has_full = True
            kb.button(text=f"✕ {label}", callback_data="full")
    await state.set_state(BookingForm.day)
    if not has_free:
        await state.clear()
        await message.edit_text(
            f"{prefix}К сожалению, у мастера {master.name} нет свободных окон на ближайшую неделю. "
            f"Позвоните нам: {salon.phone} — постараемся найти время."
        )
        return
    legend = "\n✕ — всё занято" if has_full else ""
    await message.edit_text(
        f"{prefix}<b>{service.title}</b> · {service.price} ₽ · мастер {master.name}\n\n"
        f"Выберите день:{legend}",
        reply_markup=with_cancel(kb, 3),
    )


@router.callback_query(BookingForm.day, F.data.startswith("day:"))
async def pick_day(cb: CallbackQuery, state: FSMContext):
    day = date.fromisoformat(cb.data.split(":", 1)[1])
    data = await state.get_data()
    grid = grid_for(data["master_id"], data["service_id"], day)
    if not any(free for _, free in grid):
        await cb.answer("На этот день окна уже заняли, выберите другой", show_alert=True)
        await ask_day(cb.message, state)
        return
    kb = InlineKeyboardBuilder()
    for t, free in grid:
        if free:
            kb.button(text=f"{t:%H:%M}", callback_data=f"time:{t.isoformat()}")
        else:
            kb.button(text=f"✕ {t:%H:%M}", callback_data="busy")
    legend = "\n✕ — время занято" if not all(free for _, free in grid) else ""
    await state.set_state(BookingForm.time)
    await cb.message.edit_text(f"{fmt_day(day)}. Выберите время:{legend}",
                               reply_markup=with_cancel(kb, 4))
    await cb.answer()


@router.callback_query(F.data == "busy")
async def busy_slot(cb: CallbackQuery):
    await cb.answer("Это время уже занято — выберите другое.")


@router.callback_query(F.data == "full")
async def full_day(cb: CallbackQuery):
    await cb.answer("На этот день всё занято — выберите другой.")


@router.callback_query(BookingForm.time, F.data.startswith("time:"))
async def pick_time(cb: CallbackQuery, state: FSMContext):
    starts_at = datetime.fromisoformat(cb.data.split(":", 1)[1])
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(BookingForm.name)
    await cb.message.edit_text(f"Отлично, {fmt_dt(starts_at)}.")
    await cb.message.answer("Как к вам обращаться?")
    await cb.answer()


@router.message(BookingForm.name, F.text)
async def enter_name(message: Message, state: FSMContext):
    name = message.text.strip()[:50]
    if len(name) < 2:
        await message.answer("Напишите, пожалуйста, ваше имя.")
        return
    await state.update_data(client_name=name)
    await state.set_state(BookingForm.phone)
    await message.answer(
        "Оставьте номер телефона — нажмите кнопку ниже или напишите вручную.",
        reply_markup=PHONE_KB,
    )


@router.message(BookingForm.phone, F.contact | F.text)
async def enter_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 12:
        await message.answer("Похоже, в номере ошибка. Напишите его в формате +7 900 000-00-00.")
        return
    await state.update_data(phone=phone)
    await state.set_state(BookingForm.confirm)
    data = await state.get_data()
    service = salon.services[data["service_id"]]
    master = salon.masters[data["master_id"]]
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтвердить", callback_data="confirm")
    kb.button(text="Отмена", callback_data="abort")
    await message.answer("Почти готово!", reply_markup=MAIN_KB)
    await message.answer(
        "<b>Проверьте запись:</b>\n"
        f"Услуга: {service.title} — {service.price} ₽\n"
        f"Мастер: {master.name}\n"
        f"Когда: {fmt_dt(datetime.fromisoformat(data['starts_at']))}\n"
        f"Имя: {html.escape(data['client_name'])}\n"
        f"Телефон: {html.escape(phone)}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BookingForm.confirm, F.data == "confirm")
async def confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    service = salon.services[data["service_id"]]
    starts_at = datetime.fromisoformat(data["starts_at"])
    # Время могли занять, пока клиент вводил данные
    if starts_at not in slots_for(data["master_id"], service.id, starts_at.date()):
        await ask_day(cb.message, state, prefix="Это время только что заняли, выберите другое.\n\n")
        await cb.answer()
        return
    booking = db.add(
        user_id=cb.from_user.id, username=cb.from_user.username,
        client_name=data["client_name"], phone=data["phone"],
        service_id=service.id, master_id=data["master_id"],
        starts_at=starts_at, ends_at=starts_at + timedelta(minutes=service.duration),
        now=now(),
    )
    await state.clear()
    await cb.message.edit_text(
        f"Вы записаны.\n\n{booking_text(booking)}\nАдрес: {salon.address}\n\n"
        f"Напомню о визите за {REMIND_BEFORE.seconds // 3600} часа. "
        "Отменить или посмотреть запись можно в разделе «Мои записи»."
    )
    await cb.answer("Готово!")
    username = f" (@{booking.username})" if booking.username else ""
    await notify_admin(bot, f"Новая запись\n{booking_text(booking)}\n"
                            f"{html.escape(booking.client_name)}{username}, {html.escape(booking.phone)}")


# ---------- Администратор ----------

@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>\n"
                         "Укажите его в .env как ADMIN_CHAT_ID, чтобы получать уведомления.")


@router.message(Command("bookings"))
async def cmd_bookings(message: Message):
    if message.chat.id != settings.admin_chat_id:
        await message.answer("Команда доступна только администратору.")
        return
    items = db.upcoming(now(), until=now() + timedelta(days=BOOKING_DAYS))
    if not items:
        await message.answer("На ближайшую неделю записей нет.")
        return
    lines, current = [], None
    for b in items:
        if b.starts_at.date() != current:
            current = b.starts_at.date()
            lines.append(f"\n<b>{fmt_day(current)}</b>")
        lines.append(f"{b.starts_at:%H:%M} {salon.services[b.service_id].title} — "
                     f"{salon.masters[b.master_id].name}; "
                     f"{html.escape(b.client_name)}, {html.escape(b.phone)}")
    await message.answer("\n".join(lines).strip())


# ---------- Всё остальное ----------

@router.message(StateFilter(BookingForm.service, BookingForm.master, BookingForm.day,
                            BookingForm.time, BookingForm.confirm))
async def expect_button(message: Message):
    await message.answer("Пожалуйста, выберите вариант кнопкой выше или нажмите «Отмена».")


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела — начните заново через меню.", show_alert=True)


@router.message(StateFilter(None), F.text)
async def ask_ai(message: Message, bot: Bot):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    dialog = history[message.from_user.id]
    dialog.append({"role": "user", "content": message.text[:1000]})
    answer = await assistant.reply(list(dialog))
    dialog.append({"role": "assistant", "content": answer})
    await message.answer(answer, parse_mode=None, reply_markup=book_or_ask_kb())


# ---------- Напоминания ----------

async def reminders_loop(bot: Bot) -> None:
    while True:
        try:
            for b in db.due_reminders(now(), now() + REMIND_BEFORE):
                await bot.send_message(
                    b.user_id,
                    f"Напоминаем о записи: {booking_text(b)}\nАдрес: {salon.address}\n"
                    "Если планы изменились — отмените запись в разделе «Мои записи».",
                )
                db.mark_reminded(b.id)
        except Exception:
            log.exception("Ошибка при отправке напоминаний")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="book", description="Записаться"),
        BotCommand(command="my", description="Мои записи"),
    ])
    log.info("Бот запущен, ИИ-провайдер: %s", settings.llm_provider)
    asyncio.create_task(reminders_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
