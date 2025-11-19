import json
import logging
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional

from flask import Flask, request

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    JobQueue,
    filters,
)

# =====================================================
# ЛОГИ
# =====================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# =====================================================
# КОНСТАНТЫ
# =====================================================

USERS_FILE = "users.json"

TOKEN = os.getenv("BOT_TOKEN")   # токен из переменной окружения Render
WEBHOOK_SECRET = "mindfulness-secret"  # путь вебхука

# например: mindfulness-bot.onrender.com (БЕЗ https://)
RENDER_URL = os.getenv("RENDER_URL")

MIN_COUNT = 3
MAX_COUNT = 10

DEFAULT_TZ = 0
DEFAULT_START = 9
DEFAULT_END = 19
DEFAULT_COUNT = 5

PROMPTS = [
    "Сделай паузу и три глубоких вдоха-выдоха.",
    "Проверь тело: где сейчас напряжение? Мягко расслабь.",
    "На 10 секунд просто посмотри вокруг, ничего не меняя.",
    "Заметь 3 звука, которые слышишь прямо сейчас.",
    "Чем бы ты занялся, если бы был на 5% более осознанным прямо сейчас?",
]

# =====================================================
# МОДЕЛЬ ПОЛЬЗОВАТЕЛЯ
# =====================================================

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

# =====================================================
# РАБОТА С ФАЙЛОМ
# =====================================================

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        USERS = {}
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error("Failed to load users: %s", e)
        USERS = {}
        return

    tmp: Dict[int, UserSettings] = {}
    for uid_str, v in data.items():
        try:
            uid = int(uid_str)
            tmp[uid] = UserSettings(**v)
        except Exception as e:
            log.error("Failed to load user %s: %s", uid_str, e)
    USERS = tmp
    log.info("Loaded %d users", len(USERS))


def save_users() -> None:
    try:
        data = {str(uid): asdict(s) for uid, s in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save users: %s", e)


# =====================================================
# ВСПОМОГАТЕЛЬНОЕ
# =====================================================

def get_user_tz(settings: UserSettings) -> timezone:
    return timezone(timedelta(hours=settings.tz_offset))


def clear_user_jobs(app: Application, uid: int) -> None:
    """Удаляем все задачи сообщений и полуночи для юзера."""
    scheduler = app.job_queue.scheduler
    for job in scheduler.get_jobs():
        if job.name in (f"msg_{uid}", f"midnight_{uid}"):
            job.remove()


def plan_today(app: Application, uid: int, settings: UserSettings) -> None:
    """Планируем уведомления на сегодняшний день."""
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    start = settings.start_hour
    end = settings.end_hour
    if start >= end:
        start, end = DEFAULT_START, DEFAULT_END

    times_local = []
    for _ in range(settings.count):
        h = random.randint(start, end - 1)
        m = random.randint(0, 59)
        dt_loc = datetime.combine(today_local, time(h, m), tzinfo=tz)
        times_local.append(dt_loc)

    times_local.sort()

    settings.planned_today = len(times_local)
    settings.sent_today = 0
    settings.last_plan_date_utc = now_utc.date().isoformat()
    save_users()

    jq = app.job_queue

    for dt_loc in times_local:
        dt_utc = dt_loc.astimezone(timezone.utc).replace(tzinfo=None)

        jq.run_once(
            callback=job_send_message,
            when=dt_utc,
            name=f"msg_{uid}",
            data={"uid": uid},
            job_kwargs={
                "misfire_grace_time": 60 * 60 * 24,  # 24 часа
                "coalesce": False,
            },
        )
        log.info("Scheduled msg for %s at %s", uid, dt_utc.isoformat())

    log.info("[%s] %d msgs planned for today", uid, settings.planned_today)


def schedule_midnight(app: Application, uid: int, settings: UserSettings) -> None:
    """Ставит задачу на локальную полночь юзера -> план следующего дня."""
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    next_midnight_local = datetime.combine(
        now_local.date(), time(0, 0), tzinfo=tz
    ) + timedelta(days=1)
    next_midnight_utc = next_midnight_local.astimezone(timezone.utc)
    next_midnight_utc_naive = next_midnight_utc.replace(tzinfo=None)

    app.job_queue.run_once(
        callback=job_midnight,
        when=next_midnight_utc_naive,
        name=f"midnight_{uid}",
        data={"uid": uid},
        job_kwargs={
            "misfire_grace_time": 60 * 60 * 24,
            "coalesce": False,
        },
    )
    log.info("[%s] midnight job -> %s", uid, next_midnight_utc_naive.isoformat())


# =====================================================
# JOB CALLBACKS
# =====================================================

async def job_send_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    uid = job.data["uid"]
    settings = USERS.get(uid)

    if not settings or not settings.enabled:
        log.info("job_send_message: user %s disabled or missing", uid)
        return

    text = random.choice(PROMPTS)
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        settings.sent_today += 1
        save_users()
        log.info("Sent msg to %s. Sent today: %d", uid, settings.sent_today)
    except Exception as e:
        log.error("Failed to send message to %s: %s", uid, e)


async def job_midnight(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    uid = job.data["uid"]
    app = context.application
    settings = USERS.get(uid)

    if not settings:
        log.info("midnight job: user %s not found", uid)
        return

    clear_user_jobs(app, uid)
    plan_today(app, uid, settings)
    schedule_midnight(app, uid, settings)
    log.info("Midnight job executed for %s", uid)


# =====================================================
# КОМАНДЫ
# =====================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    uid = user.id

    settings = USERS.get(uid)
    if not settings:
        settings = UserSettings()
        USERS[uid] = settings
        save_users()

    app = context.application

    clear_user_jobs(app, uid)
    plan_today(app, uid, settings)
    schedule_midnight(app, uid, settings)

    text = (
        "✨ Бот запущен!\n\n"
        "Чтобы всё работало корректно — установи часовой пояс через /settz.\n"
        "И диапазон времени через /settime.\n\n"
        "Я уже работаю и буду слать уведомления каждый день.\n"
        "Посмотреть текущие настройки можно через /status."
    )
    await update.message.reply_text(text)


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data["mode"] = "set_tz"
    await update.message.reply_text("Пришли GMT, например +11")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data["mode"] = "set_time"
    await update.message.reply_text("Пришли диапазон: начало конец (пример: 9 19)")


async def cmd_setcount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data["mode"] = "set_count"
    await update.message.reply_text("Пришли количество уведомлений (3–10).")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id

    settings = USERS.get(uid)
    if not settings:
        await update.message.reply_text("Я тебя ещё не знаю. Набери /start.")
        return

    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    jq = context.application.job_queue
    scheduler = jq.scheduler

    upcoming_local_times = []
    for job in scheduler.get_jobs():
        if job.name == f"msg_{uid}" and job.next_run_time is not None:
            run_utc = job.next_run_time.replace(tzinfo=timezone.utc)
            run_local = run_utc.astimezone(tz)
            if run_local.date() == today_local:
                upcoming_local_times.append(run_local)

    upcoming_local_times.sort()

    planned = settings.planned_today
    sent = settings.sent_today
    remaining = max(planned - sent, 0)

    lines = []
    lines.append("📊 Статус на сегодня:\n")
    lines.append(f"Часовой пояс: GMT{settings.tz_offset:+d}")
    lines.append(f"Диапазон: {settings.start_hour}–{settings.end_hour}")
    lines.append(f"Уведомлений в день: {settings.count}\n")
    lines.append(f"Сегодня отправлено: {sent}")
    lines.append(f"Осталось: {remaining}\n")

    if upcoming_local_times:
        lines.append("Ближайшие уведомления (локальное время):")
        for dt_loc in upcoming_local_times:
            mark = "👉" if dt_loc > now_local else "✓"
            lines.append(f"{mark} {dt_loc.strftime('%H:%M')}")
    else:
        if planned == 0:
            lines.append("На сегодня ещё нет плана (перезапусти /start или дождись полуночи).")
        elif remaining == 0:
            lines.append("На сегодня все уведомления уже отправлены.")
        else:
            lines.append("В очереди уведомлений не видно (возможно, всё уже разослано).")

    await update.message.reply_text("\n".join(lines))


# =====================================================
# ОБРАБОТКА ТЕКСТА (настройки)
# =====================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    mode = context.user_data.get("mode")
    if not mode:
        return

    settings = USERS.get(uid)
    if not settings:
        settings = UserSettings()
        USERS[uid] = settings

    app = context.application

    if mode == "set_tz":
        try:
            if text.lower().startswith("gmt"):
                text_clean = text[3:].strip()
            else:
                text_clean = text
            tz_val = int(text_clean)
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: +11")
            return

        if tz_val < -12 or tz_val > 14:
            await update.message.reply_text("Диапазон GMT от -12 до +14. Попробуй ещё раз.")
            return

        settings.tz_offset = tz_val
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Окей, часовой пояс: GMT{tz_val:+d}. План на день обновлён.")
        return

    if mode == "set_time":
        parts = text.replace(",", " ").split()
        if len(parts) != 2:
            await update.message.reply_text("Неверный формат. Нужны два числа, пример: 9 19")
            return

        try:
            start_h = int(parts[0])
            end_h = int(parts[1])
        except ValueError:
            await update.message.reply_text("Неверный формат. Используй целые часы, пример: 9 19")
            return

        if not (0 <= start_h <= 23 and 0 <= end_h <= 24):
            await update.message.reply_text("Часы должны быть в диапазоне 0–24. Попробуй ещё раз.")
            return

        if start_h >= end_h:
            await update.message.reply_text("Начало должно быть меньше конца. Пример: 9 19")
            return

        settings.start_hour = start_h
        settings.end_hour = end_h
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(
            f"Диапазон обновлён: с {start_h}:00 до {end_h}:00. План на сегодня пересчитан."
        )
        return

    if mode == "set_count":
        try:
            cnt = int(text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Нужна только цифра, пример: 5")
            return

        if not (MIN_COUNT <= cnt <= MAX_COUNT):
            await update.message.reply_text(
                f"Допустимый диапазон: от {MIN_COUNT} до {MAX_COUNT}. Попробуй ещё раз."
            )
            return

        settings.count = cnt
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(
            f"Окей, теперь буду слать {cnt} уведомлений в день. План на сегодня обновлён."
        )
        return


# =====================================================
# WEBHOOK + FLASK
# =====================================================

app_flask = Flask(__name__)
telegram_app: Optional[Application] = None


@app_flask.post(f"/{WEBHOOK_SECRET}")
def webhook() -> tuple[str, int]:
    global telegram_app
    if telegram_app is None:
        return "App not ready", 500

    data = request.get_json(force=True)
    update = Update.de_json(data, telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK", 200


def start_bot() -> None:
    global telegram_app

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    load_users()

    # ВАЖНО: updater(None) — чтобы НЕ создавать Updater и не ловить ошибку
    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    # Настраиваем JobQueue на UTC
    telegram_app.job_queue.scheduler.configure(timezone="UTC")

    # Хэндлеры
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("settz", cmd_settz))
    telegram_app.add_handler(CommandHandler("settime", cmd_settime))
    telegram_app.add_handler(CommandHandler("setcount", cmd_setcount))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    telegram_app.initialize()
    telegram_app.start()

    if not RENDER_URL:
        raise RuntimeError("RENDER_URL not set")

    hook_url = f"https://{RENDER_URL}/{WEBHOOK_SECRET}"
    telegram_app.bot.set_webhook(url=hook_url)
    log.info("Webhook set to %s", hook_url)


def main() -> None:
    start_bot()
    port = int(os.getenv("PORT", 5000))
    app_flask.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
