"""E2E-проверка бота на фейковой сессии Telegram."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

BOT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)
os.environ["BOT_TOKEN"] = "123456789:AAFakeTokenForOfflineTestsOnly_xxxxxxx"
os.environ["ADMIN_CHAT_ID"] = "999"
os.environ["LLM_PROVIDER"] = "none"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

import bot as app

USER = User(id=42, is_bot=False, first_name="Тест", username="tester")
CHAT = Chat(id=42, type="private")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.ids = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (SendMessage, EditMessageText)):
            self.sent.append((type(method).__name__, method.chat_id, method.text, method.reply_markup))
            self.ids.append(len(self.sent) + 100)
            return Message(message_id=len(self.sent) + 100, date=datetime.now(),
                           chat=Chat(id=method.chat_id or 42, type="private"), text=method.text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""


session = FakeSession()
bot = Bot(os.environ["BOT_TOKEN"], session=session)
dp = Dispatcher()
dp.include_router(app.router)
uid = 0


ADMIN = User(id=999, is_bot=False, first_name="Админ")
ADMIN_CHAT = Chat(id=999, type="private")


async def send(text=None, contact=None, admin=False, reply_to=None):
    global uid
    uid += 1
    chat, user = (ADMIN_CHAT, ADMIN) if admin else (CHAT, USER)
    reply = Message(message_id=reply_to, date=datetime.now(), chat=chat, text="q") if reply_to else None
    msg = Message(message_id=uid, date=datetime.now(), chat=chat, from_user=user, text=text,
                  contact=contact, reply_to_message=reply)
    await dp.feed_update(bot, Update(update_id=uid, message=msg))


async def click(data):
    global uid
    uid += 1
    bot_msg = Message(message_id=500, date=datetime.now(), chat=CHAT, text="kb")
    cb = CallbackQuery(id=str(uid), from_user=USER, chat_instance="x", message=bot_msg, data=data)
    await dp.feed_update(bot, Update(update_id=uid, callback_query=cb))


def last(n=1):
    out = session.sent[-n:]
    for kind, chat, text, markup in out:
        buttons = []
        if markup is not None and hasattr(markup, "inline_keyboard"):
            buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        print(f"  [{kind} -> {chat}] {text[:160]!r}")
        if buttons:
            print(f"     buttons: {buttons[:8]}{' ...' if len(buttons) > 8 else ''}")
    return out


def buttons_of(i=-1):
    m = session.sent[i][3]
    return [b.callback_data for row in m.inline_keyboard for b in row]


async def main():
    print("1) /start"); await send("/start"); last()
    print("2) Цены"); await send(app.BTN_PRICES); last()
    print("3) Вопрос без ИИ: оплата"); await send("Можно оплатить картой?"); last()
    print("4) Вопрос: сколько стоит маникюр"); await send("Сколько стоит маникюр?"); last()
    print("5) Запись"); await send(app.BTN_BOOK); last()
    await click("svc:manicure_gel"); print("   выбор мастера:"); last()
    await click("mst:olga"); print("   выбор дня:"); last()
    day = buttons_of()[0]
    await click(day); print("   выбор времени:"); last()
    slot = buttons_of()[0]
    await click(slot); last(2)
    print("6) Текст вместо кнопки на шаге имени — должен принять как имя"); await send("Мария"); last()
    print("7) Неверный телефон"); await send("123"); last()
    print("8) Телефон контактом"); await send(contact=Contact(phone_number="+79001234567", first_name="Мария", user_id=42)); last(2)
    await click("confirm"); print("9) Подтверждение + уведомление админу:"); last(2)
    print("10) Мои записи"); await send(app.BTN_MY); last()

    print("11) Запись через «Мастера»: Ольга -> только её услуги -> занятое время помечено ✕")
    await send(app.BTN_MASTERS); last()
    await click("bymst:olga"); last()
    assert buttons_of() == ["svc:manicure", "svc:manicure_gel", "svc:pedicure", "abort"], buttons_of()
    await click("svc:manicure"); last()
    await click(day); last()
    markup = session.sent[-1][3]
    texts = {b.text: b.callback_data for row in markup.inline_keyboard for b in row}
    hhmm = slot.split("T")[1][:5]
    assert slot not in buttons_of(), "занятый слот кликабелен!"
    assert texts.get(f"✕ {hhmm}") == "busy", f"нет пометки занятости для {hhmm}: {list(texts)[:6]}"
    assert "время занято" in session.sent[-1][2]
    busy_marks = [t for t in texts if t.startswith("✕")]
    print("   OK: занятые окна:", busy_marks)
    await click("busy"); print("   нажатие на занятое время -> всплывающее «занято», без ошибок")
    await click("abort"); last()

    print("11b) У Екатерины в это же время свободно (занятость — по мастеру)")
    await send(app.BTN_BOOK); await click("svc:manicure"); await click("mst:katya"); await click(day)
    assert slot in buttons_of(), "у другого мастера время должно быть свободно"
    assert not any(b == "busy" for b in buttons_of())
    print("   OK"); await click("abort")

    print("12) Напоминания (окно 3 ч)")
    due = app.db.due_reminders(app.now(), app.now() + app.REMIND_BEFORE)
    print("   к напоминанию сейчас:", len(due))

    print("13) Отмена записи клиентом")
    booking_id = buttons_of(-3)[0] if False else None
    items = app.db.upcoming(app.now(), user_id=42)
    await click(f"ucancel:{items[0].id}"); last(2)
    assert not app.db.upcoming(app.now(), user_id=42)

    print("14) Устаревшая кнопка"); await click("svc:haircut_w"); print("   (ответ через answerCallbackQuery, без ошибок)")
    print("15) Админ /bookings от не-админа"); await send("/bookings"); last()

    print("16) Вопрос администратору: клиент -> админ -> ответ клиенту")
    await send("Можно прийти с собакой?"); last()
    assert "ask_admin" in buttons_of(), "нет кнопки «Спросить администратора» под ответом ИИ"
    await click("ask_admin"); last()
    await send("Можно прийти с собакой?"); last(2)
    q_idx = max(i for i, m in enumerate(session.sent) if m[1] == 999)
    assert "Вопрос от клиента" in session.sent[q_idx][2]
    await send("Да, с маленькой собакой можно 🙂", admin=True, reply_to=session.ids[q_idx]); last(2)
    assert session.sent[-2][1] == 42 and "Ответ администратора" in session.sent[-2][2]
    assert session.sent[-1][1] == 999 and "отправлен" in session.sent[-1][2]
    print("   OK: ответ дошёл до клиента")

    print("17) Реплай админа НЕ на вопрос -> обычный ответ ИИ, без ошибок")
    await send("Сколько стоит стрижка?", admin=True, reply_to=1); last()
    assert session.sent[-1][1] == 999 and "Ответ администратора" not in session.sent[-1][2]
    print("   OK")

    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


asyncio.run(main())
