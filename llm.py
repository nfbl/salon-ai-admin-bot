"""ИИ-ассистент: отвечает на вопросы клиентов строго по базе знаний салона.

Провайдер выбирается в .env (LLM_PROVIDER):
  openai    — OpenAI и любые совместимые API (Gemini, DeepSeek, OpenRouter...)
  anthropic — Claude
  gigachat  — GigaChat (Сбер)
  none      — без ИИ, простой поиск по FAQ
"""
import logging
import re

from config import Settings
from salon import Salon

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — вежливый администратор салона красоты «{name}» и отвечаешь клиентам в Telegram.

Правила:
- Отвечай только на основе базы знаний ниже. Никогда не выдумывай цены, услуги, акции, мастеров, правила и условия.
- Список услуг ниже полный: если клиент спрашивает об услуге, которой в нём нет, скажи, что такой услуги в салоне нет, и предложи похожие из списка, если они есть.
- Если прямого ответа в базе знаний нет (например, про животных, напитки, бренды косметики) — не делай предположений и не отвечай ни «да», ни «нет». Скажи, что это точно подскажет администратор, и предложи нажать кнопку «Спросить администратора» под сообщением.
- Не упоминай «базу знаний» и «информацию в базе» — говори естественно, как администратор салона.
- Пиши коротко (до 4–5 предложений), дружелюбно, на «вы», без markdown-разметки и без эмодзи.
- Если клиент хочет записаться — предложи нажать кнопку «Записаться» под сообщением.
- На вопросы, не связанные с салоном, мягко возвращай разговор к услугам салона.

База знаний:
{kb}"""

FALLBACK = (
    "Не нашёл точного ответа. Нажмите «Спросить администратора» — передам вопрос человеку. "
    "Или нажмите «Записаться», чтобы выбрать удобное время."
)

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "gigachat": "GigaChat",
}


class Assistant:
    def __init__(self, settings: Settings, salon: Salon):
        self.provider = settings.llm_provider
        self.model = settings.llm_model or DEFAULT_MODELS.get(self.provider, "")
        # Для «думающих» моделей (gpt-oss и др.): low — быстрее и дешевле
        self.extra_body = (
            {"reasoning_effort": settings.llm_reasoning_effort}
            if settings.llm_reasoning_effort else None
        )
        self.salon = salon
        self.system = SYSTEM_PROMPT.format(name=salon.name, kb=salon.knowledge_base())
        self.client = self._make_client(settings)
        self._faq = _split_faq(salon.faq)

    def _make_client(self, s: Settings):
        if self.provider == "none":
            return None
        if not s.llm_api_key:
            raise SystemExit(f"Для LLM_PROVIDER={self.provider} нужен LLM_API_KEY в .env")
        if self.provider == "openai":
            from openai import AsyncOpenAI
            return AsyncOpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url)
        if self.provider == "anthropic":
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=s.llm_api_key)
        if self.provider == "gigachat":
            from gigachat import GigaChat
            return GigaChat(credentials=s.llm_api_key, model=self.model, verify_ssl_certs=False)
        raise SystemExit(f"Неизвестный LLM_PROVIDER: {self.provider}")

    async def reply(self, history: list[dict]) -> str:
        # Диалог для API должен начинаться с реплики пользователя
        messages = list(history)
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        try:
            if self.provider == "none":
                return self._faq_answer(messages[-1]["content"])
            if self.provider == "openai":
                # Запас токенов: у «думающих» моделей часть уходит на рассуждения
                resp = await self.client.chat.completions.create(
                    model=self.model, temperature=0.3, max_tokens=1500,
                    messages=[{"role": "system", "content": self.system}, *messages],
                    extra_body=self.extra_body,
                )
                return (resp.choices[0].message.content or "").strip() or FALLBACK
            if self.provider == "anthropic":
                resp = await self.client.messages.create(
                    model=self.model, system=self.system, messages=messages,
                    temperature=0.3, max_tokens=500,
                )
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            if self.provider == "gigachat":
                from gigachat.models import Chat, Messages, MessagesRole
                roles = {"user": MessagesRole.USER, "assistant": MessagesRole.ASSISTANT}
                chat = Chat(
                    temperature=0.3, max_tokens=500,
                    messages=[Messages(role=MessagesRole.SYSTEM, content=self.system)]
                    + [Messages(role=roles[m["role"]], content=m["content"]) for m in messages],
                )
                resp = await self.client.achat(chat)
                return resp.choices[0].message.content.strip()
        except Exception:
            log.exception("Ошибка LLM (%s)", self.provider)
        return FALLBACK

    def _faq_answer(self, question: str) -> str:
        """Режим без ИИ: ищем раздел FAQ с наибольшим пересечением слов."""
        q = _stems(question)
        if any(w.startswith(p) for w in q for p in ("цен", "стои", "прай", "скол", "услу")):
            return "Наши услуги и цены:\n" + re.sub(r"<[^>]+>", "", self.salon.price_list())
        best, score = None, 0
        for title, body in self._faq:
            s = len(q & _stems(title + " " + body))
            if s > score:
                best, score = body, s
        return best if best else FALLBACK


def _split_faq(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return [(p.split("\n", 1)[0], p.split("\n", 1)[1].strip()) for p in parts if "\n" in p]


def _stems(text: str) -> set[str]:
    """Грубый «стемминг»: первые 4 буквы слов длиннее 3 символов."""
    return {w[:4] for w in re.findall(r"[а-яёa-z]+", text.lower()) if len(w) > 3}
