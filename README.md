# УралМикро · Контроль обещаний

Небольшой API на Python / FastAPI для кейса «Сервис контроля обещаний».
Принимает звонки, находит обещания оператора через локальную Qwen в LM Studio
и сохраняет отдельные задачи в SQLite.

В проекте только CRUD типов обещаний, `POST /calls`, `GET /tasks` с фильтрами
и `PATCH /tasks/{id}` для смены статуса. Внешний сервер БД и очереди не нужны.

## Запуск на Windows

Нужны Python 3.11–3.14 (рекомендуется 3.12), Git и доступ к компьютеру с LM Studio.
Команды для PowerShell:

```powershell
git clone https://github.com/devcppy02/hackathon_uralmicro.git
cd hackathon_uralmicro
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Откройте [Swagger UI](http://localhost:8080/docs).
Активация virtualenv и изменение ExecutionPolicy не нужны. Все команды выполняются
из корня проекта. Один процесс Uvicorn достаточен для кейса.

Настройки в `.env`:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DATABASE_PATH` | `data/promises.sqlite3` | Файл SQLite, создаётся при запуске |
| `APP_TIMEZONE` | `Asia/Yekaterinburg` | Часовой пояс для календарных сроков |
| `LLM_BASE_URL` | `http://172.20.10.6:1234/v1` | OpenAI-совместимый адрес LM Studio |
| `LLM_MODEL` | `qwen/qwen3.8-27b` | Точный идентификатор модели |
| `LLM_API_KEY` | `lm-studio` | Ключ сервера, если он требуется |
| `LLM_TIMEOUT_SECONDS` | `120` | Тайм-аут ответа модели |
| `LLM_MAX_TOKENS` | `4096` | Максимальная длина ответа модели |

Адрес можно указать и без `/v1`: сервис добавит его. Идентификатор модели и доступность
сервера можно проверить так:

```powershell
Invoke-RestMethod http://172.20.10.6:1234/v1/models
```

На Linux / macOS аналогично: `python3 -m venv .venv`, затем используйте
`.venv/bin/python` вместо `.\.venv\Scripts\python.exe`.

## Подключение sender

`service_token` относится к внешнему API УралМикро. Получите его у организаторов
или создайте команду через `POST /api/v1/teams` в их Swagger. При необходимости
зарегистрируйте операторов через `POST /api/v1/auth/register`.

[Swagger организаторов](https://utmn-api.uralmicro.ru/swagger/index.html).
Самостоятельно опрашивать их `/calls` не нужно — это делает sender.

```powershell
.\sender_windows_amd64.exe -t <service_token> -w http://localhost:8080/calls
```

Обратите внимание на путь **`/calls`**. Если sender запущен на другом компьютере,
замените `localhost` на LAN-адрес компьютера с этим API и разрешите входящие
подключения к порту 8080 в брандмауэре Windows.

Вебхук ждёт результат Qwen и возвращает уже сохранённые задачи. Все новые звонки
одной пачки отправляются модели одним запросом. В нашей проверке 10 коротких
синтетических звонков обработались примерно за 46 секунд; время зависит от модели,
длины разговоров и оборудования. Клиенту нужен соответствующий тайм-аут.

## Методы API

| Метод | Путь | Действие |
| --- | --- | --- |
| `GET` | `/promise-types` | Список типов |
| `POST` | `/promise-types` | Добавить тип с его смысловым описанием |
| `GET` | `/promise-types/{code}` | Получить тип |
| `PUT` | `/promise-types/{code}` | Заменить название и описание типа |
| `DELETE` | `/promise-types/{code}` | Удалить неиспользуемый тип |
| `POST` | `/calls` | Обработать массив из 1–10 звонков |
| `GET` | `/tasks` | Список задач с фильтрами |
| `PATCH` | `/tasks/{task_id}` | Изменить статус задачи |

При первом запуске создаются три типа:

- `callback` — перезвонить;
- `send_documents` — отправить документы;
- `clarify_and_return` — уточнить информацию и вернуться с ответом.

Пример тела `POST /promise-types`:

```json
{
  "code": "refund",
  "name": "Возврат денег",
  "description": "Оператор обещает оформить возврат денег клиенту на карту или счёт."
}
```

`code` неизменяемый, состоит из латинских строчных букв, цифр и `_`, начинается
с буквы. Для `PUT` передаются только `name` и `description`. Удаление типа,
на который ссылаются существующие задачи, возвращает `409`.

Описания типов передаются Qwen при каждой новой обработке. Добавление типа не требует
изменений Python-кода. Уже обработанные звонки и их задачи при этом не пересчитываются.

### Входящий звонок

`POST /calls` принимает массив, соответствующий `callapi.CallResponse` из Swagger
организаторов. Например:

```json
[
  {
    "id": "demo-call-1",
    "operator_id": 7,
    "client_phone": "+70000000000",
    "started_at": "2026-10-29T10:00:00+05:00",
    "ended_at": "2026-10-29T10:05:00+05:00",
    "transcript": [
      {"speaker": "client", "text": "Мне нужен договор."},
      {"speaker": "operator", "text": "Я отправлю договор через час. Завтра в 10:00 перезвоню."}
    ]
  }
]
```

`speaker`: `operator` / `client`; также принимаются `оператор` / `клиент`.
Время должно содержать часовой пояс, окончание не может быть раньше начала.
Максимум 40 000 символов расшифровки на звонок. Исторические звонки принимаются:
если срок задачи уже прошёл, она сразу отображается как `overdue`.

Ответ содержит `processed`, `duplicates`, `tasks_created` и `tasks`.
Задача содержит тип, текст действия, исходную цитату, индекс реплики с нуля,
оператора, телефон, срок, источник срока и статус.

### Сроки и статусы

- Отсчёт идёт от `ended_at`, а не от момента получения вебхука.
- Простые относительные сроки (`через час`, `через два дня`) пересчитываются кодом.
  Остальные явные календарные сроки определяет Qwen в `APP_TIMEZONE`.
- Дата без времени означает 18:00, конец дня — 23:59:59, конец рабочего дня — 18:00.
- Если срок не указан или не определён: ровно 24 часа после окончания звонка,
  `deadline_source=default_24h`. Явный срок: `deadline_source=explicit`.
- В БД даты хранятся в UTC, API возвращает ISO 8601 с часовым поясом.
- Переходы: `created` → `in_progress` → `completed`.
  Просрочка незавершённой задачи определяется автоматически при обращении к задачам.
  Из `overdue` можно перейти в `completed`.
- Повтор установки текущего статуса безопасен. Обратный переход или пропуск
  `in_progress` для непросроченной задачи возвращает `409`.

Тело `PATCH /tasks/{task_id}`:

```json
{"status": "in_progress"}
```

Фильтры `GET /tasks` можно сочетать:

| Параметр | Значение |
| --- | --- |
| `status` | `created`, `in_progress`, `completed`, `overdue` |
| `operator_id` | Положительный ID оператора |
| `due_from` | Нижняя граница срока включительно, ISO 8601 с часовым поясом |
| `due_to` | Верхняя граница срока включительно, ISO 8601 с часовым поясом |
| `limit` | Размер страницы, 1–500, по умолчанию 100 |
| `offset` | Смещение, по умолчанию 0 |

Пример: `/tasks?operator_id=7&status=created&due_to=2026-10-30T18:00:00Z`.
Ответ: `items`, `total`, `limit`, `offset`; сортировка по сроку и ID.

### Повторы и ошибки

Один `id` звонка обрабатывается один раз. Повтор того же содержимого возвращает
существующие задачи, даже при одновременных запросах. Разные `id` остаются разными
звонками, даже если текст совпадает. Изменённый звонок с прежним `id` возвращает `409`.

При ошибке/тайм-ауте Qwen или некорректном ответе модели API возвращает `502`;
пачка не сохраняется частично. Её можно отправить снова с теми же `id`.
Проверяется, что цитата реально присутствует в реплике оператора, тип существует,
а срок процитирован из разговора. Неверные входные данные/фильтры — `422`,
несуществующий тип или задача — `404`.

## Демонстрация в PowerShell

При работающем сервере этот пример создаёт звонок с текущим временем,
получает задачи оператора и проводит одну задачу через два статуса:

```powershell
$base = "http://localhost:8080"
$end = [DateTimeOffset]::Now
$call = @{
    id = [guid]::NewGuid().ToString()
    operator_id = 7
    client_phone = "+70000000000"
    started_at = $end.AddMinutes(-5).ToString("o")
    ended_at = $end.ToString("o")
    transcript = @(
        @{ speaker = "client"; text = "Мне нужен договор." }
        @{ speaker = "operator"; text = "Договор вышлю через час. Перезвоню через два часа." }
    )
}
$json = ConvertTo-Json -InputObject @($call) -Depth 6
$body = [System.Text.Encoding]::UTF8.GetBytes($json)
$result = Invoke-RestMethod -Method Post -Uri "$base/calls" `
    -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 180
$result.tasks | Format-Table id, type_code, due_at, status
Invoke-RestMethod "$base/tasks?operator_id=7&status=created"
$taskId = $result.tasks[0].id
Invoke-RestMethod -Method Patch -Uri "$base/tasks/$taskId" `
    -ContentType "application/json" -Body '{"status":"in_progress"}'
Invoke-RestMethod -Method Patch -Uri "$base/tasks/$taskId" `
    -ContentType "application/json" -Body '{"status":"completed"}'
```

Повторный POST с тем же `$body` покажет `duplicates=1`, `tasks_created=0`.

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Обычные тесты не требуют работающей модели. Они проверяют весь HTTP-сценарий,
CRUD, фильтры, сроки, статусы, перезапуск с сохранённой БД, параллельные повторы,
откат транзакций, запросы к LM Studio и ошибочные ответы модели.
Порог покрытия с ветвлениями — 95%. GitHub Actions настроен на Windows и Linux,
Python 3.11, 3.12, 3.13 и 3.14. Зависимости зафиксированы для всех этих платформ.

Отдельный тест с **реальным Qwen** на пачке из 10 звонков, включая новый тип
«возврат», отрицания, уже выполненные действия и подтверждение просьбы клиента:

```powershell
$env:RUN_LIVE_LLM = "1"
.\.venv\Scripts\python.exe -m pytest -m live --no-cov -v
Remove-Item Env:RUN_LIVE_LLM
```

## Структура и ограничения

- `app/api.py` — маршруты FastAPI.
- `app/models.py` — входные/выходные Pydantic-модели.
- `app/storage.py` — короткие SQL-транзакции и три таблицы SQLite.
- `app/llm.py` — один запрос к Qwen и проверка результата.
- `app/deadlines.py` — относительные сроки и правило 24 часов.
- `app/config.py` — настройки окружения.

SQLite хранит типы, задачи и хеши обработанных звонков; после перезапуска всё
сохраняется. Для чистой демонстрации остановите сервер и удалите папку `data`.

Это локальный прототип для одной команды: без авторизации, уведомлений и проверки
фактического исполнения обещания. Качество смыслового извлечения и сложных дат
зависит от Qwen; проверки цитат не гарантируют отсутствие смысловых ошибок.
Уведомления исключены из минимального уровня кейса. Фонового планировщика нет:
просрочка актуализируется при чтении, сохранении и изменении задач.

ИИ-инструменты: Codex (OpenAI) использован при разработке и тестировании;
локальная `qwen/qwen3.8-27b` используется сервисом для обработки расшифровок.

Документация используемых механизмов:
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/),
[LM Studio structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html).
