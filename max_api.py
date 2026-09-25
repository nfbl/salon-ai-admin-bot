"""Небольшой асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).

Сертификат API выдан НУЦ Минцифры, которого нет в стандартном наборе корневых
сертификатов. Поэтому для запросов к MAX к набору certifi добавляется
Russian Trusted Root CA из certs/ — системное хранилище сертификатов не меняется."""
import ssl
from pathlib import Path

import aiohttp
import certifi

API_URL = "https://platform-api2.max.ru"
RU_ROOT_CA = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"


class MaxError(Exception):
    pass


def button(text: str, payload: str) -> dict:
    """Кнопка, нажатие на которую приходит боту как message_callback с этим payload."""
    return {"type": "callback", "text": text, "payload": payload}


def contact_button(text: str) -> dict:
    return {"type": "request_contact", "text": text}


def keyboard(rows: list[list[dict]]) -> dict:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def message_body(text: str, buttons: list[list[dict]] | None = None, html: bool = True) -> dict:
    # Пустой список attachments убирает у сообщения старые кнопки
    body = {"text": text, "attachments": [keyboard(buttons)] if buttons else []}
    if html:
        body["format"] = "html"
    return body


class MaxAPI:
    def __init__(self, token: str, base_url: str | None = None):
        self.token = token
        self.base_url = (base_url or API_URL).rstrip("/")
        self.ssl = ssl.create_default_context(cafile=certifi.where())
        self.ssl.load_verify_locations(RU_ROOT_CA)
        self._session: aiohttp.ClientSession | None = None

    async def call(self, method: str, path: str, *, params: dict | None = None,
                   json: dict | None = None, timeout: float = 30) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"Authorization": self.token})
        params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        async with self._session.request(
            method, self.base_url + path, params=params, json=json, ssl=self.ssl,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:  # например, HTML-страница прокси при сбое
                data = {"error": (await resp.text())[:200]}
            if resp.status != 200:
                raise MaxError(f"{method} {path}: {resp.status} {data}")
        return data

    async def me(self) -> dict:
        return await self.call("GET", "/me")

    async def updates(self, marker: int | None, timeout: int = 30) -> tuple[list[dict], int | None]:
        """Long polling: ждём новые события до timeout секунд."""
        data = await self.call("GET", "/updates", timeout=timeout + 15, params={
            "marker": marker, "timeout": timeout, "limit": 100,
            "types": "message_created,message_callback,bot_started",
        })
        return data.get("updates", []), data.get("marker")

    async def send(self, user_id: int, text: str, buttons: list[list[dict]] | None = None,
                   html: bool = True) -> dict:
        data = await self.call("POST", "/messages", params={"user_id": user_id},
                               json=message_body(text, buttons, html))
        return data["message"]

    async def answer(self, callback_id: str, *, text: str | None = None,
                     buttons: list[list[dict]] | None = None, notification: str | None = None) -> None:
        """Ответ на нажатие кнопки: заменить сообщение с кнопками и/или показать уведомление."""
        body: dict = {}
        if text is not None:
            body["message"] = message_body(text, buttons)
        if notification:
            body["notification"] = notification
        await self.call("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def typing(self, chat_id: int) -> None:
        await self.call("POST", f"/chats/{chat_id}/actions", json={"action": "typing_on"})

    async def set_commands(self, commands: dict[str, str]) -> None:
        await self.call("PATCH", "/me/commands", json={
            "commands": [{"name": name, "description": text} for name, text in commands.items()],
        })

    async def close(self) -> None:
        if self._session:
            await self._session.close()
