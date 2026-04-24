"""
Telegram-бот для учёта финансов Сарацин
Команды: /start, /add, /остатки, /отмена
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Any

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

import gspread
from google.oauth2.service_account import Credentials

# ============================================================================
# НАСТРОЙКИ (берутся из переменных окружения на Railway)
# ============================================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================================
# ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS
# ============================================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(credentials)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)
sheet_ops = spreadsheet.worksheet("Операции")
sheet_balances = spreadsheet.worksheet("Остатки")

# ============================================================================
# СПРАВОЧНИКИ (под ваш бизнес)
# ============================================================================

COMPANIES = ["ИП QapPack", "ИП Шарипов", "ТОО Сарацин"]

INCOME_CATEGORIES = [
    "Продажа", "Возврат от поставщика", "снятие с счета", "Прочий приход"
]

EXPENSE_CATEGORIES = [
    "Закуп товара", "Предоплата", "оплата за товар",
    "Бензин", "Распечатка", "парковка",
    "Заработная плата Айдар", "Аванс Айдар", "отпускные айдар",
    "Заработная плата бух", "Аренда склада", "Налоги",
    "откат", "Прочий расход",
]

SOURCES_CASH = [
    "Касса ИП QapPack", "Касса ИП Шарипов", "Касса ТОО Сарацин"
]
SOURCES_BANK = [
    "Счёт ИП QapPack", "Счёт ИП Шарипов", "Счёт ТОО Сарацин"
]
SOURCES_DEBT = ["В долг (нам должны)", "В долг (мы должны)"]

# Частые контрагенты (топ из вашего списка)
TOP_CONTRAGENTS = [
    "ТОО Вандербургер", "ТОО СанТрейд", "ТОО ЭргоПак",
    "ТОО Бипак", "ТОО Папирони", "ТОО ДаХорека",
    "ТОО Данипласт", "ТОО НурКО", "ИП Куренкеева",
    "ТОО Амбер", "ТОО Арман", "ТОО Феникс",
    "ИП Шарипов", "ИП QapPack",
]

# Этапы разговора при добавлении операции
(
    TYPE, COMPANY, CATEGORY, CONTRAGENT, CONTRAGENT_SEARCH,
    AMOUNT, SOURCE, STATUS, CONFIRM,
) = range(9)

# ============================================================================
# ПРОВЕРКА ДОСТУПА
# ============================================================================

def check_access(func):
    """Декоратор: пускает только вас."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ALLOWED_USER_ID:
            await update.message.reply_text(
                "⛔ Доступ запрещён. Этот бот приватный."
            )
            logger.warning(f"Попытка доступа от user_id={user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def make_keyboard(items, columns=2, add_cancel=True):
    """Делает клавиатуру из списка."""
    keyboard = []
    row = []
    for i, item in enumerate(items):
        row.append(KeyboardButton(item))
        if (i + 1) % columns == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    if add_cancel:
        keyboard.append([KeyboardButton("❌ Отмена")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


# ============================================================================
# КОМАНДА /start
# ============================================================================

@check_access
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить операцию")],
            [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "👋 Привет! Я ваш финансовый бот-учётчик.\n\n"
        "📋 Команды:\n"
        "➕ Добавить операцию — записать приход/расход\n"
        "💰 Остатки — показать сколько денег\n"
        "↩️ Отменить последнюю — удалить последнюю запись\n\n"
        "Выбирайте действие внизу 👇",
        reply_markup=keyboard,
    )


# ============================================================================
# КОМАНДА /остатки
# ============================================================================

@check_access
async def cmd_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Считаю остатки...")
    try:
        # Читаем главные ячейки с листа "Остатки"
        # B4 — общий итог, D11 — наличные, D18 — счета
        b4 = sheet_balances.acell("B4").value or "0"
        d11 = sheet_balances.acell("D11").value or "0"
        d18 = sheet_balances.acell("D18").value or "0"

        # Кассы отдельно (D8, D9, D10)
        cash_qappack = sheet_balances.acell("D8").value or "0"
        cash_sharipov = sheet_balances.acell("D9").value or "0"
        cash_saratsin = sheet_balances.acell("D10").value or "0"

        # Счета (D15, D16, D17)
        bank_qappack = sheet_balances.acell("D15").value or "0"
        bank_sharipov = sheet_balances.acell("D16").value or "0"
        bank_saratsin = sheet_balances.acell("D17").value or "0"

        # Дебиторка B64, кредиторка B110
        debt_in = sheet_balances.acell("B64").value or "0"
        debt_out = sheet_balances.acell("B110").value or "0"

        msg = (
            f"💰 *ВСЕГО ДЕНЕГ:* {b4}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💵 *Наличные:* {d11}\n"
            f"├ QapPack: {cash_qappack}\n"
            f"├ Шарипов: {cash_sharipov}\n"
            f"└ Сарацин: {cash_saratsin}\n\n"
            f"🏦 *На счетах:* {d18}\n"
            f"├ QapPack: {bank_qappack}\n"
            f"├ Шарипов: {bank_sharipov}\n"
            f"└ Сарацин: {bank_saratsin}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📥 *Нам должны:* {debt_in}\n"
            f"📤 *Мы должны:* {debt_out}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Ошибка при чтении остатков")
        await update.message.reply_text(f"❌ Ошибка: {e}")


# ============================================================================
# КОМАНДА /add (пошаговый диалог)
# ============================================================================

@check_access
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Новая операция*\n\nЧто за операция?",
        reply_markup=make_keyboard(["📥 Приход", "📤 Расход"], columns=2),
        parse_mode="Markdown",
    )
    return TYPE


async def add_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    if "Приход" in text:
        context.user_data["type"] = "Приход"
    elif "Расход" in text:
        context.user_data["type"] = "Расход"
    else:
        await update.message.reply_text("Выберите кнопку:")
        return TYPE

    await update.message.reply_text(
        "Какая компания?",
        reply_markup=make_keyboard(COMPANIES, columns=1),
    )
    return COMPANY


async def add_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    if text not in COMPANIES:
        await update.message.reply_text("Выберите из списка:")
        return COMPANY
    context.user_data["company"] = text

    # Категории — разные для прихода и расхода
    cats = INCOME_CATEGORIES if context.user_data["type"] == "Приход" else EXPENSE_CATEGORIES
    await update.message.reply_text(
        "Категория?",
        reply_markup=make_keyboard(cats, columns=2),
    )
    return CATEGORY


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    context.user_data["category"] = text

    # Контрагент — для некоторых категорий (бензин, парковка) можно пропустить
    no_contragent_cats = ["Бензин", "Распечатка", "парковка", "Налоги", "снятие с счета"]
    if text in no_contragent_cats:
        context.user_data["contragent"] = ""
        await update.message.reply_text(
            "Сумма в тенге (просто число):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return AMOUNT

    await update.message.reply_text(
        "Контрагент? Можно выбрать из частых или ввести текстом.",
        reply_markup=make_keyboard(TOP_CONTRAGENTS + ["✏️ Ввести другого"], columns=2),
    )
    return CONTRAGENT


async def add_contragent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    if "Ввести другого" in text:
        await update.message.reply_text(
            "Введите имя контрагента:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return CONTRAGENT_SEARCH
    context.user_data["contragent"] = text
    await update.message.reply_text(
        "Сумма в тенге (просто число):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT


async def add_contragent_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contragent"] = update.message.text
    await update.message.reply_text("Сумма в тенге (просто число):")
    return AMOUNT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите положительное число. Попробуйте ещё раз:")
        return AMOUNT
    context.user_data["amount"] = amount

    # Куда/откуда — зависит от типа
    all_sources = SOURCES_CASH + SOURCES_BANK + SOURCES_DEBT
    await update.message.reply_text(
        "Откуда/куда деньги?",
        reply_markup=make_keyboard(all_sources, columns=1),
    )
    return SOURCE


async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    context.user_data["source"] = text

    # Статус: если "В долг" — то "Не оплачено", иначе "Оплачено"
    if "В долг" in text:
        context.user_data["status"] = "Не оплачено"
    else:
        context.user_data["status"] = "Оплачено"

    # Способ оплаты: автоматически из "Откуда/куда"
    if "Касса" in text:
        context.user_data["payment_method"] = "Наличная"
    elif "Счёт" in text or "Счет" in text:
        context.user_data["payment_method"] = "Безналичная"
    else:  # В долг
        context.user_data["payment_method"] = ""

    return await add_show_confirm(update, context)


async def add_show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    icon = "📥" if d["type"] == "Приход" else "📤"
    msg = (
        f"{icon} *Проверьте операцию:*\n\n"
        f"📅 Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
        f"🏢 Компания: {d['company']}\n"
        f"🏷 Тип: {d['type']}\n"
        f"📁 Категория: {d['category']}\n"
        f"👤 Контрагент: {d.get('contragent') or '—'}\n"
        f"💵 Сумма: {d['amount']:,.0f} ₸\n"
        f"💳 Откуда/куда: {d['source']}\n"
        f"💰 Способ оплаты: {d.get('payment_method') or '—'}\n"
        f"✅ Статус: {d['status']}"
    )
    await update.message.reply_text(
        msg,
        reply_markup=make_keyboard(["✅ Записать", "❌ Отмена"], columns=2, add_cancel=False),
        parse_mode="Markdown",
    )
    return CONFIRM


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)
    if "Записать" not in text:
        await update.message.reply_text("Нажмите ✅ Записать или ❌ Отмена")
        return CONFIRM

    d = context.user_data
    try:
        # Находим первую пустую строку на листе "Операции"
        # Колонки: A=Дата, B=Компания, C=Тип, D=Категория, E=Контрагент,
        # F=Сумма, G=Откуда/куда, H=Статус, I=Комментарий
        all_values = sheet_ops.get_all_values()
        next_row = len(all_values) + 1

        row_data = [
            datetime.now().strftime("%d.%m.%Y"),  # A
            d["company"],                          # B
            d["type"],                             # C
            d["category"],                         # D
            d.get("contragent", ""),               # E
            d["amount"],                           # F
            d["source"],                           # G
            d["status"],                           # H
            f"via bot",                            # I
            d.get("payment_method", ""),           # J
        ]
        sheet_ops.update(f"A{next_row}:J{next_row}", [row_data], value_input_option="USER_ENTERED")

        # Сохраняем номер строки — для команды /отмена
        context.chat_data["last_row"] = next_row

        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Добавить операцию")],
                [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
            ],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            f"✅ Записано в таблицу (строка {next_row})\n\n"
            "Что дальше?",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.exception("Ошибка при записи")
        await update.message.reply_text(f"❌ Ошибка записи: {e}")
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить операцию")],
            [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text("❌ Отменено.", reply_markup=keyboard)
    return ConversationHandler.END


# ============================================================================
# КОМАНДА /отмена (удалить последнюю запись)
# ============================================================================

@check_access
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_row = context.chat_data.get("last_row")
    if not last_row:
        await update.message.reply_text(
            "🤔 Нет записей для отмены в этой сессии.\n"
            "Отменить можно только последнюю запись, сделанную через бот."
        )
        return
    try:
        # Получаем содержимое строки для показа
        row_values = sheet_ops.row_values(last_row)
        # Очищаем строку
        sheet_ops.batch_clear([f"A{last_row}:J{last_row}"])
        context.chat_data["last_row"] = None

        await update.message.reply_text(
            f"✅ Удалена запись (строка {last_row}):\n"
            f"{' | '.join(str(v) for v in row_values[:6])}"
        )
    except Exception as e:
        logger.exception("Ошибка при отмене")
        await update.message.reply_text(f"❌ Ошибка: {e}")


# ============================================================================
# ОБРАБОТКА ТЕКСТОВЫХ КНОПОК ГЛАВНОГО МЕНЮ
# ============================================================================

@check_access
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Добавить операцию" in text:
        return await add_start(update, context)
    elif "Остатки" in text:
        await cmd_balances(update, context)
    elif "Отменить последнюю" in text:
        await cmd_undo(update, context)
    else:
        await update.message.reply_text(
            "🤔 Не понял. Используйте кнопки внизу или команду /start"
        )


# ============================================================================
# ЗАПУСК БОТА
# ============================================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Диалог добавления операции
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex(r"Добавить операцию"), add_start),
        ],
        states={
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_type)],
            COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_company)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            CONTRAGENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_contragent)],
            CONTRAGENT_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_contragent_search)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_source)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("balances", cmd_balances))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    logger.info("🤖 Бот запускается...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
