"""E2E-проверка MAX-бота на фейковом сервере MAX Bot API (форматы — по официальной schema.yaml)."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

BOT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)
os.environ.update({
    "BOT_TOKEN": "", "ADMIN_CHAT_ID": "",
    "MAX_BOT_TOKEN": "test-token", "MAX_ADMIN_ID": "999",
    "MAX_API_URL": "http://127.0.0.1:8766",
    "LLM_PROVIDER": "none",
    "DB_PATH": os.path.join(tempfile.mkdtemp(), "test.db"),
})

from aiohttp import web  # noqa: E402

import max_bot as app  # noqa: E402

USER = {"user_id": 42, "first_name": "Мария", "username": "maria", "is_bot": False}
ADMIN = {"user_id": 999, "first_name": "Ольга", "is_bot": False}


class FakeMax:
    def __init__(self):
        self.updates, self.sent, self.mid, self.cb = [], [], 0, 0
        self.bad_auth = 0

    @staticmethod
    def buttons(body: dict) -> list:
        return [b.get("payload") or b["type"]
                for a in body.get("attachments") or [] if a["type"] == "inline_keyboard"
                for row in a["payload"]["buttons"] for b in row]

    def auth(self, req):
        if req.headers.get("Authorization") != "test-token":
            self.bad_auth += 1

    async def me(self, req):
        self.auth(req)
        return web.json_response({"user_id": 1, "first_name": "Лаванда", "username": "lavanda_test_bot", "is_bot": True})

    async def get_updates(self, req):
        self.auth(req)
        marker = int(req.query.get("marker") or 0)
        for _ in range(20):
            if len(self.updates) > marker:
                break
            await asyncio.sleep(0.05)
        return web.json_response({"updates": self.updates[marker:], "marker": len(self.updates)})

    async def post_message(self, req):
        self.auth(req)
        body, uid = await req.json(), int(req.query["user_id"])
        self.mid += 1
        mid = f"mid.{self.mid:04d}"
        self.sent.append(("send", uid, body.get("text"), self.buttons(body), body.get("format")))
        return web.json_response({"message": {"recipient": {"user_id": uid, "chat_type": "dialog"},
                                              "timestamp": 0, "body": {"mid": mid, "seq": self.mid,
                                                                       "text": body.get("text")}}})

    async def answers(self, req):
        self.auth(req)
        body = await req.json()
        if "message" in body:
            m = body["message"]
            self.sent.append(("edit", req.query["callback_id"], m.get("text"), self.buttons(m), m.get("format")))
        if body.get("notification"):
            self.sent.append(("toast", req.query["callback_id"], body["notification"], [], None))
        return web.json_response({"success": True})

    async def ok(self, req):
        self.auth(req)
        return web.json_response({"success": True})


fake = FakeMax()


def started(user=USER):
    fake.updates.append({"update_type": "bot_started", "timestamp": 0, "chat_id": 5000 + user["user_id"], "user": user})


def text(t, user=USER, link=None, attachments=None):
    msg = {"sender": user, "recipient": {"chat_id": 5000 + user["user_id"], "chat_type": "dialog"},
           "timestamp": 0, "body": {"mid": f"in.{len(fake.updates)}", "seq": 0, "text": t,
                                    "attachments": attachments or []}}
    if link:
        msg["link"] = link
    fake.updates.append({"update_type": "message_created", "timestamp": 0, "message": msg})


def click(payload, user=USER):
    fake.cb += 1
    fake.updates.append({"update_type": "message_callback", "timestamp": 0,
                         "callback": {"timestamp": 0, "callback_id": f"cb{fake.cb}", "payload": payload, "user": user}})


async def step(title, action, wait=0.6):
    print(f"\n{title}")
    n = len(fake.sent)
    action()
    await asyncio.sleep(wait)
    out = fake.sent[n:]
    for kind, who, t, btns, fmt in out:
        print(f"  [{kind} -> {who}] {(t or '')[:150]!r}" + (f"  fmt={fmt}" if fmt else ""))
        if btns:
            print(f"     buttons: {btns}")
    return out


def check(cond, msg):
    print(("   OK: " if cond else "   FAIL: ") + msg)
    if not cond:
        raise SystemExit(1)


async def main():
    server = web.Application()
    server.router.add_get("/me", fake.me)
    server.router.add_patch("/me/commands", fake.ok)
    server.router.add_get("/updates", fake.get_updates)
    server.router.add_post("/messages", fake.post_message)
    server.router.add_post("/answers", fake.answers)
    server.router.add_post("/chats/{chat_id}/actions", fake.ok)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8766).start()
    bot_task = asyncio.create_task(app.main())
    await asyncio.sleep(0.5)

    out = await step("1) Клиент открыл бота", started)
    check(out and "Здравствуйте, Мария" in out[0][2] and "book" in out[0][3], "приветствие и главное меню")

    out = await step("2) Услуги и цены (кнопка)", lambda: click("prices"))
    check(out[0][0] == "edit" and "Маникюр" in out[0][2], "прайс заменил сообщение с меню")

    await step("3) Записаться", lambda: click("book"))
    svc = next(s for s in app.salon.services.values() if len(app.salon.masters_for(s.id)) > 1)
    out = await step(f"4) Услуга «{svc.title}» (у неё несколько мастеров)", lambda: click(f"svc:{svc.id}"))
    master = app.salon.masters_for(svc.id)[0]
    out = await step(f"5) Мастер {master.name}", lambda: click(f"mst:{master.id}"))
    day = next(p for p in out[0][3] if str(p).startswith("day:"))
    out = await step(f"6) День {day}", lambda: click(day))
    slot = next(p for p in out[0][3] if str(p).startswith("time:"))
    out = await step(f"7) Время {slot}", lambda: click(slot))
    check(len(out) == 2 and out[1][2] == "Как к вам обращаться?", "подтверждение времени и вопрос об имени")

    out = await step("8) Имя", lambda: text("Мария"))
    check("request_contact" in out[0][3], "кнопка «Отправить мой номер»")

    vcf = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Мария\r\nTEL;TYPE=cell:+79001234567\r\nEND:VCARD"
    out = await step("9) Контакт кнопкой (vCard)", lambda: text("", attachments=[
        {"type": "contact", "payload": {"vcf_info": vcf, "max_info": USER}}]))
    check("Телефон: +79001234567" in out[0][2] and "confirm" in out[0][3], "номер из vCard, экран проверки")

    out = await step("10) Подтвердить", lambda: click("confirm"))
    check(any(o[0] == "edit" and "Вы записаны" in o[2] for o in out), "клиент записан")
    check(any(o[1] == 999 and "Новая запись (MAX)" in o[2] for o in out), "администратор получил уведомление")
    booking = app.db.upcoming(app.now(), user_id=42, channel="max")[0]
    check(booking.channel == "max", f"запись #{booking.id} в базе с channel=max")

    print("\n11) Общее расписание с Telegram")
    grid = dict(app.grid_for(booking.master_id, booking.service_id, booking.starts_at.date()))
    check(grid.get(booking.starts_at) is False, "время из MAX занято в сетке")
    tg_time = next(t for t, free in grid.items() if free)
    app.db.add(user_id=42, username="tg", client_name="Клиент из Telegram", phone="+79990000000",
               service_id=booking.service_id, master_id=booking.master_id, starts_at=tg_time,
               ends_at=tg_time + timedelta(minutes=app.salon.services[booking.service_id].duration),
               now=app.now(), channel="telegram")
    grid = dict(app.grid_for(booking.master_id, booking.service_id, booking.starts_at.date()))
    check(grid.get(tg_time) is False, "запись из Telegram тоже занимает время в MAX")
    mine = app.db.upcoming(app.now(), user_id=42, channel="max")
    check(len(mine) == 1, "«Мои записи» в MAX не показывают чужую запись из Telegram с тем же id")

    out = await step("12) Мои записи", lambda: click("my"))
    check(f"ucancel:{booking.id}" in out[0][3], "кнопка отмены своей записи")
    out = await step("13) Отмена записи", lambda: click(f"ucancel:{booking.id}"))
    check(any("Запись отменена" in o[2] for o in out) and any(o[1] == 999 for o in out),
          "запись отменена, администратор в курсе")

    out = await step("14) Пункт меню словами: «мастера»", lambda: text("мастера"))
    check("Наши мастера" in out[0][2] and out[0][0] == "send", "меню понимает текст")

    out = await step("15) Вопрос ИИ (режим FAQ)", lambda: text("Где можно припарковаться?"))
    check(out[0][4] is None and "ask_admin" in out[0][3], "ответ без HTML-разметки, с кнопками записи и вопроса")

    await step("16) Спросить администратора", lambda: click("ask_admin"))
    out = await step("   клиент пишет вопрос", lambda: text("Можно прийти с собакой?"))
    to_admin = next(o for o in out if o[1] == 999)
    admin_mid = f"mid.{fake.mid - 1:04d}"  # вопрос админу отправлен перед ответом клиенту
    check("Можно прийти с собакой?" in to_admin[2], "вопрос переслан администратору")
    out = await step("   администратор отвечает реплаем", lambda: text(
        "Да, с маленькой собакой можно 🙂", user=ADMIN,
        link={"type": "reply", "message": {"mid": admin_mid, "seq": 0, "text": "..."}}))
    check(any(o[1] == 42 and "Ответ администратора" in o[2] for o in out), "ответ дошёл до клиента")
    check(any(o[1] == 999 and "отправлен клиенту" in o[2] for o in out), "администратор видит подтверждение")

    out = await step("17) Устаревшая кнопка", lambda: click(f"svc:{svc.id}"))
    check(out and out[0][0] == "toast", "всплывающее уведомление вместо ошибки")
    out = await step("18) Занятое время", lambda: click("busy"))
    check(out[0][0] == "toast" and "занято" in out[0][2], "подсказка про занятое время")

    out = await step("19) /bookings от клиента и от администратора", lambda: (text("/bookings"), text("/bookings", user=ADMIN)))
    check(any("только администратору" in o[2] for o in out), "клиенту — отказ")
    check(any(o[1] == 999 and "Telegram" in o[2] for o in out), "администратору — общее расписание с пометкой канала")

    out = await step("20) /myid", lambda: text("/myid"))
    check("42" in out[0][2], "ID для настройки администратора")

    print("\n21) Напоминания уходят только в свой мессенджер")
    soon = (app.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
    app.db.add(user_id=42, username=None, client_name="А", phone="+79000000001", service_id=svc.id,
               master_id=master.id, starts_at=soon, ends_at=soon + timedelta(hours=1), now=app.now(), channel="max")
    app.db.add(user_id=43, username=None, client_name="Б", phone="+79000000002", service_id=svc.id,
               master_id=master.id, starts_at=soon, ends_at=soon + timedelta(hours=1), now=app.now(), channel="telegram")
    due_max = app.db.due_reminders(app.now(), app.now() + timedelta(hours=3), channel="max")
    due_tg = app.db.due_reminders(app.now(), app.now() + timedelta(hours=3), channel="telegram")
    names_max, names_tg = {b.client_name for b in due_max}, {b.client_name for b in due_tg}
    print(f"   MAX: {sorted(names_max)}, Telegram: {sorted(names_tg)}")
    check("А" in names_max and "Б" not in names_max and "Б" in names_tg and "А" not in names_tg,
          "каналы не смешиваются")

    check(fake.bad_auth == 0, "все запросы с заголовком Authorization")
    bot_task.cancel()
    await asyncio.gather(bot_task, return_exceptions=True)
    await runner.cleanup()
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


asyncio.run(main())
