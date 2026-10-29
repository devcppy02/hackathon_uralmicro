import asyncio
import logging
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from starlette.concurrency import run_in_threadpool

from app.models import CompletionDecision, TaskStatus
from app.storage import ConflictError

logger = logging.getLogger(__name__)

LABELS = {
    "created": "Создана",
    "in_progress": "В работе",
    "completed": "Выполнена",
    "overdue": "Просрочена",
}


def task_buttons(task):
    if task.status == "completed":
        return None
    action, label = (
        ("start", "Взять в работу") if task.status == "created" else ("done", "Выполнено")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"task:{action}:{task.id}")]
        ]
    )


def completion_buttons(suggestion_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data=f"evidence:yes:{suggestion_id}"
                ),
                InlineKeyboardButton(
                    text="Отклонить", callback_data=f"evidence:no:{suggestion_id}"
                ),
            ]
        ]
    )


def task_text(task, timezone="Asia/Yekaterinburg"):
    due = task.due_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    quote = task.quote[:600] + ("…" if len(task.quote) > 600 else "")
    return (
        f"<b>{escape(task.title)}</b>\n{escape(task.client_phone)} · оператор {task.operator_id}"
        f"\nСрок: {due}\nСтатус: {LABELS[task.status]}\n"
        f"<blockquote>{escape(quote)}</blockquote>"
    )


def build_router(storage, workflow, notifications, timezone):
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if message.chat.type != "private":
            await message.answer("Подключите уведомления в личном чате с ботом.")
            return
        if not command.args:
            await message.answer(
                "Откройте веб-интерфейс → Уведомления → Подключить оператора. "
                "Пришлите /start и выданный одноразовый код."
            )
            return
        operator_id = await run_in_threadpool(notifications.bind, command.args, message.chat.id)
        if operator_id is None:
            await message.answer("Код недействителен или истёк. Создайте новый в интерфейсе.")
            return
        await message.answer(
            f"Уведомления оператора {operator_id} подключены.\n"
            "/tasks — задачи\n/stop — отключить уведомления"
        )

    @router.message(Command("stop"))
    async def stop(message: Message):
        await run_in_threadpool(notifications.unbind, message.chat.id)
        await message.answer("Уведомления отключены. Подключиться снова можно через веб-интерфейс.")

    @router.message(Command("tasks"))
    async def tasks(message: Message):
        operator_id = await run_in_threadpool(notifications.operator, message.chat.id)
        if operator_id is None:
            await message.answer("Сначала подключите оператора через одноразовый код.")
            return
        page = await run_in_threadpool(storage.list_tasks, operator_id=operator_id, limit=500)
        active = [task for task in page.items if task.status != "completed"]
        if not active:
            await message.answer("Открытых задач нет.")
        for task in active[:10]:
            await message.answer(task_text(task, timezone), reply_markup=task_buttons(task))
        if len(active) > 10:
            await message.answer("Показаны первые 10 задач. Полный список — в веб-интерфейсе.")

    @router.callback_query(F.data.startswith("task:"))
    async def change_status(query: CallbackQuery):
        if query.data.count(":") != 2:
            await query.answer("Некорректная кнопка", show_alert=True)
            return
        _, action, task_id = query.data.split(":", 2)
        task = await run_in_threadpool(storage.get_task, task_id)
        operator_id = await run_in_threadpool(notifications.operator, query.from_user.id)
        if task is None or operator_id != task.operator_id:
            await query.answer("Эта задача вам недоступна", show_alert=True)
            return
        if action not in {"start", "done"}:
            await query.answer("Неизвестное действие", show_alert=True)
            return
        status = TaskStatus.IN_PROGRESS if action == "start" else TaskStatus.COMPLETED
        try:
            await run_in_threadpool(
                storage.update_task,
                task_id,
                status,
                f"telegram:{query.from_user.id}",
                "Кнопка Telegram",
            )
        except ConflictError:
            await query.answer(
                "Статус уже изменился. Обновите список через /tasks", show_alert=True
            )
            return
        await query.answer("Статус: " + LABELS[status], show_alert=True)

    @router.callback_query(F.data.startswith("evidence:"))
    async def review(query: CallbackQuery):
        if query.data.count(":") != 2:
            await query.answer("Некорректная кнопка", show_alert=True)
            return
        _, action, suggestion_id = query.data.split(":", 2)
        operator_id = await run_in_threadpool(notifications.operator, query.from_user.id)
        suggestions = await run_in_threadpool(workflow.suggestions, operator_id)
        if (
            operator_id is None
            or action not in {"yes", "no"}
            or not any(row["id"] == suggestion_id for row in suggestions)
        ):
            await query.answer("Предложение недоступно или уже рассмотрено", show_alert=True)
            return
        try:
            await run_in_threadpool(
                workflow.review,
                suggestion_id,
                CompletionDecision(accept=action == "yes", actor=f"telegram:{query.from_user.id}"),
            )
        except ConflictError:
            await query.answer("Задача уже изменилась", show_alert=True)
            return
        await query.answer(
            "Выполнение подтверждено" if action == "yes" else "Предложение отклонено",
            show_alert=True,
        )

    return router


class TelegramService:
    def __init__(self, settings, storage, workflow, notifications):
        self.settings, self.storage = settings, storage
        self.workflow, self.notifications = workflow, notifications
        self.username = None
        self.connected = False
        self.last_error = None

    def status(self):
        return {
            "configured": bool(self.settings.telegram_bot_token.get_secret_value()),
            "connected": self.connected,
            "username": self.username,
            "last_error": self.last_error,
            "bindings": self.notifications.bindings(),
        }

    async def send_pending(self, bot):
        await run_in_threadpool(self.notifications.prepare, self.settings.reminder_minutes)
        pending = await run_in_threadpool(self.notifications.pending)
        for item in pending:
            task = item["task"]
            prefix = {
                "new": "Новая задача",
                "reminder": "Срок скоро истекает",
                "overdue": "Задача просрочена",
                "completion": "Возможно, задача выполнена",
            }
            text = (
                "<b>"
                + prefix[item["kind"]]
                + "</b>\n\n"
                + task_text(task, self.settings.app_timezone)
            )
            buttons = task_buttons(task)
            if item["suggestion"]:
                text += (
                    "\nПодтверждение в следующем звонке:\n<blockquote>"
                    + escape(item["suggestion"]["quote"][:600])
                    + "</blockquote>\nПроверьте перед закрытием."
                )
                buttons = completion_buttons(item["suggestion_id"])
            try:
                await bot.send_message(item["chat_id"], text, reply_markup=buttons)
            except TelegramForbiddenError:
                await run_in_threadpool(
                    self.notifications.failed, item["id"], "BotBlocked", 0, True
                )
                await run_in_threadpool(self.notifications.unbind, item["chat_id"])
            except TelegramRetryAfter as error:
                await run_in_threadpool(
                    self.notifications.failed, item["id"], "RateLimit", error.retry_after
                )
            except Exception as error:
                self.last_error = type(error).__name__
                await run_in_threadpool(
                    self.notifications.failed,
                    item["id"],
                    self.last_error,
                    min(300, 15 * 2 ** min(item["attempts"], 4)),
                )
            else:
                await run_in_threadpool(self.notifications.sent, item["id"])

    async def send_loop(self, bot):
        while True:
            await self.send_pending(bot)
            await asyncio.sleep(self.settings.notification_poll_seconds)

    async def run(self):
        while True:
            try:
                async with Bot(
                    self.settings.telegram_bot_token.get_secret_value(),
                    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                ) as bot:
                    me = await bot.get_me(request_timeout=10)
                    self.username, self.connected, self.last_error = me.username, True, None
                    await bot.delete_webhook(drop_pending_updates=False, request_timeout=10)
                    dispatcher = Dispatcher()
                    dispatcher.include_router(
                        build_router(
                            self.storage,
                            self.workflow,
                            self.notifications,
                            self.settings.app_timezone,
                        )
                    )
                    sender = asyncio.create_task(self.send_loop(bot))
                    try:
                        await dispatcher.start_polling(
                            bot,
                            handle_signals=False,
                            close_bot_session=False,
                            polling_timeout=20,
                            handle_as_tasks=False,
                            allowed_updates=["message", "callback_query"],
                        )
                    finally:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
            except Exception as error:
                self.connected, self.last_error = False, type(error).__name__
                logger.warning("Telegram connection unavailable (%s)", self.last_error)
            await asyncio.sleep(10)
