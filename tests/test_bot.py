import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot import TelegramService, build_router, completion_buttons, task_buttons, task_text
from app.config import Settings
from app.notifications import Notifications
from app.workflow import Workflow
from tests.test_workflow import setup_evidence

TEST_TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmn"


class MemorySession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Test", username="test_ural_bot")
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


@pytest.fixture
def bot_setup(storage):
    session = MemorySession()
    bot = Bot(TEST_TOKEN, session=session)
    notifications = Notifications(storage)
    workflow = Workflow(storage)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(storage, workflow, notifications, "Asia/Yekaterinburg"))
    return bot, dispatcher, session, notifications


async def message(setup, text, chat_id=100, chat_type="private"):
    bot, dispatcher, session, _ = setup
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=chat_id, is_bot=False, first_name="Operator"),
        text=text,
    )
    await dispatcher.feed_update(bot, Update(update_id=len(session.requests) + 1, message=msg))
    return [item for item in session.requests if isinstance(item, SendMessage)][-1].text


async def callback(setup, data, user_id=100):
    bot, dispatcher, session, _ = setup
    query = CallbackQuery(
        id="cb",
        chat_instance="local",
        data=data,
        from_user=User(id=user_id, is_bot=False, first_name="Operator"),
    )
    await dispatcher.feed_update(
        bot, Update(update_id=len(session.requests) + 1, callback_query=query)
    )
    return [item for item in session.requests if isinstance(item, AnswerCallbackQuery)][-1].text


async def test_start_stop_and_private_binding(bot_setup):
    _, _, _, notifications = bot_setup
    assert "одноразовый код" in await message(bot_setup, "/start")
    assert "личном чате" in await message(
        bot_setup, "/start wrong", chat_id=-100, chat_type="group"
    )
    assert "недействителен" in await message(bot_setup, "/start wrong")
    code = notifications.create_link(7)["code"]
    assert "оператора 7" in await message(bot_setup, "/start " + code)
    assert notifications.operator(100) == 7
    assert "отключены" in await message(bot_setup, "/stop")
    assert notifications.operator(100) is None
    assert "одноразовый код" in await message(bot_setup, "/tasks")


async def test_tasks_and_status_callbacks(bot_setup, storage, call, extraction):
    notifications = bot_setup[-1]
    notifications.bind(notifications.create_link(7)["code"], 100)
    assert "Открытых задач нет" in await message(bot_setup, "/tasks")
    task = storage.save_calls([call], extraction).tasks[0]
    assert "Отправить договор" in await message(bot_setup, "/tasks")
    assert "недоступна" in await callback(bot_setup, "task:start:" + task.id, user_id=200)
    assert "Некорректная" in await callback(bot_setup, "task:bad")
    assert "Неизвестное" in await callback(bot_setup, "task:unknown:" + task.id)
    assert "В работе" in await callback(bot_setup, "task:start:" + task.id)
    assert "Выполнена" in await callback(bot_setup, "task:done:" + task.id)
    assert "уже изменился" in await callback(bot_setup, "task:start:" + task.id)
    assert storage.get_task(task.id).status == "completed"
    assert Workflow(storage).history(task.id)[-1]["actor"] == "telegram:100"


@pytest.mark.parametrize("accept", [True, False])
async def test_completion_callbacks(bot_setup, storage, call, extraction, accept):
    notifications = bot_setup[-1]
    workflow, task, _, _ = setup_evidence(storage, call, extraction)
    notifications.bind(notifications.create_link(7)["code"], 100)
    proposal = workflow.suggestions()[0]
    assert "Некорректная" in await callback(bot_setup, "evidence:bad")
    assert "недоступно" in await callback(bot_setup, "evidence:yes:" + proposal["id"], user_id=200)
    response = await callback(
        bot_setup, ("evidence:yes:" if accept else "evidence:no:") + proposal["id"]
    )
    assert ("подтверждено" if accept else "отклонено") in response
    assert storage.get_task(task.id).status == ("completed" if accept else "created")
    assert "недоступно" in await callback(bot_setup, "evidence:yes:" + proposal["id"])


def test_message_escaping_and_button_size(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    task.quote = "<script>" + "x" * 10000
    text = task_text(task)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert len(text) < 2000
    for markup in [task_buttons(task), completion_buttons("x" * 36)]:
        assert all(
            len(button.callback_data.encode()) <= 64
            for row in markup.inline_keyboard
            for button in row
        )
    task.status = "completed"
    assert task_buttons(task) is None


@pytest.fixture
def service(storage):
    return TelegramService(
        Settings(_env_file=None), storage, Workflow(storage), Notifications(storage)
    )


async def test_persistent_delivery_and_no_duplicate(service, storage, call, extraction):
    storage.save_calls([call], extraction)
    service.notifications.bind(service.notifications.create_link(7)["code"], 100)
    bot = AsyncMock()
    await service.send_pending(bot)
    await service.send_pending(bot)
    bot.send_message.assert_awaited_once()
    assert service.notifications.journal()["items"][0]["status"] == "sent"


@pytest.mark.parametrize("kind", ["network", "blocked", "rate_limit"])
async def test_delivery_failures_preserve_queue(service, storage, call, extraction, kind):
    storage.save_calls([call], extraction)
    service.notifications.bind(service.notifications.create_link(7)["code"], 100)
    bot = AsyncMock()
    error = RuntimeError("offline")
    if kind == "blocked":
        error = TelegramForbiddenError(method=SendMessage(chat_id=100, text="x"), message="blocked")
    if kind == "rate_limit":
        error = TelegramRetryAfter(
            method=SendMessage(chat_id=100, text="x"), message="slow", retry_after=3
        )
    bot.send_message.side_effect = error
    await service.send_pending(bot)
    item = service.notifications.journal()["items"][0]
    assert item["attempts"] == 1
    assert item["status"] == ("skipped" if kind == "blocked" else "pending")
    assert (service.notifications.operator(100) is None) == (kind == "blocked")


async def test_send_completion_notification(service, storage, call, extraction):
    setup_evidence(storage, call, extraction)
    service.notifications.bind(service.notifications.create_link(7)["code"], 100)
    bot = AsyncMock()
    await service.send_pending(bot)
    assert any("договор получил" in args.args[1] for args in bot.send_message.await_args_list)


async def test_bot_startup_and_cancellation(service):
    session = MemorySession()
    bot = Bot(TEST_TOKEN, session=session)
    with (
        patch("app.bot.Bot", return_value=bot),
        patch(
            "app.bot.Dispatcher.start_polling", new=AsyncMock(side_effect=asyncio.CancelledError)
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.run()
    assert service.connected and service.username == "test_ural_bot"
    assert session.closed


async def test_failed_startup_does_not_crash_api(service):
    session = MemorySession()
    bot = Bot(TEST_TOKEN, session=session)
    with (
        patch("app.bot.Bot", return_value=bot),
        patch.object(bot, "get_me", new=AsyncMock(side_effect=RuntimeError("offline"))),
        patch("app.bot.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.run()
    assert service.last_error == "RuntimeError" and not service.connected


async def test_sender_cancellation(service):
    service.send_pending = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await service.send_loop(AsyncMock())
