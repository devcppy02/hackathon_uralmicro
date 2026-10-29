from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.models import CallInput, Extraction, PromiseType, TaskStatus
from app.storage import ConflictError, Storage


def test_idempotent_ingestion(storage, call, extraction):
    assert storage.new_calls([call, call]) == [call]
    result = storage.save_calls([call, call], extraction)
    assert (result.processed, result.duplicates, result.tasks_created) == (1, 1, 1)
    assert len(result.tasks) == 1
    assert result.tasks[0].due_at == call.ended_at + timedelta(hours=1)
    assert storage.new_calls([call]) == []
    again = storage.save_calls([call], Extraction(calls=[]))
    assert (again.processed, again.duplicates, again.tasks_created) == (0, 1, 0)
    assert again.tasks[0].id == result.tasks[0].id


def test_duplicate_promises_are_deduplicated(storage, call, extraction):
    extraction.calls[0].promises *= 2
    result = storage.save_calls([call], extraction)
    assert result.tasks_created == 1


def test_changed_call_is_conflict(storage, call, extraction):
    changed = call.model_copy(update={"operator_id": 42})
    with pytest.raises(ConflictError):
        storage.new_calls([call, changed])
    storage.save_calls([call], extraction)
    with pytest.raises(ConflictError):
        storage.new_calls([changed])
    with pytest.raises(ConflictError):
        storage.save_calls([changed], extraction)


def test_concurrent_requests_create_tasks_once(storage, call, extraction):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: storage.save_calls([call], extraction), range(4)))
    assert sum(r.processed for r in results) == 1
    assert sum(r.tasks_created for r in results) == 1
    assert storage.list_tasks().total == 1


def test_atomic_rollback(storage, call, extraction):
    good = call.model_copy(update={"id": "good"})
    unknown_type = extraction.model_copy(deep=True)
    unknown_type.calls[0].promises[0].type_code = "deleted_type"
    # First call succeeds within the transaction; second fails its foreign key.
    data = Extraction(calls=[{"call_id": "good", "promises": []}, unknown_type.calls[0]])
    with pytest.raises(ConflictError, match="types changed"):
        storage.save_calls([good, call], data)
    assert storage.new_calls([good, call]) == [good, call]
    assert storage.list_tasks().total == 0


def test_completed_call_without_promises_stays_idempotent(storage, call):
    result = storage.save_calls([call], Extraction(calls=[{"call_id": call.id, "promises": []}]))
    assert result.processed == 1
    assert result.tasks_created == 0
    assert storage.new_calls([call]) == []


def test_default_types_not_recreated_after_deletion(storage):
    assert len(storage.list_types()) == 3
    assert storage.delete_type("callback")
    restarted = Storage(storage.path)
    restarted.initialize()
    assert restarted.get_type("callback") is None
    assert len(restarted.list_types()) == 2


def test_filtering_pagination_and_timezones(storage, call, extraction):
    second = CallInput(**{**call.model_dump(), "id": "call-2", "operator_id": 8})
    data = Extraction(
        calls=[
            extraction.calls[0],
            {
                "call_id": second.id,
                "promises": [extraction.calls[0].promises[0]],
            },
        ]
    )
    storage.save_calls([call, second], data)
    assert storage.list_tasks(operator_id=7).total == 1
    assert storage.list_tasks(operator_id=999).total == 0
    assert storage.list_tasks(status=TaskStatus.COMPLETED).total == 0
    page = storage.list_tasks(limit=1, offset=1)
    assert page.total == 2 and len(page.items) == 1
    due = call.ended_at + timedelta(hours=1)
    assert storage.list_tasks(due_from=due, due_to=due).total == 2
    assert storage.list_tasks(due_from=due + timedelta(seconds=1)).total == 0
    assert storage.list_tasks(due_to=due - timedelta(seconds=1)).total == 0


def test_task_lifecycle(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    with pytest.raises(ConflictError):
        storage.update_task(task.id, TaskStatus.COMPLETED)
    progress = storage.update_task(task.id, TaskStatus.IN_PROGRESS)
    assert progress.status == TaskStatus.IN_PROGRESS
    assert storage.update_task(task.id, TaskStatus.IN_PROGRESS) == progress
    done = storage.update_task(task.id, TaskStatus.COMPLETED)
    assert done.completed_at is not None
    assert storage.update_task(task.id, TaskStatus.COMPLETED) == done
    with pytest.raises(ConflictError):
        storage.update_task(task.id, TaskStatus.IN_PROGRESS)
    assert storage.update_task("missing", TaskStatus.IN_PROGRESS) is None


def test_overdue_is_automatic_and_completed_is_preserved(storage, call, extraction, monkeypatch):
    task = storage.save_calls([call], extraction).tasks[0]
    storage.update_task(task.id, TaskStatus.IN_PROGRESS)
    monkeypatch.setattr("app.storage.utcnow", lambda: task.due_at + timedelta(seconds=1))
    assert storage.list_tasks(status=TaskStatus.OVERDUE).items[0].id == task.id
    with pytest.raises(ConflictError):
        storage.update_task(task.id, TaskStatus.IN_PROGRESS)
    storage.update_task(task.id, TaskStatus.COMPLETED)
    assert storage.list_tasks(status=TaskStatus.COMPLETED).total == 1
    assert storage.list_tasks(status=TaskStatus.OVERDUE).total == 0


def test_persistence(storage, call, extraction):
    storage.create_type(
        PromiseType(code="refund", name="Возврат", description="Вернуть деньги клиенту")
    )
    task = storage.save_calls([call], extraction).tasks[0]
    restarted = Storage(storage.path)
    restarted.initialize()
    assert restarted.list_tasks().items[0] == task
    assert restarted.new_calls([call]) == []
    assert restarted.get_type("refund").name == "Возврат"
