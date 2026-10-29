import asyncio
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime
from starlette.concurrency import run_in_threadpool

from app.bot import TelegramService
from app.completion import CompletionChecker
from app.config import Settings
from app.llm import LLMError, QwenExtractor
from app.models import (
    AutoTypeExtraction,
    CallBatch,
    CallsResult,
    CompletionDecision,
    Extraction,
    PromiseType,
    PromiseTypeUpdate,
    Task,
    TaskCorrection,
    TaskPage,
    TaskStatus,
    TaskUpdate,
    TelegramLink,
)
from app.notifications import Notifications
from app.storage import ConflictError, Storage
from app.workflow import Workflow


def create_app(
    settings: Settings | None = None, extractor=None, completion_checker=None, start_bot=True
) -> FastAPI:
    settings = settings or Settings()
    storage = Storage(settings.database_path)
    workflow, notifications = Workflow(storage), Notifications(storage)
    telegram = TelegramService(settings, storage, workflow, notifications)
    auto_type_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage.initialize()
        # A local model must be reached directly, not through machine-wide proxies.
        async with httpx.AsyncClient(trust_env=False) as client:
            app.state.extractor = extractor or QwenExtractor(client, settings)
            app.state.checker = completion_checker or CompletionChecker(
                QwenExtractor(client, settings), workflow
            )
            worker = None
            if start_bot and settings.telegram_bot_token.get_secret_value():
                worker = asyncio.create_task(telegram.run())
            try:
                yield
            finally:
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    app = FastAPI(title="УралМикро · Контроль обещаний", version="0.1.0", lifespan=lifespan)
    app.state.telegram = telegram
    app.state.storage = storage
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def interface():
        return FileResponse(static / "index.html")

    @app.exception_handler(ConflictError)
    async def conflict_handler(request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(LLMError)
    async def llm_error_handler(request, error):
        return JSONResponse(status_code=502, content={"detail": str(error)})

    @app.get("/promise-types", response_model=list[PromiseType], tags=["Типы обещаний"])
    def list_types():
        return storage.list_types()

    @app.post("/promise-types", response_model=PromiseType, status_code=201, tags=["Типы обещаний"])
    def create_type(item: PromiseType):
        return storage.create_type(item)

    @app.get("/promise-types/{code}", response_model=PromiseType, tags=["Типы обещаний"])
    def get_type(code: str):
        item = storage.get_type(code)
        if item is None:
            raise HTTPException(404, "Promise type not found")
        return item

    @app.put("/promise-types/{code}", response_model=PromiseType, tags=["Типы обещаний"])
    def update_type(code: str, item: PromiseTypeUpdate):
        updated = storage.update_type(code, item)
        if updated is None:
            raise HTTPException(404, "Promise type not found")
        return updated

    @app.delete("/promise-types/{code}", status_code=204, tags=["Типы обещаний"])
    def delete_type(code: str):
        if not storage.delete_type(code):
            raise HTTPException(404, "Promise type not found")
        return Response(status_code=204)

    @app.post("/calls", response_model=CallsResult, tags=["Звонки"])
    async def receive_calls(calls: CallBatch):
        # Auto typing must see categories committed by the previous webhook.
        async with auto_type_lock if settings.auto_create_promise_types else nullcontext():
            new_calls = await run_in_threadpool(storage.new_calls, calls)
            types = await run_in_threadpool(storage.list_types)
            extracted = (
                await app.state.extractor.extract(new_calls, types)
                if new_calls
                else Extraction(calls=[])
            )
            new_types = extracted.new_types if isinstance(extracted, AutoTypeExtraction) else []
            if new_types and not settings.auto_create_promise_types:
                raise LLMError("Automatic promise type creation is disabled")
            result = await run_in_threadpool(storage.save_calls, calls, extracted, new_types)
        try:
            result.completions_suggested = await app.state.checker.check(calls)
        except LLMError as error:
            # Main tasks are already committed. Retrying the same IDs checks evidence again.
            result.warnings.append(
                "Задачи сохранены, проверка выполнения не завершена: " + str(error)
            )
        await run_in_threadpool(notifications.prepare, settings.reminder_minutes)
        return result

    @app.get("/tasks", response_model=TaskPage, tags=["Задачи"])
    def list_tasks(
        status: TaskStatus | None = None,
        operator_id: Annotated[int | None, Query(gt=0)] = None,
        due_from: AwareDatetime | None = None,
        due_to: AwareDatetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if due_from and due_to and due_from > due_to:
            raise HTTPException(422, "due_from must be on or before due_to")
        return storage.list_tasks(
            status=status,
            operator_id=operator_id,
            due_from=due_from,
            due_to=due_to,
            limit=limit,
            offset=offset,
        )

    @app.patch("/tasks/{task_id}", response_model=Task, tags=["Задачи"])
    def update_task(task_id: str, item: TaskUpdate):
        updated = storage.update_task(task_id, item.status, item.actor, item.reason)
        if updated is None:
            raise HTTPException(404, "Task not found")
        return updated

    @app.get("/tasks/{task_id}", response_model=Task, tags=["Задачи"])
    def get_task(task_id: str):
        task = storage.get_task(task_id)
        if task is None:
            raise HTTPException(404, "Task not found")
        return task

    @app.patch("/tasks/{task_id}/details", response_model=Task, tags=["Задачи"])
    def correct_task(task_id: str, item: TaskCorrection):
        task = workflow.correct(task_id, item)
        if task is None:
            raise HTTPException(404, "Task not found")
        return task

    @app.get("/tasks/{task_id}/history", tags=["Задачи"])
    def task_history(task_id: str):
        if storage.get_task(task_id) is None:
            raise HTTPException(404, "Task not found")
        return workflow.history(task_id)

    @app.get("/summary", tags=["Сводка"])
    def summary(operator_id: Annotated[int | None, Query(gt=0)] = None):
        return workflow.summary(operator_id)

    @app.get("/completion-suggestions", tags=["Подтверждения"])
    def suggestions(
        operator_id: Annotated[int | None, Query(gt=0)] = None,
        status: Annotated[str, Query(pattern="^(pending|accepted|rejected|obsolete)$")] = "pending",
    ):
        return workflow.suggestions(operator_id, status)

    @app.post("/completion-suggestions/{suggestion_id}/review", tags=["Подтверждения"])
    def review_suggestion(suggestion_id: str, decision: CompletionDecision):
        result = workflow.review(suggestion_id, decision)
        if result is None:
            raise HTTPException(404, "Suggestion not found")
        return result

    @app.get("/telegram/status", tags=["Уведомления"])
    def telegram_status():
        return telegram.status()

    @app.post("/telegram/link", tags=["Уведомления"])
    def telegram_link(item: TelegramLink):
        if not settings.telegram_bot_token.get_secret_value():
            raise HTTPException(503, "Configure TELEGRAM_BOT_TOKEN in .env")
        result = notifications.create_link(item.operator_id)
        result["url"] = (
            f"https://t.me/{telegram.username}?start={result['code']}"
            if telegram.username
            else None
        )
        return result

    @app.get("/notifications", tags=["Уведомления"])
    def notification_journal(
        operator_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        notifications.prepare(settings.reminder_minutes)
        return notifications.journal(operator_id, limit, offset)

    return app
