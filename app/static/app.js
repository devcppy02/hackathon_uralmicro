"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const labels = {
  created: "Создана",
  in_progress: "В работе",
  completed: "Выполнена",
  overdue: "Просрочена",
};
const views = {
  tasks: "Задачи",
  summary: "Сводка",
  types: "Типы обещаний",
  notifications: "Уведомления",
  calls: "Приём звонков",
};
const state = {
  view: "tasks",
  types: [],
  suggestions: [],
  tasks: [],
  offset: 0,
  limit: 12,
  filters: {},
  busy: false,
};
let toastTimer;

function toast(text, error = false) {
  const element = $("#toast");
  element.textContent = text;
  element.className = "toast visible" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => element.classList.remove("visible"),
    error ? 9000 : 4000,
  );
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : {},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg).join("; ")
      : data.detail;
    const messages = {
      "Promise type already exists": "Тип с таким кодом уже существует",
      "Promise type is used by existing tasks":
        "Этот тип используется в задачах. Удалить его нельзя",
      "Completed tasks cannot be corrected":
        "Выполненную задачу нельзя редактировать",
      "Suggestion has already been reviewed or is obsolete":
        "Предложение уже рассмотрено или устарело",
      "Task changed; suggestion is obsolete":
        "Задача изменилась. Подтверждение больше не актуально",
      "Cannot connect to LM Studio":
        "Нет соединения с LM Studio. Проверьте, что сервер модели запущен",
      "LM Studio timed out; retry the webhook":
        "Модель не успела ответить. Повторите отправку звонков",
      "due_from must be on or before due_to":
        "Начало периода должно быть раньше окончания",
    };
    throw new Error(
      messages[detail] || detail || `Ошибка HTTP ${response.status}`,
    );
  }
  return data;
}

function date(value, timeOnly = false) {
  if (!value) return "—";
  return new Date(value).toLocaleString(
    "ru-RU",
    timeOnly
      ? { hour: "2-digit", minute: "2-digit" }
      : {
          day: "2-digit",
          month: "short",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        },
  );
}

function localValue(value) {
  const d = new Date(value);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

const badge = (status) =>
  `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
const typeName = (code) =>
  state.types.find((type) => type.code === code)?.name || code;
const options = (selected = "") =>
  state.types
    .map(
      (type) =>
        `<option value="${esc(type.code)}" ${type.code === selected ? "selected" : ""}>${esc(type.name)}</option>`,
    )
    .join("");
const actor = () => localStorage.getItem("uralmicro_actor") || "Оператор";

function heading(eyebrow, title, subtitle, actions = "") {
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><div class="subtitle">${subtitle}</div></div><div class="heading-actions">${actions}</div></div>`;
}

function kpis(data) {
  const items = [
    ["Всего задач", data.total, "Все договорённости", "▤", ""],
    ["В работе", data.in_progress, "Оператор приступил к задаче", "↗", ""],
    ["Выполнено", data.completed, "Обязательства закрыты", "✓", "highlight"],
    ["Просрочено", data.overdue, "Требуют внимания", "◷", "warning"],
  ];
  return `<div class="kpi-grid">${items.map(([label, value, foot, icon, style]) => `<div class="kpi ${style}"><div class="kpi-label">${label}<span>${icon}</span></div><div class="kpi-value">${value}</div><div class="kpi-foot">${foot}</div></div>`).join("")}</div>`;
}

function empty(title, description, action = "") {
  return `<div class="empty"><div class="empty-icon">✓</div><h3>${title}</h3><p>${description}</p>${action}</div>`;
}

async function navigate(view) {
  state.view = view;
  $("#content").classList.remove("view-enter");
  $$(".nav-item").forEach((button) =>
    button.classList.toggle("active", button.dataset.view === view),
  );
  $("#breadcrumb").textContent = views[view];
  $("#content").innerHTML = '<div class="loading">Загружаем данные…</div>';
  try {
    if (view === "tasks") await loadTasks();
    if (view === "summary") await loadSummary();
    if (view === "types") await loadTypes();
    if (view === "notifications") await loadNotifications();
    if (view === "calls") renderCalls();
    requestAnimationFrame(() => $("#content").classList.add("view-enter"));
  } catch (error) {
    $("#content").innerHTML =
      `<div class="note error">${esc(error.message)}. Проверьте, что FastAPI запущен, и обновите страницу.</div>`;
    toast(error.message, true);
  }
}

function $$(selector, root = document) {
  return [...root.querySelectorAll(selector)];
}

async function loadTasks() {
  const query = new URLSearchParams({
    limit: state.limit,
    offset: state.offset,
    ...state.filters,
  });
  const [page, summary, suggestions, types] = await Promise.all([
    api(`/tasks?${query}`),
    api(
      "/summary" +
        (state.filters.operator_id
          ? `?operator_id=${state.filters.operator_id}`
          : ""),
    ),
    api(
      "/completion-suggestions" +
        (state.filters.operator_id
          ? `?operator_id=${state.filters.operator_id}`
          : ""),
    ),
    api("/promise-types"),
  ]);
  state.types = types;
  state.tasks = page.items;
  state.suggestions = suggestions;
  $("#nav-task-count").textContent =
    summary.totals.created +
    summary.totals.in_progress +
    summary.totals.overdue;
  const filters = state.filters;
  const rows = page.items
    .map(
      (
        task,
      ) => `<tr class="task-row" data-task="${esc(task.id)}" tabindex="0" aria-label="Открыть задачу: ${esc(task.title)}">
    <td class="task-main"><strong>${esc(task.title)}${suggestions.some((s) => s.task_id === task.id) ? '<span class="evidence-dot" title="Есть подтверждение в следующем звонке"> ● Проверить</span>' : ""}</strong><small>${esc(typeName(task.type_code))}</small></td>
    <td class="mono">${esc(task.client_phone)}</td><td><span class="operator-chip"><b>${task.operator_id}</b>Оператор ${task.operator_id}</span></td>
    <td><span class="deadline-date ${task.status === "overdue" ? "deadline-late" : ""}">${esc(date(task.due_at))}</span><span class="deadline-time">${task.deadline_source === "default_24h" ? "24 часа по умолчанию" : task.deadline_source === "manual" ? "Уточнён вручную" : "Из разговора"}</span></td>
    <td>${badge(task.status)}</td><td style="color:#a6b39f">↗</td></tr>`,
    )
    .join("");
  $("#content").innerHTML =
    heading(
      "Контроль обещаний",
      "Задачи операторов",
      "Договорённости из звонков — под контролем.",
      `<span class="date-note">${esc(new Date().toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }))}</span><button class="button" data-refresh>↻ Обновить</button>`,
    ) +
    `<div class="total-note">Обзор по всем срокам${filters.operator_id ? ` · оператор ${esc(filters.operator_id)}` : ""}</div>` +
    kpis(summary.totals) +
    (suggestions.length
      ? `<div class="evidence-strip"><span class="evidence-strip-icon">✓</span><div><strong>Найдены подтверждения выполнения</strong><small>Предложений: ${suggestions.length}. Проверьте цитаты из следующих звонков перед закрытием задач.</small></div><button class="button small" data-task="${esc(suggestions[0].task_id)}">Рассмотреть →</button></div>`
      : "") +
    `<section class="panel"><div class="panel-header"><div class="panel-title"><h2>Все обязательства</h2><small>Найдено ${page.total} · сначала ближайший срок</small></div><button class="button primary" data-goto="calls">＋ Принять звонок</button></div>
    <form id="filters" class="filters"><label>Статус<select name="status"><option value="">Все статусы</option>${Object.entries(
      labels,
    )
      .map(
        ([key, value]) =>
          `<option value="${key}" ${filters.status === key ? "selected" : ""}>${value}</option>`,
      )
      .join("")}</select></label>
    <label>Оператор<input name="operator_id" type="number" min="1" placeholder="Все операторы" value="${esc(filters.operator_id)}"></label>
    <label>Срок от<input name="due_from" type="datetime-local" value="${filters.due_from ? localValue(filters.due_from) : ""}"></label>
    <label>Срок до<input name="due_to" type="datetime-local" value="${filters.due_to ? localValue(filters.due_to) : ""}"></label>
    <div class="filter-end"><button class="button" type="submit">Применить</button><button class="button" type="button" id="reset-filters">Сбросить</button></div></form>
    ${
      rows
        ? `<div class="table-scroll"><table class="task-table"><thead><tr><th>Обещание</th><th>Клиент</th><th>Ответственный</th><th>Срок выполнения</th><th>Статус</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : empty(
            "Задач пока нет",
            "Измените фильтры или примите первый звонок. Найденные обещания появятся здесь.",
            '<button class="button" data-goto="calls">Принять звонок →</button>',
          )
    }
    <div class="table-footer"><span>${page.total ? state.offset + 1 : 0}–${Math.min(state.offset + state.limit, page.total)} из ${page.total}</span><div class="pagination"><button class="button small" id="prev-page" ${state.offset === 0 ? "disabled" : ""}>←</button><button class="button small" id="next-page" ${state.offset + state.limit >= page.total ? "disabled" : ""}>→</button></div></div></section>
    <div class="page-note"><span>●</span>Данные обновляются каждые 20 секунд. Нажмите на задачу, чтобы посмотреть цитату и историю.</div>`;
  $("#filters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.filters = {};
    for (const [key, value] of new FormData(event.target))
      if (value)
        state.filters[key] = key.startsWith("due_")
          ? new Date(value).toISOString()
          : value;
    state.offset = 0;
    refresh();
  });
  $("#reset-filters").onclick = () => {
    state.filters = {};
    state.offset = 0;
    refresh();
  };
  $("#prev-page").onclick = () => {
    state.offset = Math.max(0, state.offset - state.limit);
    refresh();
  };
  $("#next-page").onclick = () => {
    state.offset += state.limit;
    refresh();
  };
}

async function openTask(id) {
  try {
    const [task, history, proposals] = await Promise.all([
      api(`/tasks/${encodeURIComponent(id)}`),
      api(`/tasks/${encodeURIComponent(id)}/history`),
      api("/completion-suggestions"),
    ]);
    const evidence = proposals.filter((item) => item.task_id === id);
    $("#task-detail").innerHTML =
      `<div class="dialog-heading"><div><div class="eyebrow">Карточка задачи</div><h2>${esc(typeName(task.type_code))}</h2></div><button class="button icon" data-close="task-dialog" aria-label="Закрыть">×</button></div>
    <div class="dialog-body"><div class="detail-meta">${badge(task.status)}<span>Оператор ${task.operator_id}</span><span class="mono">${esc(task.client_phone)}</span></div>
    ${evidence.map((item) => `<div class="evidence-card"><span class="badge pending">Возможно, выполнена</span><h3>Подтверждение в следующем звонке</h3><blockquote>${esc(item.quote)}</blockquote><p>${esc(item.explanation)} · ${item.speaker === "client" ? "Клиент" : "Оператор"}</p><div class="details-id">Звонок ${esc(item.call_id)} · реплика ${item.replica_index + 1}</div><div class="evidence-actions"><button class="button primary small" data-review="${esc(item.id)}" data-accept="true">Подтвердить выполнение</button><button class="button small" data-review="${esc(item.id)}" data-accept="false">Отклонить</button></div></div>`).join("")}
    <form id="correction-form" class="form-grid"><label>Задача<input name="title" value="${esc(task.title)}" maxlength="500" required ${task.status === "completed" ? "disabled" : ""}></label>
    <div class="form-row"><label>Тип обещания<select name="type_code" ${task.status === "completed" ? "disabled" : ""}>${options(task.type_code)}</select></label><label>Срок · местное время<input name="due_at" type="datetime-local" value="${localValue(task.due_at)}" required ${task.status === "completed" ? "disabled" : ""}></label></div>
    <div><span class="hint">ИСТОЧНИК · реплика ${task.replica_index + 1}</span><blockquote>${esc(task.quote)}</blockquote></div>
    <div class="form-row"><label>Автор изменения<input name="actor" value="${esc(actor())}" required maxlength="100"></label><label>Причина корректировки<input name="reason" placeholder="Например: клиент уточнил срок" minlength="3" maxlength="1000" ${task.status === "completed" ? "disabled" : ""}></label></div>
    <div class="dialog-footer"><button class="button" type="submit" ${task.status === "completed" ? "disabled" : ""}>Сохранить корректировку</button><div>${task.status === "created" ? '<button type="button" class="button primary" data-status="in_progress">Взять в работу →</button>' : task.status !== "completed" ? '<button type="button" class="button primary" data-status="completed">✓ Отметить выполненной</button>' : '<span class="badge completed">✓ Задача закрыта</span>'}</div></div></form>
    <div class="dialog-section"><h3>История изменений</h3><ul class="history-list">${
      history.length
        ? history
            .map((event) => {
              const action =
                {
                  created: "Задача создана",
                  status_changed: "Статус изменён",
                  corrected: "Данные уточнены",
                  overdue: "Срок истёк",
                  completion_accepted: "Выполнение подтверждено",
                  completion_rejected: "Подтверждение отклонено",
                }[event.action] || event.action;
              const changed = event.before
                ? ["title", "type_code", "due_at", "status"]
                    .filter((key) => event.before[key] !== event.after[key])
                    .map(
                      (key) =>
                        `${{ title: "Задача", type_code: "Тип", due_at: "Срок", status: "Статус" }[key]}: ${key === "status" ? labels[event.before[key]] + " → " + labels[event.after[key]] : key === "due_at" ? date(event.before[key]) + " → " + date(event.after[key]) : event.before[key] + " → " + event.after[key]}`,
                    )
                    .join("\n")
                : "";
              return `<li><strong>${esc(action)}</strong><small>${esc(date(event.created_at))} · ${esc(event.actor)}</small>${event.reason ? `<span class="history-change">${esc(event.reason)}</span>` : ""}${changed ? `<span class="history-change">${esc(changed)}</span>` : ""}</li>`;
            })
            .join("")
        : "<li>Для задач из предыдущей версии история начинается с первого изменения.</li>"
    }</ul></div><div class="details-id">Задача ${esc(task.id)}<br>Звонок ${esc(task.call_id)}</div></div>`;
    const dialog = $("#task-dialog");
    if (!dialog.open) dialog.showModal();
    $("#correction-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      if (!form.get("reason") || form.get("reason").trim().length < 3) {
        toast("Укажите причину корректировки", true);
        return;
      }
      await act(async () => {
        localStorage.setItem("uralmicro_actor", form.get("actor"));
        const body = { reason: form.get("reason"), actor: form.get("actor") };
        if (form.get("title") !== task.title) body.title = form.get("title");
        if (form.get("type_code") !== task.type_code)
          body.type_code = form.get("type_code");
        if (form.get("due_at") !== localValue(task.due_at)) {
          body.due_at = new Date(form.get("due_at")).toISOString();
        }
        if (Object.keys(body).length === 2)
          throw new Error("Нет изменений для сохранения");
        await api(`/tasks/${id}/details`, {
          method: "PATCH",
          body,
        });
        toast("Корректировка сохранена в истории");
        await openTask(id);
        await refresh();
      });
    };
    $$("[data-status]", dialog).forEach(
      (button) =>
        (button.onclick = () =>
          act(async () => {
            const name = $("[name=actor]", dialog).value || actor();
            localStorage.setItem("uralmicro_actor", name);
            await api(`/tasks/${id}`, {
              method: "PATCH",
              body: { status: button.dataset.status, actor: name },
            });
            toast("Статус задачи обновлён");
            await openTask(id);
            await refresh();
          })),
    );
    $$("[data-review]", dialog).forEach(
      (button) =>
        (button.onclick = () =>
          act(async () => {
            await api(
              `/completion-suggestions/${button.dataset.review}/review`,
              {
                method: "POST",
                body: {
                  accept: button.dataset.accept === "true",
                  actor: $("[name=actor]", dialog).value || actor(),
                },
              },
            );
            toast(
              button.dataset.accept === "true"
                ? "Выполнение подтверждено"
                : "Предложение отклонено",
            );
            await openTask(id);
            await refresh();
          })),
    );
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSummary() {
  const data = await api("/summary");
  $("#content").innerHTML =
    heading(
      "Контакт-центр",
      "Сводка руководителя",
      "Текущее состояние обязательств по всем операторам.",
      '<button class="button" data-refresh>↻ Обновить</button>',
    ) +
    kpis(data.totals) +
    `<section class="panel"><div class="panel-header"><div class="panel-title"><h2>Результаты операторов</h2><small>Своевременность определяется по моменту закрытия задачи</small></div></div>${data.operators.length ? `<div class="table-scroll"><table class="operator-summary"><thead><tr><th>Оператор</th><th>Всего</th><th>Созданы</th><th>В работе</th><th>Просрочены</th><th>Выполнены в срок</th><th>После срока</th></tr></thead><tbody>${data.operators.map((op) => `<tr><td>Оператор ${op.operator_id}</td><td>${op.total}</td><td>${op.created}</td><td>${op.in_progress}</td><td>${op.overdue ? `<span class="badge overdue">${op.overdue}</span>` : "0"}</td><td>${op.completed_on_time}<div class="progress-track"><span style="width:${op.completed ? Math.round((op.completed_on_time / op.completed) * 100) : 0}%"></span></div></td><td>${op.completed_late}</td></tr>`).join("")}</tbody></table></div>` : empty("Пока нет данных", "Сводка появится после создания первых задач.")}</section><div class="note">Это операционная сводка, а не оценка точности Qwen. Проверяйте цитаты и отдельно измеряйте качество распознавания.</div>`;
}

async function loadTypes() {
  state.types = await api("/promise-types");
  $("#content").innerHTML =
    heading(
      "Настройка распознавания",
      "Типы обещаний",
      "Опишите новое обещание — Qwen начнёт находить его в следующих звонках.",
      '<button class="button primary" id="add-type">＋ Добавить тип</button>',
    ) +
    `<div class="type-grid">${state.types.map((type) => `<article class="type-card"><div class="type-icon">◇</div><h3>${esc(type.name)}</h3><p>${esc(type.description)}</p><footer><code>${esc(type.code)}</code><button class="button small" data-type="${esc(type.code)}">Изменить ↗</button></footer></article>`).join("")}</div><div class="page-note"><span>●</span>Изменение типа не пересчитывает уже обработанные звонки.</div>`;
  $("#add-type").onclick = () => openType();
  $$("[data-type]").forEach(
    (button) =>
      (button.onclick = () =>
        openType(
          state.types.find((type) => type.code === button.dataset.type),
        )),
  );
}

function openType(type = null) {
  $("#type-detail").innerHTML =
    `<div class="dialog-heading"><h2>${type ? "Изменить тип" : "Новое обещание"}</h2><button class="button icon" data-close="type-dialog" aria-label="Закрыть">×</button></div><div class="dialog-body"><form id="type-form" class="form-grid"><label>Код<input name="code" placeholder="Например: refund" pattern="[a-z][a-z0-9_]+" minlength="2" maxlength="64" value="${esc(type?.code)}" required ${type ? "disabled" : ""}><small>Латиница, цифры и подчёркивание. Код неизменяемый.</small></label><label>Название<input name="name" value="${esc(type?.name)}" required maxlength="200" placeholder="Возврат денег"></label><label>Смысловое описание<textarea name="description" required minlength="10" maxlength="2000" placeholder="Оператор обещает оформить возврат денег клиенту…">${esc(type?.description)}</textarea></label><div class="dialog-footer">${type ? '<button class="button danger" type="button" id="delete-type">Удалить тип</button>' : "<span></span>"}<button type="submit" class="button primary">Сохранить тип</button></div></form></div>`;
  $("#type-dialog").showModal();
  $("#type-form").onsubmit = (event) => {
    event.preventDefault();
    act(async () => {
      const form = new FormData(event.target);
      const body = {
        name: form.get("name"),
        description: form.get("description"),
      };
      if (!type) body.code = form.get("code");
      await api(type ? `/promise-types/${type.code}` : "/promise-types", {
        method: type ? "PUT" : "POST",
        body,
      });
      $("#type-dialog").close();
      toast("Тип обещания сохранён");
      await loadTypes();
    });
  };
  if (type)
    $("#delete-type").onclick = () =>
      act(async () => {
        await api(`/promise-types/${type.code}`, { method: "DELETE" });
        $("#type-dialog").close();
        toast("Тип удалён");
        await loadTypes();
      });
}

async function loadNotifications() {
  const [telegram, journal] = await Promise.all([
    api("/telegram/status"),
    api("/notifications"),
  ]);
  const status = !telegram.configured
    ? "Бот не настроен"
    : telegram.connected
      ? "Telegram подключён"
      : "Ожидаем соединение с Telegram";
  $("#content").innerHTML =
    heading(
      "На связи с оператором",
      "Уведомления",
      "Новые задачи, напоминания о сроках и предложения о выполнении.",
      '<button class="button" data-refresh>↻ Обновить</button>',
    ) +
    `<div class="section-grid"><section class="panel"><div class="panel-header"><h2>Подключить Telegram</h2></div><div class="panel-body"><div class="status-banner ${telegram.connected ? "" : "offline"}"><span class="status-dot"></span><div><strong>${status}</strong><small>${telegram.username ? "@" + esc(telegram.username) : "Один сервер · без публичного вебхука"}</small></div></div>${!telegram.configured ? '<div class="note warning">Укажите TELEGRAM_BOT_TOKEN в локальном файле .env и перезапустите сервер. Токен не нужно вводить в браузере.</div>' : '<form id="telegram-form" class="form-grid inline-form"><label>ID оператора<input name="operator_id" type="number" min="1" value="7" required></label><button class="button primary" type="submit">Получить код →</button></form><div id="telegram-link"></div><p class="hint">Одноразовый код действует 10 минут. Уведомления придут в личный чат подключённого оператора.</p>'}</div></section>
    <section class="panel"><div class="panel-header"><h2>Подключённые операторы</h2><span class="badge">${telegram.bindings.length}</span></div><div class="panel-body">${telegram.bindings.length ? telegram.bindings.map((binding) => `<div class="binding"><span>Оператор ${binding.operator_id}<small class="hint"> · ${esc(date(binding.created_at))}</small></span><span class="badge completed">Подключён</span></div>`).join("") : "<p>Подключений пока нет. Создайте код и отправьте его боту.</p>"}<div class="spacer"></div><div class="note">В боте: /tasks — открытые задачи, /stop — отключение уведомлений. Кнопки позволяют менять статус и рассматривать подтверждения.</div></div></section></div>
    <section class="panel"><div class="panel-header"><div class="panel-title"><h2>Журнал доставки</h2><small>Повторные попытки сохраняются при перезапуске</small></div></div>${journal.items.length ? `<div class="table-scroll"><table><thead><tr><th>Событие</th><th>Задача</th><th>Оператор</th><th>Создано</th><th>Доставка</th><th>Попытки</th></tr></thead><tbody>${journal.items.map((item) => `<tr><td>${{ new: "Новая задача", reminder: "Срок приближается", overdue: "Просрочка", completion: "Подтверждение" }[item.kind]}</td><td class="task-main">${esc(item.task_title)}</td><td>${item.operator_id}</td><td>${esc(date(item.created_at))}</td><td><span class="badge ${item.status}">${{ pending: "В очереди", sent: "Отправлено", skipped: "Пропущено" }[item.status]}</span>${item.last_error ? `<small class="hint"> ${esc(item.last_error)}</small>` : ""}</td><td>${item.attempts}</td></tr>`).join("")}</tbody></table></div>` : empty("Журнал пока пуст", "Уведомления появятся после создания задач.")}</section>`;
  if (telegram.configured)
    $("#telegram-form").onsubmit = (event) => {
      event.preventDefault();
      act(async () => {
        const link = await api("/telegram/link", {
          method: "POST",
          body: {
            operator_id: Number(new FormData(event.target).get("operator_id")),
          },
        });
        $("#telegram-link").innerHTML =
          `<div class="note" style="margin-top:16px">${link.url ? "Откройте бота по ссылке и нажмите «Запустить»." : "Откройте вашего бота и отправьте эту команду:"}<input class="link-code" readonly value="/start ${esc(link.code)}" aria-label="Команда подключения"><div class="heading-actions">${link.url ? `<a href="${esc(link.url)}" target="_blank" rel="noopener" class="button primary small">Открыть Telegram ↗</a>` : ""}<button class="button small" id="copy-code" type="button">Скопировать команду</button></div></div>`;
        $("#copy-code").onclick = async () => {
          const input = $(".link-code");
          input.focus();
          input.select();
          if (navigator.clipboard)
            await navigator.clipboard.writeText(input.value);
          else document.execCommand("copy");
          toast("Команда скопирована");
        };
      });
    };
}

function exampleCall() {
  const now = new Date();
  return [
    {
      id: globalThis.crypto?.randomUUID?.() || `demo-${Date.now()}`,
      operator_id: 7,
      client_phone: "+70000000000",
      started_at: new Date(now.getTime() - 300000).toISOString(),
      ended_at: now.toISOString(),
      transcript: [
        { speaker: "client", text: "Мне нужен договор." },
        {
          speaker: "operator",
          text: "Договор вышлю через час. Перезвоню через два часа.",
        },
      ],
    },
  ];
}

function renderCalls() {
  $("#content").innerHTML =
    heading(
      "Входящие данные",
      "Принять звонки",
      "Проверьте сквозной сценарий или отправьте звонки вручную.",
    ) +
    `<div class="section-grid"><section class="panel"><div class="panel-header"><h2>Массив звонков</h2><button class="button small" id="example-call">Новый пример ↻</button></div><div class="panel-body"><form id="call-form" class="form-grid"><label>JSON · от 1 до 10 звонков<textarea id="call-json" class="code-area" spellcheck="false" required>${esc(JSON.stringify(exampleCall(), null, 2))}</textarea></label><button class="button primary" type="submit" id="send-calls">Обработать через Qwen →</button></form><div id="call-result" class="call-result" aria-live="polite"></div></div></section><div><section class="panel"><div class="panel-header"><h2>Как это работает</h2></div><div class="panel-body"><div class="eyebrow">01 · Приём</div><p>Sender передаёт звонки в POST /calls. Для ручной проверки используйте форму слева.</p><div class="spacer"></div><div class="eyebrow">02 · Поиск обещаний</div><p>Qwen определяет действия и сроки. Для каждого обещания создаётся отдельная задача с исходной цитатой.</p><div class="spacer"></div><div class="eyebrow">03 · Проверка выполнения</div><p>В следующих звонках того же клиента модель ищет подтверждения. Закрытие задачи остаётся за человеком.</p></div></section><div class="note warning">Для обработки нужен запущенный Qwen. Запрос может занять одну-две минуты. Просмотр задач и работа со статусами доступны без модели.</div></div></div>`;
  $("#example-call").onclick = () => {
    $("#call-json").value = JSON.stringify(exampleCall(), null, 2);
  };
  $("#call-form").onsubmit = (event) => {
    event.preventDefault();
    act(async () => {
      let calls;
      try {
        calls = JSON.parse($("#call-json").value);
      } catch {
        throw new Error("Проверьте синтаксис JSON");
      }
      const button = $("#send-calls");
      button.disabled = true;
      button.textContent = "Qwen обрабатывает звонки…";
      try {
        const result = await api("/calls", { method: "POST", body: calls });
        const target = $("#call-result");
        if (target)
          target.innerHTML = `<div class="note"><strong>Звонки обработаны</strong><br>Новых звонков: ${result.processed} · задач: ${result.tasks_created} · повторов: ${result.duplicates}<br>Предложений о выполнении: ${result.completions_suggested}${result.warnings.map((warning) => `<div class="note warning">${esc(warning)}</div>`).join("")}<div style="margin-top:12px"><button class="button small" data-goto="tasks">К задачам →</button></div></div>`;
        toast("Результат сохранён");
      } finally {
        button.disabled = false;
        button.textContent = "Обработать через Qwen →";
      }
    });
  };
}

async function act(action) {
  if (state.busy) return;
  const button = document.activeElement?.closest("button");
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  state.busy = true;
  try {
    await action();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

async function refresh() {
  try {
    if (state.view === "tasks") await loadTasks();
    else await navigate(state.view);
  } catch (error) {
    toast(error.message, true);
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(
    "[data-view],[data-goto],[data-task],[data-close],[data-refresh]",
  );
  if (!button) return;
  if (button.dataset.view) navigate(button.dataset.view);
  if (button.dataset.goto) navigate(button.dataset.goto);
  if (button.dataset.task) openTask(button.dataset.task);
  if (button.dataset.close)
    document.getElementById(button.dataset.close).close();
  if (button.hasAttribute("data-refresh")) refresh();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.dataset.task)
    openTask(event.target.dataset.task);
});

setInterval(() => {
  if (
    state.view === "tasks" &&
    !state.busy &&
    !$("dialog[open]") &&
    !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)
  )
    refresh();
}, 20000);

api("/promise-types")
  .then((types) => {
    state.types = types;
    navigate("tasks");
  })
  .catch((error) => {
    $("#content").innerHTML =
      `<div class="note error">${esc(error.message)}</div>`;
  });
