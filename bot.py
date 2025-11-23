import json
import logging
import os
import random
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

# Токен из переменной окружения (Render: Environment → BOT_TOKEN)
TOKEN = os.environ.get("BOT_TOKEN", "").strip()

MIN_COUNT = 3
MAX_COUNT = 10

DEFAULT_TZ = 0        # GMT+0
DEFAULT_START = 9     # 9:00
DEFAULT_END = 19      # 19:00
DEFAULT_COUNT = 5

# На сколько минут вперёд от "прямо сейчас" можно ставить самое раннее напоминание
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
    tz_offset: int = DEFAULT_TZ          # сдвиг GMT, например +11
    start_hour: int = DEFAULT_START      # начальный час (локальный)
    end_hour: int = DEFAULT_END          # конечный час (локальный)
    count: int = DEFAULT_COUNT           # сколько уведомлений в день
    enabled: bool = True                 # включён ли бот для этого юзера

    planned_today: int = 0               # целевое количество на сегодня
    sent_today: int = 0                  # сколько уже отправлено сегодня
    last_plan_date_utc: Optional[str] = None  # дата (UTC), на которую был последний план


USERS: Dict[int, UserSettings] = {}

# ===================== РАБОТА С ФАЙЛОМ =====================

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        log.info("Users file not found, starting fresh")
        USERS = {}
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log.error("Failed to load users: %s", e)
        USERS = {}
        return

    tmp: Dict[int, UserSettings] = {}
    for uid_str, data in raw.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        if not isinstance(data, dict):
            continue

        migrated = dict(data)

        # миграция старых полей
        if "tz" in migrated and "tz_offset" not in migrated:
            migrated["tz_offset"] = migrated["tz"]
        if "start" in migrated and "start_hour" not in migrated:
            migrated["start_hour"] = migrated["start"]
        if "end" in migrated and "end_hour" not in migrated:
            migrated["end_hour"] = migrated["end"]

        allowed_keys = UserSettings.__dataclass_fields__.keys()
        clean_data = {k: v for k, v in migrated.items() if k in allowed_keys}

        try:
            tmp[uid] = UserSettings(**clean_data)
        except TypeError as e:
            log.error("Failed to load user %s: %s", uid, e)

    USERS = tmp
    log.info("Loaded %d users", len(USERS))


def save_users() -> None:
    try:
        data = {str(uid): asdict(settings) for uid, settings in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save users: %s", e)


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def get_user_tz(settings: UserSettings) -> timezone:
    return timezone(timedelta(hours=settings.tz_offset))


def clear_user_jobs(app: Application, uid: int) -> None:
    """Удаляем все джобы сообщений и полуночи для этого пользователя."""
    jq = app.job_queue
    scheduler = jq.scheduler
    for job in scheduler.get_jobs():
        if job.name in (f"msg_{uid}", f"midnight_{uid}"):
            job.remove()


def plan_today(app: Application, uid: int, settings: UserSettings, reset_sent: bool) -> None:
    """
    Планирует уведомления на сегодня для пользователя.

    Логика:
    - Если новый день (по UTC) или reset_sent=True:
        - sent_today = 0
        - planned_today = settings.count
    - Если день тот же и reset_sent=False:
        - НЕ переопределяем planned_today (сохраняем старый план)
        - гарантируем, что planned_today >= sent_today
    - Всегда планируем ТОЛЬКО недостающие уведомления:
        remaining_to_plan = planned_today - sent_today

    Напоминания ставятся только в будущее (>= now + MIN_OFFSET_MINUTES).
    """
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    today_utc_str = today_utc.isoformat()

    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    # Окно в локальном времени
    start_hour = settings.start_hour
    end_hour = settings.end_hour
    if start_hour >= end_hour:
        start_hour, end_hour = DEFAULT_START, DEFAULT_END

    start_dt_local = datetime.combine(today_local, time(start_hour, 0), tzinfo=tz)
    end_dt_local = datetime.combine(today_local, time(end_hour, 0), tzinfo=tz)

    # Новый день или принудительный сброс
    if reset_sent or settings.last_plan_date_utc != today_utc_str:
        settings.sent_today = 0
        settings.planned_today = settings.count
        settings.last_plan_date_utc = today_utc_str
        log.info(
            "[%s] New day or reset: planned_today=%d, sent_today=%d",
            uid, settings.planned_today, settings.sent_today
        )
    else:
        # День тот же, рестарт/перепланировка
        # План на день не трогаем, только следим за консистентностью
        if settings.planned_today < settings.sent_today:
            settings.planned_today = settings.sent_today
        if settings.planned_today == 0:
            settings.planned_today = settings.count

        log.info(
            "[%s] Same-day replan: keep planned_today=%d, sent_today=%d",
            uid, settings.planned_today, settings.sent_today
        )

    # Нижняя граница для новых напоминаний:
    min_dt_local = now_local + timedelta(minutes=MIN_OFFSET_MINUTES)
    window_start = max(start_dt_local, min_dt_local)

    if window_start >= end_dt_local:
        # На сегодня времени не осталось
        save_users()
        log.info(
            "[%s] No time left today for new messages (%s–%s local, now_local=%s)",
            uid,
            start_dt_local.isoformat(),
            end_dt_local.isoformat(),
            now_local.isoformat(),
        )
        return

    remaining_to_plan = max(settings.planned_today - settings.sent_today, 0)
    if remaining_to_plan <= 0:
        save_users()
        log.info(
            "[%s] Already reached daily target: planned_today=%d, sent_today=%d",
            uid, settings.planned_today, settings.sent_today
        )
        return

    total_minutes = int((end_dt_local - window_start).total_seconds() // 60)
    if total_minutes <= 0:
        save_users()
        log.info("[%s] No minute window left today", uid)
        return

    times_local = []
    for _ in range(remaining_to_plan):
        offset_min = random.randint(0, total_minutes - 1)
        dt_loc = window_start + timedelta(minutes=offset_min)
        times_local.append(dt_loc)

    times_local.sort()
    jq = app.job_queue

    for dt_loc in times_local:
        dt_utc = dt_loc.astimezone(timezone.utc)
        dt_utc_naive = dt_utc.replace(tzinfo=None)

        jq.run_once(
            callback=job_send_message,
            when=dt_utc_naive,
            name=f"msg_{uid}",
            data={"uid": uid},
            job_kwargs={
                # если опоздали не больше, чем на MIN_OFFSET_MINUTES — всё ещё шлём
                "misfire_grace_time": MIN_OFFSET_MINUTES * 60,
                "coalesce": False,
            },
        )
        log.info("Scheduled msg for %s at %s (UTC naive)", uid, dt_utc_naive.isoformat())

    log.info(
        "[%s] %d msgs planned for today (sent_today=%d, planned_today=%d, window %02d-%02d local)",
        uid, remaining_to_plan, settings.sent_today, settings.planned_today,
        start_hour, end_hour
    )

    save_users()


def schedule_midnight(app: Application, uid: int, settings: UserSettings) -> None:
    """Ставит джобу на локальную полночь пользователя, чтобы спланировать следующий день."""
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
            "misfire_grace_time": MIN_OFFSET_MINUTES * 60,
            "coalesce": False,
        },
    )
    log.info("[%s] midnight job -> %s", uid, next_midnight_utc_naive.isoformat())


# ===================== JOB CALLBACKS =====================

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
        log.info("Sent msg to %s. Sent today: %d (planned_today=%d)",
                 uid, settings.sent_today, settings.planned_today)
    except Exception as e:
        log.error("Failed to send message to %s: %s", uid, e)


async def job_midnight(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Полночь в локальном времени пользователя: планируем новый день и ставим следующую полночь."""
    job = context.job
    uid = job.data["uid"]
    app = context.application
    settings = USERS.get(uid)

    if not settings:
        log.info("midnight job: user %s not found", uid)
        return

    clear_user_jobs(app, uid)
    # Новый день – сбрасываем sent_today и пересоздаём дневной план
    plan_today(app, uid, settings, reset_sent=True)
    schedule_midnight(app, uid, settings)
    log.info("Midnight job executed for %s", uid)


# ===================== КОМАНДЫ =====================

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
    # /start — перепланируем только остаток дня, не сбрасывая счётчик
    plan_today(app, uid, settings, reset_sent=False)
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
    user = update.effective_user
    if not user or not update.message:
        return

    context.user_data["mode"] = "set_tz"
    await update.message.reply_text("Пришли GMT, например +11")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    context.user_data["mode"] = "set_time"
    await update.message.reply_text("Пришли диапазон: начало конец (пример: 9 19)")


async def cmd_setcount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    context.user_data["mode"] = "set_count"
    await update.message.reply_text("Пришли количество уведомлений (3–10).")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    uid = user.id

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
            if run_local.date() == today_local and run_local >= now_local:
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
    lines.append(f"Запланировано на день: {planned}")
    lines.append(f"Осталось по плану: {remaining}\n")

    if upcoming_local_times:
        lines.append("Ближайшие уведомления (локальное время):")
        for dt_loc in upcoming_local_times:
            lines.append(f"👉 {dt_loc.strftime('%H:%M')}")
    else:
        if planned == 0:
            lines.append("На сегодня ещё нет плана (перезапусти /start или дождись полуночи).")
        elif remaining == 0:
            lines.append("На сегодня все уведомления уже отправлены.")
        else:
            lines.append(
                "На сегодня запланированных уведомлений в очереди не видно "
                "(возможно, окно уже прошло или всё было разослано)."
            )

    await update.message.reply_text("\n".join(lines))


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Простой health-check: показывает, что бот жив и время на сервере."""
    now_utc = datetime.now(timezone.utc)
    await update.message.reply_text(f"pong 🧘\nUTC: {now_utc.isoformat()}")


# ===================== ОБРАБОТКА ТЕКСТА (настройки) =====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    uid = user.id
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
            if text.startswith("GMT") or text.startswith("gmt"):
                text_clean = text[3:].strip()
            else:
                text_clean = text

            tz_val = int(text_clean)
        except ValueError:
            await update.message.reply_text("Неверный формат. Попробуй ещё раз. Пример: +11")
            return

        if tz_val < -12 or tz_val > 14:
            await update.message.reply_text("Диапазон GMT от -12 до +14. Попробуй ещё раз.")
            return

        settings.tz_offset = tz_val
        save_users()

        clear_user_jobs(app, uid)
        # При смене часового пояса оставляем статистику, но перепланируем остаток дня
        plan_today(app, uid, settings, reset_sent=False)
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
        # Перепланируем только остаток дня
        plan_today(app, uid, settings, reset_sent=False)
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
        # Количество изменилось – перепланируем остаток дня
        plan_today(app, uid, settings, reset_sent=False)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(
            f"Окей, теперь буду слать {cnt} уведомлений в день. План на сегодня обновлён."
        )
        return


# ===================== STARTUP =====================

async def on_startup(app: Application) -> None:
    """
    При старте:
    - грузим пользователей
    - для каждого пользователя чистим джобы и перепланируем оставшиеся напоминания
      на ТЕКУЩИЙ день без сброса счётчика.
    """
    load_users()
    now_utc = datetime.now(timezone.utc).date()
    now_utc_str = now_utc.isoformat()

    for uid, settings in USERS.items():
        clear_user_jobs(app, uid)

        same_day = (settings.last_plan_date_utc == now_utc_str)
        log.info(
            "[%s] Startup: last_plan_date_utc=%s, today_utc=%s, same_day=%s,"
            " planned_today=%d, sent_today=%d",
            uid,
            settings.last_plan_date_utc,
            now_utc_str,
            same_day,
            settings.planned_today,
            settings.sent_today,
        )

        # Не сбрасываем sent_today на старте, только допланируем остаток дня.
        plan_today(app, uid, settings, reset_sent=False)
        schedule_midnight(app, uid, settings)

    log.info("Startup finished: users planned and midnight jobs scheduled")


def main() -> None:
    if not TOKEN:
        log.error("ERROR: BOT_TOKEN is not set in environment")
        return

    app = Application.builder().token(TOKEN).build()

    # startup-хук
    app.post_init = on_startup

    # хендлеры команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("setcount", cmd_setcount))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))

    # текст – только как ответ на режимы настройки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Настройки webhook для Render
    port = int(os.environ.get("PORT", "1000"))
    secret_path = os.environ.get("WEBHOOK_PATH", "mindfulness-secret").lstrip("/")
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

    if not base_url:
        base_url = "https://mindfulness-bot.onrender.com"

    webhook_url = f"{base_url}/{secret_path}"

    log.info("Starting webhook on port %s, url: %s", port, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=secret_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
