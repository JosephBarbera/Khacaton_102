"""Telegram-бот «Афиша культуры Башкортостана» З.

Запуск:  BOT_TOKEN=... python bot.py
Данные:  python import_events.py data/events_sample.json
"""




import asyncio
import html
import logging
import os
from datetime import datetime


from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("afisha")

PAGE_SIZE = 5
CATEGORIES = db.CATEGORIES
PERIODS = {
    "today": "📅 Сегодня",
    "tomorrow": "🌅 Завтра",
    "weekend": "📆 Выходные",
    "week": "🗓 Неделя",
    "custom": "конкретная дата",
    "any": "все даты",
}
PRICES = {
    "free": "бесплатно",
    "500": "до 500 ₽",
    "1000": "до 1000 ₽",
    "2000": "до 2000 ₽",
    "any": "любая цена",
}
DEFAULT_FILTERS = {
    "city": None,
    "category": None,
    "period": None,
    "custom_date": None,
    "price": None,
    "kids": False,
    "text": "",
}
HELP_TEXT = (
    "<b>Как пользоваться</b>\n"
    "• «🔎 Подобрать событие» — фильтры: город, категория, даты, цена, для детей.\n"
    "• Или просто напишите запрос текстом: «концерт в Уфе в выходные».\n"
    "• /subscribe — подписка на город и категорию, /unsubscribe — отписка.\n"
    "• /cancel — сбросить выбор, /help — эта справка.\n\n"
    "Все события берутся из базы — я ничего не придумываю от себя."
)

dp = Dispatcher(storage=MemoryStorage())


class Dialog(StatesGroup):
    custom_date = State()


# ---------- утилиты ----------

def esc(value):
    return html.escape(str(value)) if value else ""


def fmt_dt(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    if dt.hour or dt.minute:
        return dt.strftime("%d.%m.%Y, %H:%M")
    return dt.strftime("%d.%m.%Y")


def price_str(ev):
    if ev.get("is_free"):
        return "бесплатно"
    lo, hi = ev.get("price_min"), ev.get("price_max")
    if lo is not None and hi is not None and lo != hi:
        return "{0}–{1} ₽".format(lo, hi)
    if lo:
        return "от {0} ₽".format(lo)
    if hi:
        return "до {0} ₽".format(hi)
    return "цена уточняется"


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def period_label(f):
    if f.get("period") == "custom" and f.get("custom_date"):
        return fmt_dt(f["custom_date"])
    return PERIODS.get(f.get("period") or "any", "все даты")


async def safe_answer(c, text=None, alert=False):
    try:
        await c.answer(text, show_alert=alert)
    except Exception:
        pass


async def safe_edit(c, text, kb=None):
    try:
        if c.message:
            await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        log.warning("Не удалось обновить сообщение (вероятно, оно устарело)")


async def current_filters(state):
    data = await state.get_data()
    return {**DEFAULT_FILTERS, **(data.get("f") or {})}


def parse_date_input(text):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------- клавиатуры ----------

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔎 Подобрать событие", "f_menu")],
        [btn("📅 Сегодня", "quick:today"), btn("📆 Выходные", "quick:weekend")],
        [btn("⭐ Подписка", "subs"), btn("❓ Помощь", "help")],
    ])


def filter_kb(f):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Город: " + (f["city"] or "любой"), "f_city")],
        [btn("Категория: " + (f["category"] or "любая"), "f_cat")],
        [btn("Даты: " + period_label(f), "f_date")],
        [btn("Цена: " + PRICES.get(f["price"] or "any", "любая"), "f_price")],
        [btn("🧒 Для детей: " + ("вкл" if f["kids"] else "выкл"), "f_kids")],
        [btn("🔍 Показать события", "f_show")],
        [btn("♻️ Сбросить", "f_reset"), btn("🏠 Меню", "menu")],
    ])


# ---------- тексты ----------

def result_line(i, r):
    return "{0}. <b>{1}</b>\n   {2} · {3} · {4} · {5}".format(
        i, esc(r["title"]), esc(r["category"]), esc(r["city"]),
        fmt_dt(r["date_start"]), price_str(r),
    )


def build_results(f, rows, total, page):
    lines = []
    kb_rows = []
    for i, r in enumerate(rows, start=page * PAGE_SIZE + 1):
        lines.append(result_line(i, r))
        kb_rows.append([btn(r["title"][:32], "ev:" + r["id"])])
    if total > (page + 1) * PAGE_SIZE:
        kb_rows.append([btn("⏭ Показать ещё", "more:" + str(page + 1))])
    kb_rows.append([btn("🎛 Фильтры", "f_menu"), btn("🔄 Обновить", "more:" + str(page))])
    text = "🔍 Найдено событий: {0}\n\n{1}".format(total, "\n\n".join(lines))
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def empty_text(f):
    active = []
    if f.get("city"):
        active.append("• город: " + esc(f["city"]))
    if f.get("category"):
        active.append("• категория: " + esc(f["category"]))
    if f.get("period"):
        active.append("• даты: " + period_label(f))
    if f.get("price"):
        active.append("• цена: " + PRICES.get(f["price"], f["price"]))
    if f.get("kids"):
        active.append("• только для детей")
    if f.get("text"):
        active.append("• текст запроса: «" + esc(f["text"]) + "»")
    head = "По вашему запросу ничего не нашлось"
    if active:
        head += ":\n" + "\n".join(active)
    return (
        head
        + "\n\nПопробуйте ослабить ограничение:\n"
        "• расширить период — например, «вся неделя»;\n"
        "• убрать ограничение по цене;\n"
        "• выбрать «любой город» или «любую категорию»."
    )


def card_text(ev):
    parts = [
        "<b>" + esc(ev["title"]) + "</b>",
        "🏷 Категория: " + esc(ev["category"]),
        "🕒 Когда: " + fmt_dt(ev["date_start"]),
    ]
    if ev.get("date_end"):
        parts.append("🕓 До: " + fmt_dt(ev["date_end"]))
    place = "📍 " + esc(ev["city"])
    if ev.get("venue"):
        place += ", " + esc(ev["venue"])
    parts.append(place)
    if ev.get("address"):
        parts.append("🗺 Адрес: " + esc(ev["address"]))
    if ev.get("description"):
        desc = ev["description"]
        parts.append("\n" + esc(desc[:600]) + ("…" if len(desc) > 600 else ""))
    parts.append("💰 Цена: " + price_str(ev))
    if ev.get("age_limit"):
        parts.append("🔐 Возраст: " + str(ev["age_limit"]) + "+")
    if ev.get("language"):
        parts.append("🗣 Язык: " + esc(ev["language"]))
    if ev.get("tags"):
        parts.append("🏷 Метки: " + esc(ev["tags"]))
    if ev["status"] != "опубликовано":
        parts.append("\n⚠️ Статус события: «" + esc(ev["status"]) + "»")
    return "\n".join(parts)


# ---------- поиск ----------

async def run_search(message: Message, state: FSMContext, page: int = 0):
    f = await current_filters(state)
    rows, total = db.search_events(f, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    await state.update_data(last_f=f, last_page=page)
    if not rows:
        await message.answer(empty_text(f), reply_markup=filter_kb(f))
        return
    text, kb = build_results(f, rows, total, page)
    await message.answer(text, reply_markup=kb)


# ---------- команды ----------

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.set_data({"f": dict(DEFAULT_FILTERS)})
    await state.set_state(None)
    await m.answer(
        "Привет! Помогу найти, куда пойти: концерты, спектакли, выставки, кино "
        "и экскурсии в Башкортостане.\n\nВыберите действие на кнопках или просто "
        "напишите запрос текстом.",
        reply_markup=main_kb(),
    )


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP_TEXT, reply_markup=main_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.set_data({"f": dict(DEFAULT_FILTERS)})
    await state.set_state(None)
    await m.answer("Сбросил выбор. Начинаем заново.", reply_markup=main_kb())


@dp.message(Command("subscribe"))
async def cmd_subscribe(m: Message, state: FSMContext):
    await ask_sub_city(m, state)


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(m: Message):
    removed = db.remove_subscriptions(m.from_user.id)
    if removed:
        await m.answer("Удалил подписки ({0}).".format(removed))
    else:
        await m.answer("У вас нет активных подписок.")


# ---------- подписка ----------

async def ask_sub_city(message: Message, state: FSMContext):
    rows = [[btn(city, "subc:" + city)] for city in db.cities()]
    rows.append([btn("🌐 Любой город", "subc:*")])
    rows.append([btn("🏠 Меню", "menu")])
    await message.answer(
        "Подписка: выберите город.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data == "subs")
async def cb_subs(c: CallbackQuery, state: FSMContext):
    if c.message:
        await ask_sub_city(c.message, state)
    await safe_answer(c)


@dp.callback_query(F.data.startswith("subc:"))
async def cb_sub_city(c: CallbackQuery, state: FSMContext):
    await state.update_data(sub_city=c.data.split(":", 1)[1])
    if c.message:
        rows = [[btn(cat, "subk:" + cat)] for cat in CATEGORIES]
        rows.append([btn("🌐 Все категории", "subk:*")])
        rows.append([btn("🏠 Меню", "menu")])
        await c.message.answer("Теперь выберите категорию.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("subk:"))
async def cb_sub_cat(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city = data.get("sub_city", "*")
    category = c.data.split(":", 1)[1]
    db.add_subscription(c.from_user.id, city, category)
    city_label = "любой город" if city == "*" else city
    cat_label = "все категории" if category == "*" else category
    if c.message:
        await c.message.answer(
            "Готово! Подписка: {0}, категория «{1}». Напишу, когда появятся новые "
            "события. Отписаться: /unsubscribe".format(city_label, cat_label),
            reply_markup=main_kb(),
        )
    await safe_answer(c)


# ---------- главное меню ----------

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.set_data({"f": dict(DEFAULT_FILTERS)})
    await safe_edit(c, "Главное меню. Выберите действие:", main_kb())
    await safe_answer(c)


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await safe_edit(c, HELP_TEXT, main_kb())
    await safe_answer(c)


# ---------- фильтры ----------

@dp.callback_query(F.data == "f_menu")
async def cb_f_menu(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    await safe_edit(c, "Настройте фильтры и нажмите «Показать события». Можно и просто написать запрос текстом.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_city")
async def cb_f_city(c: CallbackQuery, state: FSMContext):
    rows = [[btn(city, "setcity:" + city)] for city in db.cities()]
    rows.append([btn("🌐 Любой город", "setcity:*")])
    rows.append([btn("⬅️ Назад", "f_menu")])
    await safe_edit(c, "Выберите город:", InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("setcity:"))
async def cb_set_city(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    value = c.data.split(":", 1)[1]
    f["city"] = None if value == "*" else value
    await state.update_data(f=f)
    await safe_edit(c, "Фильтры обновлены.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_cat")
async def cb_f_cat(c: CallbackQuery, state: FSMContext):
    rows = [[btn(cat, "setcat:" + cat)] for cat in CATEGORIES]
    rows.append([btn("🌐 Любая категория", "setcat:*")])
    rows.append([btn("⬅️ Назад", "f_menu")])
    await safe_edit(c, "Выберите категорию:", InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("setcat:"))
async def cb_set_cat(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    value = c.data.split(":", 1)[1]
    f["category"] = None if value == "*" else value
    await state.update_data(f=f)
    await safe_edit(c, "Фильтры обновлены.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_date")
async def cb_f_date(c: CallbackQuery, state: FSMContext):
    rows = [[btn(PERIODS[k], "setdate:" + k)] for k in ("today", "tomorrow", "weekend", "week")]
    rows.append([btn("📅 Конкретная дата", "setdate:custom")])
    rows.append([btn("🌐 Все даты", "setdate:*")])
    rows.append([btn("⬅️ Назад", "f_menu")])
    await safe_edit(c, "Выберите период:", InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("setdate:"))
async def cb_set_date(c: CallbackQuery, state: FSMContext):
    value = c.data.split(":", 1)[1]
    f = await current_filters(state)
    if value == "custom":
        await state.set_state(Dialog.custom_date)
        await safe_edit(c, "Введите дату в формате ДД.ММ.ГГГГ, например 21.11.2026:", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Назад", "f_date")]]))
    else:
        f["period"] = None if value == "*" else value
        f["custom_date"] = None
        await state.update_data(f=f)
        await safe_edit(c, "Фильтры обновлены.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_price")
async def cb_f_price(c: CallbackQuery, state: FSMContext):
    rows = [[btn(PRICES[k], "setprice:" + k)] for k in ("free", "500", "1000", "2000")]
    rows.append([btn("🌐 Любая цена", "setprice:*")])
    rows.append([btn("⬅️ Назад", "f_menu")])
    await safe_edit(c, "Выберите цену:", InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("setprice:"))
async def cb_set_price(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    value = c.data.split(":", 1)[1]
    f["price"] = None if value == "*" else value
    await state.update_data(f=f)
    await safe_edit(c, "Фильтры обновлены.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_kids")
async def cb_kids(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    f["kids"] = not f["kids"]
    await state.update_data(f=f)
    await safe_edit(c, "Фильтры обновлены.", filter_kb(f))
    await safe_answer(c)


@dp.callback_query(F.data == "f_reset")
async def cb_reset(c: CallbackQuery, state: FSMContext):
    await state.update_data(f=dict(DEFAULT_FILTERS))
    await safe_edit(c, "Фильтры сброшены.", filter_kb(dict(DEFAULT_FILTERS)))
    await safe_answer(c)


@dp.callback_query(F.data == "f_show")
async def cb_show(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    rows, total = db.search_events(f, 0, PAGE_SIZE)
    await state.update_data(last_f=f, last_page=0)
    if not rows:
        await safe_edit(c, empty_text(f), filter_kb(f))
    else:
        text, kb = build_results(f, rows, total, 0)
        await safe_edit(c, text, kb)
    await safe_answer(c)


@dp.callback_query(F.data.startswith("quick:"))
async def cb_quick(c: CallbackQuery, state: FSMContext):
    f = await current_filters(state)
    f["period"] = c.data.split(":", 1)[1]
    f["custom_date"] = None
    rows, total = db.search_events(f, 0, PAGE_SIZE)
    await state.update_data(f=f, last_f=f, last_page=0)
    if not rows:
        await safe_edit(c, empty_text(f), main_kb())
    else:
        text, kb = build_results(f, rows, total, 0)
        await safe_edit(c, text, kb)
    await safe_answer(c)


# ---------- результаты и карточки ----------

@dp.callback_query(F.data.startswith("more:"))
async def cb_more(c: CallbackQuery, state: FSMContext):
    page = int(c.data.split(":")[1])
    f = {**DEFAULT_FILTERS, **((await state.get_data()).get("last_f") or {})}
    rows, total = db.search_events(f, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    await state.update_data(last_f=f, last_page=page)
    if not rows:
        await safe_edit(c, empty_text(f), main_kb())
    else:
        text, kb = build_results(f, rows, total, page)
        await safe_edit(c, text, kb)
    await safe_answer(c)


@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    f = {**DEFAULT_FILTERS, **(data.get("last_f") or {})}
    page = data.get("last_page", 0)
    rows, total = db.search_events(f, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text, kb = build_results(f, rows, total, page)
    await safe_edit(c, text, kb)
    await safe_answer(c)


@dp.callback_query(F.data.startswith("ev:"))
async def cb_event(c: CallbackQuery, state: FSMContext):
    ev = db.get_event(c.data.split(":", 1)[1])
    if not ev:
        await safe_answer(c, "Событие не найдено в базе", alert=True)
        return
    rows = []
    if ev.get("org_link"):
        rows.append([InlineKeyboardButton(text="🔗 Источник / билеты", url=ev["org_link"])])
    elif ev.get("source_url"):
        rows.append([InlineKeyboardButton(text="🔗 Источник", url=ev["source_url"])])
    data = await state.get_data()
    if data.get("last_f"):
        rows.append([btn("⬅️ К результатам", "back"), btn("🔁 Похожие", "sim:" + ev["id"])])
    else:
        rows.append([btn("🏠 Меню", "menu")])
    await safe_edit(c, card_text(ev), InlineKeyboardMarkup(inline_keyboard=rows))
    await safe_answer(c)


@dp.callback_query(F.data.startswith("sim:"))
async def cb_similar(c: CallbackQuery):
    rows = db.find_similar(c.data.split(":", 1)[1])
    if not rows:
        await safe_edit(c, "Похожих событий рядом по датам не нашлось. Попробуйте фильтры.", main_kb())
        await safe_answer(c)
        return
    lines = [result_line(i, r) for i, r in enumerate(rows, 1)]
    kb_rows = [[btn(r["title"][:32], "ev:" + r["id"])] for r in rows]
    kb_rows.append([btn("🏠 Меню", "menu")])
    await safe_edit(c, "Похожие события:\n\n" + "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await safe_answer(c)


# ---------- текстовые сообщения ----------

@dp.message(F.text)
async def on_text(m: Message, state: FSMContext):
    text = m.text.strip()

    if await state.get_state() == Dialog.custom_date.state:
        parsed = parse_date_input(text)
        if not parsed:
            await m.answer("Не понял дату. Введите её в формате ДД.ММ.ГГГГ, например 21.11.2026.")
            return
        f = await current_filters(state)
        f["period"] = "custom"
        f["custom_date"] = parsed
        await state.set_state(None)
        await state.update_data(f=f)
        await m.answer("Фильтры обновлены.", reply_markup=filter_kb(f))
        return

    if text.startswith("/"):
        await m.answer("Такой команды я не знаю. Посмотрите /help.")
        return

    if len(text) > 200:
        text = text[:200]

    f = await current_filters(state)
    f["text"] = text
    await state.update_data(f=f)
    await run_search(m, state, page=0)


# ---------- обработка ошибок ----------

@dp.errors()
async def on_error(event):
    log.error("Ошибка обработки обновления", exc_info=getattr(event, "exception", None))
    message = event.update.message
    callback = event.update.callback_query
    try:
        if message:
            await message.answer("Что-то сломалось с моей стороны. Нажмите /start и попробуйте ещё раз.")
        elif callback:
            await callback.answer("Произошла ошибка, попробуйте ещё раз", show_alert=True)
    except Exception:
        pass
    return True


@dp.callback_query()
async def cb_unknown(c: CallbackQuery):
    await safe_answer(c, "Не понял кнопку, попробуйте заново", alert=True)


async def main():
    db.init_db()
    if not os.getenv("BOT_TOKEN"):
        raise SystemExit("BOT_TOKEN не задан: скопируйте .env.example в .env и вставьте токен от @BotFather.")
    bot = Bot(os.getenv("BOT_TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("Бот запущен")

    while True:
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            delay = 10
            log.warning("Соединение потеряно (%s). Переподключение через %s с…", e, delay)
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
