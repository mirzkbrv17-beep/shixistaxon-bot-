"""
KONKURS BOT (Aiogram 3 + SQLite)

Imkoniyatlar:
- Yagona admin (ADMIN_ID) konkurs yaratadi va kanalga avtomatik e'lon qiladi
- 3 xil rejim:
    manual   -> g'olibni admin o'zi tanlaydi (o'z kanalingiz uchun)
    random   -> tasodifiy g'olib(lar)
    referral -> eng ko'p odam taklif qilgan g'olib bo'ladi
- Majburiy obuna (bir nechta kanal)
- Referal havola, reyting, "mening natijam"
- Yakunlashda obunani qayta tekshirish (kanaldan chiqib ketganlar hisobga olinmaydi)
- Muddat tugaganda avtomatik yakunlash (manual rejimda adminga eslatma)
- Render uchun health-check web server
"""
import asyncio
import html
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("konkurs")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "konkurs.db")
PORT = int(os.getenv("PORT", "10000"))
TZ_OFFSET = 5  # Toshkent UTC+5

if not BOT_TOKEN or not ADMIN_ID:
    raise SystemExit("BOT_TOKEN va ADMIN_ID environment o'zgaruvchilarini kiriting!")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
user_r = Router()
admin_r = Router()
admin_r.message.filter(F.from_user.id == ADMIN_ID)
admin_r.callback_query.filter(F.from_user.id == ADMIN_ID)

MODES = {
    "manual": "🎯 G'olibni men tanlayman",
    "random": "🎲 Tasodifiy (random)",
    "referral": "🏆 Eng ko'p odam taklif qilgan",
}
_bot_username = None


# ───────────────────────── DB ─────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS contests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, description TEXT, mode TEXT,
                winners_count INTEGER DEFAULT 1,
                channels TEXT,            -- JSON: [{id,title,link}], 1-chi = e'lon kanali
                post_msg_id INTEGER,
                status TEXT DEFAULT 'active',
                ends_at INTEGER, notified INTEGER DEFAULT 0,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS participants(
                contest_id INTEGER, user_id INTEGER,
                username TEXT, full_name TEXT,
                invited_by INTEGER, joined_at INTEGER,
                PRIMARY KEY(contest_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS winners(
                contest_id INTEGER, user_id INTEGER,
                PRIMARY KEY(contest_id, user_id)
            );
            """
        )
        await db.commit()


async def q_all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return await cur.fetchall()


async def q_one(sql, args=()):
    rows = await q_all(sql, args)
    return rows[0] if rows else None


async def q_exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur


# ───────────────────────── Helpers ─────────────────────────
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name or "Foydalanuvchi")}</a>'


def fmt_ts(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=TZ_OFFSET)
    return dt.strftime("%d.%m.%Y %H:%M")


def parse_dt(text):
    dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
    return int((dt - timedelta(hours=TZ_OFFSET)).replace(tzinfo=timezone.utc).timestamp())


async def get_username():
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.me()).username
    return _bot_username


async def contest_link(cid, ref=None):
    u = await get_username()
    return f"https://t.me/{u}?start=c{cid}" + (f"_{ref}" if ref else "")


async def get_contest(cid):
    return await q_one("SELECT * FROM contests WHERE id=?", (cid,))


async def count_parts(cid):
    r = await q_one("SELECT COUNT(*) n FROM participants WHERE contest_id=?", (cid,))
    return r["n"]


async def is_subscribed(user_id, chat_id) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        if m.status in ("member", "administrator", "creator"):
            return True
        if m.status == "restricted" and getattr(m, "is_member", False):
            return True
        return False
    except Exception:
        return False


async def subscribed_all(user_id, channels) -> bool:
    for ch in channels:
        if not await is_subscribed(user_id, ch["id"]):
            return False
    return True


async def resolve_channel(text: str):
    text = text.strip()
    if "t.me/" in text:
        text = "@" + text.rstrip("/").split("/")[-1]
    if not text.startswith("@") and not text.lstrip("-").isdigit():
        text = "@" + text
    chat = await bot.get_chat(text)
    me = await bot.get_chat_member(chat.id, bot.id)
    if me.status not in ("administrator", "creator"):
        raise ValueError(f"Bot {chat.title} kanalida admin emas!")
    if chat.username:
        link = f"https://t.me/{chat.username}"
    else:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            link = chat.invite_link or ""
    return {"id": chat.id, "title": chat.title, "link": link}


def admin_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="➕ Yangi konkurs"), KeyboardButton(text="📋 Konkurslarim")]],
        resize_keyboard=True,
    )


# ───────────────────────── Foydalanuvchi qismi ─────────────────────────
def parse_arg(arg: str):
    try:
        body = arg[1:]
        if "_" in body:
            c, r = body.split("_", 1)
            return int(c), int(r)
        return int(body), None
    except Exception:
        return None, None


@user_r.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args
    if arg and arg.startswith("c"):
        cid, ref = parse_arg(arg)
        if cid:
            return await attempt_join(m.from_user, cid, ref, m.chat.id)
    if is_admin(m.from_user.id):
        await m.answer("👑 <b>Admin panel</b>\nKonkurs boshqaruvi:", reply_markup=admin_menu())
    else:
        await m.answer(
            "👋 Salom! Konkursda qatnashish uchun kanaldagi e'londa "
            "«🎁 Qatnashish» tugmasini bosing."
        )


async def attempt_join(user, cid, ref, chat_id):
    c = await get_contest(cid)
    if not c or c["status"] != "active":
        return await bot.send_message(chat_id, "❌ Konkurs topilmadi yoki yakunlangan.")
    channels = json.loads(c["channels"])

    missing = [ch for ch in channels if not await is_subscribed(user.id, ch["id"])]
    if missing:
        kb = InlineKeyboardBuilder()
        for ch in missing:
            if ch["link"]:
                kb.row(InlineKeyboardButton(text=f"➕ {ch['title']}", url=ch["link"]))
        kb.row(InlineKeyboardButton(text="✅ Tekshirish", callback_data=f"chk:{cid}:{ref or 0}"))
        return await bot.send_message(
            chat_id,
            f"🎁 <b>{html.escape(c['title'])}</b>\n\nQatnashish uchun quyidagi kanallarga a'zo bo'ling, "
            "so'ng «✅ Tekshirish» tugmasini bosing:",
            reply_markup=kb.as_markup(),
        )

    valid_ref = None
    if c["mode"] == "referral" and ref and ref != user.id:
        if await q_one("SELECT 1 FROM participants WHERE contest_id=? AND user_id=?", (cid, ref)):
            valid_ref = ref

    cur = await q_exec(
        "INSERT OR IGNORE INTO participants(contest_id,user_id,username,full_name,invited_by,joined_at) "
        "VALUES(?,?,?,?,?,?)",
        (cid, user.id, user.username, user.full_name, valid_ref, int(time.time())),
    )
    is_new = cur.rowcount > 0

    if is_new and valid_ref:
        n = (await q_one("SELECT COUNT(*) n FROM participants WHERE contest_id=? AND invited_by=?", (cid, valid_ref)))["n"]
        try:
            await bot.send_message(
                valid_ref,
                f"🎉 Sizning havolangiz orqali {mention(user.id, user.full_name)} qo'shildi!\n"
                f"Jami takliflaringiz: <b>{n}</b>",
            )
        except Exception:
            pass

    text = f"✅ <b>Siz konkursda qatnashyapsiz!</b>\n🎁 {html.escape(c['title'])}\n"
    kb = InlineKeyboardBuilder()
    if c["mode"] == "referral":
        link = await contest_link(cid, user.id)
        text += (
            f"\n🔗 <b>Sizning havolangiz:</b>\n{link}\n\n"
            "Do'stlaringizga yuboring — eng ko'p odam taklif qilgan g'olib bo'ladi!"
        )
        kb.row(
            InlineKeyboardButton(text="📊 Mening natijam", callback_data=f"me:{cid}"),
            InlineKeyboardButton(text="🏆 Reyting", callback_data=f"top:{cid}"),
        )
    else:
        text += "\nOmad tilaymiz! 🍀"
    await bot.send_message(chat_id, text, reply_markup=kb.as_markup() if c["mode"] == "referral" else None,
                           disable_web_page_preview=True)


@user_r.callback_query(F.data.startswith("chk:"))
async def cb_check(cb: CallbackQuery):
    _, cid, ref = cb.data.split(":")
    await cb.answer()
    await attempt_join(cb.from_user, int(cid), int(ref) or None, cb.message.chat.id)


@user_r.callback_query(F.data.startswith("me:"))
async def cb_me(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    n = (await q_one("SELECT COUNT(*) n FROM participants WHERE contest_id=? AND invited_by=?", (cid, cb.from_user.id)))["n"]
    await cb.answer(f"Siz {n} ta odam taklif qildingiz", show_alert=True)


@user_r.callback_query(F.data.startswith("top:"))
async def cb_top(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    await cb.answer()
    await bot.send_message(cb.message.chat.id, await top_text(cid))


async def top_text(cid, limit=10):
    rows = await q_all(
        "SELECT p.user_id, p.full_name, COUNT(i.user_id) cnt FROM participants p "
        "JOIN participants i ON i.contest_id=p.contest_id AND i.invited_by=p.user_id "
        "WHERE p.contest_id=? GROUP BY p.user_id ORDER BY cnt DESC LIMIT ?",
        (cid, limit),
    )
    if not rows:
        return "🏆 Hozircha reyting bo'sh."
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Reyting (TOP 10)</b>\n"]
    for i, r in enumerate(rows):
        mk = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mk} {html.escape(r['full_name'] or str(r['user_id']))} — <b>{r['cnt']}</b>")
    return "\n".join(lines)


# ───────────────────────── Admin: yaratish ─────────────────────────
class New(StatesGroup):
    title = State()
    desc = State()
    mode = State()
    winners = State()
    channel = State()
    extra = State()
    ends = State()


@admin_r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=admin_menu())


@admin_r.message(F.text == "➕ Yangi konkurs")
async def new_contest(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(New.title)
    await m.answer("📝 Konkurs <b>nomini</b> yuboring:\n\n(Bekor qilish: /cancel)")


@admin_r.message(New.title)
async def st_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await state.set_state(New.desc)
    await m.answer("📄 Konkurs <b>tavsifi / sovrinlar</b> haqida yozing:")


@admin_r.message(New.desc)
async def st_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text)
    await state.set_state(New.mode)
    kb = InlineKeyboardBuilder()
    for k, v in MODES.items():
        kb.row(InlineKeyboardButton(text=v, callback_data=f"mode:{k}"))
    await m.answer("⚙️ G'olib qanday aniqlanadi?", reply_markup=kb.as_markup())


@admin_r.callback_query(New.mode, F.data.startswith("mode:"))
async def st_mode(cb: CallbackQuery, state: FSMContext):
    await state.update_data(mode=cb.data.split(":")[1])
    await state.set_state(New.winners)
    await cb.message.edit_text("🏅 G'oliblar sonini yuboring (masalan: 1):")
    await cb.answer()


@admin_r.message(New.winners)
async def st_winners(m: Message, state: FSMContext):
    if not (m.text or "").isdigit() or not (1 <= int(m.text) <= 100):
        return await m.answer("1 dan 100 gacha son yuboring.")
    await state.update_data(winners=int(m.text))
    await state.set_state(New.channel)
    await m.answer(
        "📢 Konkurs <b>e'lon qilinadigan kanal</b>ni yuboring (@username yoki havola).\n"
        "⚠️ Bot o'sha kanalda <b>admin</b> bo'lishi shart."
    )


@admin_r.message(New.channel)
async def st_channel(m: Message, state: FSMContext):
    try:
        ch = await resolve_channel(m.text)
    except Exception as e:
        return await m.answer(f"❌ {html.escape(str(e))}\nQayta yuboring:")
    await state.update_data(channels=[ch])
    await state.set_state(New.extra)
    await m.answer(
        f"✅ Kanal: <b>{html.escape(ch['title'])}</b>\n\n"
        "➕ Qo'shimcha majburiy obuna kanallari bormi? @a @b ko'rinishida yuboring yoki <b>yo'q</b> deb yozing.\n"
        "(Bot ularda ham admin bo'lishi kerak)"
    )


@admin_r.message(New.extra)
async def st_extra(m: Message, state: FSMContext):
    data = await state.get_data()
    channels = data["channels"]
    if m.text.strip().lower() not in ("yo'q", "yoq", "yo‘q", "-", "no"):
        for part in m.text.replace(",", " ").split():
            try:
                ch = await resolve_channel(part)
                if ch["id"] not in [c["id"] for c in channels]:
                    channels.append(ch)
            except Exception as e:
                return await m.answer(f"❌ {html.escape(part)}: {html.escape(str(e))}\nQaytadan yuboring:")
    await state.update_data(channels=channels)
    await state.set_state(New.ends)
    await m.answer(
        "⏰ Tugash vaqti (Toshkent vaqti): <code>10.11.2026 18:00</code>\n"
        "yoki muddat kerak bo'lmasa <b>yo'q</b> deb yozing (o'zingiz yakunlaysiz)."
    )


@admin_r.message(New.ends)
async def st_ends(m: Message, state: FSMContext):
    ends = None
    if m.text.strip().lower() not in ("yo'q", "yoq", "yo‘q", "-", "no"):
        try:
            ends = parse_dt(m.text)
        except Exception:
            return await m.answer("Format xato. Masalan: <code>10.11.2026 18:00</code>")
    d = await state.get_data()
    await state.clear()
    cur = await q_exec(
        "INSERT INTO contests(title,description,mode,winners_count,channels,ends_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (d["title"], d["desc"], d["mode"], d["winners"], json.dumps(d["channels"]), ends, int(time.time())),
    )
    cid = cur.lastrowid
    post_ch = d["channels"][0]
    extra = d["channels"][1:]
    text = (
        f"🎁 <b>{html.escape(d['title'])}</b>\n\n{html.escape(d['desc'])}\n\n"
        f"🏅 G'oliblar soni: <b>{d['winners']}</b>\n"
        f"⚙️ Aniqlash: {MODES[d['mode']]}\n"
    )
    if extra:
        text += "📌 Shart: " + ", ".join(html.escape(c["title"]) for c in extra) + " kanallariga a'zo bo'lish\n"
    if ends:
        text += f"⏰ Tugash: <b>{fmt_ts(ends)}</b>\n"
    text += "\n👇 Qatnashish uchun tugmani bosing"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎁 Qatnashish", url=await contest_link(cid)))
    try:
        msg = await bot.send_message(post_ch["id"], text, reply_markup=kb.as_markup())
        await q_exec("UPDATE contests SET post_msg_id=? WHERE id=?", (msg.message_id, cid))
        await m.answer(f"✅ Konkurs #{cid} kanalga e'lon qilindi!", reply_markup=admin_menu())
    except Exception as e:
        await m.answer(f"⚠️ Konkurs saqlandi (#{cid}), lekin kanalga yuborib bo'lmadi: {html.escape(str(e))}",
                       reply_markup=admin_menu())


# ───────────────────────── Admin: boshqaruv ─────────────────────────
@admin_r.message(F.text == "📋 Konkurslarim")
async def my_contests(m: Message, state: FSMContext):
    await state.clear()
    rows = await q_all("SELECT id,title,status FROM contests ORDER BY id DESC LIMIT 20")
    if not rows:
        return await m.answer("Hali konkurs yo'q.")
    kb = InlineKeyboardBuilder()
    for r in rows:
        ico = "🟢" if r["status"] == "active" else "🏁"
        kb.row(InlineKeyboardButton(text=f"{ico} #{r['id']} {r['title'][:35]}", callback_data=f"c:{r['id']}"))
    await m.answer("📋 Konkurslaringiz:", reply_markup=kb.as_markup())


async def detail(cid):
    c = await get_contest(cid)
    n = await count_parts(cid)
    chans = ", ".join(html.escape(x["title"]) for x in json.loads(c["channels"]))
    text = (
        f"🎁 <b>#{cid} {html.escape(c['title'])}</b>\n"
        f"Holat: {'🟢 faol' if c['status'] == 'active' else '🏁 yakunlangan'}\n"
        f"Rejim: {MODES[c['mode']]}\n"
        f"G'oliblar: {c['winners_count']}\n"
        f"Kanallar: {chans}\n"
        f"Tugash: {fmt_ts(c['ends_at'])}\n"
        f"👥 Qatnashchilar: <b>{n}</b>\n"
        f"🔗 {await contest_link(cid)}"
    )
    kb = InlineKeyboardBuilder()
    if c["status"] == "active":
        if c["mode"] == "manual":
            kb.row(InlineKeyboardButton(text="👥 Qatnashchilardan g'olib tanlash", callback_data=f"pl:{cid}:0"))
        if c["mode"] == "referral":
            kb.row(InlineKeyboardButton(text="🏆 Reyting", callback_data=f"atop:{cid}"))
        kb.row(InlineKeyboardButton(text="🏁 Yakunlash", callback_data=f"fin:{cid}"))
    else:
        ws = await q_all("SELECT w.user_id, p.full_name FROM winners w LEFT JOIN participants p "
                         "ON p.contest_id=w.contest_id AND p.user_id=w.user_id WHERE w.contest_id=?", (cid,))
        if ws:
            text += "\n\n🏅 G'oliblar:\n" + "\n".join(mention(w["user_id"], w["full_name"]) for w in ws)
    return text, kb.as_markup()


@admin_r.callback_query(F.data.startswith("c:"))
async def cb_detail(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    text, kb = await detail(cid)
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@admin_r.callback_query(F.data.startswith("atop:"))
async def cb_atop(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(await top_text(int(cb.data.split(":")[1])))


async def show_pick(cb: CallbackQuery, cid: int, page: int):
    per = 10
    parts = await q_all(
        "SELECT user_id, full_name, username FROM participants WHERE contest_id=? "
        "ORDER BY joined_at LIMIT ? OFFSET ?", (cid, per, page * per))
    total = await count_parts(cid)
    chosen = {r["user_id"] for r in await q_all("SELECT user_id FROM winners WHERE contest_id=?", (cid,))}
    kb = InlineKeyboardBuilder()
    for p in parts:
        name = (p["full_name"] or str(p["user_id"]))[:25]
        uname = f" @{p['username']}" if p["username"] else ""
        kb.row(InlineKeyboardButton(
            text=("✅ " if p["user_id"] in chosen else "▫️ ") + name + uname,
            callback_data=f"pk:{cid}:{p['user_id']}:{page}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pl:{cid}:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pl:{cid}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏁 Tanlanganlar bilan yakunlash", callback_data=f"fin:{cid}"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"c:{cid}"))
    text = (f"👥 Qatnashchilar: {total} ta | Sahifa {page + 1}\n"
            f"Tanlangan g'oliblar: <b>{len(chosen)}</b>\nG'olib qilish uchun ustiga bosing:")
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        pass


@admin_r.callback_query(F.data.startswith("pl:"))
async def cb_pl(cb: CallbackQuery):
    _, cid, page = cb.data.split(":")
    await show_pick(cb, int(cid), int(page))
    await cb.answer()


@admin_r.callback_query(F.data.startswith("pk:"))
async def cb_pk(cb: CallbackQuery):
    _, cid, uid, page = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if await q_one("SELECT 1 FROM winners WHERE contest_id=? AND user_id=?", (cid, uid)):
        await q_exec("DELETE FROM winners WHERE contest_id=? AND user_id=?", (cid, uid))
    else:
        await q_exec("INSERT OR IGNORE INTO winners VALUES(?,?)", (cid, uid))
    await show_pick(cb, cid, int(page))
    await cb.answer()


@admin_r.message(Command("find"))
async def find_user(m: Message, command: CommandObject):
    """/find <konkurs_id> <ism yoki @username> — qatnashchini qidirib g'olib qilish"""
    try:
        cid_s, q = command.args.split(" ", 1)
        cid = int(cid_s)
    except Exception:
        return await m.answer("Format: <code>/find 1 ali</code>")
    q = q.strip().lstrip("@").lower()
    rows = await q_all(
        "SELECT user_id, full_name, username FROM participants WHERE contest_id=? "
        "AND (LOWER(full_name) LIKE ? OR LOWER(username) LIKE ?) LIMIT 10", (cid, f"%{q}%", f"%{q}%"))
    if not rows:
        return await m.answer("Topilmadi.")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.row(InlineKeyboardButton(text=f"🏅 {(r['full_name'] or '')[:25]} @{r['username'] or '-'}",
                                    callback_data=f"fw:{cid}:{r['user_id']}"))
    await m.answer("Topilganlar (bosing — g'olib qilinadi):", reply_markup=kb.as_markup())


@admin_r.callback_query(F.data.startswith("fw:"))
async def cb_fw(cb: CallbackQuery):
    _, cid, uid = cb.data.split(":")
    await q_exec("INSERT OR IGNORE INTO winners VALUES(?,?)", (int(cid), int(uid)))
    await cb.answer("G'olib sifatida belgilandi ✅", show_alert=True)


@admin_r.callback_query(F.data.startswith("fin:"))
async def cb_fin(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Ha, yakunlash", callback_data=f"fok:{cid}"),
           InlineKeyboardButton(text="❌ Yo'q", callback_data=f"c:{cid}"))
    await cb.message.edit_text("🏁 Konkursni yakunlab, g'oliblarni e'lon qilamizmi?", reply_markup=kb.as_markup())
    await cb.answer()


@admin_r.callback_query(F.data.startswith("fok:"))
async def cb_fok(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    await cb.answer("Yakunlanmoqda...")
    await cb.message.edit_text("⏳ G'oliblar aniqlanmoqda, kuting...")
    res = await finish_contest(cid)
    await cb.message.edit_text(res)


# ───────────────────────── Yakunlash ─────────────────────────
async def finish_contest(cid: int) -> str:
    c = await get_contest(cid)
    if not c or c["status"] != "active":
        return "Konkurs topilmadi yoki allaqachon yakunlangan."
    channels = json.loads(c["channels"])
    need = c["winners_count"]
    winners = []  # [(uid, name, extra_text)]

    parts = await q_all("SELECT user_id, full_name, invited_by FROM participants WHERE contest_id=?", (cid,))
    names = {p["user_id"]: p["full_name"] for p in parts}

    if c["mode"] == "manual":
        rows = await q_all("SELECT user_id FROM winners WHERE contest_id=?", (cid,))
        if not rows:
            return "❌ Avval kamida bitta g'olibni tanlang (👥 Qatnashchilardan g'olib tanlash)."
        winners = [(r["user_id"], names.get(r["user_id"]), "") for r in rows]

    elif c["mode"] == "random":
        pool = [p["user_id"] for p in parts]
        random.shuffle(pool)
        for uid in pool:
            if len(winners) >= need:
                break
            if await subscribed_all(uid, channels):
                winners.append((uid, names.get(uid), ""))
            await asyncio.sleep(0.05)

    else:  # referral: faqat hali ham obunada turgan taklif qilinganlar hisoblanadi
        counts = {}
        for p in parts:
            if p["invited_by"]:
                if await subscribed_all(p["user_id"], channels):
                    counts[p["invited_by"]] = counts.get(p["invited_by"], 0) + 1
                await asyncio.sleep(0.05)
        for uid, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            if len(winners) >= need:
                break
            if await subscribed_all(uid, channels):
                winners.append((uid, names.get(uid), f" — {cnt} ta taklif"))

    await q_exec("DELETE FROM winners WHERE contest_id=?", (cid,))
    for uid, _, _ in winners:
        await q_exec("INSERT OR IGNORE INTO winners VALUES(?,?)", (cid, uid))
    await q_exec("UPDATE contests SET status='finished' WHERE id=?", (cid,))

    if winners:
        lines = [f"{i + 1}. {mention(uid, name)}{extra}" for i, (uid, name, extra) in enumerate(winners)]
        ann = (f"🏁 <b>{html.escape(c['title'])}</b> yakunlandi!\n\n🏆 <b>G'oliblar:</b>\n"
               + "\n".join(lines) + "\n\nTabriklaymiz! 🎉 Adminga murojaat qiling.")
    else:
        ann = f"🏁 <b>{html.escape(c['title'])}</b> yakunlandi.\nAfsuski, shartlarga mos g'olib topilmadi."

    try:
        await bot.send_message(channels[0]["id"], ann, reply_to_message_id=c["post_msg_id"],
                               allow_sending_without_reply=True)
    except Exception as e:
        log.warning("Kanalga e'lon yuborilmadi: %s", e)

    for uid, _, _ in winners:
        try:
            await bot.send_message(uid, f"🎉 Tabriklaymiz! Siz «{html.escape(c['title'])}» konkursida g'olib bo'ldingiz!")
        except Exception:
            pass

    return "✅ Yakunlandi!\n\n" + ann


# ───────────────────────── Avto-yakunlash ─────────────────────────
async def scheduler():
    while True:
        try:
            now = int(time.time())
            rows = await q_all(
                "SELECT id, mode, notified, title FROM contests "
                "WHERE status='active' AND ends_at IS NOT NULL AND ends_at<=?", (now,))
            for r in rows:
                if r["mode"] == "manual":
                    if not r["notified"]:
                        await q_exec("UPDATE contests SET notified=1 WHERE id=?", (r["id"],))
                        await bot.send_message(
                            ADMIN_ID,
                            f"⏰ «{html.escape(r['title'])}» (#{r['id']}) muddati tugadi.\n"
                            "G'olibni tanlab, yakunlang: 📋 Konkurslarim.")
                else:
                    res = await finish_contest(r["id"])
                    await bot.send_message(ADMIN_ID, "⏰ Avto-yakunlandi:\n\n" + res)
        except Exception as e:
            log.exception("scheduler: %s", e)
        await asyncio.sleep(30)


# ───────────────────────── Render web server ─────────────────────────
async def health(_):
    return web.Response(text="Konkurs bot ishlayapti ✅")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server port %s da ishga tushdi", PORT)


async def main():
    await init_db()
    dp.include_router(user_r)
    dp.include_router(admin_r)
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
