import json
import logging
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== ЛОГИ =====================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ===================== КОНСТАНТЫ =====================

USERS_FILE = "users.json"
TOKEN = os.environ.get("BOT_TOKEN", "").strip()

MIN_COUNT = 3
MAX_COUNT = 10

DEFAULT_TZ = 0
DEFAULT_START = 9
DEFAULT_END = 19
DEFAULT_COUNT = 5

# чтобы рендер не отбрасывал слишком старые джобы
MIN_OFFSET_MINUTES = 5

PROMPTS = [
    "Сделай паузу и три глубоких вдоха-выдоха.",
    "Проверь тело: где сейчас напряжение? Мягко расслабь.",
    "На 10 секунд просто посмотри вокруг, ничего не меняя.",
    "Заметь 3 звука, которые слышишь прямо сейчас.",
    "Чем бы ты занялся, если бы был на 5% более осознанным прямо сейчас?",
]

# ===================== МОДЕЛЬ ПОЛЬЗОВАТЕЛЯ =====================

@dataclass
class UserSettings:
    tz_offset: int = DEFAULT_TZ
    start_hour: int = DEFAULT_START
    end_hour: int = DEFAULT_END
    count: int = DEFAULT_COUNT
    enabled: bool = True

    planned_today: int = 0
    sent_today: int = 0
    last_plan_date_utc: Optional[str] = None


USERS: Dict[int, UserSettings] = {}

# ===================== РАБОТА С ФАЙЛОМ =====================

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        USERS = {}
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log.error("Failed to load users: %s", e)
        USERS = {}
        return

    tmp = {}
    for uid_str, data in raw.items():
        try:
            uid = int(uid_str)
        except:
            continue

        if not isinstance(data, dict):
            continue

        migrated = dict(data)

        if "tz" in migrated and "tz_offset" not in migrated:
            migrated["tz_offset"] = migrated["tz"]
        if "start" in migrated and "start_hour" not in migrated:
            migrated["start_hour"] = migrated["start"]
        if "end" in migrated and "end_hour" not in migrated:
            migrated["end_hour"] = migrated["end"]

        allowed = UserSettings.__dataclass_fields__.keys()
        clean = {k: v for k, v in migrated.items() if k in allowed}

        try:
            tmp[uid] = UserSettings(**clean)
        except Exception as e:
            log.error("Failed to load user %s: %s", uid, e)

    USERS = tmp
    log.info("Loaded %d users", len(USERS))


def save_users() -> None:
    try:
        data = {str(uid): asdict(s) for uid, s in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save users: %s", e)

# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def get_user_tz(settings: UserSettings) -> timezone:
    return timezone(timedelta(hours=settings.tz_offset))

def clear_user_jobs(app: Application, uid: int) -> None:
    jq = app.job_queue
    scheduler = jq.scheduler
    for job in scheduler.get_jobs():
        if job.name in (f"msg_{uid}", f"midnight_{uid}"):
            job.remove()


def plan_today(app: Application, uid: int, settings: UserSettings, reset_sent: bool) -> None:
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    today_utc_str = today_utc.isoformat()

    tz = get_user_tz(settings)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    start_h = settings.start_hour
    end_h = settings.end_hour
    if start_h >= end_h:
        start_h, end_h = DEFAULT_START, DEFAULT_END

    start_dt_local = datetime.combine(today_local, time(start_h, 0), tzinfo=tz)
    end_dt_local = datetime.combine(today_local, time(end_h, 0), tzinfo=tz)

    if reset_sent or settings.last_plan_date_utc != today_utc_str:
        settings.sent_today = 0
        settings.planned_today = settings.count
        settings.last_plan_date_utc = today_utc_str
        log.info("[%s] New day: planned_today=%d", uid, settings.planned_today)
    else:
        if settings.planned_today < settings.sent_today:
            settings.planned_today = settings.sent_today
        if settings.planned_today == 0:
            settings.planned_today = settings.count

    min_dt_local = now_local + timedelta(minutes=MIN_OFFSET_MINUTES)
    window_start = max(start_dt_local, min_dt_local)

    if window_start >= end_dt_local:
        save_users()
        log.info("[%s] No window left for today", uid)
        return

    remaining = max(settings.planned_today - settings.sent_today, 0)
    if remaining <= 0:
        save_users()
        log.info("[%s] Already delivered all planned", uid)
        return

    total_minutes = int((end_dt_local - window_start).total_seconds() // 60)
    if total_minutes <= 0:
        save_users()
        return

    jq = app.job_queue
    times = []

    for _ in range(remaining):
        offset = random.randint(0, total_minutes - 1)
        dt_local = window_start + timedelta(minutes=offset)
        times.append(dt_local)

    times.sort()

    for dt_local in times:
        dt_utc = dt_local.astimezone(timezone.utc)
        dt_utc_naive = dt_utc.replace(tzinfo=None)

        jq.run_once(
            job_send_message,
            when=dt_utc_naive,
            name=f"msg_{uid}",
            data={"uid": uid},
            job_kwargs={
                "misfire_grace_time": MIN_OFFSET_MINUTES * 60,
                "coalesce": False,
            },
        )
        log.info("Scheduled msg for %s at %s UTC", uid, dt_utc_naive)

    save_users()


def schedule_midnight(app: Application, uid: int, settings: UserSettings) -> None:
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    next_midnight_local = datetime.combine(today := now_local.date(), time(0, 0), tzinfo=tz) + timedelta(days=1)
    next_midnight_utc = next_midnight_local.astimezone(timezone.utc)
    naive = next_midnight_utc.replace(tzinfo=None)

    app.job_queue.run_once(
        job_midnight,
        when=naive,
        name=f"midnight_{uid}",
        data={"uid": uid},
        job_kwargs={
            "misfire_grace_time": MIN_OFFSET_MINUTES * 60,
            "coalesce": False,
        },
    )
    log.info("[%s] midnight scheduled at %s", uid, naive)


# ===================== JOB CALLBACKS =====================

async def job_send_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = context.job.data["uid"]
    settings = USERS.get(uid)
    if not settings or not settings.enabled:
        return

    text = random.choice(PROMPTS)
    try:
        await context.bot.send_message(uid, text)
        settings.sent_today += 1
        save_users()
        log.info("Sent to %s (%d/%d)", uid, settings.sent_today, settings.planned_today)
    except Exception as e:
        log.error("Send fail to %s: %s", uid, e)


async def job_midnight(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = context.job.data["uid"]
    app = context.application
    settings = USERS.get(uid)
    if not settings:
        return

    clear_user_jobs(app, uid)
    plan_today(app, uid, settings, reset_sent=True)
    schedule_midnight(app, uid, settings)


# ===================== КОМАНДЫ =====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user: return
    uid = user.id

    settings = USERS.get(uid)
    if not settings:
        settings = UserSettings()
        USERS[uid] = settings
        save_users()

    app = context.application
    clear_user_jobs(app, uid)

    plan_today(app, uid, settings, reset_sent=False)
    schedule_midnight(app, uid, settings)

    await update.message.reply_text(
        "✨ Бот запущен!\n\n"
        "Чтобы всё работало корректно — установи часовой пояс через /settz.\n"
        "И диапазон времени через /settime.\n\n"
        "Я уже работаю и буду слать уведомления каждый день.\n"
        "Посмотреть текущие настройки можно через /status."
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    await update.message.reply_text(f"pong ✅\nUTC: {now.isoformat()}")


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_tz"
    await update.message.reply_text("Пришли GMT, например +11")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_time"
    await update.message.reply_text("Пришли диапазон: начало конец (пример: 9 19)")


async def cmd_setcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_count"
    await update.message.reply_text("Пришли количество уведомлений (3–10).")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return
    uid = user.id
    settings = USERS.get(uid)

    if not settings:
        await update.message.reply_text("Я тебя ещё не знаю. Набери /start.")
        return

    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()

    jq = context.application.job_queue.scheduler
    upcoming = []

    for job in jq.get_jobs():
        if job.name == f"msg_{uid}" and job.next_run_time:
            utc = job.next_run_time.replace(tzinfo=timezone.utc)
            loc = utc.astimezone(tz)
            if loc.date() == today and loc >= now_local:
                upcoming.append(loc)

    upcoming.sort()

    msg = [
        "📊 Статус на сегодня:\n",
        f"Часовой пояс: GMT{settings.tz_offset:+d}",
        f"Диапазон: {settings.start_hour}–{settings.end_hour}",
        f"Уведомлений в день: {settings.count}\n",
        f"Сегодня отправлено: {settings.sent_today}",
        f"Запланировано на день: {settings.planned_today}",
        f"Осталось: {max(settings.planned_today - settings.sent_today, 0)}\n",
    ]

    if upcoming:
        msg.append("Ближайшие уведомления:")
        msg += [f"👉 {t.strftime('%H:%M')}" for t in upcoming]
    else:
        msg.append("На сегодня больше уведомлений нет.")

    await update.message.reply_text("\n".join(msg))


# ===================== ТЕКСТ-ОБРАБОТЧИК =====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    mode = context.user_data.get("mode")
    if not mode:
        return

    settings = USERS.get(uid) or UserSettings()
    USERS[uid] = settings
    app = context.application

    if mode == "set_tz":
        try:
            if text.lower().startswith("gmt"):
                text = text[3:].strip()
            tz = int(text)
        except:
            await update.message.reply_text("Неверный формат. Пример: +11")
            return

        if tz < -12 or tz > 14:
            await update.message.reply_text("Допустимый диапазон: -12…+14")
            return

        settings.tz_offset = tz
        save_users()
        clear_user_jobs(app, uid)
        plan_today(app, uid, settings, reset_sent=False)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Окей, GMT{tz:+d}. План обновлён.")
        return

    if mode == "set_time":
        parts = text.replace(",", " ").split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: 9 19")
            return
        try:
            s, e = int(parts[0]), int(parts[1])
        except:
            await update.message.reply_text("Нужны числа, пример: 9 19")
            return

        if not (0 <= s < 24 and 0 < e <= 24) or s >= e:
            await update.message.reply_text("Часы неверные. Пример: 9 19")
            return

        settings.start_hour = s
        settings.end_hour = e
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings, reset_sent=False)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Диапазон обновлён: {s}:00–{e}:00")
        return

    if mode == "set_count":
        try:
            c = int(text)
        except:
            await update.message.reply_text("Нужна цифра, пример: 5")
            return

        if not (MIN_COUNT <= c <= MAX_COUNT):
            await update.message.reply_text(
                f"Допустимо {MIN_COUNT}–{MAX_COUNT}"
            )
            return

        settings.count = c
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings, reset_sent=False)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(
            f"Теперь буду слать {c} уведомлений."
        )


# ===================== Health-check сервер =====================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = 10000
    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    def run():
        log.info("Health server running on port %s", port)
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


# ===================== STARTUP =====================

async def on_startup(app: Application) -> None:
    load_users()
    now = datetime.now(timezone.utc).date().isoformat()

    for uid, settings in USERS.items():
        clear_user_jobs(app, uid)
        same_day = settings.last_plan_date_utc == now
        plan_today(app, uid, settings, reset_sent=not same_day)
        schedule_midnight(app, uid, settings)

    log.info("Startup planning complete")


# ===================== MAIN =====================

def main():
    if not TOKEN:
        log.error("BOT_TOKEN missing")
        return

    start_health_server()

    app = Application.builder().token(TOKEN).build()
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("setcount", cmd_setcount))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    port = int(os.environ.get("PORT", "1000"))
    secret = os.environ.get("WEBHOOK_PATH", "mindfulness-secret").lstrip("/")
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

    if not base:
        base = "https://mindfulness-bot.onrender.com"

    webhook_url = f"{base}/{secret}"
    log.info("Starting webhook on %s", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=secret,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
