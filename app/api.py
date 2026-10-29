from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.llm import LLMError, QwenExtractor
from app.models import (
    CallBatch,
    CallsResult,
    Extraction,
    PromiseType,
    PromiseTypeUpdate,
    Task,
    TaskPage,
    TaskStatus,
    TaskUpdate,
)
from app.storage import ConflictError, Storage


def create_app(settings: Settings | None = None, extractor=None) -> FastAPI:
    settings = settings or Settings()
    storage = Storage(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage.initialize()
        async with httpx.AsyncClient() as client:
            app.state.extractor = extractor or QwenExtractor(client, settings)
            yield

    app = FastAPI(title="УралМикро · Контроль обещаний", version="0.1.0", lifespan=lifespan)

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
        new_calls = await run_in_threadpool(storage.new_calls, calls)
        types = await run_in_threadpool(storage.list_types)
        extracted = (
            await app.state.extractor.extract(new_calls, types)
            if new_calls
            else Extraction(calls=[])
        )
        return await run_in_threadpool(storage.save_calls, calls, extracted)

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
        updated = storage.update_task(task_id, item.status)
        if updated is None:
            raise HTTPException(404, "Task not found")
        return updated

    return app
