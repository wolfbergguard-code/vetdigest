#!/usr/bin/env python3
"""
VetDigest — приёмник оценок из Telegram (long polling).

* Нажатие 👍 / 🤔 / 👎 под карточкой в основном чате → запись в data/ratings.json,
  кнопка помечается ✅ (оценку можно поменять повторным нажатием).
* Оценки учитываются в следующем дайджесте (подсказки для Sonar).
* /chatid в любом чате, где есть бот → бот ответит id этого чата
  (нужно, чтобы узнать SECONDARY_CHAT_ID).
* /stats в основном чате → сводка оценок.

Работает постоянно как systemd-служба vetdigest-feedback.service.
"""

from __future__ import annotations

import logging
import sys
import time

import requests

import vetdigest as vd

POLL_TIMEOUT = 50


def main() -> int:
    cfg = vd.load_cfg()
    logger = vd.setup_logging("feedback")
    token, owner_chat = cfg["tg_token"], cfg["tg_chat"]
    if not token or not owner_chat:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")
        return 2

    wh = vd.tg_call(token, "getWebhookInfo", {}, logger, retries=2)
    if wh and wh["result"].get("url"):
        logger.error("У бота установлен webhook (%s): токен занят другим сервисом. "
                     "Приём оценок невозможен — нужен отдельный бот. Жду 1 час.",
                     wh["result"]["url"])
        time.sleep(3600)
        return 4

    logger.info("=== VetDigest feedback v%s запущен ===", vd.VERSION)
    offset = 0
    url = f"{vd.TG_API_ROOT}/bot{token}/getUpdates"
    while True:
        try:
            resp = requests.post(url, json={
                "offset": offset, "timeout": POLL_TIMEOUT,
                "allowed_updates": ["callback_query", "message", "channel_post",
                                    "my_chat_member"]},
                timeout=(10, POLL_TIMEOUT + 15))
        except requests.RequestException as exc:
            logger.warning("getUpdates: сеть %s", exc)
            time.sleep(10)
            continue
        if resp.status_code == 409:
            logger.error("409 Conflict: этого бота уже опрашивает другой процесс "
                         "(getUpdates/webhook). Повтор через 120 с.")
            time.sleep(120)
            continue
        if resp.status_code != 200:
            logger.warning("getUpdates HTTP %d: %s", resp.status_code, resp.text[:200])
            time.sleep(15)
            continue
        for upd in resp.json().get("result", []):
            offset = max(offset, upd["update_id"] + 1)
            try:
                handle(upd, token, owner_chat, logger)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка обработки апдейта: %s", exc)


def handle(upd: dict, token: str, owner_chat: str, logger: logging.Logger) -> None:
    if "callback_query" in upd:
        return handle_callback(upd["callback_query"], token, owner_chat, logger)

    if "my_chat_member" in upd:
        m = upd["my_chat_member"]
        chat = m.get("chat", {})
        logger.info("Статус бота в чате %s (%s, %s): %s", chat.get("id"), chat.get("type"),
                    chat.get("title") or chat.get("username") or chat.get("first_name"),
                    m.get("new_chat_member", {}).get("status"))
        return

    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat", {})
    cid = str(chat.get("id"))
    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd == "/chatid":
        logger.info("/chatid из чата %s (%s)", cid, chat.get("type"))
        vd.tg_send(token, cid, f"chat_id этого чата: <code>{cid}</code>", logger)
    elif cmd == "/stats" and cid == owner_chat:
        vd.tg_send(token, cid, stats_text(), logger)


def handle_callback(cq: dict, token: str, owner_chat: str, logger: logging.Logger) -> None:
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id"))
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "r":
        vd.tg_call(token, "answerCallbackQuery", {"callback_query_id": cq["id"]}, logger, 1)
        return
    if chat_id != owner_chat:
        vd.tg_call(token, "answerCallbackQuery",
                   {"callback_query_id": cq["id"], "text": "Оценки принимаются только от владельца"},
                   logger, 1)
        return
    _, key, val = parts
    rating = int(val) if val in ("0", "1", "2") else 1
    with vd.state_lock():
        cards = vd.load_json(vd.CARDS_FILE, {})
        ratings = vd.load_json(vd.RATINGS_FILE, {})
        card = cards.get(key, {})
        ratings[key] = {"rating": rating, "ts": int(time.time()),
                        "uid": card.get("uid"), "title": card.get("title", ""),
                        "journal": card.get("journal", ""), "axis": card.get("axis"),
                        "score": card.get("score"), "digest_date": card.get("date")}
        vd.save_json(vd.RATINGS_FILE, ratings)
    vd.tg_call(token, "editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": msg.get("message_id"),
        "reply_markup": vd.rating_keyboard(key, val)}, logger, 1)
    label = dict(vd.RATING_BUTTONS).get(val, "")
    vd.tg_call(token, "answerCallbackQuery",
               {"callback_query_id": cq["id"], "text": f"Сохранено: {label}"}, logger, 1)
    logger.info("Оценка %s → %s (%s)", key, val, card.get("title", "")[:70])


def stats_text() -> str:
    ratings = vd.load_json(vd.RATINGS_FILE, {})
    if not ratings:
        return "Оценок пока нет."
    by_axis: dict[str, list[int]] = {}
    for r in ratings.values():
        by_axis.setdefault(r.get("axis") or "?", []).append(r.get("rating", 1))
    lines = [f"<b>Оценки находок</b> · всего {len(ratings)}"]
    for ax in vd.AXIS_ORDER + ["?"]:
        v = by_axis.get(ax)
        if not v:
            continue
        lines.append(f"{vd.AXES.get(ax, 'Прочее')}: 👍 {v.count(2)} · 🤔 {v.count(1)} · 👎 {v.count(0)}")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
