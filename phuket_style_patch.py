# -*- coding: utf-8 -*-
"""Editorial style patch for PhuketMirror posts.

Keeps the mirror's factual/numeric validation unchanged while making the public
caption follow the Cozy Asia Phuket house style and contact block.
"""
from __future__ import annotations

import json
import os

_applied = False


def apply():
    global _applied
    if _applied:
        return

    import phuket_mirror
    from openai import OpenAI

    def styled_rewrite_sync(body: str, max_chars: int) -> dict:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        client = OpenAI(
            api_key=api_key,
            project=os.environ.get("OPENAI_PROJECT", "").strip() or None,
            organization=os.environ.get("OPENAI_ORG", "").strip() or None,
            timeout=45,
        )

        body_limit = max(500, max_chars - 500)
        system = f"""
Ты редактор Telegram-канала Cozy Asia о покупке недвижимости на Пхукете.
На входе публикация другого агентства. Верни ТОЛЬКО JSON.

ФИЛЬТР:
1) publish=true только если материал относится к недвижимости Пхукета и полезен покупателю/инвестору:
   новый проект, конкретный объект/лот, условия застройщика, подборка проектов или рыночная аналитика.
2) Самуи-only, аренду-only, вакансии, мероприятия и посторонний контент пропускай.

ФАКТЫ — ЖЁСТКО:
3) Полностью перепиши рекламные и описательные формулировки своим языком, но НИКОГДА не меняй и не придумывай факты.
4) Нельзя менять, округлять, пересчитывать или дополнять: цены, валюты, площади, проценты, сроки, даты,
   расстояния, этажность, количество объектов, графики платежей, доходность, ownership/freehold/leasehold,
   названия проектов и застройщиков.
5) ВСЕ числовые факты исходного содержательного текста должны сохраниться. Не добавляй никаких новых чисел,
   даже в типовых фразах вроде 24/7, если этого числа не было в исходнике.
6) Не копируй контакты, промокоды, ссылки, название агентства и CTA источника.

СТИЛЬ COZY ASIA — ориентир на такой визуальный формат:
7) Заголовок яркий, премиальный, с эмодзи. Для проекта/лота/подборки обычно:
   «🔱📈 Инвестиции | Название / главный оффер 🌊✨».
   Для аналитики допустимо «🔱📊 Аналитика | ...».
8) После заголовка — 1–2 коротких вводных абзаца. Пиши живо, но без пустых рекламных обещаний.
9) Используй разделитель «⸻» между крупными блоками.
10) Используй только те смысловые блоки, для которых в исходнике реально есть данные. Подходящие названия:
   «🏡 Инфраструктура и окружение»
   «📐 Планировки и параметры»
   «💰 Цены и условия покупки»
   «💎 Инвестиционная ценность»
   «📍 Локация»
   Не добавляй инфраструктуру, преимущества, семейность, безопасность, доходность и т.п., если источник этого не подтверждает.
11) Внутри блоков — короткие строки/списки с уместными эмодзи, как в хорошем Telegram-посте.
12) Русский язык. Основной текст без нашего CTA и без блока контактов. Максимум {body_limit} символов для title+text.
13) В hashtags дай 8–14 релевантных хэштегов одной строкой. Используй общие теги и только подтверждённые исходником
   район/проект/тип объекта. Предпочтительно нижний регистр. Не добавляй цифры в хэштеги.
14) Не говори, что это рерайт, копия или материал другого канала.

JSON:
{{
  "publish": true/false,
  "kind": "listing|project|analytics|selection|other",
  "title": "готовый заголовок с эмодзи",
  "text": "готовый структурированный основной текст",
  "hashtags": "#phuket #пхукет ..."
}}
""".strip()

        resp = client.chat.completions.create(
            model=phuket_mirror.MODEL,
            temperature=0.35,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": body},
            ],
            max_tokens=2200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        return {
            "publish": bool(data.get("publish")),
            "kind": str(data.get("kind") or "other").strip().lower(),
            "title": str(data.get("title") or "").strip(),
            "text": str(data.get("text") or "").strip(),
            "hashtags": str(data.get("hashtags") or "").strip(),
        }

    def styled_final_text(data: dict) -> str:
        title = str(data.get("title") or "").strip()
        body = str(data.get("text") or "").strip()
        contact = os.environ.get("PHUKET_CONTACT", "@cozy_asia").strip() or "@cozy_asia"
        phone = os.environ.get("PHUKET_PHONE", "+7(993)-999-1133").strip() or "+7(993)-999-1133"
        tags = str(data.get("hashtags") or "").strip()
        if not tags:
            tags = "#phuket #пхукет #thailand #таиланд #недвижимостьпхукет #инвестиции #апартаменты #realestate"

        footer = (
            "📩 Хотите получить подбор доступных лотов, цены, планировки и рекомендации "
            "под вашу задачу (для жизни / аренды / инвестиции)? Напишите нам 👇\n\n"
            "Контакты:\n"
            f"📲 Telegram: {contact}\n"
            f"📞 WhatsApp / MAX: {phone}"
        )
        return f"{title}\n\n{body}\n\n⸻\n\n{footer}\n\n{tags}".strip()

    phuket_mirror._rewrite_sync = styled_rewrite_sync
    phuket_mirror._final_text = styled_final_text
    _applied = True
