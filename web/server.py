

"""Встроенный HTTP-сервер (стандартная библиотека).

Чат "только в окне": сервер ничего не хранит на диске. История ведётся в
памяти вкладки и присылается целиком в POST /api/ask.

Сервер работает через АГЕНТА (rtk_app.agent.Agent) — отдельную сущность,
которая инкапсулирует всю логику запросов к LLM. Агент принимает вопрос и
историю диалога, сам обращается к моделям через API и возвращает готовый
результат (html, text, ответы моделей). HTTP-обработчик лишь передаёт данные
между браузером и агентом.

Запрос пользователя уходит агенту, который опрашивает модели:
  - DeepSeek-flash,
  - GigaChat (базовая, простая).

JSON-API:
    GET  /                  - страница (index.html)
    GET  /css/*,/js/*       - стили и скрипты
    GET  /api/model         - список моделей, которые обслуживает агент
    GET  /api/session       - сохранённая история + стратегия + facts + ветки
    POST /api/ask           - {question, models[], max_tokens?, compact?,
                               strategy?} -> ответ
    POST /api/newchat       - начать новый разговор (очистить историю)
    POST /api/compact       - {enabled, keep} -> настройки сжатия (summary)
    POST /api/compact_summary - {keep} -> дописать вытесненное в summary
    POST /api/strategy      - {strategy, window} -> стратегия контекста
                              (none / sliding / facts / branch)
    POST /api/facts         - {facts:{ключ:значение}} -> сохранить блок facts
    POST /api/memory        - {type:"working"|"longterm", …} -> память агента
                              (действия: set/delete/replace/clear, GET-снимок)
    POST /api/branches      - {action:"create"|"switch"|"delete"|"rename", …}
                              -> ветки (rename: {index, name})
    GET  /api/profiles      - список профилей (персон) + активный
    POST /api/profiles      - {action:"create"|"switch"|"update"|"delete", …}
                              -> профили (персоны): при создании задаются
                              name, model (модель персоны), character
                              (характер/тон) и style (характер ответов).
                              Ответы даёт АКТИВНАЯ персона своей моделью;
                              при отсутствии персон диалог идёт с моделями.
    """
import json
import mimetypes
import os
import webbrowser
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rtk_app import config
from rtk_app.agent import Agent
from rtk_app.session_store import SessionStore


class _ServerState:
    """Глобальное состояние сервера: агент (единая сущность) и сессия."""
    agent = None
    session = None


def _set_agent(agent):
    """Запоминает агента, который обслуживает все запросы."""
    _ServerState.agent = agent


def _ensure_session():
    """Лениво создаёт единственное хранилище сессии диалога."""
    if _ServerState.session is None:
        _ServerState.session = SessionStore()
    return _ServerState.session


class WebRequestHandler(BaseHTTPRequestHandler):
    server_version = "MultiModelChat/1.0"

    @property
    def agent(self):
        """Единый агент (rtk_app.agent.Agent), созданный при старте сервера."""
        return _ServerState.agent

    @property
    def session(self):
        """Хранилище сессии диалога (rtk_app.session_store.SessionStore)."""
        return _ensure_session()

    def _send_bytes(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # ---------------- GET ----------------
    def do_GET(self):
        import urllib.parse
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            return self._serve_static_safe("index.html")
        if path.startswith(("/css/", "/js/")):
            return self._serve_static_safe(path.lstrip("/"))
        if path == "/api/model":
            return self._send_json(200, {
                "ok": True,
                "model": self.agent.label if self.agent else config.MODEL,
                "models": [m["label"] for m in
                           (self.agent.available() if self.agent else [])],
                "available": (self.agent.available() if self.agent else []),
            })
        if path == "/api/session":
            has = self.session.has_history()
            return self._send_json(200, {
                "ok": True,
                "has_history": has,
                "messages": self.session.snapshot(),
                "compact": self.session.get_compact(),
                "context": self.session.context_stats(),
                "strategy": self.session.get_strategy(),
                "facts": self.session.get_facts(),
                "branches": self.session.branches_state(),
                "memory": self.session.memory_state(),
                "profiles": self.session.profiles_state(),
            })
        if path == "/api/profiles":
            state = self.session.profiles_state()
            return self._send_json(200, {
                "ok": True,
                "profiles": state["profiles"],
                "active": state["active"],
            })
        self._send_json(404, {"ok": False, "error": "Not Found"})

    def do_POST(self):
        import urllib.parse
        if urllib.parse.urlparse(self.path).path == "/api/ask":
            return self._handle_ask()
        if urllib.parse.urlparse(self.path).path == "/api/newchat":
            self.session.reset()
            return self._send_json(200, {"ok": True,
                                         "messages": self.session.snapshot()})
        if urllib.parse.urlparse(self.path).path == "/api/compact":
            return self._handle_compact()
        if urllib.parse.urlparse(self.path).path == "/api/compact_summary":
            return self._handle_compact_summary()
        if urllib.parse.urlparse(self.path).path == "/api/strategy":
            return self._handle_strategy()
        if urllib.parse.urlparse(self.path).path == "/api/facts":
            return self._handle_facts()
        if urllib.parse.urlparse(self.path).path == "/api/memory":
            return self._handle_memory()
        if urllib.parse.urlparse(self.path).path == "/api/branches":
            return self._handle_branches()
        if urllib.parse.urlparse(self.path).path == "/api/profiles":
            return self._handle_profiles()
        self._send_json(404, {"ok": False, "error": "Not Found"})

    # ---------------- Статика ----------------
    def _serve_static_safe(self, rel):
        root_real = os.path.realpath(config.WEB_ROOT)
        full = os.path.realpath(os.path.join(root_real, os.path.normpath(rel)))
        if not (full.startswith(root_real + os.sep) or full == root_real):
            return self._send_bytes(403, "Forbidden", "text/plain")
        if not os.path.isfile(full):
            return self._send_bytes(404, "Not Found", "text/plain")
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- Обработчики: делегируют всю работу агенту ----------------

    def _maybe_auto_compact(self):
        """Инкрементальное сжатие истории: дописывает вытесненные сообщения.

        Логика: как только история становится больше keep, каждое новое
        вытесненное сообщение ДОПИСЫВАЕТСЯ в summary (Вариант A). Для keep = 5
        сжатие начинается с 6-го сообщения и продолжается по мере вытеснения.
        Summary хранится ОТДЕЛЬНО (compact.summary) и подставляется в
        следующий запрос вместо вытесненной части истории.
        """
        try:
            if not self.session.should_auto_compact():
                return
            head, end = self.session.head_to_compact()
            if not head:
                return
            keep = self.session.get_compact().get("keep", config.COMPACT_KEEP)
            prev_summary = self.session.get_compact().get("summary", "")
            # Дописываем вытесненную часть в существующее summary.
            summary = self.agent.compact_update(prev_summary, head)
            if summary:
                # Сохраняем summary и новую границу: сообщения [0:end) покрыты.
                self.session.apply_summary(summary, upto=end, keep=keep)
                print("[COMPACT] сжатие: +%d сообщ. -> summary (upto=%d)"
                      % (len(head), end), flush=True)
        except Exception as exc:
            # Сжатие не должно ломать основной запрос.
            print("[COMPACT] сжатие не удалось: %s" % exc, flush=True)

    def _handle_ask(self):
        """Принимает запрос пользователя и передаёт его агенту.

        История диалога хранится на сервере (папка session/) и подхватывается
        при каждом запуске. Сервер передаёт агенту историю, выбор моделей и
        температуру, а после успешного ответа добавляет ход в сессию и
        сохраняет её на диск.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        question = str(data.get("question", "")).strip()
        # Выбор моделей пользователем: [{"label": "…", "temperature": 0.7}]
        selected = data.get("models")
        if not isinstance(selected, list) or not selected:
            selected = None

        # РЕЖИМ ОТВЕТА зависит от наличия персон:
        #   * ЕСТЬ активная персона — отвечает ПЕРСОНА, используя СВОЮ модель
        #     (выбор моделей сверху недоступен, но если клиент всё же прислал
        #     список — модель персоны имеет приоритет);
        #   * НЕТ персон — диалог идёт напрямую с ВЫБРАННЫМИ моделями.
        pid, pname, pmodel, pchar, pstyle = self.session.active_profile_attrs()
        if pid:
            # Отвечает персона. Заголовок блока — имя персоны.
            answer_title = pname or "Персона"
            profile = {"name": pname, "character": pchar, "style": pstyle}
            # Модель персоны имеет приоритет; если у персоны модель не задана —
            # используем выбранные сверху модели, а если и их нет — все
            # доступные модели агента (передаём None).
            if pmodel:
                selected = [{"label": pmodel}]
            elif selected is None:
                selected = None  # агент опросит все доступные модели
        else:
            # Персон нет — режим «напрямую с моделями».
            answer_title = None
            profile = None
            if selected is None:
                return self._send_json(200, {
                    "ok": False,
                    "error": "Не выбрана ни одна модель. Включите модель для "
                             "запроса.",
                    "text": "", "html": "", "answers": [],
                })

        # Глобальное ограничение max_tokens (None — не применяется)
        max_tokens = data.get("max_tokens")
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

        # Настройки сжатия
        compact = data.get("compact")
        if isinstance(compact, dict):
            # Клиент может прислать свои настройки (enabled/keep) — применяем.
            self.session.set_compact(compact.get("enabled"),
                                     compact.get("keep"),
                                     None)
        # Стратегия управления контекстом (если клиент прислал).
        strategy = data.get("strategy")
        if isinstance(strategy, dict):
            self.session.set_strategy(strategy.get("strategy"),
                                      strategy.get("window"))
        compact = self.session.get_compact()

        # Управление контекстом: применяем АКТИВНУЮ стратегию
        # (sliding / facts / branch) и, поверх неё, сжатие summary.
        history = self.session.get_context_messages()

        # ПРОВЕРКА НАЛИЧИЯ данных в памяти при формировании запроса:
        # если в рабочей/долговременной памяти есть данные — они уже
        # подмешаны в контекст (memory_message) как системное сообщение.
        memory = self.session.memory_state()
        mem_items = (len(memory.get("working") or {})
                     + len(memory.get("longterm") or {}))
        if mem_items:
            print("[MEMORY] в запрос добавлена память: рабочая=%d, "
                  "долговременная=%d (всего %d элементов)"
                  % (len(memory.get("working") or {}),
                     len(memory.get("longterm") or {}), mem_items),
                  flush=True)
        else:
            print("[MEMORY] память пуста — в запрос не добавляется", flush=True)

        # Профиль (персона): характер и характер ответов активного профиля
        # подставляются в системный промпт каждой модели (тон + формат/длина).
        # pid/pname/... и profile уже получены выше (см. выбор режима).

        result = self.agent.answer(question, history, selected,
                                   max_tokens=max_tokens,
                                   compact=compact,
                                   memory=memory,
                                   profile=profile,
                                   answer_title=answer_title)
        if result.get("ok"):
            # По одному ходу на ответ модели с уже готовой разметкой
            self.session.append_turn(question, {
                "role": "assistant",
                "content": result.get("text", ""),
                "html": result.get("html", ""),
                "answers": result.get("answers", []),
                "meta": result.get("meta", ""),
                "usage": result.get("usage"),
            })
            # Стратегия Facts: обновляем блок facts после каждого сообщения
            # пользователя (через GigaChat).
            self._maybe_update_facts(question, result)
            # Счётчики использования памяти: сколько фрагментов ответа
            # заимствовано из рабочей/долговременной памяти (для интерфейса).
            mused = result.get("memory_used") or {}
            self.session.add_memory_usage(mused.get("working", 0),
                                          mused.get("longterm", 0))
            # Сжатие: если появились вытесненные сообщения — дописываем их
            # в summary (инкрементально; работает только при keep > 0).
            self._maybe_auto_compact()
            result["compact"] = self.session.get_compact()
            result["strategy"] = self.session.get_strategy()
            result["facts"] = self.session.get_facts()
            result["branches"] = self.session.branches_state()
            result["memory"] = self.session.memory_state()
            # Статистика управления контекстом (сжатых/использованных из
            # summary сообщений) для панели интерфейса.
            result["context"] = self.session.context_stats()
        return self._send_json(200, result)

    def _maybe_update_facts(self, question, result):
        """Обновляет блок facts после хода пользователя (стратегия Facts)."""
        try:
            if self.session.get_strategy().get("strategy") != "facts":
                return
            prev = self.session.get_facts()
            last_turn = [
                {"role": "user", "content": str(question)},
                {"role": "assistant", "content": result.get("text", "")},
            ]
            facts = self.agent.update_facts(prev, last_turn)
            if isinstance(facts, dict):
                self.session.set_facts(facts)
        except Exception as exc:
            print("[FACTS] обновление не удалось: %s" % exc, flush=True)

    def _handle_compact(self):
        """Обновляет настройки сжатия сессии (enabled/keep/summary)."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        enabled = data.get("enabled")
        keep = data.get("keep")
        summary = data.get("summary")
        # Пустой summary из клиента НЕ должен затирать уже сгенерированный
        # на сервере — иначе настройки каждый раз обнуляют сжатие.
        if not summary:
            summary = None
        self.session.set_compact(enabled, keep, summary)
        return self._send_json(200, {
            "ok": True,
            "compact": self.session.get_compact(),
        })

    def _handle_compact_summary(self):
        """Дописывает вытесненную часть истории в summary (по запросу).

        Сжимаем только то, что вышло за пределы последних keep сообщений,
        и ДОПИСЫВАЕМ это в существующее summary (инкрементально, Вариант A).
        Summary сохраняется отдельно и будет подставлено в следующий запрос
        вместо вытесненной части истории.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        keep = data.get("keep", config.COMPACT_KEEP)
        try:
            keep = max(0, int(keep))
        except (TypeError, ValueError):
            keep = config.COMPACT_KEEP
        # Применяем актуальный keep перед вычислением вытесняемой части.
        self.session.set_compact(True, keep, None)
        head, end = self.session.head_to_compact()
        if not head:
            return self._send_json(200, {
                "ok": True, "summary": self.session.get_compact().get("summary", ""),
                "detail": "Нет вытесненных сообщений — сжимать нечего.",
            })
        prev_summary = self.session.get_compact().get("summary", "")
        summary = self.agent.compact_update(prev_summary, head)
        if summary:
            self.session.apply_summary(summary, upto=end, keep=keep)
            return self._send_json(200, {
                "ok": True, "summary": summary,
                "detail": "Summary дополнен (%d сообщ.)." % len(head),
            })
        return self._send_json(200, {
            "ok": False, "summary": self.session.get_compact().get("summary", ""),
            "detail": "Не удалось обновить summary.",
        })

    def _handle_strategy(self):
        """Управление стратегией контекста: none / sliding / facts / branch."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        state = self.session.set_strategy(data.get("strategy"),
                                          data.get("window"))
        return self._send_json(200, {
            "ok": True,
            "strategy": state,
            "context": self.session.context_stats(),
            "facts": self.session.get_facts(),
            "branches": self.session.branches_state(),
        })

    def _handle_facts(self):
        """Просмотр/редактирование фактов (key-value) стратегии Facts.

        Факты — это данные ПАМЯТИ агента: каждый факт раскладывается в
        ВЫБРАННЫЙ пользователем слой памяти: mem_map = {ключ: "working"|
        "longterm"}. Ключи без явного выбора считаются рабочей памятью.
        Отдельного хранилища facts нет — читать/писать их можно через память.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        facts = data.get("facts")
        if isinstance(facts, dict):
            facts = {str(k): str(v) for k, v in facts.items()}
            mem_map = data.get("mem_map")
            if not isinstance(mem_map, dict):
                mem_map = {}
            # Раскладываем факты по слоям памяти согласно выбору у значения.
            working, longterm = {}, {}
            for k, v in facts.items():
                layer = str(mem_map.get(k, "working")).strip().lower()
                if layer == "longterm":
                    longterm[k] = v
                else:
                    working[k] = v
            try:
                self.session.set_memory_bulk("working", working)
                self.session.set_memory_bulk("longterm", longterm)
            except ValueError as exc:
                return self._send_json(400, {"ok": False, "error": str(exc)})
        return self._send_json(200, {
            "ok": True,
            "facts": self.session.get_facts(),
            "memory": self.session.memory_state(),
        })

    def _handle_memory(self):
        """Память агента: чтение/запись трёх типов (short/working/longterm).

        Тело запроса (все поля необязательны, но action определяет смысл):
            action: "state"  — вернуть снимок всех типов (по умолчанию);
                    "set"    — записать одну пару: {type, key, value};
                    "delete" — удалить ключ: {type, key};
                    "replace"— заменить тип целиком: {type, data:{…}};
                    "clear"  — очистить тип: {type}.
            type:   "working" | "longterm" (для "short" запись запрещена —
                    краткосрочная память ведётся самим диалогом).

        Именно здесь реализован ЯВНЫЙ выбор «что и куда сохраняется» (задание
        B2): тип памяти задаётся вызывающим кодом, слои не пересекаются.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "state")).strip().lower()
        mem_type = data.get("type")

        try:
            if action == "set":
                entry = self.session.set_memory_key(
                    mem_type, data.get("key"), data.get("value"))
                return self._send_json(200, {
                    "ok": True, "saved": entry,
                    "memory": self.session.memory_state(),
                })
            if action == "delete":
                removed = self.session.delete_memory_key(mem_type,
                                                         data.get("key"))
                return self._send_json(200, {
                    "ok": True, "removed": removed,
                    "memory": self.session.memory_state(),
                })
            if action == "replace":
                saved = self.session.set_memory_bulk(mem_type, data.get("data"))
                return self._send_json(200, {
                    "ok": True, "saved": saved,
                    "memory": self.session.memory_state(),
                })
            if action == "clear":
                mt = str(mem_type or "").strip()
                if mt not in config.MEMORY_TYPES:
                    return self._send_json(400, {
                        "ok": False,
                        "error": "Неизвестный тип памяти: %r" % (mem_type,),
                    })
                if mt == "short":
                    return self._send_json(400, {
                        "ok": False,
                        "error": "Краткосрочная память (диалог) очищается "
                                 "кнопкой «Новый разговор».",
                    })
                self.session.set_memory_bulk(mt, {})
                return self._send_json(200, {
                    "ok": True, "memory": self.session.memory_state(),
                })
            # action == "state" и всё прочее — отдаём снимок.
            return self._send_json(200, {
                "ok": True, "memory": self.session.memory_state(),
            })
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {"ok": False,
                                         "error": "Ошибка памяти: %s" % exc})

    def _handle_branches(self):
        """Управление ветками: create / switch / delete / rename.

        Ожидаемые поля: action = "create" | "switch" | "delete" | "rename",
        name (для create/rename), count (для create),
        index (для switch/delete/rename).
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "")).strip().lower()
        if action == "create":
            state = self.session.create_branch(
                name=data.get("name"),
                count=data.get("count"),
                from_checkpoint=bool(data.get("from_checkpoint", True)))
        elif action == "switch":
            state = self.session.switch_branch(data.get("index"))
        elif action == "delete":
            state = self.session.delete_branch(data.get("index"))
        elif action == "rename":
            state = self.session.rename_branch(data.get("index"),
                                               data.get("name"))
        else:
            return self._send_json(400, {
                "ok": False,
                "error": "Неизвестное действие с ветками (action).",
            })
        return self._send_json(200, {
            "ok": True,
            "branches": state,
            "messages": self.session.snapshot(),
        })

    def _handle_profiles(self):
        """Управление ПРОФИЛЯМИ (персонами).

        Профиль — именованная персона со СВОЕЙ памятью (рабочая +
        долговременная), своим диалогом и настройками. Атрибуты character
        (характер — тон общения) и style (характер ответов — формат/длина)
        задаются ПРИ СОЗДАНИИ и подставляются в системный промпт каждой
        модели. Переключение профиля заменяет активную память и диалог.

        Ожидаемые поля: action = "create" | "switch" | "update" | "delete";
        id (для switch/update/delete); name, character, style, model (для
        create/update). model — метка модели, которой отвечает персона.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "list")).strip().lower()
        try:
            if action == "create":
                state = self.session.create_profile(
                    data.get("name"),
                    character=data.get("character"),
                    style=data.get("style"),
                    model=data.get("model"),
                    activate=bool(data.get("activate", True)))
            elif action == "switch":
                state = self.session.switch_profile(data.get("id"))
            elif action == "update":
                state = self.session.update_profile(
                    data.get("id"),
                    name=data.get("name"),
                    character=data.get("character"),
                    style=data.get("style"),
                    model=data.get("model"))
            elif action == "delete":
                state = self.session.delete_profile(data.get("id"))
            elif action in ("list", "state", ""):
                state = self.session.profiles_state()
            else:
                return self._send_json(400, {
                    "ok": False,
                    "error": "Неизвестное действие с профилями (action).",
                })
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        # После переключения/удаления активным может стать другой профиль —
        # возвращаем актуальные память и диалог, чтобы интерфейс обновился.
        return self._send_json(200, {
            "ok": True,
            "profiles": state.get("profiles", []),
            "active": state.get("active"),
            "messages": self.session.snapshot(),
            "memory": self.session.memory_state(),
            "branches": self.session.branches_state(),
            "compact": self.session.get_compact(),
            "strategy": self.session.get_strategy(),
            "context": self.session.context_stats(),
        })


def create_server(agent, host=None, port=None):
    """Создаёт HTTP-сервер, связанный с конкретным экземпляром агента."""
    if agent is None:
        raise ValueError("create_server: требуется экземпляр агента (Agent).")
    _set_agent(agent)
    # ВАЖНО: проверяем именно None, а не «ложность»: port=0 — валидное
    # значение (ОС сама выберет свободный порт), которое нельзя подменять
    # значением по умолчанию, иначе тесты/инстансы будут конфликтовать
    # на общем порту config.WEB_PORT.
    addr = (host or config.WEB_HOST,
            config.WEB_PORT if port is None else port)
    return ThreadingHTTPServer(addr, WebRequestHandler)


def _open_browser_later(url, delay=1.0):
    def _job():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:
            print("[WEB] браузер: %s" % exc)
    threading.Thread(target=_job, daemon=True).start()


def serve(agent_or_key, host=None, port=None, open_page=True, agent=None):
    """Запускает сервер, используя агента в качестве единой сущности.

    agent_or_key — либо строка API-ключа DeepSeek (тогда создаётся агент),
    либо уже готовый экземпляр rtk_app.agent.Agent.
    agent — ещё один способ передать готового агента явно.
    """
    if agent is None:
        agent = agent_or_key

    # Агент — отдельная сущность, построенная вокруг ключа либо переданная.
    if not isinstance(agent, Agent):
        agent = Agent(agent)

    httpd = create_server(agent, host, port)
    shown_host, shown_port = httpd.server_address[:2]
    url = "http://%s:%s/" % (shown_host, shown_port)
    print("[WEB] Сервер запущен: %s" % url)
    print("[WEB] Агент обслуживает модели: %s" % agent.label)
    if open_page:
        _open_browser_later(url)
    print("[WEB] Остановка: нажмите Ctrl+C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[WEB] Остановка...")
    finally:
        httpd.server_close()


def main():
    import sys
    from rtk_app.key_store import read_api_key
    try:
        api_key = read_api_key(config.DS_KEY_FILE)
        print("[OK] Ключ DeepSeek прочитан из %s" % config.DS_KEY_FILE)
    except FileNotFoundError:
        # Файла ключа нет: сервер всё равно стартует — интерфейс откроется,
        # а недоступные модели покажут подсказку, как добавить ключ
        # (самодиагностика уже напечатана через rtk_web.py/key_check).
        api_key = ""
        print("[!] Файл %s не найден. DeepSeek-flash в чате станет "
              "недоступен до тех пор, пока вы не впишете ключ в apidpsk.txt."
              % config.DS_KEY_FILE)
    args = sys.argv[1:]
    host, port = config.WEB_HOST, config.WEB_PORT
    if args and args[0].isdigit():
        port = int(args[0])
        if len(args) > 1:
            host = args[1]
    sys.exit(serve(api_key, host, port) or 0)