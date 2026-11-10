from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from app.config import settings

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def split_text(text, limit=3900):
    chunks, current = [], ""
    for line in text.splitlines():
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

async def publish_report(report_date, items):
    bot = Bot(settings.telegram_bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        lines = ["📰 <b>گزارش روزانه تحولات خاورمیانه</b>", f"📅 {report_date}", ""]
        if not items:
            lines.append("در بازه موردنظر خبر واجد شرایطی ثبت نشده است.")
        for i, item in enumerate(items, 1):
            lines += [f"<b>{i}. {esc(item['headline'])}</b>", esc(item["summary"]),
                f"<i>منبع: {esc(item['source'])} | وضعیت: {esc(item['verification_status'])}</i>",
                f'<a href="{esc(item["url"])}">لینک منبع</a>', ""]
        destinations = [settings.telegram_channel_id, str(settings.telegram_admin_chat_id)]
        sent = []
        for chat_id in destinations:
            for part in split_text("\n".join(lines)):
                msg = await bot.send_message(chat_id=chat_id, text=part, disable_web_page_preview=True)
                sent.append({"chat_id": chat_id, "message_id": msg.message_id})
        return sent
    finally:
        await bot.session.close()
