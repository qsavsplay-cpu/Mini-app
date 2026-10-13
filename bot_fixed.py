# bot.py
# ToxiVPN — Telegram bot
# Python 3.11+
#
# Requirements:
# pyTelegramBotAPI
# supabase
# flask
# requests
#
# Railway Variables:
# BOT_TOKEN
# SUPABASE_URL
# SUPABASE_KEY
# OWNER_ID
# ADMIN_IDS
# REQUIRED_CHANNEL=@ToxiVPN
# SUPPORT_USERNAME=ToxiSupport
# PHOTO_URL
# HAPP_CRYPTO_V4_PUBLIC_KEY
# API_SECRET
#
# Важно:
# 1. Укажи OWNER_ID в Railway Variables.
# 2. SQL из предыдущего сообщения должен быть выполнен.
# 3. Реальный подсчёт трафика требует статистики VPN-панели.
# 4. Ограничение устройств по IP/User-Agent не равно точному
#    подсчёту устройств внутри VPN-панели.

import os
import re
import json
import time
import uuid
import random
import secrets
import hashlib
import hmac
import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

import requests
import telebot
from telebot import types
from flask import Flask, request, jsonify, Response, send_file, redirect
from supabase import create_client


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_IDS = os.getenv("ADMIN_IDS", "")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@ToxiVPN")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "ToxiSupport")
PHOTO_URL = os.getenv("PHOTO_URL", "")
API_SECRET = os.getenv("API_SECRET", "")
HAPP_CRYPTO_V4_PUBLIC_KEY = os.getenv(
    "HAPP_CRYPTO_V4_PUBLIC_KEY", ""
)
PUBLIC_DOMAIN = os.getenv(
    "RAILWAY_PUBLIC_DOMAIN",
    os.getenv("DOMAIN", "")
).strip()

if PUBLIC_DOMAIN and not PUBLIC_DOMAIN.startswith("http"):
    PUBLIC_DOMAIN = "https://" + PUBLIC_DOMAIN

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Set BOT_TOKEN, SUPABASE_URL and SUPABASE_KEY in Railway Variables."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
db = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)


@app.route("/app", methods=["GET"])
def mini_app_page():
    """Serve the ToxiVPN Telegram Mini App from the same public domain as its API."""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toxivpn_miniapp.html")
    if not os.path.isfile(html_path):
        return "Mini App HTML file is missing from the deployment", 404
    return send_file(html_path, mimetype="text/html")


@app.route("/app-logo", methods=["GET"])
def mini_app_logo():
    """Use the same banner image configured for the Telegram bot as the Mini App logo."""
    if not PHOTO_URL:
        return "", 404
    return redirect(PHOTO_URL, code=302)


@app.before_request
def handle_api_preflight():
    if request.path.startswith("/api/v1/") and request.method == "OPTIONS":
        return Response(status=204)


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith("/api/v1/"):
        origin = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-API-Key, X-Telegram-Init-Data, Accept"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
    return response

TARIFFS = {
    "7": {"name": "7 дней", "days": 7, "price": Decimal("40.56")},
    "30": {"name": "30 дней", "days": 30, "price": Decimal("154.96")},
    "90": {"name": "90 дней", "days": 90, "price": Decimal("364.00")},
    "3650": {
        "name": "Пожизненная",
        "days": 3650,
        "price": Decimal("830.96")
    }
}

TRIAL_DAYS = 1
DEFAULT_TRAFFIC_GB = 50
DEFAULT_MAX_DEVICES = 2
MIN_TOPUP = Decimal("50")
MAX_TOPUP = Decimal("10000")
REFERRAL_PERCENT = Decimal("10")

VLESS_SERVERS = {
    "poland": "🇵🇱 Польша",
    "usa": "🇺🇸 США",
    "turkey": "🇹🇷 Турция",
    "belarus": "🇧🇾 Беларусь",
    "lithuania": "🇱🇹 Литва",
}

# Состояния пользовательских диалогов.
states = {}
pending_gifts = {}
pending_promo_requests = {}
pending_broadcasts = {}
pending_giveaways = {}


# =========================================================
# GENERAL HELPERS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def money(value):
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def escape(value):
    import html
    return html.escape(str(value or ""))


def run_query(query):
    try:
        return query.execute().data or []
    except Exception:
        logging.exception("Database query failed")
        raise


def get_user(telegram_id):
    rows = run_query(
        db.table("users")
        .select("*")
        .eq("telegram_id", int(telegram_id))
        .limit(1)
    )
    return rows[0] if rows else None


def save_user(user):
    telegram_id = int(user.id)
    username = user.username or ""
    full_name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    )

    existing = get_user(telegram_id)

    payload = {
        "telegram_id": telegram_id,
        "username": username,
        "full_name": full_name,
    }

    if not existing:
        payload["balance"] = 0
        payload["is_blocked"] = False

    run_query(
        db.table("users").upsert(
            payload,
            on_conflict="telegram_id"
        )
    )

    if not get_subscription(telegram_id):
        create_subscription(telegram_id)


def get_subscription(telegram_id):
    rows = run_query(
        db.table("subscriptions")
        .select("*")
        .eq("telegram_id", int(telegram_id))
        .limit(1)
    )
    return rows[0] if rows else None


def get_subscription_by_key(sub_key):
    rows = run_query(
        db.table("subscriptions")
        .select("*")
        .eq("sub_key", sub_key)
        .limit(1)
    )
    return rows[0] if rows else None


def create_subscription(telegram_id):
    existing = get_subscription(telegram_id)
    if existing:
        return existing

    payload = {
        "telegram_id": int(telegram_id),
        "sub_key": secrets.token_urlsafe(32),
        "expire": (now_utc() - timedelta(days=1)).isoformat(),
        "traffic_limit_gb": DEFAULT_TRAFFIC_GB,
        "traffic_used_gb": 0,
        "max_devices": DEFAULT_MAX_DEVICES,
        "is_blocked": False,
        "used_trial": False,
    }

    run_query(
        db.table("subscriptions").insert(payload)
    )

    return get_subscription(telegram_id)


def get_role(telegram_id):
    telegram_id = int(telegram_id)

    if OWNER_ID and telegram_id == OWNER_ID:
        return "owner"

    rows = run_query(
        db.table("bot_roles")
        .select("role")
        .eq("telegram_id", telegram_id)
        .limit(1)
    )

    if rows:
        return rows[0]["role"]

    raw_admins = {
        int(x.strip())
        for x in ADMIN_IDS.split(",")
        if x.strip().isdigit()
    }

    if telegram_id in raw_admins:
        return "admin"

    return "user"


def is_staff(telegram_id):
    return get_role(telegram_id) in ("owner", "admin", "moderator")


def is_admin(telegram_id):
    return get_role(telegram_id) in ("owner", "admin")


def is_owner(telegram_id):
    return get_role(telegram_id) == "owner"


def role_title(role):
    return {
        "owner": "Владелец",
        "admin": "Администратор",
        "moderator": "Модератор",
        "user": "Пользователь",
    }.get(role, "Пользователь")


def safe_answer(call, text=None, alert=False):
    try:
        bot.answer_callback_query(
            call.id,
            text=text or "",
            show_alert=alert
        )
    except Exception:
        pass


def edit_or_send(call, text, keyboard=None):
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )


def inline_button(text, callback):
    return types.InlineKeyboardButton(
        text=text,
        callback_data=callback
    )


def main_keyboard(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        inline_button("🚀 Подключить VPN", "connect"),
        inline_button("💳 Купить подписку", "buy")
    )
    kb.add(
        inline_button("👤 Профиль", "profile"),
        inline_button("💰 Баланс", "balance")
    )
    kb.add(
        inline_button("🎁 Промокод", "promo"),
        inline_button("🎟 Пробник", "trial")
    )
    kb.add(
        inline_button("👥 Рефералы", "referrals"),
        inline_button("🎫 Тикеты", "tickets")
    )
    kb.add(
        inline_button("📚 FAQ", "faq"),
        inline_button("📜 Документы", "documents")
    )
    kb.add(
        types.InlineKeyboardButton(
            "🆘 Поддержка",
            url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"
        ),
        types.InlineKeyboardButton(
            "📢 Канал",
            url="https://t.me/ToxiVPN"
        )
    )

    if is_staff(user_id):
        kb.add(inline_button("🛠 Панель управления", "admin"))

    return kb


def send_menu(chat_id, user_id, text="Добро пожаловать в ToxiVPN!"):
    kb = main_keyboard(user_id)

    if PHOTO_URL:
        try:
            bot.send_photo(
                chat_id,
                PHOTO_URL,
                caption=text,
                reply_markup=kb
            )
            return
        except Exception:
            logging.exception("PHOTO_URL send failed")

    bot.send_message(chat_id, text, reply_markup=kb)


def check_channel(user_id):
    if not REQUIRED_CHANNEL:
        return True

    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in (
            "creator", "administrator", "member"
        )
    except Exception:
        # Если бот не имеет доступа к каналу, не блокируем всех пользователей.
        logging.warning("Channel membership check unavailable")
        return True


def user_is_blocked(user_id):
    user = get_user(user_id)
    return bool(user and user.get("is_blocked"))


def maintenance_enabled():
    rows = run_query(
        db.table("bot_settings")
        .select("setting_value")
        .eq("setting_key", "maintenance")
        .limit(1)
    )
    if not rows:
        return False
    return bool(rows[0]["setting_value"].get("enabled"))


def user_allowed_during_maintenance(user_id):
    return is_staff(user_id)


def ensure_user_access(message):
    user_id = message.from_user.id

    if user_is_blocked(user_id):
        bot.send_message(message.chat.id, "Доступ к боту ограничен.")
        return False

    if maintenance_enabled() and not user_allowed_during_maintenance(user_id):
        bot.send_message(
            message.chat.id,
            "🛠 Ведутся технические работы. Попробуйте позже."
        )
        return False

    return True


# =========================================================
# SUBSCRIPTIONS
# =========================================================

def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def subscription_reason(sub):
    if not sub:
        return "missing"

    if sub.get("is_blocked"):
        return "blocked"

    expire = parse_datetime(sub.get("expire"))
    if not expire or expire <= now_utc():
        return "expired"

    used = Decimal(str(sub.get("traffic_used_gb") or 0))
    limit = Decimal(str(
        sub.get("traffic_limit_gb") or DEFAULT_TRAFFIC_GB
    ))

    if used >= limit:
        return "traffic"

    return None


def activate_subscription(telegram_id, days):
    sub = get_subscription(telegram_id)

    if not sub:
        sub = create_subscription(telegram_id)

    current_expire = parse_datetime(sub.get("expire"))
    base = max(current_expire or now_utc(), now_utc())
    new_expire = base + timedelta(days=int(days))

    run_query(
        db.table("subscriptions")
        .update({
            "expire": new_expire.isoformat(),
            "traffic_limit_gb": DEFAULT_TRAFFIC_GB,
            "traffic_used_gb": 0,
            "max_devices": DEFAULT_MAX_DEVICES,
            "is_blocked": False,
        })
        .eq("telegram_id", int(telegram_id))
    )

    attach_all_servers(sub["sub_key"])
    return new_expire


def attach_all_servers(sub_key):
    rows = [
        {"sub_key": sub_key, "server_key": key}
        for key in VLESS_SERVERS
    ]

    run_query(
        db.table("subscription_servers").upsert(
            rows,
            on_conflict="sub_key,server_key"
        )
    )


def subscription_http_url(sub):
    if not PUBLIC_DOMAIN:
        return None
    return f"{PUBLIC_DOMAIN}/sub/{quote(sub['sub_key'])}"


def happ_link_for_url(url):
    # Возвращаем Happ-ссылку только при наличии реализации шифрования.
    # Не выдаём обычную HTTP-ссылку за зашифрованную Happ Crypto.
    if not url or not HAPP_CRYPTO_V4_PUBLIC_KEY:
        return None

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64

        pem = HAPP_CRYPTO_V4_PUBLIC_KEY.replace("\\n", "\n").encode()
        public_key = serialization.load_pem_public_key(pem)

        encrypted = public_key.encrypt(
            url.encode(),
            padding.PKCS1v15()
        )

        encoded = base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
        return "happ://crypt4/" + encoded

    except Exception:
        logging.exception("Happ Crypto encryption failed")
        return None


def get_server_configs(sub_key):
    linked = run_query(
        db.table("subscription_servers")
        .select("server_key")
        .eq("sub_key", sub_key)
    )

    keys = [x["server_key"] for x in linked]

    if not keys:
        attach_all_servers(sub_key)
        keys = list(VLESS_SERVERS.keys())

    rows = run_query(
        db.table("servers")
        .select("server_key,config")
        .in_("server_key", keys)
        .eq("is_active", True)
    )

    return [row["config"] for row in rows]


def empty_config(reason):
    names = {
        "devices": "⛔ Лимит устройств исчерпан",
        "traffic": "⛔ Лимит трафика исчерпан",
        "expired": "⛔ Подписка закончилась",
        "blocked": "⛔ Подписка заблокирована",
    }

    return (
        "vless://00000000-0000-0000-0000-000000000000"
        "@127.0.0.1:1"
        "?encryption=none&type=tcp#"
        + quote(names.get(reason, "⛔ Недоступно"))
    )


def device_fingerprint():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ip = ip.split(",")[0].strip()
    ua = request.headers.get("User-Agent", "")
    return hashlib.sha256(
        f"{ip}|{ua}".encode("utf-8")
    ).hexdigest()


def register_device(sub_key, fingerprint, max_devices):
    rows = run_query(
        db.table("subscription_devices")
        .select("*")
        .eq("sub_key", sub_key)
    )

    for device in rows:
        if device["device_hash"] == fingerprint:
            run_query(
                db.table("subscription_devices")
                .update({"last_seen": iso_now()})
                .eq("id", device["id"])
            )
            return not device.get("is_blocked", False)

    run_query(
        db.table("subscription_devices").insert({
            "sub_key": sub_key,
            "device_hash": fingerprint,
            "is_blocked": False
        })
    )

    rows = run_query(
        db.table("subscription_devices")
        .select("*")
        .eq("sub_key", sub_key)
    )

    allowed = [d for d in rows if not d.get("is_blocked")]

    if len(allowed) <= max_devices:
        return True

    victim = random.choice(allowed)

    run_query(
        db.table("subscription_devices")
        .update({"is_blocked": True})
        .eq("id", victim["id"])
    )

    return victim["device_hash"] != fingerprint


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "ToxiVPN"})


@app.route("/sub/<sub_key>", methods=["GET"])
def subscription_endpoint(sub_key):
    try:
        sub = get_subscription_by_key(sub_key)

        if not sub:
            return Response(
                empty_config("blocked"),
                mimetype="text/plain"
            )

        reason = subscription_reason(sub)

        if reason:
            return Response(
                empty_config(reason),
                mimetype="text/plain"
            )

        fingerprint = device_fingerprint()

        allowed = register_device(
            sub_key,
            fingerprint,
            int(sub.get("max_devices") or DEFAULT_MAX_DEVICES)
        )

        if not allowed:
            return Response(
                empty_config("devices"),
                mimetype="text/plain"
            )

        configs = get_server_configs(sub_key)

        return Response(
            "\n".join(configs),
            mimetype="text/plain"
        )

    except Exception:
        logging.exception("Subscription endpoint error")
        return Response(
            empty_config("blocked"),
            mimetype="text/plain",
            status=500
        )


# =========================================================
# BALANCE AND PAYMENTS
# =========================================================

def get_balance(user_id):
    user = get_user(user_id)
    return money(user.get("balance", 0) if user else 0)


def change_balance(user_id, amount):
    user_id = int(user_id)
    amount = money(amount)
    user = get_user(user_id)

    if not user:
        raise ValueError("Пользователь не найден")

    new_balance = money(
        Decimal(str(user.get("balance") or 0)) + amount
    )

    if new_balance < 0:
        raise ValueError("Недостаточно средств")

    run_query(
        db.table("users")
        .update({"balance": float(new_balance)})
        .eq("telegram_id", user_id)
    )

    return new_balance


def create_payment_record(user_id, recipient_id, amount, days, kind):
    order_id = "TX-" + uuid.uuid4().hex[:20].upper()

    run_query(
        db.table("payments").insert({
            "order_id": order_id,
            "telegram_id": int(user_id),
            "recipient_id": int(recipient_id),
            "amount": float(money(amount)),
            "payment_type": kind,
            "tariff_days": int(days),
            "status": "PAID",
            "created_at": iso_now(),
            "paid_at": iso_now()
        })
    )

    return order_id


def purchase_with_balance(buyer_id, recipient_id, tariff_key):
    tariff = TARIFFS.get(str(tariff_key))
    if not tariff:
        raise ValueError("Тариф не найден")

    price = money(tariff["price"])

    if get_balance(buyer_id) < price:
        raise ValueError("Недостаточно средств на балансе")

    change_balance(buyer_id, -price)
    activate_subscription(recipient_id, tariff["days"])

    create_payment_record(
        buyer_id,
        recipient_id,
        price,
        tariff["days"],
        "balance_subscription"
    )


def show_tariffs(chat_id, buyer_id, recipient_id=None):
    recipient_id = recipient_id or buyer_id
    kb = types.InlineKeyboardMarkup(row_width=1)

    for key, tariff in TARIFFS.items():
        kb.add(inline_button(
            f"{tariff['name']} — {tariff['price']} ₽",
            f"buybal:{key}:{recipient_id}"
        ))

    kb.add(inline_button("⬅ Назад", "home"))

    bot.send_message(
        chat_id,
        "Выбери тариф.\n"
        f"Покупатель: {buyer_id}\n"
        f"Получатель: {recipient_id}\n"
        f"Баланс: {get_balance(buyer_id)} ₽",
        reply_markup=kb
    )


# =========================================================
# PROMOCODES
# =========================================================

def get_promo(code):
    rows = run_query(
        db.table("promos")
        .select("*")
        .eq("code", code.upper())
        .eq("is_active", True)
        .limit(1)
    )
    return rows[0] if rows else None


def activate_promo(code, user_id):
    promo = get_promo(code)

    if not promo:
        raise ValueError("Промокод не найден или отключён")

    target_id = promo.get("target_telegram_id")
    if target_id and int(target_id) != int(user_id):
        raise ValueError("Этот промокод предназначен другому пользователю")

    max_uses = promo.get("max_activations")
    if max_uses is not None and int(promo["activations"]) >= int(max_uses):
        raise ValueError("Лимит активаций исчерпан")

    previous = run_query(
        db.table("promo_activations")
        .select("id")
        .eq("promo_id", promo["id"])
        .eq("telegram_id", int(user_id))
        .limit(1)
    )

    if previous:
        raise ValueError("Ты уже активировал этот промокод")

    kind = promo["reward_type"]
    value = Decimal(str(promo.get("reward_value") or 0))

    if kind == "balance":
        change_balance(user_id, value)

    elif kind == "subscription":
        days = int(promo.get("subscription_days") or value)
        if days <= 0:
            raise ValueError("Некорректная длительность подписки")
        activate_subscription(user_id, days)

    elif kind == "item":
        # Предметы требуют отдельного подключённого инвентаря.
        # Бот не может выдавать предметы в сторонней игре без её API.
        raise ValueError(
            "Автоматическая выдача предметов не подключена к API игры"
        )

    run_query(
        db.table("promo_activations").insert({
            "promo_id": promo["id"],
            "telegram_id": int(user_id)
        })
    )

    run_query(
        db.table("promos")
        .update({
            "activations": int(promo["activations"]) + 1
        })
        .eq("id", promo["id"])
    )

    return promo


def create_promo(code, reward_type, value, max_uses, target_id, creator_id):
    code = code.strip().upper()

    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        raise ValueError("Код: 3–32 символа, латиница, цифры, _ или -")

    payload = {
        "code": code,
        "reward_type": reward_type,
        "reward_value": float(value),
        "max_activations": max_uses,
        "activations": 0,
        "target_telegram_id": target_id,
        "created_by": int(creator_id),
        "is_active": True
    }

    if reward_type == "subscription":
        payload["subscription_days"] = int(value)

    run_query(
        db.table("promos").insert(payload)
    )

    if target_id:
        return (
            f"https://t.me/{bot.get_me().username}"
            f"?start=promo_{code}"
        )

    return None


# =========================================================
# TICKETS
# =========================================================

def create_ticket(user_id, subject="Обращение"):
    rows = run_query(
        db.table("tickets").insert({
            "telegram_id": int(user_id),
            "subject": subject,
            "status": "open"
        })
    )
    return rows[0]["id"]


def ticket_message(ticket_id, sender_id, sender_role, message):
    run_query(
        db.table("ticket_messages").insert({
            "ticket_id": int(ticket_id),
            "sender_id": int(sender_id),
            "sender_role": sender_role,
            "message": message
        })
    )

    run_query(
        db.table("tickets")
        .update({
            "updated_at": iso_now(),
            "status": "answered" if sender_role != "user" else "open"
        })
        .eq("id", int(ticket_id))
    )


def ticket_keyboard(ticket_id, status):
    kb = types.InlineKeyboardMarkup()
    kb.add(inline_button(
        "💬 Ответить",
        f"ticketreply:{ticket_id}"
    ))
    if status != "closed":
        kb.add(inline_button(
            "🔒 Закрыть",
            f"ticketclose:{ticket_id}"
        ))
    return kb


def notify_staff_ticket(ticket_id, user_id, text):
    # В общую модерацию не передаём username и имя пользователя.
    for role in ("owner", "admin", "moderator"):
        rows = run_query(
            db.table("bot_roles")
            .select("telegram_id")
            .eq("role", role)
        )

        recipient_ids = {int(x["telegram_id"]) for x in rows}

        if role == "owner" and OWNER_ID:
            recipient_ids.add(OWNER_ID)

        for recipient in recipient_ids:
            try:
                bot.send_message(
                    recipient,
                    f"🎫 Тикет #{ticket_id}\n"
                    f"Отправитель: пользователь\n\n"
                    f"{escape(text)}",
                    reply_markup=ticket_keyboard(ticket_id, "open")
                )
            except Exception:
                logging.warning("Cannot notify staff %s", recipient)


def user_tickets_keyboard(user_id):
    rows = run_query(
        db.table("tickets")
        .select("*")
        .eq("telegram_id", int(user_id))
        .order("created_at", desc=True)
        .limit(20)
    )

    kb = types.InlineKeyboardMarkup(row_width=1)

    for ticket in rows:
        kb.add(inline_button(
            f"#{ticket['id']} — {ticket['status']}",
            f"ticketuser:{ticket['id']}"
        ))

    kb.add(inline_button("➕ Новый тикет", "ticketnew"))
    kb.add(inline_button("⬅ Назад", "home"))
    return kb


def list_staff_tickets(chat_id):
    rows = run_query(
        db.table("tickets")
        .select("*")
        .neq("status", "closed")
        .order("updated_at", desc=True)
        .limit(50)
    )

    kb = types.InlineKeyboardMarkup(row_width=1)

    for ticket in rows:
        kb.add(inline_button(
            f"#{ticket['id']} — {ticket['status']}",
            f"ticketstaff:{ticket['id']}"
        ))

    kb.add(inline_button("⬅ Назад", "admin"))
    bot.send_message(chat_id, "🎫 Открытые тикеты:", reply_markup=kb)


def display_ticket(chat_id, ticket_id, staff_view=False):
    rows = run_query(
        db.table("tickets")
        .select("*")
        .eq("id", int(ticket_id))
        .limit(1)
    )

    if not rows:
        bot.send_message(chat_id, "Тикет не найден.")
        return

    ticket = rows[0]

    if not staff_view and int(ticket["telegram_id"]) != chat_id:
        bot.send_message(chat_id, "Это не твой тикет.")
        return

    messages = run_query(
        db.table("ticket_messages")
        .select("*")
        .eq("ticket_id", int(ticket_id))
        .order("created_at")
    )

    output = [f"🎫 Тикет #{ticket_id} — {ticket['status']}"]

    for msg in messages:
        role = msg["sender_role"]

        if role == "user":
            label = "Ты" if not staff_view else "Пользователь"
        else:
            label = role_title(role)

        output.append(
            f"\n<b>{escape(label)}:</b>\n{escape(msg['message'])}"
        )

    kb = types.InlineKeyboardMarkup()

    if ticket["status"] != "closed":
        kb.add(inline_button(
            "💬 Ответить",
            f"ticketreply:{ticket_id}"
        ))
        if staff_view:
            kb.add(inline_button(
                "🔒 Закрыть",
                f"ticketclose:{ticket_id}"
            ))

    bot.send_message(
        chat_id,
        "\n".join(output),
        reply_markup=kb
    )


# =========================================================
# STAFF MANAGEMENT
# =========================================================

def list_users(chat_id, page=0):
    offset = page * 10

    rows = run_query(
        db.table("users")
        .select("telegram_id,username,full_name")
        .order("created_at", desc=True)
        .range(offset, offset + 9)
    )

    kb = types.InlineKeyboardMarkup(row_width=1)

    for user in rows:
        user_id = int(user["telegram_id"])
        role = get_role(user_id)
        label = user.get("full_name") or user.get("username") or str(user_id)
        username = f"@{user['username']}" if user.get("username") else "без username"

        kb.add(inline_button(
            f"{label} ({username}) · {role_title(role)}",
            f"userselect:{user_id}"
        ))

    nav = []
    if page > 0:
        nav.append(inline_button("⬅", f"userlist:{page-1}"))
    if len(rows) == 10:
        nav.append(inline_button("➡", f"userlist:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.add(inline_button("⬅ Назад", "admin"))

    bot.send_message(
        chat_id,
        "👥 Список пользователей. Выбери пользователя:",
        reply_markup=kb
    )


def show_user_management(chat_id, target_id):
    user = get_user(target_id)
    if not user:
        bot.send_message(chat_id, "Пользователь не найден.")
        return

    role = get_role(target_id)
    label = user.get("full_name") or "Без имени"
    username = "@" + user["username"] if user.get("username") else "не указан"

    kb = types.InlineKeyboardMarkup(row_width=1)

    if is_owner(chat_id) and int(target_id) != OWNER_ID:
        kb.add(inline_button("👤 Назначить пользователем", f"setrole:user:{target_id}"))
        kb.add(inline_button("🛡 Назначить модератором", f"setrole:moderator:{target_id}"))
        kb.add(inline_button("🔧 Назначить администратором", f"setrole:admin:{target_id}"))
        if role != "user":
            kb.add(inline_button("❌ Снять роль", f"setrole:user:{target_id}"))

    # Финансовые операции, дни и блокировки — только владельцу.
    if is_owner(chat_id) and int(target_id) != OWNER_ID:
        kb.add(inline_button("➕ Изменить баланс", f"staffbalance:{target_id}"))
        kb.add(inline_button("📅 Добавить дни", f"staffdays:{target_id}"))
        kb.add(inline_button("🚫 Блокировать/разблокировать", f"staffblock:{target_id}"))

    kb.add(inline_button("⬅ К пользователям", "userlist:0"))

    bot.send_message(
        chat_id,
        f"👤 {escape(label)}\n"
        f"Username: {escape(username)}\n"
        f"ID: <code>{target_id}</code>\n"
        f"Роль: {role_title(role)}\n"
        f"Баланс: {get_balance(target_id)} ₽",
        reply_markup=kb
    )


def set_role(actor_id, target_id, role):
    if not is_owner(actor_id):
        raise PermissionError("Только владелец может менять роли")

    if int(target_id) == OWNER_ID:
        raise ValueError("Нельзя изменить роль владельца")

    if role not in ("user", "moderator", "admin"):
        raise ValueError("Неизвестная роль")

    if role == "user":
        run_query(
            db.table("bot_roles")
            .delete()
            .eq("telegram_id", int(target_id))
        )
    else:
        run_query(
            db.table("bot_roles").upsert({
                "telegram_id": int(target_id),
                "role": role,
                "assigned_by": int(actor_id)
            }, on_conflict="telegram_id")
        )


# =========================================================
# ADMIN KEYBOARDS
# =========================================================

def admin_keyboard(user_id):
    role = get_role(user_id)
    kb = types.InlineKeyboardMarkup(row_width=2)

    if role == "moderator":
        kb.add(inline_button("🎫 Тикеты", "ticketlist"))
        kb.add(inline_button("⬅ Главное меню", "home"))
        return kb

    if role == "admin":
        kb.add(inline_button("🎫 Тикеты", "ticketlist"))
        kb.add(inline_button("🎉 Конкурсы", "giveaways"))
        kb.add(inline_button("🎁 Запросить промокод", "promorequest"))
        kb.add(inline_button("📢 Рассылка", "broadcast"))
        kb.add(inline_button("⬅ Главное меню", "home"))
        return kb

    if role == "owner":
        kb.add(inline_button("📊 Статистика", "stats"), inline_button("👥 Пользователи", "userlist:0"))
        kb.add(inline_button("🎫 Тикеты", "ticketlist"), inline_button("📢 Рассылка", "broadcast"))
        kb.add(inline_button("🎁 Промокоды", "promolist"), inline_button("🎉 Конкурсы", "giveaways"))
        kb.add(inline_button("🛠 Техработы", "maintenance"), inline_button("🔎 Поиск", "usersearch"))
        kb.add(inline_button("👑 Управление ролями", "userlist:0"), inline_button("📨 Запросы промокодов", "promorequests"))
    kb.add(inline_button("⬅ Главное меню", "home"))
    return kb


# =========================================================
# GIVEAWAYS
# =========================================================

def create_giveaway(title, reward_type, reward_value, days,
                    item_name, winners_count, duration_minutes, creator):
    ends_at = (
        now_utc() + timedelta(minutes=int(duration_minutes))
    ).isoformat()

    rows = run_query(
        db.table("giveaways").insert({
            "title": title,
            "reward_type": reward_type,
            "reward_value": float(reward_value),
            "subscription_days": days,
            "item_name": item_name,
            "winners_count": int(winners_count),
            "participants_count": 0,
            "status": "active",
            "ends_at": ends_at,
            "created_by": int(creator)
        })
    )

    return rows[0]["id"]


def join_giveaway(giveaway_id, user_id):
    rows = run_query(
        db.table("giveaways")
        .select("*")
        .eq("id", int(giveaway_id))
        .eq("status", "active")
        .limit(1)
    )

    if not rows:
        raise ValueError("Конкурс завершён")

    giveaway = rows[0]
    end = parse_datetime(giveaway.get("ends_at"))

    if end and end <= now_utc():
        finish_giveaway(giveaway_id)
        raise ValueError("Время участия закончилось")

    run_query(
        db.table("giveaway_participants").insert({
            "giveaway_id": int(giveaway_id),
            "telegram_id": int(user_id)
        })
    )

    count = len(run_query(
        db.table("giveaway_participants")
        .select("id")
        .eq("giveaway_id", int(giveaway_id))
    ))

    run_query(
        db.table("giveaways")
        .update({"participants_count": count})
        .eq("id", int(giveaway_id))
    )


def deliver_prize(giveaway, winner_id):
    kind = giveaway["reward_type"]

    if kind == "balance":
        change_balance(
            winner_id,
            Decimal(str(giveaway.get("reward_value") or 0))
        )

    elif kind == "subscription":
        activate_subscription(
            winner_id,
            int(giveaway.get("subscription_days") or 1)
        )

    elif kind == "item":
        # Выдача предметов во внешней игре выполняется только
        # через официальный API игры, если он подключён.
        bot.send_message(
            winner_id,
            "Ты выиграл предмет: "
            + escape(giveaway.get("item_name") or giveaway.get("title") or "Предмет")
            + ". Автоматическая выдача ожидает подключения API игры."
        )
        return False

    return True


def finish_giveaway(giveaway_id):
    rows = run_query(
        db.table("giveaways")
        .select("*")
        .eq("id", int(giveaway_id))
        .eq("status", "active")
        .limit(1)
    )

    if not rows:
        return

    giveaway = rows[0]

    participants = run_query(
        db.table("giveaway_participants")
        .select("telegram_id")
        .eq("giveaway_id", int(giveaway_id))
    )

    ids = list({
        int(x["telegram_id"])
        for x in participants
    })

    if not ids:
        run_query(
            db.table("giveaways")
            .update({"status": "finished"})
            .eq("id", int(giveaway_id))
        )
        return

    winners = random.sample(
        ids,
        min(len(ids), int(giveaway["winners_count"]))
    )

    for winner_id in winners:
        rows = run_query(
            db.table("giveaway_winners").insert({
                "giveaway_id": int(giveaway_id),
                "telegram_id": winner_id,
                "prize_delivered": False
            })
        )

        delivered = deliver_prize(giveaway, winner_id)

        if delivered:
            run_query(
                db.table("giveaway_winners")
                .update({
                    "prize_delivered": True,
                    "delivered_at": iso_now()
                })
                .eq("id", rows[0]["id"])
            )

        try:
            bot.send_message(
                winner_id,
                f"🎉 Ты выиграл конкурс «{escape(giveaway['title'])}»!"
            )
        except Exception:
            pass

    run_query(
        db.table("giveaways")
        .update({"status": "finished"})
        .eq("id", int(giveaway_id))
    )


def giveaway_worker():
    while True:
        try:
            rows = run_query(
                db.table("giveaways")
                .select("*")
                .eq("status", "active")
            )

            for giveaway in rows:
                end = parse_datetime(giveaway.get("ends_at"))
                if end and end <= now_utc():
                    finish_giveaway(giveaway["id"])

        except Exception:
            logging.exception("Giveaway worker error")

        time.sleep(30)


# =========================================================
# CALLBACKS
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):
    if not ensure_user_access(message):
        return

    save_user(message.from_user)
    user_id = message.from_user.id

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload = args[1].strip()

        if payload.startswith("promo_"):
            code = payload[6:]
            try:
                promo = activate_promo(code, user_id)
                bot.send_message(
                    message.chat.id,
                    f"Промокод {escape(promo['code'])} активирован!"
                )
            except Exception as exc:
                bot.send_message(message.chat.id, escape(exc))

        elif payload.startswith("giveaway_"):
            giveaway_id = payload[9:]
            try:
                join_giveaway(giveaway_id, user_id)
                bot.send_message(message.chat.id, "Ты участвуешь в конкурсе!")
            except Exception as exc:
                bot.send_message(message.chat.id, escape(exc))

        elif payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                if referrer_id != user_id:
                    user = get_user(user_id)
                    if user and not user.get("referrer_id"):
                        run_query(
                            db.table("users")
                            .update({"referrer_id": referrer_id})
                            .eq("telegram_id", user_id)
                        )
            except Exception:
                pass

    if not check_channel(user_id):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "📢 Подписаться",
            url="https://t.me/ToxiVPN"
        ))
        kb.add(inline_button("✅ Проверить", "checkchannel"))
        bot.send_message(
            message.chat.id,
            "Сначала подпишись на канал ToxiVPN.",
            reply_markup=kb
        )
        return

    send_menu(
        message.chat.id,
        user_id,
        "Добро пожаловать в ToxiVPN!"
    )


@bot.message_handler(commands=["status", "stat"])
def status_command(message):
    if maintenance_enabled() and not is_staff(message.from_user.id):
        bot.send_message(message.chat.id, "🛠 Ведутся технические работы.")
        return

    sub = get_subscription(message.from_user.id)
    if not sub:
        bot.send_message(message.chat.id, "Подписка не найдена.")
        return

    expire = parse_datetime(sub.get("expire"))
    expire_text = expire.strftime("%d.%m.%Y %H:%M UTC") if expire else "не задан"

    bot.send_message(
        message.chat.id,
        f"Статус: {subscription_reason(sub) or 'активна'}\n"
        f"Окончание: {expire_text}\n"
        f"Трафик: {sub.get('traffic_used_gb', 0)} / "
        f"{sub.get('traffic_limit_gb', DEFAULT_TRAFFIC_GB)} GB\n"
        f"Устройств: максимум {sub.get('max_devices', DEFAULT_MAX_DEVICES)}"
    )


@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_staff(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        "🛠 Панель управления",
        reply_markup=admin_keyboard(message.from_user.id)
    )


@bot.message_handler(func=lambda m: m.from_user.id in states)
def state_handler(message):
    user_id = message.from_user.id
    state = states.get(user_id)

    if not state:
        return

    if state["action"] == "promo":
        del states[user_id]
        try:
            promo = activate_promo(message.text.strip(), user_id)
            bot.send_message(
                message.chat.id,
                f"Промокод {escape(promo['code'])} активирован."
            )
        except Exception as exc:
            bot.send_message(message.chat.id, escape(exc))
        return

    if state["action"] == "ticket_new":
        del states[user_id]
        ticket_id = create_ticket(user_id)
        ticket_message(ticket_id, user_id, "user", message.text)
        notify_staff_ticket(ticket_id, user_id, message.text)
        bot.send_message(
            message.chat.id,
            f"Тикет #{ticket_id} создан. Ответ придёт сюда."
        )
        return

    if state["action"] == "ticket_reply":
        ticket_id = int(state["ticket_id"])
        del states[user_id]

        ticket_rows = run_query(
            db.table("tickets")
            .select("*")
            .eq("id", ticket_id)
            .limit(1)
        )

        if not ticket_rows:
            bot.send_message(message.chat.id, "Тикет не найден.")
            return

        ticket = ticket_rows[0]
        role = get_role(user_id)

        if role == "user":
            if int(ticket["telegram_id"]) != user_id:
                bot.send_message(message.chat.id, "Нет доступа.")
                return
            sender_role = "user"
        else:
            if not is_staff(user_id):
                bot.send_message(message.chat.id, "Нет доступа.")
                return
            sender_role = role

        ticket_message(ticket_id, user_id, sender_role, message.text)

        if sender_role == "user":
            notify_staff_ticket(ticket_id, user_id, message.text)
            bot.send_message(message.chat.id, "Сообщение отправлено поддержке.")
        else:
            # Пользователю показываем должность, не имя и username сотрудника.
            try:
                bot.send_message(
                    int(ticket["telegram_id"]),
                    f"Ответ поддержки ({role_title(sender_role)}):\n\n"
                    f"{escape(message.text)}"
                )
            except Exception:
                pass

            bot.send_message(message.chat.id, "Ответ отправлен.")

        return

    if state["action"] == "search":
        del states[user_id]

        if not is_admin(user_id):
            return

        query = message.text.strip()
        if query.startswith("@"):
            query = query[1:]

        rows = run_query(
            db.table("users")
            .select("telegram_id,username,full_name")
            .ilike("username", query)
            .limit(20)
        )

        kb = types.InlineKeyboardMarkup(row_width=1)

        for user in rows:
            kb.add(inline_button(
                f"{user.get('full_name') or user.get('username') or user['telegram_id']}",
                f"userselect:{user['telegram_id']}"
            ))

        bot.send_message(
            message.chat.id,
            "Результаты поиска:",
            reply_markup=kb
        )
        return

    if state["action"] == "broadcast":
        del states[user_id]

        if not is_admin(user_id):
            return

        pending_broadcasts[user_id] = {
            "text": message.text,
            "buttons": []
        }

        kb = types.InlineKeyboardMarkup()
        kb.add(inline_button("➕ Добавить URL-кнопку", "broadcastaddbutton"))
        kb.add(inline_button("📨 Отправить рассылку", "broadcastsend"))
        kb.add(inline_button("❌ Отмена", "admin"))

        bot.send_message(
            message.chat.id,
            "Текст рассылки сохранён.",
            reply_markup=kb
        )
        return

    if state["action"] == "broadcast_button":
        del states[user_id]

        if not is_admin(user_id):
            return

        try:
            label, url = message.text.split("|", 1)
            if not url.startswith(("https://", "http://", "tg://")):
                raise ValueError("URL должен начинаться с https://, http:// или tg://")

            pending_broadcasts[user_id]["buttons"].append({
                "text": label.strip(),
                "url": url.strip()
            })

            kb = types.InlineKeyboardMarkup()
            kb.add(inline_button("➕ Ещё кнопка", "broadcastaddbutton"))
            kb.add(inline_button("📨 Отправить", "broadcastsend"))
            kb.add(inline_button("❌ Отмена", "admin"))

            bot.send_message(
                message.chat.id,
                "Кнопка добавлена.",
                reply_markup=kb
            )

        except Exception as exc:
            bot.send_message(
                message.chat.id,
                f"Формат: Название кнопки | https://example.com\n{escape(exc)}"
            )
        return

    if state["action"] == "create_promo":
        del states[user_id]

        if not is_owner(user_id):
            return

        try:
            # CODE TYPE VALUE MAX_USES TARGET_ID
            parts = message.text.strip().split()

            if len(parts) < 4:
                raise ValueError(
                    "Формат: CODE TYPE VALUE MAX_USES [TELEGRAM_ID]"
                )

            code = parts[0]
            reward_type = parts[1].lower()
            value = Decimal(parts[2])
            max_uses = None if parts[3].lower() == "unlimited" else int(parts[3])
            target_id = int(parts[4]) if len(parts) > 4 else None

            if reward_type not in ("discount", "balance", "subscription"):
                raise ValueError("TYPE: discount, balance или subscription")

            link = create_promo(
                code,
                reward_type,
                value,
                max_uses,
                target_id,
                user_id
            )

            bot.send_message(
                message.chat.id,
                "Промокод создан."
                + (f"\nПерсональная ссылка: {escape(link)}" if link else "")
            )

        except Exception as exc:
            bot.send_message(message.chat.id, escape(exc))
        return

    if state["action"] == "promo_request":
        del states[user_id]

        if get_role(user_id) != "admin":
            return

        payload = {
            "text": message.text,
            "requester_id": user_id
        }

        rows = run_query(
            db.table("promo_requests").insert({
                "requester_id": user_id,
                "promo_data": payload,
                "status": "pending"
            })
        )

        request_id = rows[0]["id"]

        kb = types.InlineKeyboardMarkup()
        kb.add(
            inline_button("✅ Согласиться", f"promoreqapprove:{request_id}"),
            inline_button("❌ Отказать", f"promoreqreject:{request_id}")
        )

        if OWNER_ID:
            bot.send_message(
                OWNER_ID,
                f"Администратор {escape(message.from_user.first_name)} "
                f"хочет создать промокод:\n\n{escape(message.text)}",
                reply_markup=kb
            )

        bot.send_message(
            message.chat.id,
            "Запрос отправлен владельцу на рассмотрение."
        )
        return

    if state["action"] == "maintenance_message":
        del states[user_id]

        if not is_admin(user_id):
            return

        rows = run_query(
            db.table("bot_settings")
            .select("setting_value")
            .eq("setting_key", "maintenance")
            .limit(1)
        )

        current = rows[0]["setting_value"] if rows else {}

        run_query(
            db.table("bot_settings").upsert({
                "setting_key": "maintenance",
                "setting_value": {
                    "enabled": bool(current.get("enabled")),
                    "message": message.text
                },
                "updated_by": user_id,
                "updated_at": iso_now()
            }, on_conflict="setting_key")
        )

        bot.send_message(message.chat.id, "Текст техработ сохранён.")
        return


@bot.message_handler(content_types=["web_app_data"])
def handle_mini_app_action(message):
    """Bridge Mini App menu taps to the existing bot callback menu safely."""
    try:
        payload = json.loads(message.web_app_data.data or "{}")
        action = str(payload.get("action", ""))
    except (ValueError, TypeError, AttributeError):
        bot.send_message(message.chat.id, "Не удалось прочитать действие Mini App. Открой главное меню бота.", reply_markup=main_keyboard(message.from_user.id))
        return

    allowed = {"buy", "trial", "profile", "connect", "balance", "promo", "referrals", "tickets", "faq", "documents"}
    if action not in allowed:
        bot.send_message(message.chat.id, "Это действие недоступно.", reply_markup=main_keyboard(message.from_user.id))
        return

    # Mini App sends no user-supplied IDs or payment values; all sensitive work stays in bot callbacks.
    labels = {
        "buy": "💳 Купить подписку", "trial": "🎟 Пробник", "profile": "👤 Моя подписка",
        "connect": "🔗 Ссылка подписки", "balance": "💎 Баланс", "promo": "🎁 Промокод",
        "referrals": "👥 Рефералы", "tickets": "🎫 Тикеты", "faq": "📚 FAQ", "documents": "📜 Документы"
    }
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(inline_button(labels[action], action))
    keyboard.add(inline_button("↩️ Главное меню", "home"))
    bot.send_message(message.chat.id, "Открой действие кнопкой ниже — оно выполнится в защищённом меню ToxiVPN.", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda c: True)
def callbacks(call):
    user_id = call.from_user.id
    data = call.data or ""

    safe_answer(call)

    try:
        save_user(call.from_user)
    except Exception:
        logging.exception("User save failed")

    if data == "home":
        edit_or_send(
            call,
            "Главное меню ToxiVPN",
            main_keyboard(user_id)
        )
        return

    if data == "checkchannel":
        if check_channel(user_id):
            edit_or_send(
                call,
                "Подписка проверена.",
                main_keyboard(user_id)
            )
        else:
            safe_answer(call, "Подпишись на канал.", True)
        return

    if maintenance_enabled() and not is_staff(user_id):
        safe_answer(call, "Ведутся технические работы.", True)
        return

    if user_is_blocked(user_id):
        safe_answer(call, "Доступ ограничен.", True)
        return

    if data == "profile":
        user = get_user(user_id)
        sub = get_subscription(user_id)
        expire = parse_datetime(sub.get("expire")) if sub else None
        expire_text = (
            expire.strftime("%d.%m.%Y %H:%M UTC")
            if expire else "не задан"
        )

        reason = subscription_reason(sub) if sub else "missing"

        kb = types.InlineKeyboardMarkup()
        kb.add(inline_button("🔗 Получить ссылку", "connect"))
        kb.add(inline_button("💳 Купить", "buy"))
        kb.add(inline_button("⬅ Назад", "home"))

        edit_or_send(
            call,
            f"👤 <b>Профиль</b>\n"
            f"ID: <code>{user_id}</code>\n"
            f"Баланс: {get_balance(user_id)} ₽\n"
            f"Статус: {escape(reason or 'активна')}\n"
            f"До: {expire_text}\n"
            f"Трафик: {sub.get('traffic_used_gb', 0)} / "
            f"{sub.get('traffic_limit_gb', DEFAULT_TRAFFIC_GB)} GB\n"
            f"Лимит устройств: {sub.get('max_devices', DEFAULT_MAX_DEVICES)}",
            kb
        )
        return

    if data == "balance":
        kb = types.InlineKeyboardMarkup()
        kb.add(inline_button("➕ Пополнить баланс", "topup"))
        kb.add(inline_button("⬅ Назад", "home"))
        edit_or_send(
            call,
            f"💰 Баланс: {get_balance(user_id)} ₽",
            kb
        )
        return

    if data == "topup":
        states[user_id] = {"action": "topup"}
        bot.send_message(
            call.message.chat.id,
            f"Минимум: {MIN_TOPUP} ₽\n"
            f"Максимум: {MAX_TOPUP} ₽\n"
            "Пополнение через внутренний баланс пока недоступно без "
            "подключённого платёжного провайдера."
        )
        return

    if data == "buy":
        show_tariffs(call.message.chat.id, user_id)
        return

    if data.startswith("buybal:"):
        try:
            _, tariff_key, recipient = data.split(":", 2)
            recipient = int(recipient)

            if recipient != user_id and not get_user(recipient):
                raise ValueError("Получатель ещё не запускал бота")

            purchase_with_balance(user_id, recipient, tariff_key)

            bot.send_message(
                call.message.chat.id,
                "Оплата с баланса прошла успешно.\n"
                f"Подписка активирована для {recipient}."
            )

            if recipient != user_id:
                try:
                    bot.send_message(
                        recipient,
                        "🎉 Тебе подарили подписку ToxiVPN!"
                    )
                except Exception:
                    pass

        except Exception as exc:
            safe_answer(call, str(exc), True)
        return

    if data == "trial":
        sub = get_subscription(user_id)

        if sub and sub.get("used_trial"):
            safe_answer(call, "Ты уже использовал пробник.", True)
            return

        activate_subscription(user_id, TRIAL_DAYS)

        run_query(
            db.table("subscriptions")
            .update({"used_trial": True})
            .eq("telegram_id", user_id)
        )

        edit_or_send(
            call,
            "🎟 Пробная подписка активирована на 1 день.\n"
            "Лимит трафика: 50 GB.",
            main_keyboard(user_id)
        )
        return

    if data == "connect":
        sub = get_subscription(user_id)

        if not sub:
            safe_answer(call, "Подписка не найдена.", True)
            return

        reason = subscription_reason(sub)
        if reason:
            safe_answer(call, "Подписка неактивна. Купи или активируй пробник.", True)
            return

        url = subscription_http_url(sub)
        happ = happ_link_for_url(url)

        kb = types.InlineKeyboardMarkup()

        if happ:
            kb.add(types.InlineKeyboardButton(
                "Открыть в Happ",
                url=happ
            ))
            edit_or_send(
                call,
                "🔗 Твоя зашифрованная ссылка Happ Crypto:",
                kb
            )
        else:
            edit_or_send(
                call,
                "Happ Crypto не настроен. Проверь "
                "HAPP_CRYPTO_V4_PUBLIC_KEY и адрес сервиса в Railway.",
                main_keyboard(user_id)
            )
        return

    if data == "promo":
        states[user_id] = {"action": "promo"}
        bot.send_message(
            call.message.chat.id,
            "Отправь промокод сообщением."
        )
        return

    if data == "referrals":
        rows = run_query(
            db.table("users")
            .select("telegram_id")
            .eq("referrer_id", user_id)
        )

        me = bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user_id}"

        kb = types.InlineKeyboardMarkup()
        kb.add(inline_button("⬅ Назад", "home"))

        edit_or_send(
            call,
            f"👥 Рефералов: {len(rows)}\n"
            f"Бонус: {REFERRAL_PERCENT}%\n\n"
            f"Твоя ссылка:\n<code>{escape(link)}</code>",
            kb
        )
        return

    if data == "faq":
        edit_or_send(
            call,
            "📚 FAQ\n\n"
            "1. Активируй подписку.\n"
            "2. Открой ссылку в Happ.\n"
            "3. Если подписка закончилась, продли её.\n"
            "4. Если возникла проблема, создай тикет.",
            main_keyboard(user_id)
        )
        return

    if data == "documents":
        edit_or_send(
            call,
            "📜 Документы и условия предоставляются службой поддержки.",
            main_keyboard(user_id)
        )
        return

    if data == "tickets":
        bot.send_message(
            call.message.chat.id,
            "🎫 Твои обращения:",
            reply_markup=user_tickets_keyboard(user_id)
        )
        return

    if data == "ticketnew":
        states[user_id] = {"action": "ticket_new"}
        bot.send_message(
            call.message.chat.id,
            "Опиши проблему одним сообщением. "
            "Сотрудники увидят обращение без твоего username."
        )
        return

    if data.startswith("ticketuser:"):
        ticket_id = int(data.split(":")[1])
        display_ticket(call.message.chat.id, ticket_id, False)
        return

    if data == "admin":
        if not is_staff(user_id):
            return
        edit_or_send(
            call,
            f"🛠 Панель управления\nРоль: {role_title(get_role(user_id))}",
            admin_keyboard(user_id)
        )
        return

    if data == "stats":
        if not is_owner(user_id):
            safe_answer(call, "Статистика доступна только владельцу.", True)
            return

        users = run_query(
            db.table("users").select("telegram_id")
        )
        subs = run_query(
            db.table("subscriptions").select("telegram_id")
        )
        tickets = run_query(
            db.table("tickets").select("id").eq("status", "open")
        )
        promos = run_query(
            db.table("promos").select("id").eq("is_active", True)
        )

        edit_or_send(
            call,
            f"📊 Статистика\n"
            f"Пользователи: {len(users)}\n"
            f"Подписки: {len(subs)}\n"
            f"Открытые тикеты: {len(tickets)}\n"
            f"Активные промокоды: {len(promos)}",
            admin_keyboard(user_id)
        )
        return

    if data.startswith("userlist:"):
        if not is_owner(user_id):
            safe_answer(call, "Список пользователей доступен только владельцу.", True)
            return
        list_users(
            call.message.chat.id,
            int(data.split(":")[1])
        )
        return

    if data.startswith("userselect:"):
        if not is_owner(user_id):
            safe_answer(call, "Управление пользователями доступно только владельцу.", True)
            return
        show_user_management(
            call.message.chat.id,
            int(data.split(":")[1])
        )
        return

    if data.startswith("setrole:"):
        if not is_owner(user_id):
            return

        _, role, target = data.split(":")
        target = int(target)

        try:
            set_role(user_id, target, role)
            safe_answer(call, "Роль обновлена.")
            show_user_management(call.message.chat.id, target)
        except Exception as exc:
            safe_answer(call, str(exc), True)
        return

    if data == "ticketlist":
        if not is_staff(user_id):
            return
        list_staff_tickets(call.message.chat.id)
        return

    if data.startswith("ticketstaff:"):
        if not is_staff(user_id):
            return
        display_ticket(
            call.message.chat.id,
            int(data.split(":")[1]),
            True
        )
        return

    if data.startswith("ticketreply:"):
        ticket_id = int(data.split(":")[1])

        rows = run_query(
            db.table("tickets")
            .select("*")
            .eq("id", ticket_id)
            .limit(1)
        )

        if not rows:
            safe_answer(call, "Тикет не найден.", True)
            return

        ticket = rows[0]
        role = get_role(user_id)

        if role == "user" and int(ticket["telegram_id"]) != user_id:
            safe_answer(call, "Нет доступа.", True)
            return

        if role == "moderator" or role in ("admin", "owner"):
            pass
        elif int(ticket["telegram_id"]) != user_id:
            safe_answer(call, "Нет доступа.", True)
            return

        states[user_id] = {
            "action": "ticket_reply",
            "ticket_id": ticket_id
        }

        bot.send_message(
            call.message.chat.id,
            f"Напиши ответ для тикета #{ticket_id}."
        )
        return

    if data.startswith("ticketclose:"):
        if not is_staff(user_id):
            return

        ticket_id = int(data.split(":")[1])

        rows = run_query(
            db.table("tickets")
            .select("telegram_id")
            .eq("id", ticket_id)
            .limit(1)
        )

        if rows:
            run_query(
                db.table("tickets")
                .update({
                    "status": "closed",
                    "updated_at": iso_now()
                })
                .eq("id", ticket_id)
            )

            try:
                bot.send_message(
                    int(rows[0]["telegram_id"]),
                    f"Тикет #{ticket_id} закрыт поддержкой."
                )
            except Exception:
                pass

        safe_answer(call, "Тикет закрыт.")
        return

    if data == "broadcast":
        if not is_admin(user_id):
            return

        states[user_id] = {"action": "broadcast"}
        bot.send_message(
            call.message.chat.id,
            "Отправь текст рассылки одним сообщением."
        )
        return

    if data == "broadcastaddbutton":
        if not is_admin(user_id):
            return

        if user_id not in pending_broadcasts:
            safe_answer(call, "Сначала напиши текст рассылки.", True)
            return

        states[user_id] = {"action": "broadcast_button"}
        bot.send_message(
            call.message.chat.id,
            "Отправь кнопку в формате:\n"
            "Название кнопки | https://example.com"
        )
        return

    if data == "broadcastsend":
        if not is_admin(user_id):
            return

        payload = pending_broadcasts.pop(user_id, None)
        if not payload:
            safe_answer(call, "Нет подготовленной рассылки.", True)
            return

        rows = run_query(
            db.table("broadcasts").insert({
                "created_by": user_id,
                "message_text": payload["text"],
                "buttons": payload["buttons"],
                "status": "sending"
            })
        )

        kb = types.InlineKeyboardMarkup(row_width=1)
        for button in payload["buttons"]:
            kb.add(types.InlineKeyboardButton(
                text=button["text"],
                url=button["url"]
            ))

        users = run_query(
            db.table("users").select("telegram_id")
        )

        sent = 0
        for user in users:
            try:
                bot.send_message(
                    int(user["telegram_id"]),
                    payload["text"],
                    reply_markup=kb if payload["buttons"] else None
                )
                sent += 1
                time.sleep(0.04)
            except Exception:
                continue

        run_query(
            db.table("broadcasts")
            .update({"status": "sent"})
            .eq("id", rows[0]["id"])
        )

        bot.send_message(
            call.message.chat.id,
            f"Рассылка завершена. Отправлено: {sent}.",
            reply_markup=admin_keyboard(user_id)
        )
        return

    if data == "maintenance":
        if not is_owner(user_id):
            safe_answer(call, "Техработы доступны только владельцу.", True)
            return

        rows = run_query(
            db.table("bot_settings")
            .select("setting_value")
            .eq("setting_key", "maintenance")
            .limit(1)
        )

        setting = rows[0]["setting_value"] if rows else {
            "enabled": False,
            "message": "Ведутся технические работы"
        }

        enabled = not bool(setting.get("enabled"))

        run_query(
            db.table("bot_settings").upsert({
                "setting_key": "maintenance",
                "setting_value": {
                    "enabled": enabled,
                    "message": setting.get(
                        "message",
                        "Ведутся технические работы"
                    )
                },
                "updated_by": user_id,
                "updated_at": iso_now()
            }, on_conflict="setting_key")
        )

        kb = types.InlineKeyboardMarkup()
        kb.add(inline_button(
            "✏ Изменить текст",
            "maintenancemessage"
        ))
        kb.add(inline_button("⬅ Назад", "admin"))

        edit_or_send(
            call,
            "Технические работы: "
            + ("ВКЛЮЧЕНЫ" if enabled else "ВЫКЛЮЧЕНЫ"),
            kb
        )
        return

    if data == "maintenancemessage":
        if not is_owner(user_id):
            safe_answer(call, "Только владелец может менять это сообщение.", True)
            return

        states[user_id] = {"action": "maintenance_message"}
        bot.send_message(
            call.message.chat.id,
            "Отправь новый текст сообщения о технических работах."
        )
        return

    if data == "usersearch":
        if not is_owner(user_id):
            safe_answer(call, "Поиск пользователей доступен только владельцу.", True)
            return

        states[user_id] = {"action": "search"}
        bot.send_message(
            call.message.chat.id,
            "Введи username пользователя без @."
        )
        return

    if data == "promolist":
        if not is_admin(user_id):
            return

        rows = run_query(
            db.table("promos")
            .select("*")
            .order("created_at", desc=True)
            .limit(30)
        )

        kb = types.InlineKeyboardMarkup(row_width=1)

        for promo in rows:
            max_uses = promo.get("max_activations")
            max_text = str(max_uses) if max_uses is not None else "∞"
            target = promo.get("target_telegram_id")
            target_text = str(target) if target else "все"

            kb.add(inline_button(
                f"{promo['code']} · {promo['reward_type']} · "
                f"{promo['activations']}/{max_text} · {target_text}",
                f"promodetail:{promo['id']}"
            ))

        if is_owner(user_id):
            kb.add(inline_button("➕ Создать промокод", "promocreate"))
        elif get_role(user_id) == "admin":
            kb.add(inline_button("📨 Запросить промокод", "promorequest"))

        kb.add(inline_button("⬅ Назад", "admin"))

        edit_or_send(call, "🎁 Промокоды:", kb)
        return

    if data == "promocreate":
        if not is_owner(user_id):
            return

        states[user_id] = {"action": "create_promo"}

        bot.send_message(
            call.message.chat.id,
            "Формат:\n"
            "CODE TYPE VALUE MAX_USES [TELEGRAM_ID]\n\n"
            "TYPE: balance или subscription\n"
            "Примеры:\n"
            "GIFT50 balance 50 100\n"
            "DAY1 subscription 1 100\n"
            "PERSONAL balance 100 1 123456789\n"
            "Для бесконечных активаций укажи unlimited."
        )
        return

    if data == "promorequest":
        if get_role(user_id) != "admin":
            return

        states[user_id] = {"action": "promo_request"}
        bot.send_message(
            call.message.chat.id,
            "Опиши промокод, который хочешь создать. "
            "Владелец получит запрос и сможет одобрить его."
        )
        return

    if data == "promorequests":
        if not is_owner(user_id):
            return

        rows = run_query(
            db.table("promo_requests")
            .select("*")
            .eq("status", "pending")
            .order("created_at")
            .limit(30)
        )

        kb = types.InlineKeyboardMarkup(row_width=1)

        for item in rows:
            payload = item.get("promo_data") or {}
            kb.add(inline_button(
                f"Запрос #{item['id']} от {item['requester_id']}",
                f"promoreqview:{item['id']}"
            ))

        kb.add(inline_button("⬅ Назад", "admin"))
        edit_or_send(call, "📨 Запросы на промокоды:", kb)
        return

    if data.startswith("promoreqview:"):
        if not is_owner(user_id):
            return

        request_id = int(data.split(":")[1])

        rows = run_query(
            db.table("promo_requests")
            .select("*")
            .eq("id", request_id)
            .limit(1)
        )

        if not rows:
            safe_answer(call, "Запрос не найден.", True)
            return

        item = rows[0]
        payload = item.get("promo_data") or {}

        kb = types.InlineKeyboardMarkup()
        kb.add(
            inline_button("✅ Одобрить", f"promoreqapprove:{request_id}"),
            inline_button("❌ Отказать", f"promoreqreject:{request_id}")
        )

        edit_or_send(
            call,
            f"Запрос #{request_id}\n"
            f"Администратор ID: {item['requester_id']}\n"
            f"Описание:\n{escape(payload.get('text', ''))}",
            kb
        )
        return

    if data.startswith(("promoreqapprove:", "promoreqreject:")):
        if not is_owner(user_id):
            return

        action, request_id = data.split(":")
        request_id = int(request_id)
        status = "approved" if action == "promoreqapprove" else "rejected"

        rows = run_query(
            db.table("promo_requests")
            .select("*")
            .eq("id", request_id)
            .eq("status", "pending")
            .limit(1)
        )

        if not rows:
            safe_answer(call, "Запрос уже обработан.", True)
            return

        item = rows[0]

        run_query(
            db.table("promo_requests")
            .update({
                "status": status,
                "reviewed_by": user_id
            })
            .eq("id", request_id)
        )

        try:
            bot.send_message(
                int(item["requester_id"]),
                "Твой запрос на создание промокода "
                + ("одобрен." if status == "approved" else "отклонён.")
            )
        except Exception:
            pass

        if status == "approved":
            states[user_id] = {"action": "create_promo"}
            bot.send_message(
                call.message.chat.id,
                "Запрос одобрен. Теперь отправь параметры промокода:\n"
                "CODE TYPE VALUE MAX_USES [TELEGRAM_ID]"
            )
        else:
            safe_answer(call, "Запрос отклонён.")

        return

    if data == "giveaways":
        if not is_admin(user_id):
            return

        rows = run_query(
            db.table("giveaways")
            .select("*")
            .order("created_at", desc=True)
            .limit(30)
        )

        kb = types.InlineKeyboardMarkup(row_width=1)

        for giveaway in rows:
            kb.add(inline_button(
                f"#{giveaway['id']} {giveaway['title']} "
                f"({giveaway['status']})",
                f"giveawaydetail:{giveaway['id']}"
            ))

        kb.add(inline_button("➕ Создать конкурс", "giveawaycreate"))
        kb.add(inline_button("⬅ Назад", "admin"))

        edit_or_send(call, "🎉 Конкурсы:", kb)
        return

    if data == "giveawaycreate":
        if not is_admin(user_id):
            return

        pending_giveaways[user_id] = {"step": "title"}
        bot.send_message(
            call.message.chat.id,
            "Отправь название конкурса."
        )
        return

    if data.startswith("giveawaydetail:"):
        if not is_admin(user_id):
            return

        giveaway_id = int(data.split(":")[1])

        rows = run_query(
            db.table("giveaways")
            .select("*")
            .eq("id", giveaway_id)
            .limit(1)
        )

        if not rows:
            safe_answer(call, "Конкурс не найден.", True)
            return

        giveaway = rows[0]
        kb = types.InlineKeyboardMarkup()

        if giveaway["status"] == "active":
            kb.add(inline_button(
                "🏁 Завершить сейчас",
                f"giveawayfinish:{giveaway_id}"
            ))

        me = bot.get_me()
        kb.add(types.InlineKeyboardButton(
            "🔗 Ссылка участия",
            url=f"https://t.me/{me.username}?start=giveaway_{giveaway_id}"
        ))

        kb.add(inline_button("⬅ Назад", "giveaways"))

        edit_or_send(
            call,
            f"🎉 {escape(giveaway['title'])}\n"
            f"ID: {giveaway_id}\n"
            f"Приз: {escape(giveaway['reward_type'])}\n"
            f"Участников: {giveaway['participants_count']}\n"
            f"Победителей: {giveaway['winners_count']}\n"
            f"Статус: {giveaway['status']}",
            kb
        )
        return

    if data.startswith("giveawayfinish:"):
        if not is_admin(user_id):
            return

        giveaway_id = int(data.split(":")[1])
        finish_giveaway(giveaway_id)
        safe_answer(call, "Конкурс завершён.")
        return

    if data == "broadcast":
        if not is_admin(user_id):
            return

    if data.startswith("staffbalance:"):
        if not is_owner(user_id):
            safe_answer(call, "Только владелец может менять баланс.", True)
            return

        target = int(data.split(":")[1])
        states[user_id] = {
            "action": "staffbalance",
            "target_id": target
        }

        bot.send_message(
            call.message.chat.id,
            "Введи сумму для изменения баланса. "
            "Положительное число добавит деньги, отрицательное снимет."
        )
        return

    if data.startswith("staffdays:"):
        if not is_owner(user_id):
            safe_answer(call, "Только владелец может выдавать дни.", True)
            return

        target = int(data.split(":")[1])
        states[user_id] = {
            "action": "staffdays",
            "target_id": target
        }

        bot.send_message(
            call.message.chat.id,
            "Введи количество дней для добавления."
        )
        return

    if data.startswith("staffblock:"):
        if not is_owner(user_id):
            safe_answer(call, "Только владелец может блокировать пользователей.", True)
            return

        target = int(data.split(":")[1])
        if target == OWNER_ID:
            safe_answer(call, "Владельца нельзя заблокировать.", True)
            return
        user = get_user(target)

        if not user:
            safe_answer(call, "Пользователь не найден.", True)
            return

        new_value = not bool(user.get("is_blocked"))

        run_query(
            db.table("users")
            .update({"is_blocked": new_value})
            .eq("telegram_id", target)
        )

        safe_answer(
            call,
            "Пользователь заблокирован." if new_value
            else "Пользователь разблокирован."
        )
        show_user_management(call.message.chat.id, target)
        return

    safe_answer(call, "Неизвестная команда.", True)


# =========================================================
# ADMIN TEXT STATES
# =========================================================

@bot.message_handler(
    func=lambda m: m.from_user.id in states
    and states[m.from_user.id].get("action") in ("staffbalance", "staffdays")
)
def staff_state_handler(message):
    user_id = message.from_user.id
    state = states.pop(user_id, None)

    if not state or not is_owner(user_id):
        return

    target_id = int(state["target_id"])
    if target_id == OWNER_ID:
        bot.send_message(message.chat.id, "Нельзя менять ограничения владельца.")
        return

    try:
        value = int(message.text.strip())

        if state["action"] == "staffbalance":
            change_balance(target_id, Decimal(value))
            bot.send_message(message.chat.id, "Баланс обновлён.")

        elif state["action"] == "staffdays":
            if value <= 0:
                raise ValueError("Количество дней должно быть больше нуля.")

            activate_subscription(target_id, value)
            bot.send_message(message.chat.id, "Срок подписки обновлён.")

    except Exception as exc:
        bot.send_message(message.chat.id, escape(exc))


# =========================================================
# GIVEAWAY CREATION WIZARD
# =========================================================

@bot.message_handler(
    func=lambda m: m.from_user.id in pending_giveaways
)
def giveaway_wizard(message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        pending_giveaways.pop(user_id, None)
        return

    state = pending_giveaways[user_id]
    step = state["step"]
    text = message.text.strip()

    if step == "title":
        state["title"] = text
        state["step"] = "type"

        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(inline_button("💰 Рубли на баланс", "gwtype:balance"))
        kb.add(inline_button("🎟 Подписка", "gwtype:subscription"))
        kb.add(inline_button("🎁 Предмет", "gwtype:item"))

        bot.send_message(
            message.chat.id,
            "Выбери тип приза:",
            reply_markup=kb
        )
        return

    if step == "value":
        try:
            if state.get("type") == "item":
                if not text:
                    raise ValueError("Название предмета пустое")
                state["item_name"] = text[:150]
                state["value"] = Decimal("0")
            else:
                value = Decimal(text)
                if value <= 0:
                    raise ValueError("Значение должно быть больше нуля")
                state["value"] = value
            state["step"] = "winners"
            bot.send_message(message.chat.id, "Сколько победителей?")
        except Exception as exc:
            bot.send_message(message.chat.id, escape(str(exc) or "Введи корректное значение."))
        return

    if step == "winners":
        try:
            state["winners"] = max(1, min(100, int(text)))
            state["step"] = "duration"
            bot.send_message(
                message.chat.id,
                "Продолжительность конкурса в минутах?"
            )
        except Exception:
            bot.send_message(message.chat.id, "Введи целое число.")
        return

    if step == "duration":
        try:
            duration = max(1, min(43200, int(text)))

            giveaway_id = create_giveaway(
                title=state["title"],
                reward_type=state["type"],
                reward_value=state.get("value", 0),
                days=(
                    int(state.get("value", 1))
                    if state["type"] == "subscription"
                    else None
                ),
                item_name=(state.get("item_name") if state["type"] == "item" else None),
                winners_count=state["winners"],
                duration_minutes=duration,
                creator=user_id
            )

            pending_giveaways.pop(user_id, None)

            me = bot.get_me()
            link = (
                f"https://t.me/{me.username}"
                f"?start=giveaway_{giveaway_id}"
            )

            bot.send_message(
                message.chat.id,
                f"Конкурс создан!\n"
                f"ID: {giveaway_id}\n"
                f"Ссылка: {escape(link)}"
            )

        except Exception as exc:
            bot.send_message(message.chat.id, escape(exc))


@bot.callback_query_handler(
    func=lambda c: c.data.startswith("gwtype:")
)
def giveaway_type_callback(call):
    if not is_admin(call.from_user.id):
        safe_answer(call, "Нет доступа.", True)
        return

    user_id = call.from_user.id
    state = pending_giveaways.get(user_id)

    if not state:
        safe_answer(call, "Создание конкурса не запущено.", True)
        return

    kind = call.data.split(":")[1]
    state["type"] = kind
    state["step"] = "value"

    safe_answer(call)

    if kind == "balance":
        bot.send_message(
            call.message.chat.id,
            "Сколько рублей получит победитель?"
        )
    elif kind == "subscription":
        bot.send_message(
            call.message.chat.id,
            "На сколько дней подписка?"
        )
    else:
        bot.send_message(
            call.message.chat.id,
            "Укажи название предмета."
        )


# =========================================================
# API FOR YOUR OWN PROJECTS
# =========================================================

def verify_telegram_init_data(init_data):
    """Validate Telegram Mini App initData using the bot token."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        from urllib.parse import parse_qsl
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(values.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > 86400:
            return None
        user_data = json.loads(values.get("user", "{}"))
        return int(user_data["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def api_telegram_user():
    return verify_telegram_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


def api_authorized():
    # API key remains available for server-to-server integrations.
    supplied = (
        request.headers.get("X-API-Key", "")
        or request.args.get("api_key", "")
    )
    if API_SECRET and supplied and secrets.compare_digest(supplied, API_SECRET):
        return True
    # Mini App requests authenticate through Telegram's signed initData.
    return api_telegram_user() is not None


@app.route("/api/v1/health", methods=["GET"])
def api_health():
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    return jsonify({
        "ok": True,
        "service": "ToxiVPN API",
        "servers": list(VLESS_SERVERS.keys())
    })


@app.route("/api/v1/servers", methods=["GET"])
def api_servers():
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    rows = run_query(
        db.table("servers")
        .select("server_key,name,is_active")
        .eq("is_active", True)
    )

    return jsonify({"servers": rows})


@app.route("/api/v1/subscription/<int:telegram_id>", methods=["GET"])
def api_subscription(telegram_id):
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    verified_user_id = api_telegram_user()
    has_api_key = bool(
        API_SECRET and secrets.compare_digest(
            request.headers.get("X-API-Key", "") or request.args.get("api_key", ""),
            API_SECRET,
        )
    )
    if verified_user_id is not None and verified_user_id != telegram_id and not has_api_key:
        return jsonify({"error": "forbidden"}), 403

    sub = get_subscription(telegram_id)

    if not sub:
        return jsonify({"error": "not_found"}), 404

    return jsonify({
        "telegram_id": telegram_id,
        "expire": sub.get("expire"),
        "traffic_limit_gb": sub.get("traffic_limit_gb", 50),
        "traffic_used_gb": sub.get("traffic_used_gb", 0),
        "max_devices": sub.get("max_devices", 2),
        "status": subscription_reason(sub) or "active",
        "is_active": not bool(subscription_reason(sub)),
        "is_blocked": bool(sub.get("is_blocked", False)),
        "max_devices": sub.get("max_devices", 2),
        "plan_name": sub.get("plan_name") or "ToxiVPN",
        "subscription_url": subscription_http_url(sub),
        "sub_key": sub.get("sub_key")
    })


@app.route("/api/v1/traffic/<sub_key>", methods=["POST"])
def api_update_traffic(sub_key):
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    try:
        used_gb = Decimal(str(payload["used_gb"]))
        if used_gb < 0:
            raise ValueError
    except Exception:
        return jsonify({"error": "used_gb must be a non-negative number"}), 400

    sub = get_subscription_by_key(sub_key)

    if not sub:
        return jsonify({"error": "subscription_not_found"}), 404

    run_query(
        db.table("subscriptions")
        .update({"traffic_used_gb": float(used_gb)})
        .eq("sub_key", sub_key)
    )

    updated = get_subscription_by_key(sub_key)

    return jsonify({
        "ok": True,
        "used_gb": float(used_gb),
        "limit_gb": float(
            updated.get("traffic_limit_gb") or DEFAULT_TRAFFIC_GB
        ),
        "status": subscription_reason(updated) or "active"
    })


@app.route("/api/v1/subscription/<int:telegram_id>/activate", methods=["POST"])
def api_activate_subscription(telegram_id):
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    try:
        days = int(payload.get("days", 0))
        if days < 1 or days > 3650:
            raise ValueError
    except Exception:
        return jsonify({"error": "days must be between 1 and 3650"}), 400

    if not get_user(telegram_id):
        return jsonify({"error": "user_not_found"}), 404

    expire = activate_subscription(telegram_id, days)

    return jsonify({
        "ok": True,
        "telegram_id": telegram_id,
        "expire": expire.isoformat()
    })


@app.route("/api/v1/subscription/<int:telegram_id>/balance", methods=["POST"])
def api_change_balance(telegram_id):
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    try:
        amount = money(payload["amount"])
    except Exception:
        return jsonify({"error": "amount is required"}), 400

    if not get_user(telegram_id):
        return jsonify({"error": "user_not_found"}), 404

    try:
        balance = change_balance(telegram_id, amount)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "ok": True,
        "telegram_id": telegram_id,
        "balance": float(balance)
    })


# =========================================================
# SERVER START
# =========================================================

def run_http_server():
    port = int(os.getenv("PORT", "8080"))
    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
        use_reloader=False
    )


def main():
    if OWNER_ID:
        try:
            run_query(
                db.table("bot_roles").upsert({
                    "telegram_id": OWNER_ID,
                    "role": "owner",
                    "assigned_by": OWNER_ID
                }, on_conflict="telegram_id")
            )
        except Exception:
            logging.exception("Owner role setup failed")

    threading.Thread(
        target=run_http_server,
        daemon=True
    ).start()

    threading.Thread(
        target=giveaway_worker,
        daemon=True
    ).start()

    logging.info("ToxiVPN bot started")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )


if __name__ == "__main__":
    main()
