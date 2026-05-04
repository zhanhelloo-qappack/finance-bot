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
    AMOUNT, SOURCE, PAYMENT_METHOD, STATUS, CONFIRM,
) = range(10)

# Этапы массового режима
BATCH_COMPANY, BATCH_DATE, BATCH_DATE_CUSTOM = range(100, 103)

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
# МАССОВЫЙ РЕЖИМ (фиксирует компанию и дату)
# ============================================================================

@check_access
async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск массового режима — сначала спрашиваем компанию."""
    # Очищаем предыдущие батч-данные
    context.chat_data.pop("batch_company", None)
    context.chat_data.pop("batch_date", None)
    context.chat_data["batch_count"] = 0

    await update.message.reply_text(
        "⚡ *Массовый ввод*\n\n"
        "Зафиксирую компанию и дату — дальше будете вводить операции быстрее.\n\n"
        "Компания для всех операций?",
        reply_markup=make_keyboard(COMPANIES + ["🌐 Разные (не фиксировать)"], columns=1),
        parse_mode="Markdown",
    )
    return BATCH_COMPANY


async def batch_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await batch_cancel(update, context)

    if "Разные" in text:
        context.chat_data["batch_company"] = None  # не фиксируем
    elif text in COMPANIES:
        context.chat_data["batch_company"] = text
    else:
        await update.message.reply_text("Выберите из списка:")
        return BATCH_COMPANY

    # Теперь спрашиваем дату
    today = datetime.now().strftime("%d.%m.%Y")
    yesterday_ts = datetime.now().timestamp() - 86400
    yesterday = datetime.fromtimestamp(yesterday_ts).strftime("%d.%m.%Y")
    day_before_ts = datetime.now().timestamp() - 86400 * 2
    day_before = datetime.fromtimestamp(day_before_ts).strftime("%d.%m.%Y")

    await update.message.reply_text(
        "Дата для всех операций?",
        reply_markup=make_keyboard(
            [f"📅 Сегодня ({today})", f"📅 Вчера ({yesterday})",
             f"📅 Позавчера ({day_before})", "✏️ Ввести дату", "🌐 Разные (не фиксировать)"],
            columns=1,
        ),
    )
    return BATCH_DATE


async def batch_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await batch_cancel(update, context)

    if "Сегодня" in text:
        context.chat_data["batch_date"] = datetime.now().strftime("%d.%m.%Y")
    elif "Вчера" in text and "Позавчера" not in text:
        ts = datetime.now().timestamp() - 86400
        context.chat_data["batch_date"] = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    elif "Позавчера" in text:
        ts = datetime.now().timestamp() - 86400 * 2
        context.chat_data["batch_date"] = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    elif "Разные" in text:
        context.chat_data["batch_date"] = None
    elif "Ввести дату" in text:
        await update.message.reply_text(
            "Введите дату в формате ДД.ММ.ГГГГ (например: 20.04.2026):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return BATCH_DATE_CUSTOM
    else:
        await update.message.reply_text("Выберите из списка:")
        return BATCH_DATE

    return await batch_finish_setup(update, context)


async def batch_date_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        # Проверяем формат
        dt = datetime.strptime(text, "%d.%m.%Y")
        context.chat_data["batch_date"] = dt.strftime("%d.%m.%Y")
    except ValueError:
        await update.message.reply_text(
            "❌ Не понял формат. Введите как в примере: 20.04.2026"
        )
        return BATCH_DATE_CUSTOM

    return await batch_finish_setup(update, context)


async def batch_finish_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка массового режима завершена — показываем сводку и запускаем ввод."""
    context.chat_data["batch_mode"] = True

    company = context.chat_data.get("batch_company") or "любая (будет спрашивать)"
    date = context.chat_data.get("batch_date") or "любая (будет спрашивать)"

    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Ещё операция")],
            [KeyboardButton("🏁 Готово / в меню")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"✅ *Массовый режим включён*\n\n"
        f"🏢 Компания: {company}\n"
        f"📅 Дата: {date}\n\n"
        f"Теперь просто нажимайте «➕ Ещё операция» — компанию и дату спрашивать не буду.\n"
        f"Когда закончите — «🏁 Готово».",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def batch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("batch_mode", None)
    context.chat_data.pop("batch_company", None)
    context.chat_data.pop("batch_date", None)
    context.chat_data.pop("batch_count", None)
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить операцию"), KeyboardButton("⚡ Массовый ввод")],
            [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text("❌ Массовый режим отменён.", reply_markup=keyboard)
    return ConversationHandler.END


# ============================================================================
# КОМАНДА /start
# ============================================================================

@check_access
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сбрасываем массовый режим при /start
    context.chat_data.pop("batch_mode", None)
    context.chat_data.pop("batch_company", None)
    context.chat_data.pop("batch_date", None)
    context.chat_data.pop("batch_count", None)

    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить операцию"), KeyboardButton("⚡ Массовый ввод")],
            [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "👋 Привет! Я ваш финансовый бот-учётчик.\n\n"
        "📋 Команды:\n"
        "➕ Добавить операцию — записать приход/расход\n"
        "⚡ Массовый ввод — закрепить компанию/дату и быстро вводить\n"
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
        # Читаем все данные с листа "Остатки" одним запросом
        all_data = sheet_balances.get_all_values()

        def find_value_by_label(label, col=4):
            """Ищет строку с заданным текстом в колонке A, возвращает значение из указанной колонки.
            col=2 для B, col=3 для C, col=4 для D"""
            for row in all_data:
                if len(row) > 0 and label.lower() in row[0].lower():
                    if len(row) >= col:
                        return row[col - 1] or "0"
            return "0"

        # Главный итог — B4 (всегда фиксированный)
        b4 = all_data[3][1] if len(all_data) > 3 and len(all_data[3]) > 1 else "0"

        # Кассы — ищем по названию
        cash_qappack = find_value_by_label("Касса ИП QapPack")
        cash_sharipov = find_value_by_label("Касса ИП Шарипов")
        cash_saratsin = find_value_by_label("Касса ТОО Сарацин")
        cash_total = find_value_by_label("ИТОГО наличных")

        # Счета — ищем по названию
        bank_qappack = find_value_by_label("Счёт ИП QapPack")
        bank_sharipov = find_value_by_label("Счёт ИП Шарипов")
        bank_saratsin = find_value_by_label("Счёт ТОО Сарацин")
        bank_total = find_value_by_label("ИТОГО на счетах")

        # Дебиторка и кредиторка — ищем по названию (col=2 это B)
        debt_in = find_value_by_label("ИТОГО нам должны", col=2)
        debt_out = find_value_by_label("ИТОГО мы должны", col=2)

        msg = (
            f"💰 *ВСЕГО ДЕНЕГ:* {b4}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💵 *Наличные:* {cash_total}\n"
            f"├ QapPack: {cash_qappack}\n"
            f"├ Шарипов: {cash_sharipov}\n"
            f"└ Сарацин: {cash_saratsin}\n\n"
            f"🏦 *На счетах:* {bank_total}\n"
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
    # Сохраняем зафиксированные значения из массового режима (если он активен)
    batch_company = context.chat_data.get("batch_company")
    batch_date = context.chat_data.get("batch_date")

    context.user_data.clear()

    # Если массовый режим — подставляем зафиксированные значения
    if batch_company:
        context.user_data["company"] = batch_company
    if batch_date:
        context.user_data["date"] = batch_date

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

    # Если компания уже известна (массовый режим) — пропускаем этап
    if context.user_data.get("company"):
        cats = INCOME_CATEGORIES if context.user_data["type"] == "Приход" else EXPENSE_CATEGORIES
        await update.message.reply_text(
            f"🏢 Компания: {context.user_data['company']} (массовый режим)\n\nКатегория?",
            reply_markup=make_keyboard(cats, columns=2),
        )
        return CATEGORY

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

    # Предлагаю способ оплаты — с подсказкой по умолчанию
    if "Касса" in text:
        hint = "(обычно для кассы — Наличная)"
    elif "Счёт" in text or "Счет" in text:
        hint = "(обычно для счёта — Безналичная)"
    else:
        hint = "(для долгов обычно — В долг)"

    await update.message.reply_text(
        f"Способ оплаты?\n{hint}",
        reply_markup=make_keyboard(
            ["💵 Наличная", "💳 Безналичная", "📋 В долг"],
            columns=2,
        ),
    )
    return PAYMENT_METHOD


async def add_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        return await add_cancel(update, context)

    if "Наличная" in text:
        context.user_data["payment_method"] = "Наличная"
    elif "Безналичная" in text:
        context.user_data["payment_method"] = "Безналичная"
    elif "В долг" in text:
        context.user_data["payment_method"] = "В долг"
    else:
        await update.message.reply_text("Выберите кнопку:")
        return PAYMENT_METHOD

    return await add_show_confirm(update, context)


async def add_show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    icon = "📥" if d["type"] == "Приход" else "📤"
    op_date = d.get("date") or datetime.now().strftime("%d.%m.%Y")
    msg = (
        f"{icon} *Проверьте операцию:*\n\n"
        f"📅 Дата: {op_date}\n"
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
        all_values = sheet_ops.get_all_values()
        next_row = len(all_values) + 1

        # Дата: из массового режима или сегодняшняя
        op_date = d.get("date") or datetime.now().strftime("%d.%m.%Y")

        row_data = [
            op_date,                               # A
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

        # Счётчик записей в массовом режиме
        if context.chat_data.get("batch_mode"):
            context.chat_data["batch_count"] = context.chat_data.get("batch_count", 0) + 1
            count = context.chat_data["batch_count"]
            batch_info = f"\n📦 Массовый режим: записано {count}"
        else:
            batch_info = ""

        # Клавиатура с кнопками "Ещё"
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Ещё операция"), KeyboardButton("➕ Ещё (та же компания)")],
                [KeyboardButton("🏁 Готово / в меню")],
            ],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            f"✅ Записано в таблицу (строка {next_row}){batch_info}\n\nЧто дальше?",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.exception("Ошибка при записи")
        await update.message.reply_text(f"❌ Ошибка записи: {e}")
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    # Если был массовый режим — остаёмся в нём
    if context.chat_data.get("batch_mode"):
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Ещё операция")],
                [KeyboardButton("🏁 Готово / в меню")],
            ],
            resize_keyboard=True,
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Добавить операцию"), KeyboardButton("⚡ Массовый ввод")],
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

    # Кнопка "Ещё операция" — такая же как "Добавить операцию"
    if "Ещё операция" in text or "Добавить операцию" in text:
        return await add_start(update, context)

    # Кнопка "Ещё (та же компания)" — фиксируем компанию, запускаем добавление
    if "Ещё (та же компания)" in text:
        last_company = context.user_data.get("company")
        if last_company:
            # Временно включаем "мини-массовый режим" — только компания
            context.chat_data["batch_company"] = last_company
            await update.message.reply_text(
                f"🏢 Компания зафиксирована: {last_company}"
            )
        return await add_start(update, context)

    # Кнопка "Готово / в меню" — выходим из массового режима
    if "Готово" in text or "в меню" in text:
        count = context.chat_data.get("batch_count", 0)
        context.chat_data.pop("batch_mode", None)
        context.chat_data.pop("batch_company", None)
        context.chat_data.pop("batch_date", None)
        context.chat_data.pop("batch_count", None)

        summary = f"\n📦 Записано операций: {count}" if count > 0 else ""

        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Добавить операцию"), KeyboardButton("⚡ Массовый ввод")],
                [KeyboardButton("💰 Остатки"), KeyboardButton("↩️ Отменить последнюю")],
            ],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            f"🏁 Готово!{summary}\n\nВыбирайте действие:",
            reply_markup=keyboard,
        )
        return

    # Кнопка "Массовый ввод" — запустить массовый режим
    if "Массовый ввод" in text or "массовый" in text.lower():
        return await batch_start(update, context)

    if "Остатки" in text:
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
            MessageHandler(filters.Regex(r"Добавить операцию|Ещё операция|Ещё \(та же компания\)"), add_start),
        ],
        states={
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_type)],
            COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_company)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            CONTRAGENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_contragent)],
            CONTRAGENT_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_contragent_search)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_source)],
            PAYMENT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_method)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )

    # Диалог массового режима
    batch_conv = ConversationHandler(
        entry_points=[
            CommandHandler("batch", batch_start),
            MessageHandler(filters.Regex(r"Массовый ввод"), batch_start),
        ],
        states={
            BATCH_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_company)],
            BATCH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_date)],
            BATCH_DATE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_date_custom)],
        },
        fallbacks=[CommandHandler("cancel", batch_cancel)],
    )

    app.add_handler(batch_conv)
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
