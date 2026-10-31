# -*- coding: utf-8 -*-
"""
بوت الرد التلقائي - Telegram Auto-Reply Bot
ملف واحد: main.py  |  جاهز للنشر على Render

الفكرة:
- المستخدم يسجل دخوله عبر البوت (Telethon) فتُحفظ الجلسة في MongoDB.
- أي رسالة خاصة تصل لحساب المستخدم وتحتوي على كلمة مفتاحية يرد عليها الحساب تلقائيا.
- إذا كان للرد أزرار، تُرسل عبر وضع Inline الخاص بالبوت (لأن حسابات المستخدمين
  لا تستطيع إرسال أزرار مباشرة)، وتتحدث الرسالة نفسها عند الضغط على الأزرار.

مهم: فعّل وضع Inline للبوت من BotFather:  /setinline
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time

import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession

# ════════════════════════════════════════════════════════════
#  الإعدادات (استبدل القيم أو ضعها كمتغيرات بيئة في Render)
# ════════════════════════════════════════════════════════════
API_ID = int(os.getenv("API_ID", "36111125"))                       # ← API ID
API_HASH = os.getenv("API_HASH", "5bb474764d293d0e63c4e1b7539b181b")                   # ← API Hash
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://acfaaa13_db_user:wLFi9W0LOMbraIMo@cluster0.jofcmiu.mongodb.net/?appName=Cluster0")              # ← MongoDB URL
BOT_TOKEN = os.getenv("BOT_TOKEN", "9070343619:AAFLcBblmYzIKHth-t5nVq4XPJUqSVfM8PU")                # ← Bot Token

# اختياري (موصى به): مفتاح لتشفير الجلسات داخل قاعدة البيانات
# لتوليده:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

PORT = int(os.getenv("PORT", "10000"))
REPLY_DELAY = 1.0          # تأخير بسيط قبل الرد (ثانية)
COOLDOWN = 20              # أقل مدة بين ردّين لنفس الشخص على نفس الكلمة (ثانية)
MAX_BUTTONS = 12           # أقصى عدد أزرار داخل الرد الواحد

# ════════════════════════════════════════════════════════════
#  أساسيات
# ════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("autoreply")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FERNET = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None

HTTP: aiohttp.ClientSession = None
db = None
BOT_USERNAME = ""

STATE = {}      # حالة كل مستخدم: {"step":..., "panel": message_id, ...}
LOGIN = {}      # جلسات تسجيل الدخول المعلقة
CLIENTS = {}    # عملاء Telethon العاملون: uid -> client
LAST = {}       # للتحكم في تكرار الرد

BLUE, GREEN, RED = "primary", "success", "danger"   # ألوان الأزرار في Bot API


def enc(s):
    return FERNET.encrypt(s.encode()).decode() if FERNET else s


def dec(s):
    return FERNET.decrypt(s.encode()).decode() if FERNET else s


def esc(s):
    return html.escape(str(s), quote=False)


def newid():
    return secrets.token_hex(4)


# ════════════════════════════════════════════════════════════
#  Bot API (طلبات مباشرة لدعم الأزرار الملونة style)
# ════════════════════════════════════════════════════════════
def strip_style(o):
    if isinstance(o, dict):
        return {k: strip_style(v) for k, v in o.items() if k != "style"}
    if isinstance(o, list):
        return [strip_style(i) for i in o]
    return o


async def api(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    for attempt in (1, 2):
        try:
            async with HTTP.post(f"{API_URL}/{method}", json=params) as r:
                data = await r.json()
        except Exception as e:  # noqa
            log.warning("api %s failed: %s", method, e)
            return {"ok": False, "description": str(e)}
        if data.get("ok"):
            return data
        desc = data.get("description", "")
        if attempt == 1 and "style" in desc.lower():
            params = strip_style(params)      # احتياط إذا لم يدعم الخادم الألوان
            continue
        if "not modified" not in desc:
            log.warning("api %s: %s", method, desc)
        return data


def btn(text, data, style=None):
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


async def put(chat_id, mid, text, markup):
    """تحديث الرسالة الحالية (inline update) أو إرسال رسالة جديدة إن لم توجد."""
    if mid:
        r = await api("editMessageText", chat_id=chat_id, message_id=mid, text=text,
                      parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        if r.get("ok") or "not modified" in r.get("description", ""):
            return mid
    r = await api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  reply_markup=markup, disable_web_page_preview=True)
    return r["result"]["message_id"] if r.get("ok") else None


async def ui(uid, chat_id, text, markup):
    S = STATE.setdefault(uid, {})
    S["panel"] = await put(chat_id, S.get("panel"), text, markup)


# ════════════════════════════════════════════════════════════
#  الواجهات
# ════════════════════════════════════════════════════════════
HR = "〰︎〰︎〰︎〰︎〰︎〰︎〰︎〰︎"


def cancel_kb():
    return kb([btn("إلغاء", "start", RED)])


async def show_welcome(uid, chat_id, name):
    await ui(uid, chat_id,
             f"♡ ┆ أهلاً بك يا <b>{esc(name)}</b> في بوت الرد التلقائي\n{HR}\n➤ سجّل دخولك للبدء",
             kb([btn("تسجيل الدخول", "login", GREEN)]))


async def show_main(uid, chat_id):
    await ui(uid, chat_id,
             f"♡ ┆ <b>القائمة الرئيسية</b>\n{HR}\n➤ اختر أحد الخيارات من الأزرار أدناه",
             kb([btn("الرد التلقائي", "ar", BLUE)],
                [btn("تسجيل الخروج", "lo", RED)]))


async def show_start(uid, chat_id, name):
    user = await db.users.find_one({"_id": uid, "active": True})
    if user:
        await show_main(uid, chat_id)
    else:
        await show_welcome(uid, chat_id, name)


async def show_ar_menu(uid, chat_id):
    await ui(uid, chat_id,
             f"♡ ┆ <b>الرد التلقائي</b>\n{HR}\n➤ أضف كلمة مفتاحية وردّها، ثم زوّدها بأزرار إن أردت",
             kb([btn("إضافة رد جديد", "new", GREEN)],
                [btn("ردودي", "list", BLUE)],
                [btn("رجوع", "menu", RED)]))


async def show_list(uid, chat_id):
    rules = await db.nodes.find({"owner": uid, "parent": None}).sort("ts", 1).to_list(40)
    if rules:
        text = f"♡ ┆ <b>ردودي</b>\n{HR}\n➤ عدد الردود: {len(rules)}\n• اختر رداً لإدارته"
    else:
        text = f"♡ ┆ <b>ردودي</b>\n{HR}\n• لا توجد ردود بعد"
    rows = [[btn(r["keyword"][:40], f"n:{r['_id']}", BLUE)] for r in rules]
    rows.append([btn("رجوع", "ar", RED)])
    await ui(uid, chat_id, text, kb(*rows))


async def children(nid):
    return await db.nodes.find({"parent": nid}).sort("ts", 1).to_list(50)


async def show_node(uid, chat_id, node):
    kids = await children(node["_id"])
    if node["parent"] is None:
        head = f"★ الكلمة المفتاحية: <b>{esc(node['keyword'])}</b>"
    else:
        head = f"★ الزر: <b>{esc(node['label'])}</b>"
    preview = node["text"] if len(node["text"]) <= 300 else node["text"][:300] + "..."
    text = (f"♡ ┆ <b>إدارة الرد</b>\n{HR}\n{head}\n┆\n"
            f"➤ الرد:\n{esc(preview)}\n┆\n• عدد الأزرار: {len(kids)}")
    nid = node["_id"]
    rows = [[btn(k["label"][:40], f"n:{k['_id']}", RED)] for k in kids]
    rows.append([btn("إضافة زر", f"ab:{nid}", GREEN), btn("تعديل الرد", f"et:{nid}", BLUE)])
    if node["parent"] is None:
        rows.append([btn("تعديل الكلمة", f"ek:{nid}", BLUE)])
    rows.append([btn("حذف", f"dl:{nid}", RED)])
    rows.append([btn("رجوع", f"n:{node['parent']}" if node["parent"] else "list", RED)])
    await ui(uid, chat_id, text, kb(*rows))


async def delete_tree(nid):
    ids, stack = [nid], [nid]
    while stack:
        cur = stack.pop()
        async for k in db.nodes.find({"parent": cur}, {"_id": 1}):
            ids.append(k["_id"])
            stack.append(k["_id"])
    await db.nodes.delete_many({"_id": {"$in": ids}})


async def get_node(nid, owner):
    return await db.nodes.find_one({"_id": nid, "owner": owner})


# ════════════════════════════════════════════════════════════
#  واجهة العملاء (الأزرار الحمراء التي يراها من يراسلك)
# ════════════════════════════════════════════════════════════
async def customer_view(node):
    kids = await children(node["_id"])
    rows = [[btn(k["label"][:40], f"c:{k['_id']}", RED)] for k in kids]
    if node["parent"]:
        rows.append([btn("رجوع", f"c:{node['parent']}", RED)])
    return node["text"], (kb(*rows) if rows else {"inline_keyboard": []})


async def customer_cb(cb):
    node = await db.nodes.find_one({"_id": cb["data"][2:]})
    if not node:
        await api("answerCallbackQuery", callback_query_id=cb["id"],
                  text="هذا الزر لم يعد متاحاً", show_alert=True)
        return
    text, markup = await customer_view(node)
    if cb.get("inline_message_id"):
        await api("editMessageText", inline_message_id=cb["inline_message_id"],
                  text=text, reply_markup=markup)
    elif cb.get("message"):
        m = cb["message"]
        await api("editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"],
                  text=text, reply_markup=markup)
    await api("answerCallbackQuery", callback_query_id=cb["id"])


async def on_inline(iq):
    """يستدعيها حساب المستخدم (Telethon) ليرسل الرد مع الأزرار عبر البوت."""
    results = []
    q = iq.get("query", "")
    if q.startswith("r"):
        user = await db.users.find_one({"account_id": iq["from"]["id"], "active": True})
        if user:
            node = await db.nodes.find_one({"_id": q[1:], "owner": user["_id"]})
            if node:
                text, markup = await customer_view(node)
                results.append({
                    "type": "article", "id": node["_id"], "title": "رد تلقائي",
                    "input_message_content": {"message_text": text},
                    "reply_markup": markup,
                })
    await api("answerInlineQuery", inline_query_id=iq["id"], results=results,
              cache_time=0, is_personal=True)


# ════════════════════════════════════════════════════════════
#  Telethon: الاستماع للرسائل الخاصة والرد
# ════════════════════════════════════════════════════════════
async def deliver(client, chat_id, node):
    kids = await db.nodes.count_documents({"parent": node["_id"]})
    if kids and BOT_USERNAME:
        try:
            res = await client.inline_query(BOT_USERNAME, f"r{node['_id']}")
            if len(res):
                await res[0].click(chat_id)
                return
        except Exception as e:  # noqa
            log.warning("inline send failed, fallback to text: %s", e)
    await client.send_message(chat_id, node["text"])


def attach(uid, client):
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if not event.is_private or event.out:
                return
            text = (event.raw_text or "").strip().lower()
            if not text:
                return
            sender = await event.get_sender()
            if (not sender or getattr(sender, "bot", False)
                    or getattr(sender, "is_self", False) or sender.id == 777000):
                return
            rules = await db.nodes.find({"owner": uid, "parent": None}).to_list(500)
            hits = [r for r in rules if r["keyword"].strip().lower() in text]
            if not hits:
                return
            rule = max(hits, key=lambda r: len(r["keyword"]))
            key = (uid, sender.id, rule["_id"])
            now = time.time()
            if now - LAST.get(key, 0) < COOLDOWN:
                return
            LAST[key] = now
            await asyncio.sleep(REPLY_DELAY)
            await deliver(client, event.chat_id, rule)
        except Exception as e:  # noqa
            log.exception("listener error: %s", e)


async def restore_sessions():
    async for u in db.users.find({"active": True}):
        uid = u["_id"]
        try:
            client = TelegramClient(StringSession(dec(u["session"])), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await db.users.update_one({"_id": uid}, {"$set": {"active": False}})
                await client.disconnect()
                continue
            CLIENTS[uid] = client
            attach(uid, client)
            log.info("restored session for %s", uid)
        except Exception as e:  # noqa
            log.warning("restore failed for %s: %s", uid, e)


async def drop_login(uid):
    p = LOGIN.pop(uid, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:  # noqa
            pass


async def finish_login(uid, chat_id, client):
    me = await client.get_me()
    session = client.session.save()
    old = CLIENTS.pop(uid, None)
    if old:
        try:
            await old.disconnect()
        except Exception:  # noqa
            pass
    await db.users.update_one(
        {"_id": uid},
        {"$set": {"account_id": me.id, "session": enc(session),
                  "phone": getattr(me, "phone", ""), "name": me.first_name or "",
                  "active": True, "ts": time.time()}},
        upsert=True)
    CLIENTS[uid] = client
    attach(uid, client)
    LOGIN.pop(uid, None)
    STATE.setdefault(uid, {})["step"] = None
    await show_main(uid, chat_id)


# ════════════════════════════════════════════════════════════
#  معالجة الرسائل النصية (خطوات الإدخال)
# ════════════════════════════════════════════════════════════
async def on_message(m):
    if m["chat"]["type"] != "private" or "from" not in m:
        return
    uid, chat_id = m["from"]["id"], m["chat"]["id"]
    text = (m.get("text") or "").strip()
    name = m["from"].get("first_name", "")

    if text.startswith("/start"):
        await drop_login(uid)
        STATE[uid] = {}
        await show_start(uid, chat_id, name)
        return

    S = STATE.setdefault(uid, {})
    step = S.get("step")
    if not step or not text:
        return
    await api("deleteMessage", chat_id=chat_id, message_id=m["message_id"])

    # ── تسجيل الدخول: رقم الهاتف ──
    if step == "phone":
        phone = re.sub(r"\s+", "", text)
        if not re.fullmatch(r"\+\d{8,15}", phone):
            await ui(uid, chat_id,
                     f"♡ ┆ رقم غير صالح\n{HR}\n• أرسل الرقم مع رمز الدولة\n• مثال: <code>+20111111111</code>",
                     cancel_kb())
            return
        await ui(uid, chat_id, "♡ ┆ جارٍ إرسال كود التحقق ...", None)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except errors.FloodWaitError as e:
            await client.disconnect()
            await ui(uid, chat_id, f"♡ ┆ حاول لاحقاً بعد {e.seconds} ثانية", cancel_kb())
            return
        except Exception as e:  # noqa
            await client.disconnect()
            log.warning("send_code failed: %s", e)
            await ui(uid, chat_id,
                     f"♡ ┆ تعذر إرسال الكود\n{HR}\n• تأكد من الرقم ومن إعدادات API\n• أرسل الرقم من جديد",
                     cancel_kb())
            return
        await drop_login(uid)
        LOGIN[uid] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
        S["step"] = "code"
        await ui(uid, chat_id,
                 f"♡ ┆ <b>كود التحقق</b>\n{HR}\n➤ وصلك كود على حسابك في تيليجرام\n"
                 f"• أرسله مع مسافات بين الأرقام\n• مثال: <code>0 0 0 0 0</code>",
                 cancel_kb())
        return

    # ── تسجيل الدخول: الكود ──
    if step == "code":
        p = LOGIN.get(uid)
        if not p:
            S["step"] = "phone"
            await ui(uid, chat_id, "♡ ┆ انتهت الجلسة، أرسل رقمك من جديد", cancel_kb())
            return
        if not re.fullmatch(r"\d(\s+\d)+", text):
            await ui(uid, chat_id,
                     f"♡ ┆ صيغة غير صحيحة\n{HR}\n• أرسل الكود مع مسافات بين الأرقام\n• مثال: <code>0 0 0 0 0</code>",
                     cancel_kb())
            return
        code = re.sub(r"\s+", "", text)
        try:
            await p["client"].sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
        except errors.SessionPasswordNeededError:
            S["step"] = "password"
            await ui(uid, chat_id,
                     f"♡ ┆ <b>التحقق بخطوتين</b>\n{HR}\n➤ أرسل كلمة مرور حسابك",
                     cancel_kb())
            return
        except errors.PhoneCodeInvalidError:
            await ui(uid, chat_id, f"♡ ┆ الكود غير صحيح\n{HR}\n• أعد إرساله بمسافات بين الأرقام",
                     cancel_kb())
            return
        except errors.PhoneCodeExpiredError:
            await drop_login(uid)
            S["step"] = "phone"
            await ui(uid, chat_id, f"♡ ┆ انتهت صلاحية الكود\n{HR}\n➤ أرسل رقمك من جديد", cancel_kb())
            return
        except Exception as e:  # noqa
            log.warning("sign_in failed: %s", e)
            await drop_login(uid)
            S["step"] = "phone"
            await ui(uid, chat_id, f"♡ ┆ فشل تسجيل الدخول\n{HR}\n➤ أرسل رقمك من جديد", cancel_kb())
            return
        await finish_login(uid, chat_id, p["client"])
        return

    # ── تسجيل الدخول: كلمة المرور ──
    if step == "password":
        p = LOGIN.get(uid)
        if not p:
            S["step"] = "phone"
            await ui(uid, chat_id, "♡ ┆ انتهت الجلسة، أرسل رقمك من جديد", cancel_kb())
            return
        try:
            await p["client"].sign_in(password=text)
        except errors.PasswordHashInvalidError:
            await ui(uid, chat_id, f"♡ ┆ كلمة المرور خاطئة\n{HR}\n➤ حاول مرة أخرى", cancel_kb())
            return
        except Exception as e:  # noqa
            log.warning("2fa failed: %s", e)
            await drop_login(uid)
            S["step"] = "phone"
            await ui(uid, chat_id, f"♡ ┆ فشل تسجيل الدخول\n{HR}\n➤ أرسل رقمك من جديد", cancel_kb())
            return
        await finish_login(uid, chat_id, p["client"])
        return

    # ── خطوات إدارة الردود (تتطلب حساباً مسجلاً) ──
    if not await db.users.find_one({"_id": uid, "active": True}):
        S["step"] = None
        return
    back = kb([btn("إلغاء", "ar", RED)])

    if step == "kw":
        if len(text) > 100:
            await ui(uid, chat_id, "♡ ┆ الكلمة طويلة جداً (الحد 100 حرف)", back)
            return
        S["kw"], S["step"] = text, "kw_text"
        await ui(uid, chat_id,
                 f"♡ ┆ <b>إضافة رد جديد</b>\n{HR}\n• الكلمة: <b>{esc(text)}</b>\n➤ أرسل الآن نص الرد", back)
        return

    if step == "kw_text":
        if len(text) > 4000:
            await ui(uid, chat_id, "♡ ┆ النص طويل جداً", back)
            return
        node = {"_id": newid(), "owner": uid, "parent": None,
                "keyword": S.pop("kw"), "text": text, "ts": time.time()}
        await db.nodes.insert_one(node)
        S["step"] = None
        await show_node(uid, chat_id, node)
        return

    if step == "btn_label":
        if len(text) > 40:
            await ui(uid, chat_id, "♡ ┆ اسم الزر طويل (الحد 40 حرفاً)", back)
            return
        S["label"], S["step"] = text, "btn_text"
        await ui(uid, chat_id,
                 f"♡ ┆ <b>إضافة زر</b>\n{HR}\n• الزر: <b>{esc(text)}</b>\n➤ أرسل الآن الرد الذي يظهر عند الضغط عليه",
                 kb([btn("إلغاء", f"n:{S['pid']}", RED)]))
        return

    if step == "btn_text":
        pid = S["pid"]
        parent = await get_node(pid, uid)
        if not parent:
            S["step"] = None
            await show_list(uid, chat_id)
            return
        node = {"_id": newid(), "owner": uid, "parent": pid,
                "label": S.pop("label"), "text": text[:4000], "ts": time.time()}
        await db.nodes.insert_one(node)
        S["step"] = None
        await show_node(uid, chat_id, parent)
        return

    if step in ("edit_text", "edit_kw"):
        node = await get_node(S.get("nid"), uid)
        S["step"] = None
        if not node:
            await show_list(uid, chat_id)
            return
        field = "text" if step == "edit_text" else "keyword"
        value = text[:4000] if field == "text" else text[:100]
        await db.nodes.update_one({"_id": node["_id"]}, {"$set": {field: value}})
        node[field] = value
        await show_node(uid, chat_id, node)
        return


# ════════════════════════════════════════════════════════════
#  معالجة الأزرار
# ════════════════════════════════════════════════════════════
async def on_callback(cb):
    data = cb.get("data", "")
    if data.startswith("c:"):
        await customer_cb(cb)
        return
    msg = cb.get("message")
    await api("answerCallbackQuery", callback_query_id=cb["id"])
    if not msg:
        return
    uid, chat_id = cb["from"]["id"], msg["chat"]["id"]
    name = cb["from"].get("first_name", "")
    S = STATE.setdefault(uid, {})
    S["panel"] = msg["message_id"]
    S["step"] = None
    if data != "login":
        await drop_login(uid)

    # ── قبل تسجيل الدخول ──
    if data == "login":
        await drop_login(uid)
        S["step"] = "phone"
        await ui(uid, chat_id,
                 f"♡ ┆ <b>تسجيل الدخول</b>\n{HR}\n➤ أرسل رقم هاتفك مع رمز الدولة\n"
                 f"• مثال: <code>+20111111111</code>",
                 cancel_kb())
        return
    if data == "start":
        await show_start(uid, chat_id, name)
        return

    user = await db.users.find_one({"_id": uid, "active": True})
    if not user:
        await show_welcome(uid, chat_id, name)
        return

    # ── القوائم ──
    if data == "menu":
        await show_main(uid, chat_id)
    elif data == "ar":
        await show_ar_menu(uid, chat_id)
    elif data == "list":
        await show_list(uid, chat_id)
    elif data == "new":
        S["step"] = "kw"
        await ui(uid, chat_id,
                 f"♡ ┆ <b>إضافة رد جديد</b>\n{HR}\n➤ أرسل الكلمة المفتاحية",
                 kb([btn("إلغاء", "ar", RED)]))

    # ── تسجيل الخروج ──
    elif data == "lo":
        await ui(uid, chat_id,
                 f"♡ ┆ <b>تسجيل الخروج</b>\n{HR}\n➤ سيتوقف الرد التلقائي وتُنهى الجلسة\n• ردودك تبقى محفوظة",
                 kb([btn("تأكيد الخروج", "lc", RED)], [btn("رجوع", "menu", BLUE)]))
    elif data == "lc":
        client = CLIENTS.pop(uid, None)
        if client:
            try:
                await client.log_out()
            except Exception:  # noqa
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa
                pass
        await db.users.delete_one({"_id": uid})
        await show_welcome(uid, chat_id, name)

    # ── إدارة العقد (الردود والأزرار) ──
    elif data.startswith(("n:", "ab:", "et:", "ek:", "dl:", "dy:")):
        action, nid = data.split(":", 1)
        node = await get_node(nid, uid)
        if not node:
            await show_list(uid, chat_id)
            return
        if action == "n":
            await show_node(uid, chat_id, node)
        elif action == "ab":
            if await db.nodes.count_documents({"parent": nid}) >= MAX_BUTTONS:
                await api("answerCallbackQuery", callback_query_id=cb["id"],
                          text=f"الحد الأقصى {MAX_BUTTONS} أزرار", show_alert=True)
                await show_node(uid, chat_id, node)
                return
            S.update(step="btn_label", pid=nid)
            await ui(uid, chat_id,
                     f"♡ ┆ <b>إضافة زر</b>\n{HR}\n➤ أرسل اسم الزر",
                     kb([btn("إلغاء", f"n:{nid}", RED)]))
        elif action == "et":
            S.update(step="edit_text", nid=nid)
            await ui(uid, chat_id,
                     f"♡ ┆ <b>تعديل الرد</b>\n{HR}\n➤ أرسل النص الجديد",
                     kb([btn("إلغاء", f"n:{nid}", RED)]))
        elif action == "ek":
            S.update(step="edit_kw", nid=nid)
            await ui(uid, chat_id,
                     f"♡ ┆ <b>تعديل الكلمة</b>\n{HR}\n➤ أرسل الكلمة المفتاحية الجديدة",
                     kb([btn("إلغاء", f"n:{nid}", RED)]))
        elif action == "dl":
            await ui(uid, chat_id,
                     f"♡ ┆ <b>تأكيد الحذف</b>\n{HR}\n➤ سيُحذف هذا الرد مع كل أزراره وردوده الفرعية من قاعدة البيانات نهائياً",
                     kb([btn("نعم، احذف", f"dy:{nid}", RED)], [btn("إلغاء", f"n:{nid}", BLUE)]))
        elif action == "dy":
            parent = node["parent"]
            await delete_tree(nid)
            if parent:
                pn = await get_node(parent, uid)
                if pn:
                    await show_node(uid, chat_id, pn)
                    return
            await show_list(uid, chat_id)
    else:
        await show_main(uid, chat_id)


# ════════════════════════════════════════════════════════════
#  التشغيل
# ════════════════════════════════════════════════════════════
async def safe(coro):
    try:
        await coro
    except Exception as e:  # noqa
        log.exception("update error: %s", e)


async def handle_update(u):
    if "message" in u:
        await on_message(u["message"])
    elif "callback_query" in u:
        await on_callback(u["callback_query"])
    elif "inline_query" in u:
        await on_inline(u["inline_query"])


async def poll():
    offset = None
    while True:
        r = await api("getUpdates", offset=offset, timeout=50,
                      allowed_updates=["message", "callback_query", "inline_query"])
        if not r.get("ok"):
            await asyncio.sleep(3)
            continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            asyncio.create_task(safe(handle_update(u)))


async def health(_):
    return web.Response(text="OK")


async def main():
    global HTTP, db, BOT_USERNAME
    if "YOUR_" in (API_HASH + MONGO_URL + BOT_TOKEN) or API_ID == 12345678:
        raise SystemExit("ضع قيم API_ID و API_HASH و MONGO_URL و BOT_TOKEN أولاً.")

    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75))
    db = AsyncIOMotorClient(MONGO_URL)["autoreply_bot"]
    await db.nodes.create_index([("owner", 1), ("parent", 1)])
    await db.users.create_index("account_id")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    me = await api("getMe")
    BOT_USERNAME = me["result"]["username"]
    log.info("bot started as @%s", BOT_USERNAME)
    await api("deleteWebhook")

    asyncio.create_task(safe(restore_sessions()))
    await poll()


if __name__ == "__main__":
    asyncio.run(main())
