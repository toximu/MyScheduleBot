import asyncio
from datetime import datetime
import json
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
import aiosqlite
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)
DB_NAME = "tasks.db"


# ---------------- База данных ----------------
async def init_db():
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                due_date TEXT,
                is_completed INTEGER DEFAULT 0
            )
        """)
    await db.commit()


# ---------------- Схема ответа ----------------
class TaskExtraction(BaseModel):
  title: str = Field(description="Краткая суть задачи")
  due_date: str | None = Field(
      description="Дедлайн в формате YYYY-MM-DD HH:MM или null, если не указан"
  )


# ---------------- Логика Gemini ----------------
def get_system_prompt() -> str:
  current_time = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
  return f"""
    Текущие дата и время: {current_time}.
    Твоя задача — извлечь из входящего сообщения (аудио или текста) суть задачи и точный дедлайн/время напоминания.
    Если пользователь называет относительное время ("завтра утром", "в пятницу в 18:00"), вычисли точную дату от текущей.
    Верни результат строго в JSON по заданной схеме.
    """


async def process_with_gemini(
    audio_bytes: bytes = None, text: str = None
) -> TaskExtraction:
  contents = []
  if audio_bytes:
    contents.append(
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    )
    contents.append("Извлеки задачу и дедлайн из этого аудио.")
  else:
    contents.append(text)

  # Вызов Gemini в отдельном потоке (SDK синхронный)
  response = await asyncio.to_thread(
      ai_client.models.generate_content,
      model="gemini-2.5-flash",
      contents=contents,
      config=types.GenerateContentConfig(
          system_instruction=get_system_prompt(),
          response_mime_type="application/json",
          response_schema=TaskExtraction,
          temperature=0.1,
      ),
  )
  return TaskExtraction.model_validate_json(response.text)


# ---------------- Хэндлеры ----------------

# Хэндлер обычных текстовых сообщений
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
  # Показываем статус "печатает...", пока Gemini парсит текст
  await bot.send_chat_action(chat_id=message.chat.id, action="typing")

  # Передаем текст пользователя напрямую в Gemini
  parsed_task = await process_with_gemini(text=message.text)

  # Сохраняем результат в SQLite
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute(
        "INSERT INTO tasks (user_id, title, due_date) VALUES (?, ?, ?)",
        (message.from_user.id, parsed_task.title, parsed_task.due_date),
    )
    await db.commit()

  deadline_text = (
      f"`{parsed_task.due_date}`"
      if parsed_task.due_date
      else "без точного времени"
  )
  await message.reply(
      f"✅ **Задача сохранена!**\n\n"
      f"📌 **Суть:** {parsed_task.title}\n"
      f"⏰ **Срок:** {deadline_text}",
      parse_mode="Markdown",
  )

@dp.message(CommandStart())
async def cmd_start(message: Message):
  await message.answer(
      "👋 Отправь голосовое или текст с задачей.\n\n"
      "Например: *'Напомни завтра в 11 созвониться с клиентом'*\n\n"
      "Список задач: /tasks",
      parse_mode="Markdown",
  )


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
  async with aiosqlite.connect(DB_NAME) as db:
    cursor = await db.execute(
        "SELECT id, title, due_date FROM tasks WHERE user_id = ? AND is_completed = 0 ORDER BY due_date ASC",
        (message.from_user.id,),
    )
    tasks = await cursor.fetchall()

  if not tasks:
    await message.answer("Активных задач нет 🎉")
    return

  text = "📋 **Твои задачи:**\n\n"
  for _, title, due_date in tasks:
    deadline_str = f" ⏳ `{due_date}`" if due_date else " *(без дедлайна)*"
    text += f"• **{title}**{deadline_str}\n"

  await message.answer(text, parse_mode="Markdown")


# Обработка голосовых сообщений
@dp.message(F.voice)
async def handle_voice(message: Message):
  status_msg = await message.answer("🎙️ Обрабатываю голос через Gemini...")

  # Скачиваем файл в память без сохранения на диск
  file = await bot.get_file(message.voice.file_id)
  file_stream = await bot.download_file(file.file_path)
  audio_bytes = file_stream.read()

  # Отправляем напрямую в Gemini
  parsed_task = await process_with_gemini(audio_bytes=audio_bytes)

  # Сохраняем в БД
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute(
        "INSERT INTO tasks (user_id, title, due_date) VALUES (?, ?, ?)",
        (message.from_user.id, parsed_task.title, parsed_task.due_date),
    )
    await db.commit()

  deadline_text = (
      f"`{parsed_task.due_date}`"
      if parsed_task.due_date
      else "без точного времени"
  )
  await status_msg.edit_text(
      f"✅ **Задача сохранена!**\n\n"
      f"📌 **Суть:** {parsed_task.title}\n"
      f"⏰ **Срок:** {deadline_text}",
      parse_mode="Markdown",
  )


# Обработка текста
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
  parsed_task = await process_with_gemini(text=message.text)

  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute(
        "INSERT INTO tasks (user_id, title, due_date) VALUES (?, ?, ?)",
        (message.from_user.id, parsed_task.title, parsed_task.due_date),
    )
    await db.commit()

  deadline_text = (
      f"`{parsed_task.due_date}`"
      if parsed_task.due_date
      else "без точного времени"
  )
  await message.answer(
      f"✅ **Задача сохранена!**\n\n"
      f"📌 **Суть:** {parsed_task.title}\n"
      f"⏰ **Срок:** {deadline_text}",
      parse_mode="Markdown",
  )


# ---------------- Запуск ----------------
async def main():
  await init_db()
  print("Бот запущен на Gemini...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())