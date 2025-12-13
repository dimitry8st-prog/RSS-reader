import os
import asyncio
import logging
import re
import sqlite3
from typing import Optional, List, Tuple
from urllib.parse import urlparse

import feedparser
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "bot_data.db")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен! Установите переменную окружения BOT_TOKEN.")


# Инициализация БД
def init_db() -> None:
    """Инициализирует базу данных и создает необходимые таблицы."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # Таблица подписок: user_id -> url
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, url)
                )
            ''')
            # Таблица состояния лент: url -> last_entry_id (чтобы не спамить)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feed_state (
                    url TEXT PRIMARY KEY,
                    last_entry_id TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Таблица "Прочитать позже": user_id -> title, link
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS read_later (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, link)
                )
            ''')
            conn.commit()
            logger.info("База данных инициализирована успешно")
    except sqlite3.Error as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        raise


init_db()


# --- Вспомогательные функции БД ---

def db_add_subscription(user_id: int, url: str) -> bool:
    """Добавляет подписку пользователя на RSS-ленту."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT INTO subscriptions (user_id, url) VALUES (?, ?)",
                (user_id, url)
            )
            conn.commit()
            logger.info(f"Пользователь {user_id} подписался на {url}")
            return True
    except sqlite3.IntegrityError:
        logger.debug(f"Пользователь {user_id} уже подписан на {url}")
        return False
    except sqlite3.Error as e:
        logger.error(f"Ошибка при добавлении подписки: {e}")
        return False


def db_get_unique_urls() -> List[str]:
    """Возвращает список всех уникальных URL подписок."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return [row[0] for row in conn.cursor().execute(
                "SELECT DISTINCT url FROM subscriptions"
            ).fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении URL: {e}")
        return []


def db_get_subscribers(url: str) -> List[int]:
    """Возвращает список ID пользователей, подписанных на указанную ленту."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return [row[0] for row in conn.cursor().execute(
                "SELECT user_id FROM subscriptions WHERE url = ?",
                (url,)
            ).fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении подписчиков: {e}")
        return []


def db_get_last_entry(url: str) -> Optional[str]:
    """Возвращает ID последней обработанной записи для указанной ленты."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            res = conn.cursor().execute(
                "SELECT last_entry_id FROM feed_state WHERE url = ?",
                (url,)
            ).fetchone()
            return res[0] if res else None
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении последней записи: {e}")
        return None


def db_update_last_entry(url: str, entry_id: str) -> None:
    """Обновляет ID последней обработанной записи для указанной ленты."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO feed_state (url, last_entry_id, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (url, entry_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка при обновлении последней записи: {e}")


def db_add_read_later(user_id: int, title: str, link: str) -> bool:
    """Добавляет статью в список 'Прочитать позже' для пользователя."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT INTO read_later (user_id, title, link) VALUES (?, ?, ?)",
                (user_id, title, link)
            )
            conn.commit()
            logger.info(f"Статья '{title}' добавлена в список для пользователя {user_id}")
            return True
    except sqlite3.IntegrityError:
        logger.debug(f"Статья '{link}' уже в списке для пользователя {user_id}")
        return False
    except sqlite3.Error as e:
        logger.error(f"Ошибка при добавлении в список: {e}")
        return False


def db_get_read_later(user_id: int) -> List[Tuple[int, str, str]]:
    """Возвращает список статей 'Прочитать позже' для пользователя."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.cursor().execute(
                "SELECT id, title, link FROM read_later WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка: {e}")
        return []


def db_remove_read_later(item_id: int, user_id: int) -> bool:
    """Удаляет статью из списка 'Прочитать позже'."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM read_later WHERE id = ? AND user_id = ?",
                (item_id, user_id)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Статья {item_id} удалена из списка пользователя {user_id}")
                return True
            return False
    except sqlite3.Error as e:
        logger.error(f"Ошибка при удалении из списка: {e}")
        return False


def validate_url(url: str) -> bool:
    """Проверяет валидность URL."""
    try:
        result = urlparse(url)
        return all([result.scheme in ['http', 'https'], result.netloc])
    except Exception:
        return False


# --- Команды ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    if not update.message:
        return
    await update.message.reply_text(
        "👋 Привет! Я новостной агрегатор.\n\n"
        "Команды:\n"
        "/add_feed <url> - Добавить ленту\n"
        "/list - Мои подписки\n"
        "/reading_list - Список 'Прочитать позже'"
    )


async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Добавляет RSS-ленту в подписки пользователя."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ Используйте: /add_feed <url>")
        return

    url = context.args[0].strip()

    # Валидация URL
    if not validate_url(url):
        await update.message.reply_text("❌ Неверный формат URL. Используйте http:// или https://")
        return

    msg = await update.message.reply_text("🔄 Проверяю ленту...")

    # Парсим асинхронно, чтобы не блокировать бота
    loop = asyncio.get_running_loop()
    try:
        # Используем executor для feedparser (он синхронный)
        feed = await loop.run_in_executor(None, feedparser.parse, url)

        # Проверка на ошибки парсинга
        if hasattr(feed, 'bozo') and feed.bozo:
            logger.warning(f"Ошибка парсинга RSS для {url}: {feed.bozo_exception}")

        feed_title = getattr(feed.feed, 'title', None) if hasattr(feed, 'feed') else None
        if not feed.entries and not feed_title:
            await msg.edit_text("❌ Неверная ссылка или пустая RSS-лента.")
            return

        title = feed_title or 'Без названия'

        if db_add_subscription(user_id, url):
            await msg.edit_text(
                f"✅ Подписка оформлена: **{title}**\n\n"
                f"Лента: `{url}`",
                parse_mode="Markdown"
            )

            # Инициализируем состояние ленты текущей последней записью, чтобы не спамить старым
            if feed.entries and len(feed.entries) > 0:
                first_entry = feed.entries[0]
                last_entry_id = str(getattr(first_entry, 'id', None) or getattr(first_entry, 'link', ''))
                if last_entry_id:
                    db_update_last_entry(url, last_entry_id)
        else:
            await msg.edit_text("ℹ️ Вы уже подписаны на эту ленту.")

    except (ConnectionError, OSError) as e:
        logger.error(f"Ошибка сети при добавлении ленты {url}: {e}")
        await msg.edit_text("❌ Ошибка сети. Проверьте подключение к интернету и URL.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при добавлении ленты {url}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {str(e)}")


async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список подписок пользователя."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    try:
        with sqlite3.connect(DB_FILE) as conn:
            urls = [row[0] for row in conn.cursor().execute(
                "SELECT url FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()]

        if not urls:
            await update.message.reply_text("📭 У вас нет подписок.\n\nИспользуйте /add_feed <url> для добавления.")
            return

        text = "📋 **Ваши подписки:**\n\n" + "\n".join([f"• `{url}`" for url in urls])
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка подписок: {e}")
        await update.message.reply_text("❌ Ошибка при получении списка подписок.")


# --- Логика "Прочитать позже" ---

async def save_read_later_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Прочитать позже'."""
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()

    try:
        # Извлекаем данные из сообщения через entities
        message_entities = query.message.entities or query.message.caption_entities
        link = None
        title = "Сохраненная статья"

        # Ищем ссылку в сообщении
        if message_entities:
            for entity in message_entities:
                if entity.type == 'text_link':
                    link = entity.url
                    # Пытаемся взять текст ссылки как заголовок
                    if query.message.text:
                        title = query.message.text[entity.offset: entity.offset + entity.length]
                    break

        # Если не нашли через entities, пытаемся извлечь из текста
        if not link and query.message.text:
            # Пытаемся найти ссылку в формате Markdown [text](url)
            match = re.search(r'\[([^\]]+)\]\(([^)]+)\)', query.message.text)
            if match:
                title = match.group(1)
                link = match.group(2)

        if not link:
            await query.edit_message_reply_markup(None)
            await query.message.reply_text("❌ Не удалось найти ссылку в сообщении.")
            return

        user_id = query.from_user.id if query.from_user else 0
        if user_id and db_add_read_later(user_id, title, link):
            await query.message.reply_text(f"💾 Сохранено: {title}")
        else:
            await query.message.reply_text("ℹ️ Уже в списке.")

    except Exception as e:
        logger.error(f"Ошибка в callback сохранения: {e}", exc_info=True)
        if query and query.message:
            await query.message.reply_text("❌ Ошибка при сохранении.")


async def show_reading_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список статей 'Прочитать позже'."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    items = db_get_read_later(user_id)

    if not items:
        await update.message.reply_text("📭 Список для чтения пуст.")
        return

    text = "📚 **Прочитать позже:**\n\n"
    keyboard = []

    for item_id, title, link in items:
        # Ограничиваем длину заголовка для читаемости
        display_title = title[:50] + "..." if len(title) > 50 else title
        text += f"• [{display_title}]({link})\n"
        # Кнопка для удаления из списка (callback: "del:<id>")
        button_text = f"❌ Удалить: {title[:20]}..." if len(title) > 20 else f"❌ Удалить: {title}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"del:{item_id}")])

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )


async def delete_read_later_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик удаления статьи из списка 'Прочитать позже'."""
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()

    try:
        if not query.data:
            await query.message.edit_text("❌ Ошибка: неверный формат данных.")
            return
        item_id = int(query.data.split(":")[1])
        user_id = query.from_user.id if query.from_user else 0
        
        if db_remove_read_later(item_id, user_id):
            await query.message.edit_text("✅ Статья удалена из списка.")
        else:
            await query.message.edit_text("❌ Статья не найдена или уже удалена.")
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга callback data: {e}")
        await query.message.edit_text("❌ Ошибка: неверный формат данных.")
    except Exception as e:
        logger.error(f"Ошибка при удалении: {e}", exc_info=True)
        await query.message.edit_text("❌ Ошибка удаления.")


# --- Фоновая задача (Job Queue) ---

async def check_feeds_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фоновая задача для проверки RSS-лент на новые записи."""
    unique_urls = db_get_unique_urls()
    
    if not unique_urls:
        logger.debug("Нет подписок для проверки")
        return

    logger.info(f"Проверка {len(unique_urls)} RSS-лент...")
    loop = asyncio.get_running_loop()

    for url in unique_urls:
        try:
            # Запуск feedparser в отдельном потоке
            feed = await loop.run_in_executor(None, feedparser.parse, url)

            # Проверка на ошибки парсинга
            if hasattr(feed, 'bozo') and feed.bozo:
                logger.warning(f"Ошибка парсинга RSS для {url}: {feed.bozo_exception}")

            if not feed.entries:
                logger.debug(f"Лента {url} не содержит записей")
                continue

            if not feed.entries or len(feed.entries) == 0:
                continue
                
            last_entry = feed.entries[0]
            # Используем ID записи или ссылку как уникальный идентификатор
            entry_id = str(getattr(last_entry, 'id', None) or getattr(last_entry, 'link', ''))

            prev_entry_id = db_get_last_entry(url)

            # Если ID совпадает с сохраненным - новостей нет
            if entry_id == prev_entry_id:
                continue

            # Найдена новая запись!
            title = str(getattr(last_entry, 'title', None) or 'Без заголовка')
            link = str(getattr(last_entry, 'link', None) or '')
            
            if not link:
                logger.warning(f"Запись в ленте {url} не содержит ссылки")
                continue

            # Сохраняем новый ID в БД
            if entry_id:
                db_update_last_entry(url, entry_id)

            # Рассылка всем подписчикам этой ленты
            subscribers = db_get_subscribers(url)
            
            if not subscribers:
                continue

            feed_title = str(getattr(feed.feed, 'title', None) or 'News')
            callback_data = "save:new"

            keyboard = [[InlineKeyboardButton("💾 Прочитать позже", callback_data=callback_data)]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Ограничиваем длину заголовка для Telegram
            display_title = (title[:200] + "...") if len(title) > 200 else title

            for user_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📰 *{feed_title}*\n\n[{display_title}]({link})",
                        parse_mode="Markdown",
                        reply_markup=reply_markup,
                        disable_web_page_preview=False
                    )
                    logger.info(f"Новая статья отправлена пользователю {user_id} из ленты {url}")
                except Exception as e:
                    # Пользователь мог заблокировать бота или произошла другая ошибка
                    logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")

        except (ConnectionError, OSError) as e:
            logger.error(f"Ошибка сети при проверке ленты {url}: {e}")
        except Exception as e:
            logger.error(f"Ошибка проверки ленты {url}: {e}", exc_info=True)


def main() -> None:
    """Основная функция запуска бота."""
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не установлен!")
        raise ValueError("BOT_TOKEN не установлен!")
    
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Хендлеры команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("add_feed", add_feed))
        application.add_handler(CommandHandler("list", list_feeds))
        application.add_handler(CommandHandler("reading_list", show_reading_list))

        # Callback Query с Regex фильтром
        application.add_handler(CallbackQueryHandler(save_read_later_callback, pattern="^save:"))
        application.add_handler(CallbackQueryHandler(delete_read_later_callback, pattern="^del:"))

        # Job Queue - проверка лент каждые 10 минут
        job_queue = application.job_queue
        if job_queue:
            # Запуск раз в 10 минут (600 сек), первый запуск через 10 сек
            job_queue.run_repeating(check_feeds_job, interval=600, first=10)
            logger.info("Job Queue инициализирован: проверка лент каждые 10 минут")

        logger.info("Бот запущен и готов к работе...")
        application.run_polling()
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()