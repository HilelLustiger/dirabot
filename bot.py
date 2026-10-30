"""Telegram adapter for the apartment-hunt bot."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import tempfile
from datetime import time as dtime

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core
import llm
from core import STATUSES, TRANSITIONS, TZ, fmt_when, label

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = int(os.environ["ALLOWED_CHAT_ID"]) if os.environ.get("ALLOWED_CHAT_ID") else None
DIGEST_TIME = os.environ.get("DIGEST_TIME", "08:30")
DB_PATH = os.environ.get("DB_PATH") or ("/data/apartments.db" if os.path.isdir("/data") else "apartments.db")

store = core.Store(DB_PATH)

# ================================================================ formatting

def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def title(apt: dict) -> str:
    return apt.get("address") or apt.get("neighborhood") or f"דירה #{apt['id']}"


def fmt_price(p) -> str:
    return f"{p:,}₪" if p else ""


def fmt_rooms(r) -> str:
    if not r:
        return ""
    return f"{int(r) if float(r).is_integer() else r} חד׳"


def who(update: Update) -> str:
    u = update.effective_user
    return (u.first_name or u.username or str(u.id)) if u else "?"


def full_title(apt: dict) -> str:
    """Card headline: address, neighborhood, city (whatever we have)."""
    parts = [apt.get(k) for k in ("address", "neighborhood", "city") if apt.get(k)]
    return ", ".join(parts) if parts else f"דירה #{apt['id']}"


def status_line(apt: dict) -> str:
    emoji, name = STATUSES[apt["status"]]
    when = fmt_when(apt.get("viewing_at"))
    if apt["status"] == "scheduled" and when:
        return f"{emoji} <b>{name}</b> ליום {when}"
    if apt["status"] in ("seen", "want") and when:
        return f"{emoji} <b>{name}</b> (ביקור ביום {when})"
    if apt["status"] == "new":
        return f"{emoji} <b>{name}</b>, עוד לא יצרנו קשר"
    return f"{emoji} <b>{name}</b>"


def card_text(apt: dict) -> str:
    lines = [f"<b>{esc(full_title(apt))}</b>"]
    contact = " ".join(x for x in (apt.get("contact_name"), apt.get("phone")) if x)
    facts = " · ".join(x for x in (fmt_price(apt.get("price")), fmt_rooms(apt.get("rooms")), esc(contact)) if x)
    if facts:
        lines.append(facts)
    lines += ["", status_line(apt)]
    notes = store.notes(apt["id"], limit=10)
    if notes:
        body = "\n".join(f"<i>{esc(n['user'])}:</i> {esc(n['value'][:300])}" for n in notes)
        lines.append(f"<blockquote expandable>{body}</blockquote>")
    footer = f"<code>#{apt['id']}</code>"
    if apt.get("source"):
        footer += f" · {esc(apt['source'])}"
    lines.append(footer)
    if store.missing_details(apt):
        lines.append("<i>↩️ חסרים פרטים: עשו Reply לכרטיס עם טקסט המודעה</i>")
    return "\n".join(lines)


def card_keyboard(apt: dict) -> InlineKeyboardMarkup:
    if apt["status"] in core.FINAL:
        rows = [[InlineKeyboardButton("↩️ החזר לטיפול", callback_data=f"re:{apt['id']}")]]
    else:
        buttons = []
        for st in TRANSITIONS[apt["status"]]:
            text = "📅 שינוי מועד" if (st == "scheduled" and apt["status"] == "scheduled") else label(st)
            buttons.append(InlineKeyboardButton(text, callback_data=f"st:{apt['id']}:{st}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    bottom = [InlineKeyboardButton("✏️ תיקון פרטים", callback_data=f"ed:{apt['id']}")]
    if apt.get("url"):
        bottom.append(InlineKeyboardButton("🔗 פתיחת המודעה", url=apt["url"]))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def list_line(apt: dict) -> str:
    parts = [f"<b>#{apt['id']}</b> {esc(title(apt))}"]
    for x in (fmt_price(apt.get("price")), fmt_rooms(apt.get("rooms"))):
        if x:
            parts.append(x)
    if apt["status"] == "scheduled" and apt.get("viewing_at"):
        parts.append(f"📅 {fmt_when(apt['viewing_at'])}")
    return " · ".join(parts)


def grouped(apts: list[dict]) -> str:
    out = []
    for st in core.ORDER:
        group = [a for a in apts if a["status"] == st]
        if group:
            out.append(f"\n<b>{label(st)} ({len(group)})</b>")
            out += [list_line(a) for a in group]
    return "\n".join(out).strip()


async def send_long(bot, chat_id: int, text: str):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ================================================================ card I/O

async def send_card(bot, chat_id: int, apt_id: int, reply_to: int | None = None) -> Message:
    apt = store.get(apt_id)
    msg = await bot.send_message(
        chat_id,
        card_text(apt),
        parse_mode=ParseMode.HTML,
        reply_markup=card_keyboard(apt),
        disable_web_page_preview=True,
        reply_to_message_id=reply_to,
    )
    store.register_msg(chat_id, msg.message_id, apt_id, "card")
    return msg


async def refresh_cards(bot, apt_id: int):
    apt = store.get(apt_id)
    text, kb = card_text(apt), card_keyboard(apt)
    for chat_id, msg_id in store.cards_of(apt_id):
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id, parse_mode=ParseMode.HTML,
                reply_markup=kb, disable_web_page_preview=True,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.info("could not edit card %s/%s: %s", chat_id, msg_id, e)


async def log_line(bot, chat_id: int, apt_id: int, text: str):
    msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    store.register_msg(chat_id, msg.message_id, apt_id, "log")


async def ask_when(bot, chat_id: int, apt_id: int, retry: bool = False, reply_to: int | None = None):
    apt = store.get(apt_id)
    q = ("לא הצלחתי להבין מתי 🤔 נסו למשל: <i>ראשון 18:00</i> או <i>3.11 17:30</i>"
         if retry else f"📅 מתי הביקור ב<b>{esc(title(apt))}</b>? (#{apt_id})\nעשו Reply, למשל: <i>ראשון 18:00</i>")
    msg = await bot.send_message(
        chat_id, q, parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="ראשון 18:00"),
        reply_to_message_id=reply_to,
    )
    store.register_msg(chat_id, msg.message_id, apt_id, "ask_when")


FIX_PROMPT = ("✏️ מה לתקן ב<b>{title}</b>? (#{id})\n"
              "עשו Reply, שורה לכל שדה, למשל:\n"
              "<code>עיר: תל אביב</code>\n"
              "<code>שכונה: נווה צדק</code>\n"
              "או במילים: <i>זה ברמת גן, לא בתל אביב</i>\n"
              "שדות: כתובת, שכונה, עיר, מחיר, חדרים, איש קשר, טלפון. כדי למחוק שדה כתבו <code>-</code>")


async def ask_fix(bot, chat_id: int, apt_id: int, retry: bool = False, reply_to: int | None = None):
    apt = store.get(apt_id)
    q = ("לא הבנתי מה לתקן 🤔 נסו שורה כמו <code>שכונה: נווה צדק</code>" if retry
         else FIX_PROMPT.format(title=esc(full_title(apt)), id=apt_id))
    msg = await bot.send_message(
        chat_id, q, parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="שכונה: נווה צדק"),
        reply_to_message_id=reply_to,
    )
    store.register_msg(chat_id, msg.message_id, apt_id, "ask_fix")


FIX_PREFIX_RE = re.compile(r"^\s*(תיקון|לתקן|תקן)\s*[:\-]?\s*")


async def apply_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE, apt_id: int, text: str) -> bool:
    """Apply a correction to an apartment. Returns False if nothing could be understood."""
    msg = update.effective_message
    apt = store.get(apt_id)
    raw = core.parse_field_lines(text)
    if not raw:
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        raw = await llm.extract_fix(apt, FIX_PREFIX_RE.sub("", text)) or {}
    fields, bad = {}, []
    for k, v in raw.items():
        try:
            fields[k] = core.clean_value(k, v)
        except ValueError:
            bad.append(core.FIELD_NAMES[k])
    if not fields:
        if bad:
            await msg.reply_text(f"לא הצלחתי לקרוא את הערך של: {', '.join(bad)}")
            return True
        return False
    changed = store.update_fields(apt_id, fields, who(update))
    if not changed:
        await msg.reply_text("הפרטים כבר כאלה, לא שיניתי כלום.")
        return True
    await refresh_cards(ctx.bot, apt_id)
    shown = []
    for k, v in changed.items():
        if v is None:
            shown.append(f"{core.FIELD_NAMES[k]}: נמחק")
        else:
            shown.append(f"{core.FIELD_NAMES[k]}: {fmt_price(v) if k == 'price' else fmt_rooms(v) if k == 'rooms' else v}")
    note = f"\nלא הצלחתי לקרוא: {', '.join(bad)}" if bad else ""
    await msg.reply_text("✏️ עודכן\n" + "\n".join(shown) + note)
    return True


async def react(msg: Message, emoji: str = "👍"):
    try:
        await msg.set_reaction(emoji)
    except Exception:
        await msg.reply_text("✅ נשמר")


# ================================================================ message handling

def message_urls(msg: Message) -> list[str]:
    urls = []
    ents = msg.parse_entities([MessageEntity.URL, MessageEntity.TEXT_LINK]) if msg.text else \
        msg.parse_caption_entities([MessageEntity.URL, MessageEntity.TEXT_LINK])
    for ent, txt in ents.items():
        urls.append(ent.url if ent.type == MessageEntity.TEXT_LINK else txt)
    for u in core.find_urls(msg.text or msg.caption or ""):
        if u not in urls:
            urls.append(u)
    return urls


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    text = msg.text or msg.caption or ""
    chat_id = msg.chat_id

    # 1) Reply to one of the bot's messages
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == ctx.bot.id:
        hit = store.lookup_msg(chat_id, msg.reply_to_message.message_id)
        if hit:
            await handle_reply(update, ctx, hit[0], hit[1], text)
        return

    # 2) New listing: has a link, or was forwarded
    urls = message_urls(msg)
    if urls or msg.forward_origin:
        await handle_new(update, ctx, urls, text)
    # 3) anything else: regular chat, ignore


async def handle_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE, urls: list[str], text: str):
    msg = update.effective_message
    chat_id = msg.chat_id
    url = core.normalize_url(urls[0]) if urls else None

    if url:
        existing = store.by_url(url)
        if existing:
            await msg.reply_text(f"👀 כבר יש לנו את זה (#{existing['id']}, {label(existing['status'])})")
            await send_card(ctx.bot, chat_id, existing["id"])
            return

    body = text
    for u in urls:
        body = body.replace(u, "")
    fields = None
    if len(body.strip()) > 40:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        fields = await llm.extract_listing(text)
        if not url and fields is not None and not fields.get("is_listing"):
            return  # forwarded something that isn't an ad

    apt_id = store.create(url=url, raw_text=text or None, fields=fields, user=who(update))
    await send_card(ctx.bot, chat_id, apt_id, reply_to=msg.message_id)


async def handle_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, apt_id: int, kind: str, text: str):
    msg = update.effective_message
    chat_id = msg.chat_id
    user = who(update)
    apt = store.get(apt_id)
    if not apt or not text.strip():
        return

    # Answer to "when is the viewing?"
    if kind == "ask_when":
        when = core.parse_when(text) or await llm.parse_when(text)
        if not when:
            await ask_when(ctx.bot, chat_id, apt_id, retry=True, reply_to=msg.message_id)
            return
        try:
            store.set_status(apt_id, "scheduled", user, viewing_at=when)
        except core.TransitionError:
            await msg.reply_text(f"הסטטוס של #{apt_id} כבר השתנה ל{label(apt['status'])}, לא עדכנתי.")
            return
        await refresh_cards(ctx.bot, apt_id)
        await log_line(ctx.bot, chat_id, apt_id,
                       f"📅 {esc(user)}: קבענו ל<b>{fmt_when(when)}</b> — {esc(title(apt))} (#{apt_id})")
        return

    # Answer to "what to fix?", or a reply that starts with "תיקון" / has "שדה: ערך" lines
    if kind == "ask_fix" or FIX_PREFIX_RE.match(text) or core.is_fix_text(text):
        if not await apply_fix(update, ctx, apt_id, text):
            await ask_fix(ctx.bot, chat_id, apt_id, retry=True, reply_to=msg.message_id)
        return

    # Reply with a link to an apartment that has none
    urls = message_urls(msg)
    if urls and not apt.get("url"):
        store.set_url(apt_id, core.normalize_url(urls[0]))

    # Reply that looks like ad text -> fill details; otherwise -> note
    if len(text) > 150 or store.missing_details(apt):
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        fields = await llm.extract_listing(text)
        if fields and fields.get("is_listing"):
            filled = store.fill_details(apt_id, fields, text, user)
            await refresh_cards(ctx.bot, apt_id)
            await msg.reply_text("✅ השלמתי פרטים בכרטיס" if filled else "לא מצאתי פרטים חדשים, שמרתי את הטקסט.")
            return

    store.add_note(apt_id, text.strip(), user)
    await refresh_cards(ctx.bot, apt_id)
    await react(msg)


# ================================================================ buttons

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    user = who(update)
    parts = q.data.split(":")
    apt_id = int(parts[1])
    apt = store.get(apt_id)
    if not apt:
        await q.answer("לא נמצא")
        return

    if parts[0] == "ed":
        await q.answer()
        await ask_fix(ctx.bot, chat_id, apt_id)
        return

    if parts[0] == "re":
        try:
            prev = store.reopen(apt_id, user)
        except core.TransitionError:
            await q.answer("כבר פתוח")
            await refresh_cards(ctx.bot, apt_id)
            return
        await q.answer()
        await refresh_cards(ctx.bot, apt_id)
        await log_line(ctx.bot, chat_id, apt_id,
                       f"↩️ {esc(user)}: החזיר/ה לטיפול ({label(prev)}) — {esc(title(apt))} (#{apt_id})")
        return

    status = parts[2]
    if status not in TRANSITIONS[apt["status"]]:
        await q.answer(f"הסטטוס כבר {STATUSES[apt['status']][1]}")
        await refresh_cards(ctx.bot, apt_id)
        return

    if status == "scheduled":
        await q.answer()
        await ask_when(ctx.bot, chat_id, apt_id)
        return

    store.set_status(apt_id, status, user)
    await q.answer(label(status))
    await refresh_cards(ctx.bot, apt_id)
    await log_line(ctx.bot, chat_id, apt_id,
                   f"{STATUSES[status][0]} {esc(user)}: {STATUSES[status][1]} — {esc(title(apt))} (#{apt_id})")


# ================================================================ commands

HELP = f"""<b>🏠 מה אפשר לעשות</b>

<b>➕ דירה חדשה</b>
שלחו קישור למודעה, רצוי עם הטקסט שלה באותה הודעה, או העבירו (Forward) מודעה.

<b>🔄 עדכון סטטוס</b>
כפתורים בכרטיס. כשקובעים ביקור, אשאל מתי.

<b>✏️ תיקון פרטים</b>
כפתור "✏️ תיקון פרטים" בכרטיס, או Reply לכרטיס עם שורה כמו:
<code>עיר: תל אביב</code>
<code>שכונה: נווה צדק</code>
אפשר גם במילים: <i>תיקון: זה ברמת גן</i>

<b>📝 הערה</b>
Reply לכרטיס עם כל טקסט אחר.

<b>📋 פקודות</b>
/open · כל מה שפתוח, לפי סטטוס
/week · ביקורים בשבוע הקרוב
/all · הכול, כולל מה שדחינו
/card 12 · להקפיץ את כרטיס 12
/digest · סיכום עכשיו (נשלח לבד כל בוקר ב-{DIGEST_TIME})
/export · גיבוי של מסד הנתונים
/help · לשלוח ולנעוץ מחדש את ההודעה הזו"""

COMMANDS = [
    ("open", "כל מה שפתוח"),
    ("week", "ביקורים בשבוע הקרוב"),
    ("all", "הכול, כולל מה שדחינו"),
    ("card", "להקפיץ כרטיס, למשל /card 12"),
    ("digest", "סיכום עכשיו"),
    ("export", "גיבוי של מסד הנתונים"),
    ("help", "מה אפשר לעשות"),
]


async def pin_help(bot, chat_id: int, force_new: bool = False) -> bool:
    """Keep one pinned help message per chat. Edits it in place when HELP changes;
    with force_new, sends a fresh one at the bottom. Returns False if pinning failed."""
    # meta value: "<msg_id>|<help digest>|<1 if pinned else 0>"
    key = f"help:{chat_id}"
    digest = hashlib.sha1(HELP.encode()).hexdigest()[:12]
    parts = (store.get_meta(key) or "").split("|")
    old_id = int(parts[0]) if parts[0] else None
    old_digest = parts[1] if len(parts) > 1 else None
    pinned = len(parts) > 2 and parts[2] == "1"

    async def pin(msg_id: int) -> bool:
        try:
            await bot.pin_chat_message(chat_id, msg_id, disable_notification=True)
            return True
        except TelegramError as e:
            log.info("could not pin help: %s", e)
            return False

    if old_id and not force_new:
        current = old_digest == digest
        if not current:
            try:
                await bot.edit_message_text(HELP, chat_id=chat_id, message_id=old_id, parse_mode=ParseMode.HTML)
                current = True
            except BadRequest as e:
                current = "not modified" in str(e).lower()
                if not current:
                    log.info("could not edit pinned help: %s", e)
        if current:
            # e.g. the bot was made admin after it first tried to pin
            pinned = pinned or await pin(old_id)
            store.set_meta(key, f"{old_id}|{digest}|{int(pinned)}")
            return pinned

    msg = await bot.send_message(chat_id, HELP, parse_mode=ParseMode.HTML)
    if old_id:
        try:
            await bot.unpin_chat_message(chat_id, message_id=old_id)
        except TelegramError:
            pass
    pinned = await pin(msg.message_id)
    store.set_meta(key, f"{msg.message_id}|{digest}|{int(pinned)}")
    return pinned


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await pin_help(ctx.bot, update.effective_chat.id, force_new=True):
        await update.effective_message.reply_text(
            "כדי שאוכל לנעוץ את ההודעה, הפכו אותי למנהל בקבוצה עם הרשאה לנעיצת הודעות.")


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"chat id: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apts = store.list([s for s in core.ORDER if s != "rejected"])
    await send_long(ctx.bot, update.effective_chat.id, grouped(apts) or "אין כרגע דירות פתוחות.")


async def cmd_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_long(ctx.bot, update.effective_chat.id, grouped(store.list()) or "עוד לא נוספו דירות.")


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    apts = store.upcoming(7)
    text = "<b>📅 ביקורים בשבוע הקרוב</b>\n" + "\n".join(list_line(a) for a in apts) if apts \
        else "אין ביקורים קבועים בשבוע הקרוב."
    await send_long(ctx.bot, update.effective_chat.id, text)


async def cmd_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        apt_id = int(ctx.args[0].lstrip("#"))
    except (IndexError, ValueError):
        await update.effective_message.reply_text("שימוש: /card 12")
        return
    if not store.get(apt_id):
        await update.effective_message.reply_text(f"אין דירה #{apt_id}")
        return
    await send_card(ctx.bot, update.effective_chat.id, apt_id)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, f"apartments-{core.now().strftime('%Y%m%d-%H%M')}.db")
        store.backup_to(path)
        with open(path, "rb") as f:
            await ctx.bot.send_document(update.effective_chat.id, f, filename=os.path.basename(path))


# ================================================================ digest

def build_digest() -> tuple[str, InlineKeyboardMarkup | None] | None:
    d = store.digest()
    sections, rows = [], []

    def sec(head, apts):
        if apts:
            sections.append(f"<b>{head}</b>\n" + "\n".join(list_line(a) for a in apts))

    sec("📅 ביקורים היום", d["today"])
    sec("🗓 ביקורים מחר", d["tomorrow"])
    if d["passed"]:
        sec("⏰ ביקורים שעברו, מה היה?", d["passed"])
        for a in d["passed"]:
            rows.append([
                InlineKeyboardButton(f"#{a['id']} 👀 ראינו", callback_data=f"st:{a['id']}:seen"),
                InlineKeyboardButton(f"#{a['id']} ❌ דחינו", callback_data=f"st:{a['id']}:rejected"),
            ])
    sec("🆕 מחכות ליצירת קשר", d["new_stale"])
    sec("📵 לא ענו, לנסות שוב?", d["no_answer_stale"])
    sec("🤔 ראינו ועוד לא החלטנו", d["seen_stale"])
    for a in d["seen_stale"]:
        rows.append([
            InlineKeyboardButton(f"#{a['id']} ❤️ רוצים", callback_data=f"st:{a['id']}:want"),
            InlineKeyboardButton(f"#{a['id']} ❌ דחינו", callback_data=f"st:{a['id']}:rejected"),
        ])
    if not sections:
        return None
    return "☀️ <b>בוקר טוב! מצב חיפוש הדירה</b>\n\n" + "\n\n".join(sections), \
        (InlineKeyboardMarkup(rows) if rows else None)


async def send_digest(bot, chat_id: int, empty_msg: bool = False):
    res = build_digest()
    if not res:
        if empty_msg:
            await bot.send_message(chat_id, "אין כרגע שום דבר שדורש תשומת לב 👌")
        return
    text, kb = res
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                           disable_web_page_preview=True)


async def digest_job(ctx: ContextTypes.DEFAULT_TYPE):
    await send_digest(ctx.bot, ctx.job.chat_id)


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_digest(ctx.bot, update.effective_chat.id, empty_msg=True)


# ================================================================ main

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("handler error", exc_info=ctx.error)


async def setup_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "הבוט עוד לא מוגדר. הריצו /chatid בקבוצה והגדירו את ALLOWED_CHAT_ID.")


async def post_init(app: Application):
    await app.bot.set_my_commands(COMMANDS)
    if ALLOWED is not None:
        try:
            await pin_help(app.bot, ALLOWED)
        except TelegramError as e:
            log.warning("could not send help to %s: %s", ALLOWED, e)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    if ALLOWED is None:
        log.warning("ALLOWED_CHAT_ID not set: setup mode, only /chatid works")
        app.add_handler(CommandHandler(["start", "help"], setup_mode))
    else:
        chat = filters.Chat(ALLOWED)
        app.add_handler(CommandHandler(["start", "help"], cmd_help, filters=chat))
        app.add_handler(CommandHandler("open", cmd_open, filters=chat))
        app.add_handler(CommandHandler("all", cmd_all, filters=chat))
        app.add_handler(CommandHandler("week", cmd_week, filters=chat))
        app.add_handler(CommandHandler("card", cmd_card, filters=chat))
        app.add_handler(CommandHandler("export", cmd_export, filters=chat))
        app.add_handler(CommandHandler("digest", cmd_digest, filters=chat))
        app.add_handler(CallbackQueryHandler(on_button))
        app.add_handler(MessageHandler(chat & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))

        h, m = map(int, DIGEST_TIME.split(":"))
        app.job_queue.run_daily(digest_job, time=dtime(h, m, tzinfo=TZ), chat_id=ALLOWED, name="digest")
        log.info("digest scheduled daily at %s Israel time", DIGEST_TIME)

    app.add_error_handler(on_error)
    log.info("DB at %s | LLM %s", DB_PATH, "on" if llm.enabled() else "OFF (no ANTHROPIC_API_KEY)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
