"""Агент чата — отдельная сущность, инкапсулирующая всю логику запросов к LLM.

Агент:
  * принимает запросы пользователя (вопрос + историю диалога);
  * позволяет выбрать, какие модели опросить, и задать температуру каждой;
  * сам строит итоговый набор сообщений (включая системный промпт);
  * обращается к выбранным большим языковым моделям через API;
  * собирает метрики (время, токены, стоимость);
  * возвращает готовый результат (текст, HTML-разметка, метаданные).

Логика обработки запроса спрятана внутри агента — внешний код
(HTTP-обработчик) лишь передаёт пользовательский ввод и получает ответ.
"""
import json
import time
from datetime import datetime

from . import config, deepseek, gigachat, html_report

__all__ = ["Agent"]


def json_dumps_safe(obj):
    """Аккуратно сериализует объект в JSON (для промптов)."""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def parse_facts_json(text):
    """Извлекает словарь фактов (key-value) из ответа модели.

    Принимает строку, возможно с обрамлением в markdown-блок ```json … ```.
    Возвращает dict строк или None, если разобрать не удалось.
    """
    if not text:
        return None
    s = str(text).strip()
    # Снимаем markdown-обёртку ```json ... ```
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                s = rest
        s = s.strip()
    # Находим границы JSON-объекта.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    frag = s[start:end + 1]
    try:
        data = json.loads(frag)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}

# Источники "голосов": (провайдер, имя модели, метка для отображения, css-класс)
SOURCES = (
    ("deepseek", config.DS_MODELS[0], "DeepSeek-flash", "ds-flash"),
    ("gigachat", config.GC_MODEL, "GigaChat", "gc-base"),
)

# Системный промпт агента (роль). Ставится в начало каждого диалога.
SYSTEM_PROMPT = ("Ты — дружелюбный ассистент. Отвечай по-русски, понятно и "
                 "структурированно, следуя запросам пользователя.")


class Agent:
    """Единая точка общения пользователя с большими языковыми моделями."""

    def __init__(self, api_key, sources=None):
        """Создаёт агента.

        api_key — ключ DeepSeek; sources — последовательность кортежей
        (provider, model, label, css_class), если нужен свой набор моделей.
        """
        self.api_key = api_key
        self.sources = list(sources or SOURCES)
        self.enabled_models = [s[2] for s in self.sources]
        # Индекс доступных моделей по их метке (label) для быстрого выбора
        # по имени в запросе пользователя.
        self.by_label = {s[2]: s for s in self.sources}

    # ---- открытый интерфейс агента ----

    @property
    def label(self):
        """Краткое описание состава агента (для интерфейса)."""
        return " | ".join(self.enabled_models)

    def available(self):
        """Список моделей, которые умеет обслуживать агент (метаданные для UI)."""
        out = []
        for provider, model, label, cls in self.sources:
            out.append({
                "label": label,
                "cls": cls,
                "provider": provider,
                "model": model,
            })
        return out

    def _coerce_temperature(self, value):
        """Приводит значение температуры к float в допустимом диапазоне."""
        try:
            t = float(value)
        except (TypeError, ValueError):
            return None
        if t < config.TEMP_MIN:
            return float(config.TEMP_MIN)
        if t > config.TEMP_MAX:
            return float(config.TEMP_MAX)
        return t

    def _coerce_max_tokens(self, value):
        """Приводит значение max_tokens к int в допустимом диапазоне.

        None и нечисловые значения означают «не применять» (возвращаем None —
        модель сама выбирает лимит вывода). Выход за границы клампится.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        if v < config.MAX_TOKENS_MIN:
            return int(config.MAX_TOKENS_MIN)
        if v > config.MAX_TOKENS_MAX:
            return int(config.MAX_TOKENS_MAX)
        return v

    def _selected_sources(self, selected):
        """Разрешает пользовательский выбор в кортежи источников.

        selected — список элементов вида {label, temperature}. Возвращает
        словарь {label: (source_tuple, temperature_or_None)} только для тех
        моделей, что реально доступны агенту. Если выборка пуста или не
        содержит подходящих меток — используются все источники (без явной
        температуры, т.е. системное значение модели).
        """
        chosen = {}
        if isinstance(selected, list) and selected:
            for opt in selected:
                if isinstance(opt, dict):
                    label = str(opt.get("label", ""))
                else:
                    label = str(opt)
                base = self.by_label.get(label)
                if base is None:
                    continue
                temp = self._coerce_temperature(
                    opt.get("temperature") if isinstance(opt, dict) else None)
                chosen[label] = (base, temp)
        if not chosen:
            for base in self.sources:
                chosen[base[2]] = (base, None)
        return chosen

    def answer(self, question, history=None, selected=None, max_tokens=None,
               compact=None, memory=None, profile=None, answer_title=None):
        """Обрабатывает запрос пользователя и возвращает результат.

        Принимает:
            question — строка с вопросом пользователя;
            history  — список сообщений {role, content} (допустим пустой);
            selected — список dict {label, temperature}: модели, которые нужно
                       опросить, и температура каждой (необязательно; если
                       пусто — опрашиваются все доступные модели);
            max_tokens — int или None: глобальное ограничение числа новых
                        токенов ответа для каждой модели. None — не применять
                        (модель сама выбирает лимит вывода);
            compact — dict {enabled, keep, summary} или None — настройки
                      сжатия истории.
            memory  — dict {"working": {...}, "longterm": {...}} или None:
                      данные памяти агента; фрагменты ответа, совпадающие с
                      ними, подсвечиваются цветом (рабочая — фисташковым,
                      долговременная — фуксией).
            profile — dict {"name", "character", "style"} или None: характер
                      (тон) и характер ответов (формат/длина) активного
                      профиля пользователя. Подставляются в системный промпт,
                      так что меняют поведение КАЖДОЙ модели.
            answer_title — строка-заголовок блока ответа. Если задана (ответ
                      даёт персона) — в шапке карточки показывается имя
                      персоны, а не метка модели.
        Возвращает dict, единообразный для успеха и ошибок:
            ok      — True, если хотя бы одна модель ответила;
            text    — текстовое представление ответов;
            html    — HTML-разметка для интерфейса;
            answers — список {label, text, temperature} с ответами моделей;
            meta    — строка служебных данных.
        Внутренние исключения перехватываются и не выходят за пределы агента.
        """
        trace = []                      # «ход запросов» агента для правой панели
        question = str(question or "").strip()
        trace.append({"kind": "enter",
                      "title": "Агент принял запрос пользователя"})
        print("[TRACE] Agent.answer() ВХОД. question=%r" % question, flush=True)
        if not question:
            step = {"kind": "exit", "ok": False,
                    "title": "Агент: пустой вопрос",
                    "detail": "вернул {ok:False, error:'Пустой вопрос.'}"}
            trace.append(step)
            print("[TRACE] Agent.answer() пустой вопрос -> ранний выход", flush=True)
            return Ok(self).value(ok=False, error="Пустой вопрос.", trace=trace)

        # Сжатие истории.
        # Основной путь: сервер уже прислал сжатую историю
        # ([summary] + последние keep сообщений). Тогда повторно сжимать
        # НЕЛЬЗЯ — иначе summary будет отброшен. Здесь _apply_compact
        # используется лишь как fallback, если история пришла полной,
        # но сжатие включено (например, при вызове агента вне сервера).
        already_compacted = (
            isinstance(history, list) and history
            and isinstance(history[0], dict)
            and history[0].get("role") == "system"
        )
        if (not already_compacted and compact
                and compact.get("enabled") and compact.get("summary")):
            history = self._apply_compact(history, compact)
            trace.append({"kind": "act", "title": "Агент: применено сжатие истории",
                          "detail": "сохранено %d последних сообщений, остальное — summary"
                                    % compact.get("keep", config.COMPACT_KEEP)})
        elif already_compacted:
            trace.append({"kind": "act",
                          "title": "Агент: контекст уже сжат (summary + последние)",
                          "detail": "сжатие применено на сервере, повторно не выполняется"})

        messages = self._build_messages(history, question, profile=profile)
        hlen = len(history) if isinstance(history, list) else 0
        prof_note = ""
        if isinstance(profile, dict) and (profile.get("character")
                                          or profile.get("style")):
            prof_note = " | профиль: %s" % (profile.get("name") or "без имени")
        trace.append({"kind": "act", "title": "Агент собрал сообщения для API",
                      "detail": "%d сообщений (%d из истории + текущий) | системный "
                                "промпт добавлен%s" % (len(messages), hlen,
                                                       prof_note)})
        print("[TRACE] Agent.answer() собрал %d сообщений для API" % len(messages),
              flush=True)

        sources = self._selected_sources(selected)
        trace.append({"kind": "branch", "title": "Агент -> запрос к LLM",
                      "detail": "агент опрашивает %d модель(ей) из выбранных"
                                % len(sources)})
        print("[TRACE] Agent.answer() опрашивает %d источника:"
              % len(sources), flush=True)

        blocks, text_parts, collected = [], [], []
        ok_any = False
        input_total = 0
        output_total = 0
        # Сколько фрагментов ответа заимствовано из памяти (по всем моделям).
        memory_used = {"working": 0, "longterm": 0}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        mt = self._coerce_max_tokens(max_tokens)
        mt_note = ("max_tokens=%d" % mt) if mt is not None else "max_tokens=auto"

        for label, (base, temperature) in sources.items():
            provider, model, _lbl, cls = base
            temp_note = ("temperature=%.2f" % temperature
                         if temperature is not None else "temperature=auto")
            node = {"kind": "llm", "model": label,
                    "title": "LLM: %s" % label,
                    "detail": "отправка… " + temp_note + " · " + mt_note}
            trace.append(node)
            print("[TRACE]   -> вызов модели label=%r %s %s"
                  % (label, temp_note, mt_note), flush=True)
            single = self._call_one(provider, model, messages, label, temperature,
                                    max_tokens=mt)
            node["ok"] = single["ok"]
            node["detail"] = ("запрос выполнен за %.2f c · ввод %d/вывод %d ток"
                              % (single["elapsed"], single["prompt_tokens"],
                                 single["completion_tokens"]))
            node["dur"] = round(single["elapsed"], 2)
            node["dur_ms"] = int(single["elapsed"] * 1000)
            print("[TRACE]   <- результат %r: ok=%s" % (label, single["ok"]),
                  flush=True)
            input_total += single["prompt_tokens"]
            output_total += single["completion_tokens"]
            if single["ok"]:
                ok_any = True
            block_html, mem_counts = self._render_block(
                provider, model, label, cls, single, memory,
                title=answer_title)
            blocks.append(block_html)
            memory_used["working"] += mem_counts["working"]
            memory_used["longterm"] += mem_counts["longterm"]
            text_parts.append(self._render_text(provider, model, label, single,
                                                title=answer_title))
            collected.append({
                "label": answer_title or label,
                "model": label,
                "text": single["content"],
                "temperature": temperature,
                "input": single["prompt_tokens"],
                "output": single["completion_tokens"],
                "cost": self._estimate_cost(provider, model,
                                            single["prompt_tokens"],
                                            single["completion_tokens"]),
            })

        total_tokens = input_total + output_total
        # Оценка доли входных токенов, пришедшейся на контекст-историю
        # (система и текущий вопрос не считаются историей).
        history_tokens = self._history_token_estimate(messages, input_total)
        meta = self._build_meta(ts, total_tokens, ok_any)
        html = "".join(blocks)
        text = "\n\n".join(text_parts)
        trace.append({"kind": "exit", "ok": ok_any,
                      "title": "Агент возвращает ответ",
                      "detail": "собрано %d ответов · суммарно %d ток · ok=%s"
                                % (len(collected), total_tokens, ok_any)})
        print("[TRACE] Agent.answer() ГОТОВО. ok=%s, токенов=%d, история~%d"
              % (ok_any, total_tokens, history_tokens), flush=True)
        usage = {
            "input": input_total,
            "output": output_total,
            "total": total_tokens,
            "history": history_tokens,
        }
        return Ok(self).value(ok=ok_any, html=html, text=text,
                              answers=collected, meta=meta, trace=trace,
                              usage=usage, memory_used=memory_used)

    # ---- приватная логика запроса (инкапсулирована в агенте) ----

    @staticmethod
    def _apply_compact(history, compact):
        """Применяет сжатие к истории диалога.

        history — полный список сообщений.
        compact — dict {enabled, keep, summary}.
        Возвращает новый список: summary + последние keep сообщений.
        """
        if not history or not compact.get("summary"):
            return history
        keep = max(0, compact.get("keep", config.COMPACT_KEEP))
        # keep = 0 → сжатие не применяется: возвращаем полную историю.
        if keep <= 0:
            return history
        if keep >= len(history):
            return history
        recent = list(history[-keep:]) if keep > 0 else []
        summary_msg = {"role": "system", "content": compact["summary"]}
        return [summary_msg] + recent

    @staticmethod
    def profile_system_prompt(profile):
        """Собирает системную инструкцию из профиля (характер + стиль).

        profile — dict {"name", "character", "style"} или None. Возвращает
        строку-инструкцию либо "" (пустую строку), если задавать нечего.

        ХАРАКТЕР (character) задаёт ТОН общения (например, «дружелюбный»),
        а ХАРАКТЕР ОТВЕТОВ (style) — ФОРМАТ и ДЛИНУ (например, «кратко,
        по пунктам»). Инструкция подставляется в системный промпт, поэтому
        применяется к каждой модели одинаково.
        """
        if not isinstance(profile, dict):
            return ""
        character = str(profile.get("character") or "").strip()
        style = str(profile.get("style") or "").strip()
        name = str(profile.get("name") or "").strip()
        parts = []
        if character:
            parts.append("Характер общения (тон): %s." % character)
        if style:
            parts.append("Характер ответов (формат и длина): %s." % style)
        if not parts:
            return ""
        head = "Персона, от лица которой ты отвечаешь"
        if name:
            head += " («%s»)" % name
        return head + ": " + " ".join(parts)

    def _build_messages(self, history, question, profile=None):
        """Собирает полный список сообщений для API (системный промпт + диалог).

        ВАЖНО: из истории сохраняются НЕ только user/assistant, но и
        SYSTEM-сообщения (память агента и summary сжатия). Раньше они
        отбрасывались, из-за чего модель НЕ получала память (рабочую и
        долговременную) и summary — будто памяти не существует.

        Все системные сообщения (промпт агента + память + summary + профиль)
        ОБЪЕДИНЯЮТСЯ в ОДНО ведущее system-сообщение: не все провайдеры
        корректно принимают несколько system-сообщений, а GigaChat ожидает
        системную инструкцию в начале. Диалог (user/assistant) идёт далее
        в исходном порядке, затем — текущий вопрос пользователя.
        """
        system_parts = [SYSTEM_PROMPT]
        # Характер/стиль профиля (персоны) — задают тон и формат ответов.
        prof_prompt = self.profile_system_prompt(profile)
        if prof_prompt:
            system_parts.append(prof_prompt)
        dialog = []
        if isinstance(history, list):
            for m in history:
                if (isinstance(m, dict)
                        and isinstance(m.get("content"), str)
                        and m["content"]):
                    role = m.get("role")
                    if role == "system":
                        # Память агента и summary — в общую системную часть.
                        system_parts.append(m["content"])
                    elif role in ("user", "assistant"):
                        dialog.append({"role": role, "content": m["content"]})
        messages = [{"role": "system", "content": "\n\n".join(system_parts)}]
        messages.extend(dialog)
        messages.append({"role": "user", "content": question})
        return messages

    @staticmethod
    def _history_token_estimate(messages, input_tokens):
        """Оценка числа входных токенов, приходящихся на историю диалога.

        Точной разбивки по сообщениям API не даёт, поэтому распределяем
        фактическое число входных токенов пропорционально длине текста:
        история — это все сообщения, кроме системного промпта (первое) и
        текущего вопроса пользователя (последнее).
        """
        try:
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            hist_chars = sum(len(str(m.get("content", "")))
                             for m in messages[1:-1])
        except Exception:
            return 0
        if total_chars <= 0 or input_tokens <= 0:
            return 0
        return int(round(input_tokens * hist_chars / total_chars))

    @staticmethod
    def _estimate_cost(provider, model, prompt_tokens, completion_tokens):
        """Оценка стоимости запроса в юанях.

        Возвращает число (¥) для моделей DeepSeek по известным ценам или
        None, если цена для модели неизвестна (например, GigaChat).
        """
        if provider != "deepseek":
            return None
        in_price = config.DS_PRICE_INPUT_PER_M.get(model)
        out_price = config.DS_PRICE_OUTPUT_PER_M.get(model)
        if in_price is None or out_price is None:
            return None
        return (prompt_tokens * in_price
                + completion_tokens * out_price) / 1_000_000.0

    def _call_one(self, provider, model, messages, label, temperature=None,
                  max_tokens=None):
        """Вызывает одну конкретную модель через API и фиксирует результат.

        Любые ошибки превращаются в "мягкий" результат с признаком ok=False,
        чтобы сбой одной модели не ломал весь агент.
        """
        t0 = time.perf_counter()
        try:
            if provider == "deepseek":
                res = deepseek.chat(self.api_key, messages,
                                    model=model, temperature=temperature,
                                    max_tokens=max_tokens)
            else:
                res = gigachat.chat(messages, model=model,
                                    temperature=temperature,
                                    max_tokens=max_tokens)
            return {
                "ok": True,
                "content": res.get("content", ""),
                "prompt_tokens": int(res.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(res.get("completion_tokens", 0) or 0),
                "elapsed": time.perf_counter() - t0,
                "error": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "content": "[Ошибка модели %s: %s]" % (label, exc),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "elapsed": time.perf_counter() - t0,
                "error": str(exc),
            }

    def _render_block(self, provider, model, label, cls, single, memory=None,
                      title=None):
        """HTML-блок (карточка ответа одной модели) и счётчики памяти.

        В заголовке карточки показываем title (имя персоны, если ответ даёт
        персона) либо метку модели. Время, токены и стоимость в шапке ответа
        не выводим (метрики остаются в текстовом представлении и статистике).

        Фрагменты ответа, совпадающие с данными памяти агента (memory),
        подсвечиваются: рабочая — фисташковым, долговременная — фуксией.
        Возвращает кортеж (html, counts), где counts = {"working": N,
        "longterm": M} — сколько фрагментов заимствовано из каждой памяти.
        """
        content = single["content"]
        counts = {"working": 0, "longterm": 0}
        try:
            frag, counts = html_report.render_with_memory_counts(
                content, memory=memory)
        except Exception:
            frag = html_report.escape_html(content)

        block = (
            "<div class='variant variant-%s'>"
            "<div class='variant-head'>"
            "<span class='variant-name'>%s</span>"
            "</div>"
            "<div class='variant-body'>%s</div></div>"
            % (cls, title or label, frag)
        )
        return block, counts

    def _render_text(self, provider, model, label, single, title=None):
        """Текстовая строка-представление ответа одной модели.

        Показываем title (имя персоны) либо метку модели и её ответ — без
        времени, токенов и стоимости (метрики остаются в статистике сессии
        и служебных логах).
        """
        return "%s:\n%s" % (title or label, single["content"])

    def _build_meta(self, ts, total_tokens, ok_any):
        """Служебная строка с информацией о генерации."""
        sources = ", ".join(self.enabled_models)
        if not ok_any:
            return ("Сгенерировано: %s · есть ошибки моделей · моделей: %s"
                    % (ts, sources))
        return ("Сгенерировано: %s · суммарно токенов: %d · моделей: %s"
                % (ts, total_tokens, sources))

    # ---- вспомогательные операции с историей (summary / facts) ----

    def compact_history(self, messages, keep=None):
        """Генерирует summary для ВЫТЕСНЯЕМОЙ части истории диалога.

        messages — полный список сообщений диалога (без системного промпта).
        keep — сколько последних сообщений НЕ сжимать (остаются как есть).

        Возвращает строку summary (по части истории до последних keep
        сообщений) или None, если сжимать нечего / произошла ошибка.
        """
        keep = config.COMPACT_KEEP if keep is None else max(0, int(keep))
        # Сжимаем только то, что вытесняется из контекста: всё, кроме
        # последних keep сообщений. Если истории мало — сжимать нечего.
        tail = messages[-keep:] if keep > 0 else []
        head = messages[:-keep] if keep > 0 else list(messages)
        if len(head) < config.COMPACT_MIN:
            return None
        return self.compact_update("", head)

    def compact_update(self, prev_summary, new_messages):
        """Инкрементально ДОПИСЫВАЕТ вытесненные сообщения в summary (Вариант A).

        prev_summary — уже существующее summary (может быть пустым).
        new_messages — НОВЫЕ вытесненные сообщения (ещё не сжатые).

        Возвращает обновлённое summary (строку) или None при ошибке/пустоте.
        Новое summary = слияние прежнего текста и нового фрагмента.
        """
        new_messages = [m for m in (new_messages or []) if isinstance(m, dict)]
        if not new_messages:
            return None

        cap = config.COMPACT_MSG_CAP
        lines = [
            "Обнови краткое summary диалога, добавив в него новый фрагмент.",
            "Сохрани ключевые факты, темы, договорённости и решения,",
            "чтобы по summary можно было продолжить беседу.",
            "Не повторяйся, объедини прежнее и новое в единый связный текст.",
            "",
        ]
        if prev_summary:
            lines += ["Прежнее summary:", str(prev_summary), ""]
        lines.append("Новый фрагмент диалога (вытеснен из контекста):")
        for m in new_messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))
            if not content:
                continue
            if len(content) > cap:
                content = content[:cap] + " […]"
            if role == "user":
                lines.append("Пользователь: " + content)
            elif role == "assistant":
                lines.append("Ассистент: " + content)

        prompt = "\n\n".join(lines)
        messages_for_compact = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            t0 = time.perf_counter()
            res = gigachat.chat(
                messages_for_compact,
                model=config.GC_MODEL,
                temperature=0.3,
            )
            elapsed = time.perf_counter() - t0
            summary = res.get("content", "") if isinstance(res, dict) else str(res)
            if summary:
                print("[COMPACT] summary дополнен %.2f c, длина %d, +%d сообщ."
                      % (elapsed, len(summary), len(new_messages)), flush=True)
            return summary
        except Exception as exc:
            print("[COMPACT] ошибка: %s" % exc, flush=True)
            return None

    def update_facts(self, prev_facts, new_messages):
        """Обновляет блок facts (key-value) после нового фрагмента диалога.

        prev_facts — текущий словарь фактов (может быть пустым).
        new_messages — новые сообщения (последний ход пользователя/ассистента).

        Возвращает обновлённый словарь фактов или prev_facts при ошибке.
        Факты извлекаются моделью GigaChat в формате JSON key-value:
        цель, ограничения, предпочтения, решения, договорённости и т.п.
        """
        prev_facts = {str(k): str(v) for k, v in (prev_facts or {}).items()}
        new_messages = [m for m in (new_messages or []) if isinstance(m, dict)]
        if not new_messages:
            return prev_facts

        cap = config.COMPACT_MSG_CAP
        lines = [
            "Ты ведёшь блок фактов (key-value) о диалоге.",
            "Обнови факты, добавив/изменив важное из нового фрагмента диалога.",
            "Храни: цель, ограничения, предпочтения, решения, договорённости.",
            "Верни ТОЛЬКО валидный JSON-объект (словарь строка-строка),",
            "без пояснений и без markdown. Если факт устарел — убери его.",
            "",
        ]
        if prev_facts:
            lines += ["Текущие факты (JSON):",
                      json_dumps_safe(prev_facts), ""]
        else:
            lines += ["Текущих фактов нет.", ""]
        lines.append("Новый фрагмент диалога:")
        for m in new_messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))
            if not content:
                continue
            if len(content) > cap:
                content = content[:cap] + " […]"
            if role == "user":
                lines.append("Пользователь: " + content)
            elif role == "assistant":
                lines.append("Ассистент: " + content)

        prompt = "\n\n".join(lines)
        messages_for_facts = [
            {"role": "system",
             "content": "Ты извлекаешь факты из диалога и отвечаешь строго JSON."},
            {"role": "user", "content": prompt},
        ]

        try:
            t0 = time.perf_counter()
            res = gigachat.chat(
                messages_for_facts,
                model=config.GC_MODEL,
                temperature=0.2,
            )
            elapsed = time.perf_counter() - t0
            content = res.get("content", "") if isinstance(res, dict) else str(res)
            facts = parse_facts_json(content)
            if facts is not None:
                print("[FACTS] факты обновлены %.2f c: %d ключей"
                      % (elapsed, len(facts)), flush=True)
                return facts
            print("[FACTS] не удалось разобрать JSON, оставляем прежние факты",
                  flush=True)
            return prev_facts
        except Exception as exc:
            print("[FACTS] ошибка: %s" % exc, flush=True)
            return prev_facts


class Ok:
    """Маленький помощник конструирования единообразного результата агента."""

    def __init__(self, agent):
        self.agent = agent

    def value(self, **kwargs):
        d = {"ok": False, "html": "", "text": "", "answers": [],
             "meta": "", "model": self.agent.label,
             "usage": {"input": 0, "output": 0, "total": 0, "history": 0}}
        d.update(kwargs)
        d.setdefault("error", None)
        return d

    def error(self, message):
        return self.value(ok=False, error=message)
