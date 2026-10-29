# УралМикро · Контроль обещаний

API на Python / FastAPI для кейса «Сервис контроля обещаний». Принимает звонки от Windows sender, выделяет обещания через локальную Qwen в LM Studio и создаёт отдельные задачи.

Небольшой синхронный сервис: CRUD типов обещаний, `POST /calls`, `GET /tasks`
с фильтрами и `PATCH /tasks/{id}` для смены статуса. Никаких внешних БД и очередей.

## Требования

- Python 3.11–3.14 (рекомендуется 3.12), Windows x64 / Linux / macOS.
- Доступ к LM Studio: `http://172.20.10.6:1234/v1`, модель `qwen/qwen3.8-27b`.
- SQLite встроен в Python; отдельный сервер БД не нужен.

## Запуск на Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Swagger: <http://localhost:8080/docs>. Команды выполняются из корня репозитория.
Активация virtualenv и изменение ExecutionPolicy не нужны.

Отправитель направляйте на `/calls`:

```powershell
.\sender_windows_amd64.exe -t <service_token> -w http://localhost:8080/calls
```

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Тесты не требуют работающей модели. Зафиксированные зависимости общие для Windows,
Linux и macOS. Подробные примеры запросов и сквозной сценарий добавляются следующим коммитом.
