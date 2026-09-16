/* Логика чата:
   слева — ввод + история, справа — область ответа и «ход запросов».

   Особенности версии:
   - История диалога хранится на сервере в папке session/ (JSON) и
     подхватывается при открытии страницы (непрерывность беседы).
   - Вверху выбраны модели (кнопки, несколько) и температура для каждой.
   - Запрос уходит на сервер вместе с выбором моделей и температур.
*/
(function () {
    "use strict";

    var qEl = document.getElementById("question");
    var submit = document.getElementById("submit");
    var statusEl = document.getElementById("status");
    var streamEl = document.getElementById("stream");
    var traceEl = document.getElementById("trace");
    var modelBox = document.getElementById("model-buttons");

    // Автоматическое сжатие истории (без кнопки — включается само).
    var compactKeep = document.getElementById("compact-keep");
    // Стратегия управления контекстом (Sliding / Facts / Branch).
    var strategyGroup = document.getElementById("strategy-group");
    var strategyWindow = document.getElementById("strategy-window");
    var factsBox = document.getElementById("facts-box");
    var branchesBox = document.getElementById("branches-box");
    var tsTotal = document.getElementById("ts-total");
    var tsIn = document.getElementById("ts-in");
    var tsOut = document.getElementById("ts-out");
    var tsHist = document.getElementById("ts-hist");
    var tsHistPct = document.getElementById("ts-hist-pct");
    var tsModelsTitle = document.getElementById("ts-models-title");
    var tsModels = document.getElementById("ts-models");
    var tsCompacted = document.getElementById("ts-compacted");
    var tsFromSummary = document.getElementById("ts-from-summary");
    var segIn = document.getElementById("seg-in");
    var segOut = document.getElementById("seg-out");

    var busy = false;

    // Текущий диалог в окне: [{role:'user'|'assistant', content, html, answers}]
    var items = [];

    // Состояние выбранных моделей: [{label, cls, on:bool, temp:number|null}]
    var modelState = [];
    // Значение температуры по умолчанию (минимальные/шаг берём из сервера).
    var DEFAULT_TEMP = 0.7;
    var TEMP_MIN = 0.0, TEMP_MAX = 1.0, TEMP_STEP = 0.05;

    // Состояние НАСТРОЙКИ сжатия: сколько последних сообщений хранить
    // полностью. Само сжатие (генерация summary) происходит автоматически
    // на сервере после накопления порога несжатых сообщений.
    var compactState = {
        keep: 10,
    };

    // ------------------------------------------------------------------
    // Хелперы
    // ------------------------------------------------------------------
    function esc(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function setStatus(message, kind) {
        statusEl.hidden = !message;
        statusEl.textContent = message || "";
        statusEl.className = "side-status" + (kind === "error" ? " error" : (kind === "ok" ? " ok" : ""));
    }

    // Полная перерисовка потока ответов из items.
    function render() {
        streamEl.innerHTML = "";

        items.forEach(function (item) {
            if (item.role === "assistant") {
                var msg = document.createElement("div");
                msg.className = "msg a";
                var b = document.createElement("div");
                b.className = "bubble";
                b.innerHTML = item.html || esc(item.content);
                msg.appendChild(b);
                streamEl.appendChild(msg);
            } else {
                var qMsg = document.createElement("div");
                qMsg.className = "msg q";
                var qb = document.createElement("div");
                qb.className = "bubble";
                qb.textContent = item.content;
                qMsg.appendChild(qb);
                streamEl.appendChild(qMsg);
            }
        });

        streamEl.scrollTop = streamEl.scrollHeight;
    }

    var TRACE_KIND = {
        enter: "агент", act: "работа", branch: "запрос",
        llm: "LLM", exit: "агент"
    };

    // Компактная цепочка «Ход запросов · агент»: короткие метки шагов,
    // выстроенные слева направо с переносом по строкам (без прокрутки).
    function renderTrace(trace, meta) {
        if (!traceEl) return;
        if (!trace || !trace.length) {
            traceEl.innerHTML = '<div class="trace-empty">ход: агент → работа → LLM → ответ</div>';
            return;
        }
        var wrap = document.createElement("div");
        wrap.className = "ts-chain";
        trace.forEach(function (s) {
            var label = TRACE_KIND[s.kind] || s.kind || "?";
            if (s.kind === "llm" && s.model) label = s.model;
            var span = document.createElement("span");
            var cls = "ts-chip " + (s.kind || "act");
            if (s.kind === "llm" && s.ok === false) cls += " fail";
            span.className = cls;
            span.textContent = label;
            if (s.ok === false) span.title = "ошибка";
            wrap.appendChild(span);
        });
        traceEl.innerHTML = "";
        traceEl.appendChild(wrap);
    }

    // ------------------------------------------------------------------
    // Панель выбора моделей (кнопки-переключатели + температура)
    // ------------------------------------------------------------------
    function buildModelControls(available) {
        if (!modelBox) return;
        modelBox.innerHTML = "";
        modelState = available.map(function (m) {
            return { label: m.label, cls: m.cls || "", on: true, temp: DEFAULT_TEMP };
        });
        modelState.forEach(function (st) {
            var wrap = document.createElement("div");
            wrap.className = "model-ctrl active";
            wrap.dataset.label = st.label;

            // кнопка с названием модели
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "m-btn";
            btn.innerHTML = '<span class="m-dot"></span>' + esc(st.label);
            btn.addEventListener("click", function () {
                st.on = !st.on;
                syncModelUI(st.label);
                updateModelTag();
            });
            wrap.appendChild(btn);

            // параметр температура
            var tempWrap = document.createElement("div");
            tempWrap.className = "model-temp";
            var lab = document.createElement("label");
            lab.textContent = "temp";
            var inp = document.createElement("input");
            inp.type = "number";
            inp.min = TEMP_MIN;
            inp.max = TEMP_MAX;
            inp.step = TEMP_STEP;
            inp.value = st.temp;
            inp.title = "Температура генерации (0 – строго, выше – разнообразнее)";
            inp.addEventListener("change", function () {
                var v = parseFloat(inp.value);
                if (isNaN(v)) v = DEFAULT_TEMP;
                if (v < TEMP_MIN) v = TEMP_MIN;
                if (v > TEMP_MAX) v = TEMP_MAX;
                inp.value = v;
                st.temp = v;
            });
            tempWrap.appendChild(lab);
            tempWrap.appendChild(inp);
            wrap.appendChild(tempWrap);

            modelBox.appendChild(wrap);
        });
        updateModelTag();
    }

    // Изменяет внешний вид карточки модели по текущему состоянию вкл/выкл.
    function syncModelUI(label) {
        var st = modelState.find(function (m) { return m.label === label; });
        if (!modelBox || !st) return;
        var cards = modelBox.querySelectorAll('.model-ctrl');
        for (var i = 0; i < cards.length; i++) {
            if (cards[i].dataset.label === label) {
                cards[i].classList.toggle("active", st.on);
                cards[i].classList.toggle("inactive", !st.on);
            }
        }
    }

    function updateModelTag() {
        var on = modelState.filter(function (m) { return m.on; }).map(function (m) { return m.label; });
        renderModelsTitle(on);
    }

    // Заголовок блока статистики («Токены по моделям») приводим в
    // соответствие с реально используемыми моделями.
    function renderModelsTitle(labels) {
        if (!tsModelsTitle) return;
        var on = labels || modelState
            .filter(function (m) { return m.on; })
            .map(function (m) { return m.label; });
        if (!on.length) {
            tsModelsTitle.textContent = "Токены по моделям";
            return;
        }
        tsModelsTitle.innerHTML = 'Токены по моделям <span class="tstat-head-models">' +
            esc(on.join(" · ")) + "</span>";
    }

    // Собирает список выбранных моделей для отправки на сервер.
    function selectedModelsPayload() {
        return modelState
            .filter(function (m) { return m.on; })
            .map(function (m) { return { label: m.label, temperature: m.temp }; });
    }

    // ------------------------------------------------------------------
    // Управление контекстом (автоматическое сжатие истории)
    // ------------------------------------------------------------------
    // Настройка: сколько последних сообщений сохранять полностью.
    // Ранняя история автоматически заменяется на summary на сервере —
    // отдельной кнопки «Сжать» нет.

    function getCompactPayload() {
        // Сжатие всегда включено (автоматическое). От клиента передаём
        // только настройку keep — сколько последних сообщений оставлять.
        // keep = 0 → сжатие НЕ применяется (ни подстановка summary, ни автосжатие).
        var keep = parseInt(compactKeep ? compactKeep.value : compactState.keep, 10);
        if (isNaN(keep) || keep < 0) keep = compactState.keep;
        if (keep < 0) keep = 0;
        return { enabled: true, keep: keep };
    }

    function applyCompactFromServer(compact) {
        if (!compact) return;
        var keep = parseInt(compact.keep, 10);
        if (isNaN(keep) || keep < 0) keep = compactState.keep;
        compactState.keep = keep;
        if (compactKeep) compactKeep.value = keep;
    }

    if (compactKeep) {
        compactState.keep = parseInt(compactKeep.value, 10);
        if (isNaN(compactState.keep)) compactState.keep = 10;
        compactKeep.addEventListener("change", function () {
            var v = parseInt(compactKeep.value, 10);
            if (isNaN(v) || v < 0) v = 0;     // 0 = сжатие выключено
            if (v > 100) v = 100;
            compactKeep.value = v;
            compactState.keep = v;
            saveCompactSettings();
        });
    }

    function saveCompactSettings() {
        // Сохраняем настройку keep на сервер (сжатие включено всегда).
        fetch("/api/compact", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getCompactPayload()),
        }).catch(function () {});
    }

    // ------------------------------------------------------------------
    // Стратегия управления контекстом: Sliding / Facts / Branch
    // ------------------------------------------------------------------
    var strategyState = { strategy: "none", window: 10 };

    function getStrategyPayload() {
        var w = parseInt(strategyWindow ? strategyWindow.value : strategyState.window, 10);
        if (isNaN(w) || w < 0) w = strategyState.window;
        return { strategy: strategyState.strategy, window: w };
    }

    function saveStrategySettings() {
        fetch("/api/strategy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getStrategyPayload()),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) return;
            strategyState.strategy = d.strategy.strategy;
            strategyState.window = d.strategy.window;
            syncStrategyUI();
            renderFacts(d.facts);
            renderBranches(d.branches);
            applyContextStats(d.context);
        })
        .catch(function () {});
    }

    function syncStrategyUI() {
        if (!strategyGroup) return;
        var btns = strategyGroup.querySelectorAll(".strat-btn");
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle("active",
                btns[i].dataset.strategy === strategyState.strategy);
        }
        if (strategyWindow) strategyWindow.value = strategyState.window;
        // Панели facts/веток показываем только в соответствующих стратегиях.
        var factsTitle = document.getElementById("facts-title");
        var brTitle = document.getElementById("branches-title");
        if (factsTitle) factsTitle.style.display =
            strategyState.strategy === "facts" ? "" : "none";
        if (factsBox) factsBox.style.display =
            strategyState.strategy === "facts" ? "" : "none";
        if (brTitle) brTitle.style.display =
            strategyState.strategy === "branch" ? "" : "none";
        if (branchesBox) branchesBox.style.display =
            strategyState.strategy === "branch" ? "" : "none";
    }

    function applyStrategyFromServer(strategy) {
        if (!strategy) return;
        if (strategy.strategy) strategyState.strategy = strategy.strategy;
        var w = parseInt(strategy.window, 10);
        if (!isNaN(w)) strategyState.window = w;
        syncStrategyUI();
    }

    if (strategyGroup) {
        strategyGroup.addEventListener("click", function (e) {
            var btn = e.target.closest(".strat-btn");
            if (!btn) return;
            strategyState.strategy = btn.dataset.strategy;
            syncStrategyUI();
            saveStrategySettings();
        });
    }
    if (strategyWindow) {
        strategyWindow.addEventListener("change", function () {
            var v = parseInt(strategyWindow.value, 10);
            if (isNaN(v) || v < 0) v = 0;
            if (v > 200) v = 200;
            strategyWindow.value = v;
            strategyState.window = v;
            saveStrategySettings();
        });
    }

    // ---- Факты (key-value), стратегия Facts ----
    // Каждый факт хранится в одном из слоёв ПАМЯТИ агента, выбор — у значения:
    //   "working"  — Рабочая память (данные текущей задачи);
    //   "longterm" — Долговременная память (профиль/решения/знания).
    // Память по фактам приходит из снимка памяти сервера (mem.working /
    // mem.longterm); ключ, найденный в слое, показывает этот слой в select.
    var factsMemory = {};   // {ключ: "working"|"longterm"} — текущий выбор

    function renderFacts(facts) {
        if (!factsBox) return;
        factsBox.innerHTML = "";
        var keys = facts ? Object.keys(facts) : [];
        if (!keys.length) {
            var empty = document.createElement("div");
            empty.className = "facts-empty";
            empty.textContent = "фактов пока нет (обновляются после каждого запроса)";
            factsBox.appendChild(empty);
        } else {
            keys.forEach(function (k) {
                factsBox.appendChild(factRow(k, facts[k], factsMemory[k] || "working"));
            });
        }
        var actions = document.createElement("div");
        actions.className = "facts-actions";
        var add = document.createElement("button");
        add.type = "button";
        add.textContent = "+ факт";
        add.addEventListener("click", function () {
            factsBox.insertBefore(factRow("", "", "working"), actions);
        });
        var save = document.createElement("button");
        save.type = "button";
        save.className = "primary";
        save.textContent = "Сохранить";
        save.addEventListener("click", saveFacts);
        actions.appendChild(add);
        actions.appendChild(save);
        factsBox.appendChild(actions);
    }

    function factRow(key, val, memType) {
        var row = document.createElement("div");
        row.className = "fact-row";
        var k = document.createElement("input");
        k.type = "text"; k.className = "fact-key"; k.value = key || "";
        k.placeholder = "ключ";
        var v = document.createElement("input");
        v.type = "text"; v.className = "fact-val"; v.value = val || "";
        v.placeholder = "значение";
        // Выбор памяти, в которую попадёт значение: Рабочая / Долгосрочная.
        var mem = document.createElement("select");
        mem.className = "fact-mem";
        mem.title = "В какую память попадёт факт";
        [
            { value: "working", text: "Рабочая" },
            { value: "longterm", text: "Долговременная" }
        ].forEach(function (opt) {
            var o = document.createElement("option");
            o.value = opt.value;
            o.textContent = opt.text;
            mem.appendChild(o);
        });
        mem.value = (memType === "longterm") ? "longterm" : "working";
        var del = document.createElement("button");
        del.type = "button"; del.className = "fact-del"; del.textContent = "\u00d7";
        del.title = "Удалить факт";
        del.addEventListener("click", function () { row.remove(); });
        row.appendChild(k); row.appendChild(v); row.appendChild(mem); row.appendChild(del);
        return row;
    }

    function saveFacts() {
        if (!factsBox) return;
        var out = {};
        var memMap = {};
        var rows = factsBox.querySelectorAll(".fact-row");
        for (var i = 0; i < rows.length; i++) {
            var k = rows[i].querySelector(".fact-key").value.trim();
            var v = rows[i].querySelector(".fact-val").value.trim();
            var memSel = rows[i].querySelector(".fact-mem");
            if (k) {
                out[k] = v;
                memMap[k] = memSel && memSel.value === "longterm" ? "longterm" : "working";
            }
        }
        fetch("/api/facts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ facts: out, mem_map: memMap }),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) return;
            factsMemory = memMap;
            renderFacts(d.facts);
            if (d.memory) renderMemory(d.memory);
        })
        .catch(function () {});
    }

    // ---- Ветки диалога, стратегия Branch ----
    function renderBranches(state) {
        if (!branchesBox) return;
        branchesBox.innerHTML = "";
        if (!state || !state.branches) return;
        var active = state.active_branch || 0;
        state.branches.forEach(function (b, i) {
            var row = document.createElement("div");
            row.className = "branch-row" + (i === active ? " active" : "");
            row.title = "Клик — переключиться; двойной клик по имени — переименовать";

            var name = document.createElement("span");
            name.className = "b-name";
            name.textContent = b.name;
            name.title = "Двойной клик — переименовать ветку";
            // Двойной клик по имени — редактирование «на месте».
            name.addEventListener("dblclick", function (e) {
                e.stopPropagation();
                startRenameBranch(row, name, i, b.name);
            });
            row.appendChild(name);

            var size = document.createElement("span");
            size.className = "b-size";
            size.textContent = b.size + " сообщ.";
            row.appendChild(size);

            // Кнопка переименования (карандаш).
            var ren = document.createElement("button");
            ren.type = "button"; ren.className = "b-ren"; ren.textContent = "\u270e";
            ren.title = "Переименовать ветку";
            ren.addEventListener("click", function (e) {
                e.stopPropagation();
                startRenameBranch(row, name, i, b.name);
            });
            row.appendChild(ren);

            if (state.branches.length > 1) {
                var del = document.createElement("button");
                del.type = "button"; del.className = "b-del"; del.textContent = "\u00d7";
                del.title = "Удалить ветку";
                del.addEventListener("click", function (e) {
                    e.stopPropagation();
                    branchAction({ action: "delete", index: i });
                });
                row.appendChild(del);
            }
            row.addEventListener("click", function () {
                branchAction({ action: "switch", index: i });
            });
            branchesBox.appendChild(row);
        });

        var actions = document.createElement("div");
        actions.className = "branches-actions";
        var countInp = document.createElement("input");
        countInp.type = "number"; countInp.min = "1"; countInp.max = "20";
        countInp.value = "2"; countInp.title = "Сколько веток создать";
        var create = document.createElement("button");
        create.type = "button"; create.className = "primary";
        create.textContent = "Создать ветки от текущего";
        create.addEventListener("click", function () {
            branchAction({ action: "create", count: parseInt(countInp.value, 10) || 2 });
        });
        actions.appendChild(countInp);
        actions.appendChild(create);
        branchesBox.appendChild(actions);
    }

    // Запускает inline-редактирование имени ветки прямо в строке.
    function startRenameBranch(row, nameEl, index, currentName) {
        if (row.querySelector(".b-name-edit")) return;   // уже редактируется
        nameEl.style.display = "none";

        var inp = document.createElement("input");
        inp.type = "text";
        inp.className = "b-name-edit";
        inp.value = currentName || "";
        inp.maxLength = 80;
        row.insertBefore(inp, nameEl);
        inp.focus();
        inp.select();

        var finished = false;
        function commit() {
            if (finished) return;
            finished = true;
            var newName = inp.value.trim();
            inp.remove();
            nameEl.style.display = "";
            if (newName && newName !== currentName) {
                branchAction({ action: "rename", index: index, name: newName });
            } else {
                nameEl.textContent = currentName;   // без изменений
            }
        }
        function cancel() {
            if (finished) return;
            finished = true;
            inp.remove();
            nameEl.style.display = "";
            nameEl.textContent = currentName;
        }

        inp.addEventListener("click", function (e) { e.stopPropagation(); });
        inp.addEventListener("dblclick", function (e) { e.stopPropagation(); });
        inp.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        });
        inp.addEventListener("blur", commit);
    }

    function branchAction(payload) {
        fetch("/api/branches", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) return;
            renderBranches(d.branches);
            // Переименование историю не меняет. Create/switch/delete могут
            // сменить активную ветку — тогда обновляем окно из серверных данных.
            if (payload.action !== "rename" && Array.isArray(d.messages)) {
                items = d.messages.slice();
                render();
            }
        })
        .catch(function () {});
    }

    // ------------------------------------------------------------------
    // Статистика токенов в заголовке (текущая сессия)
    // ------------------------------------------------------------------
    // Суммарный расход токенов за сессию: tokIn — вход (запрос), tokOut — выход (ответ)
    var tokIn = 0;
    var tokOut = 0;
    // Оценка входных токенов, ушедших на контекст-историю диалога
    var tokHist = 0;

    // Форматирует число с разделителями тысяч.
    function fmt(n) {
        return (n || 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    }

    function resetTokenStats() {
        tokIn = 0;
        tokOut = 0;
        tokHist = 0;
        renderTokenStats();
    }

    // Учитывает трафик очередного обмена (input/output токенов за запрос).
    function addTokenUsage(u) {
        if (!u) return;
        var i = Math.max(0, parseInt(u.input, 10) || 0);
        var o = Math.max(0, parseInt(u.output, 10) || 0);
        var h = Math.max(0, parseInt(u.history, 10) || 0);
        tokIn += i;
        tokOut += o;
        tokHist += h;
        renderTokenStats();
    }

    // Обновляет цифры и полоску-диаграмму «запрос/ответ» + долю истории.
    function renderTokenStats() {
        var total = tokIn + tokOut;
        if (tsTotal) tsTotal.textContent = fmt(total);
        if (tsIn) tsIn.textContent = fmt(tokIn);
        if (tsOut) tsOut.textContent = fmt(tokOut);
        if (tsHist) tsHist.textContent = fmt(tokHist);
        if (tsHistPct) {
            var pct = total > 0 ? (tokHist / total * 100) : 0;
            tsHistPct.textContent = "(" + pct.toFixed(1) + "%)";
        }
        var empty = document.getElementById("ts-empty");
        if (empty) empty.style.display = total ? "none" : "";
        if (segIn && segOut) {
            if (total) {
                segIn.style.width = (tokIn / total * 100).toFixed(2) + "%";
                segOut.style.width = (tokOut / total * 100).toFixed(2) + "%";
            } else {
                segIn.style.width = "50%";
                segOut.style.width = "50%";
            }
        }
    }

    // Счётчики использования памяти в заголовке: сколько РАЗ данные были
    // заимствованы из РАБОЧЕЙ (фисташковая) и ДОЛГОВРЕМЕННОЙ (фуксия) памяти
    // за сессию (накопительно). «Раз» = за сколько ответов память пригодилась.
    function applyContextStats(ctx) {
        if (!ctx) return;
        if (tsCompacted) tsCompacted.textContent = fmt(parseInt(ctx.memory_use_count_working, 10) || 0);
        if (tsFromSummary) tsFromSummary.textContent = fmt(parseInt(ctx.memory_use_count_longterm, 10) || 0);
    }

    function resetContextStats() {
        if (tsCompacted) tsCompacted.textContent = "0";
        if (tsFromSummary) tsFromSummary.textContent = "0";
    }

    // ------------------------------------------------------------------
    // Накопительная статистика по каждой модели (в столбик в заголовке)
    // ------------------------------------------------------------------
    // modelStats: { label: {input, output, cost, hasCost} }
    var modelStats = {};
    // Порядок отображения моделей (метки из /api/model).
    var availableLabels = [];

    function ensureModel(label) {
        if (!label) return null;
        if (!modelStats[label]) {
            modelStats[label] = { input: 0, output: 0, cost: 0, hasCost: false };
        }
        return modelStats[label];
    }

    // Учитывает расход по моделям за один обмен (массив answers).
    function addAnswerUsage(answers) {
        if (!answers || !answers.length) return;
        answers.forEach(function (a) {
            if (!a) return;
            var st = ensureModel(a.label);
            if (!st) return;
            st.input += Math.max(0, parseInt(a.input, 10) || 0);
            st.output += Math.max(0, parseInt(a.output, 10) || 0);
            if (a.cost != null && !isNaN(parseFloat(a.cost))) {
                st.cost += parseFloat(a.cost);
                st.hasCost = true;
            }
        });
        renderModelStats();
    }

    function resetModelStats() {
        Object.keys(modelStats).forEach(function (k) {
            modelStats[k] = { input: 0, output: 0, cost: 0, hasCost: false };
        });
        renderModelStats();
    }

    // Собирает упорядоченный список меток: сначала доступные, затем прочие.
    function orderedLabels() {
        var seen = {}, out = [];
        availableLabels.forEach(function (l) { if (!seen[l]) { seen[l] = 1; out.push(l); } });
        Object.keys(modelStats).forEach(function (l) { if (!seen[l]) { seen[l] = 1; out.push(l); } });
        return out;
    }

    function renderModelStats() {
        if (!tsModels) return;
        var labels = orderedLabels();
        tsModels.innerHTML = "";
        var any = false;
        labels.forEach(function (label) {
            var st = modelStats[label] || { input: 0, output: 0, cost: 0, hasCost: false };
            var tot = st.input + st.output;
            if (tot > 0) any = true;
            var li = document.createElement("li");
            li.className = "tstat-model";
            var cost = "";
            if (st.hasCost) cost = " · ¥" + st.cost.toFixed(6);
            li.innerHTML = '<span class="tm-name">' + esc(label) + '</span>' +
                '<span class="tm-val">' + fmt(st.input) + ' / ' + fmt(st.output) + cost + '</span>';
            tsModels.appendChild(li);
        });
        // Если ни одна модель ещё не отвечала — подсказка
        if (!labels.length || !any) {
            tsModels.innerHTML = '<li class="tstat-model empty">нет данных — задайте вопрос</li>';
        }
    }

    // ------------------------------------------------------------------
    // Память агента: три типа (short / working / longterm)
    // ------------------------------------------------------------------
    // 1) краткосрочная — текущий диалог (автоматическая, только чтение);
    // 2) рабочая      — данные текущей задачи (редактируется вручную);
    // 3) долговременная — профиль/решения/знания (редактируется вручную).
    // Запись идёт ЧЕРЕЗ /api/memory с ЯВНЫМ указанием типа (задание B2).
    var memShortInfo = document.getElementById("mem-short-info");
    var memWorkingBox = document.getElementById("mem-working-box");
    var memLongtermBox = document.getElementById("mem-longterm-box");

    // «Грязные» слои памяти: пользователь изменил строки, но ещё не сохранил.
    // Фоновые обновления (после ответа/загрузки сессии) НЕ должны затирать
    // такие правки — иначе введённое в одном слое терялось бы при
    // сохранении/обновлении другого слоя. Ключ — тип слоя, значение — bool.
    var memDirty = { working: false, longterm: false };

    // Помечает слой «грязным» при любом ручном вводе.
    function markMemDirty(memType) {
        if (memType in memDirty) memDirty[memType] = true;
    }

    // Рисует строки key=value для редактируемого слоя памяти.
    // memType нужен, чтобы отмечать слой «грязным» при ручном редактировании.
    function memRow(key, valValDiv, memType) {
        var row = document.createElement("div");
        row.className = "mem-row";
        var k = document.createElement("input");
        k.type = "text"; k.className = "mem-key"; k.value = key || "";
        k.placeholder = "ключ";
        var v = document.createElement("input");
        v.type = "text"; v.className = "mem-val"; v.value = valValDiv || "";
        v.placeholder = "значение";
        // Любой ручной ввод в поле/удаление строки — слоя касались, значит
        // его нельзя затирать фоновым обновлением из снимка сервера.
        k.addEventListener("input", function () { markMemDirty(memType); });
        v.addEventListener("input", function () { markMemDirty(memType); });
        var del = document.createElement("button");
        del.type = "button"; del.className = "mem-del"; del.textContent = "\u00d7";
        del.title = "Удалить строку";
        del.addEventListener("click", function () {
            markMemDirty(memType);
            row.remove();
        });
        row.appendChild(k); row.appendChild(v); row.appendChild(del);
        return row;
    }

    // Отрисовка редактируемого слоя памяти из словаря.
    function renderMemRows(box, data, memType) {
        if (!box) return;
        box.innerHTML = "";
        var keys = data ? Object.keys(data) : [];
        if (!keys.length) {
            var empty = document.createElement("div");
            empty.className = "mem-empty";
            empty.textContent = "пусто — добавьте строку";
            box.appendChild(empty);
            return;
        }
        keys.forEach(function (k) { box.appendChild(memRow(k, data[k], memType)); });
    }

    // Собирает словарь из строк редактируемого слоя.
    function collectMemRows(box) {
        var out = {};
        if (!box) return out;
        var rows = box.querySelectorAll(".mem-row");
        for (var i = 0; i < rows.length; i++) {
            var k = rows[i].querySelector(".mem-key").value.trim();
            var v = rows[i].querySelector(".mem-val").value.trim();
            if (k) out[k] = v;
        }
        return out;
    }

    // Отрисовка слоёв памяти из снимка сервера.
    //
    // onlyType — если задан ("working"/"longterm"), перерисовываем ТОЛЬКО
    // этот слой (например, после его сохранения), не трогая другой: тогда
    // несохранённые правки в другом слое сохраняются.
    // force — если true, перерисовать и «грязные» слои (используется только
    // при явном сохранении/очистке и принудительной синхронизации).
    // По умолчанию «грязные» слои НЕ перерисовываются фоновыми обновлениями.
    function renderMemory(mem, onlyType, force) {
        if (!mem) return;
        // 1) краткосрочная — только информация о диалоге.
        if (memShortInfo) {
            var s = mem.short || {};
            memShortInfo.textContent = "сообщений в диалоге: " + (s.items || 0) +
                " · веток: " + (s.branches || 1);
        }
        // Связываем факты с их слоем памяти (для select в панели «Факты»).
        // Обновляем карту только по тем слоям, которые сейчас синхронизируем.
        if (!onlyType) factsMemory = {};
        if (!onlyType) {
            Object.keys(mem.working || {}).forEach(function (k) {
                factsMemory[k] = "working";
            });
            Object.keys(mem.longterm || {}).forEach(function (k) {
                factsMemory[k] = "longterm";
            });
        }
        // 2) рабочая и 3) долговременная — редактируемые.
        var wantWorking = (!onlyType || onlyType === "working");
        var wantLongterm = (!onlyType || onlyType === "longterm");
        if (wantWorking && (force || !memDirty.working)) {
            renderMemRows(memWorkingBox, mem.working || {}, "working");
            memDirty.working = false;
        }
        if (wantLongterm && (force || !memDirty.longterm)) {
            renderMemRows(memLongtermBox, mem.longterm || {}, "longterm");
            memDirty.longterm = false;
        }
    }

    // ЯВНО сохраняет слой памяти на сервер (action=replace, type=<слой>).
    function saveMemoryLayer(memType, box) {
        var payload = {
            action: "replace",
            type: memType,
            data: collectMemRows(box),
        };
        return fetch("/api/memory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (d && d.ok) {
                // Перерисовываем ТОЛЬКО сохранённый слой: несохранённые
                // правки в другом слое остаются нетронутыми.
                renderMemory(d.memory, memType, true);
                setStatus("Память «" + memType + "» сохранена.", "ok");
            } else {
                setStatus("Не удалось сохранить память.", "error");
            }
        })
        .catch(function () { setStatus("Ошибка связи при сохранении памяти.", "error"); });
    }

    // ЯВНО очищает слой памяти на сервере.
    function clearMemoryLayer(memType, box) {
        return fetch("/api/memory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "clear", type: memType }),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (d && d.ok) {
                // Очищаем и перерисовываем только этот слой.
                renderMemory(d.memory, memType, true);
                setStatus("Память «" + memType + "» очищена.", "ok");
            }
        })
        .catch(function () {});
    }

    function bindMemoryLayer(memType, box, addBtn, saveBtn, clearBtn) {
        if (addBtn) addBtn.addEventListener("click", function () {
            var empty = box.querySelector(".mem-empty");
            if (empty) empty.remove();
            markMemDirty(memType);
            box.appendChild(memRow("", "", memType));
        });
        if (saveBtn) saveBtn.addEventListener("click", function () {
            saveMemoryLayer(memType, box);
        });
        if (clearBtn) clearBtn.addEventListener("click", function () {
            clearMemoryLayer(memType, box);
        });
    }

    bindMemoryLayer("working",
        document.getElementById("mem-working-box"),
        document.getElementById("mem-working-add"),
        document.getElementById("mem-working-save"),
        document.getElementById("mem-working-clear"));
    bindMemoryLayer("longterm",
        document.getElementById("mem-longterm-box"),
        document.getElementById("mem-longterm-add"),
        document.getElementById("mem-longterm-save"),
        document.getElementById("mem-longterm-clear"));

    // ------------------------------------------------------------------
    // ПРОФИЛИ (персоны)
    // ------------------------------------------------------------------
    // Профиль — именованный набор состояния: своя память, свой диалог,
    // свои настройки и своя МАНЕРА ОБЩЕНИЯ. У профиля два атрибута:
    //   character — ХАРАКТЕР (тон общения);
    //   style     — ХАРАКТЕР ОТВЕТОВ (формат и длина).
    // Оба подставляются в системный промпт каждой модели. Переключение
    // профиля заменяет активную память и диалог (сервер возвращает их снимок).
    var profilesBox = document.getElementById("profiles-box");
    var profileNameInp = document.getElementById("profile-name");
    var profileCharInp = document.getElementById("profile-character");
    var profileStyleInp = document.getElementById("profile-style");
    var profileCreateBtn = document.getElementById("profile-create");
    // Текущий список профилей и id активного (снимок сервера).
    var profilesState = { profiles: [], active: null };

    // Рисует строки профилей: имя, характер/стиль и кнопки действий.
    function renderProfiles(state) {
        if (!profilesBox) return;
        if (state && Array.isArray(state.profiles)) profilesState = state;
        var list = profilesState.profiles || [];
        profilesBox.innerHTML = "";
        if (!list.length) {
            var empty = document.createElement("div");
            empty.className = "profiles-empty";
            empty.textContent = "профилей пока нет";
            profilesBox.appendChild(empty);
            return;
        }
        list.forEach(function (p) {
            var row = document.createElement("div");
            row.className = "profile-row" + (p.active ? " active" : "");
            row.title = "Клик — переключиться на этот профиль";

            var name = document.createElement("span");
            name.className = "p-name";
            name.textContent = p.name || "(без имени)";
            row.appendChild(name);

            var meta = document.createElement("span");
            meta.className = "p-meta";
            var bits = [];
            if (p.character) bits.push(p.character);
            if (p.style) bits.push(p.style);
            meta.textContent = bits.join(" · ") || "без характера";
            row.appendChild(meta);

            var size = document.createElement("span");
            size.className = "p-size";
            size.textContent = (p.size || 0) + " сообщ.";
            row.appendChild(size);

            // Кнопка редактирования (карандаш) — просим новые атрибуты.
            var ren = document.createElement("button");
            ren.type = "button"; ren.className = "p-ren"; ren.textContent = "\u270e";
            ren.title = "Изменить имя/характер/стиль";
            ren.addEventListener("click", function (e) {
                e.stopPropagation();
                var newName = window.prompt("Имя профиля:", p.name || "");
                if (newName === null) return;
                var newChar = window.prompt("Характер (тон):", p.character || "");
                if (newChar === null) return;
                var newStyle = window.prompt("Характер ответов (формат/длина):",
                                             p.style || "");
                if (newStyle === null) return;
                profileAction({ action: "update", id: p.id, name: newName,
                                character: newChar, style: newStyle });
            });
            row.appendChild(ren);

            // Удаление доступно, только если профилей больше одного.
            if (list.length > 1) {
                var del = document.createElement("button");
                del.type = "button"; del.className = "p-del"; del.textContent = "\u00d7";
                del.title = "Удалить профиль";
                del.addEventListener("click", function (e) {
                    e.stopPropagation();
                    if (!window.confirm("Удалить профиль «" + (p.name || "") +
                                        "» вместе с его памятью и диалогом?")) return;
                    profileAction({ action: "delete", id: p.id });
                });
                row.appendChild(del);
            }

            row.addEventListener("click", function () {
                if (p.active) return;
                profileAction({ action: "switch", id: p.id });
            });
            profilesBox.appendChild(row);
        });
    }

    // Применяет снимок, пришедший от сервера после действий с профилями:
    // заменяет диалог, память и настройки на состояние активного профиля.
    function applyProfileSnapshot(d) {
        if (!d) return;
        if (d.profiles) renderProfiles(d.profiles);
        if (Array.isArray(d.messages)) {
            items = d.messages.slice();
            render();
        }
        if (d.memory) renderMemory(d.memory, null, true);
        if (d.branches) renderBranches(d.branches);
        if (d.compact) applyCompactFromServer(d.compact);
        if (d.strategy) applyStrategyFromServer(d.strategy);
        if (d.context) applyContextStats(d.context);
    }

    function profileAction(payload) {
        fetch("/api/profiles", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) {
                setStatus((d && d.error) || "Не удалось выполнить действие.",
                          "error");
                return;
            }
            applyProfileSnapshot(d);
            var act = payload.action;
            if (act === "create") setStatus("Профиль создан.", "ok");
            else if (act === "switch") setStatus("Профиль переключён.", "ok");
            else if (act === "update") setStatus("Профиль обновлён.", "ok");
            else if (act === "delete") setStatus("Профиль удалён.", "ok");
        })
        .catch(function () { setStatus("Ошибка связи с профилями.", "error"); });
    }

    if (profileCreateBtn) {
        profileCreateBtn.addEventListener("click", function () {
            var name = profileNameInp ? profileNameInp.value.trim() : "";
            if (!name) { setStatus("Введите имя профиля.", "error"); return; }
            profileAction({
                action: "create",
                name: name,
                character: profileCharInp ? profileCharInp.value.trim() : "",
                style: profileStyleInp ? profileStyleInp.value.trim() : "",
            });
            if (profileNameInp) profileNameInp.value = "";
            if (profileCharInp) profileCharInp.value = "";
            if (profileStyleInp) profileStyleInp.value = "";
        });
    }

    // ------------------------------------------------------------------
    // Отправка запроса
    // ------------------------------------------------------------------
    function ask() {
        var question = qEl.value.trim();
        if (!question || busy) { if (!question) setStatus("Введите запрос.", "error"); return; }

        busy = true;
        submit.disabled = true;
        setStatus("Обрабатываю…", "");
        qEl.value = "";

        // Сервер сам держит историю диалога; передаём вопрос, выбор моделей
        // и настройки сжатия/стратегии.
        var body = {
            question: question,
            models: selectedModelsPayload(),
            compact: getCompactPayload(),
            strategy: getStrategyPayload(),
        };

        fetch("/api/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error: "Сервер вернул некорректный ответ." };
            });
        })
        .then(function (data) {
            setStatus("", "");
            if (!data.ok) {
                setStatus(data.error || "Произошла ошибка.", "error");
                return;
            }
            items.push({ role: "user", content: question });
            items.push({ role: "assistant", content: data.text || "",
                         html: data.html || "", answers: data.answers || [] });
            render();
            addTokenUsage(data.usage);
            addAnswerUsage(data.answers);
            applyContextStats(data.context);
            if (data.memory) renderMemory(data.memory);
            if (data.facts) renderFacts(data.facts);
            if (data.branches) renderBranches(data.branches);
            renderTrace(data.trace, data.meta);
        })
        .catch(function (err) { setStatus("Ошибка связи: " + err.message, "error"); })
        .finally(function () {
            busy = false;
            submit.disabled = false;
        });
    }

    // Сброс (очистка) диалога — на сервере и локально.
    function startNewChat() {
        fetch("/api/newchat", { method: "POST" })
            .then(function () { clearLocalChat("Начат новый разговор."); })
            .catch(function () {
                // даже если сеть не ответила, очистим окно
                clearLocalChat("Новый разговор (сессия очищена локально).");
            });
    }

    function clearLocalChat(statusText) {
        items = [];
        resetTokenStats();
        resetModelStats();
        resetContextStats();
        render();
        renderTrace(null, null);
        setStatus(statusText, "ok");
        qEl.focus();
    }

    // Загрузка сохранённой истории с сервера (непрерывность беседы).
    function loadSession() {
        return fetch("/api/session")
            .then(function (r) { return r.ok ? r.json() : { ok: false }; })
            .then(function (d) {
                if (d && d.ok !== false && Array.isArray(d.messages)) {
                    items = d.messages.slice();
                    // Применяем настройки сжатия и стратегии с сервера
                    applyCompactFromServer(d.compact);
                    applyStrategyFromServer(d.strategy);
                    if (d.profiles) renderProfiles(d.profiles);
                    renderMemory(d.memory);
                    renderFacts(d.facts);
                    renderBranches(d.branches);
                    // накапливаем статистику токенов из сохранённой истории
                    tokIn = 0;
                    tokOut = 0;
                    tokHist = 0;
                    modelStats = {};
                    items.forEach(function (m) {
                        if (m && m.role === "assistant") {
                            if (m.usage) {
                                tokIn += Math.max(0, parseInt(m.usage.input, 10) || 0);
                                tokOut += Math.max(0, parseInt(m.usage.output, 10) || 0);
                                tokHist += Math.max(0, parseInt(m.usage.history, 10) || 0);
                            }
                            if (Array.isArray(m.answers)) {
                                m.answers.forEach(function (a) {
                                    if (!a || !a.label) return;
                                    var st = ensureModel(a.label);
                                    st.input += Math.max(0, parseInt(a.input, 10) || 0);
                                    st.output += Math.max(0, parseInt(a.output, 10) || 0);
                                    if (a.cost != null && !isNaN(parseFloat(a.cost))) {
                                        st.cost += parseFloat(a.cost);
                                        st.hasCost = true;
                                    }
                                });
                            }
                        }
                    });
                    renderTokenStats();
                    renderModelStats();
                    applyContextStats(d.context);
                    render();
                    if (d.has_history) {
                        setStatus("Загружена сохранённая сессия.", "ok");
                    }
                }
            })
            .catch(function () {});
    }

    // ------------------------------------------------------------------
    // Инициализация
    // ------------------------------------------------------------------
    submit.addEventListener("click", ask);
    qEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            ask();
        }
    });

    var newchatBtn = document.getElementById("newchat");
    if (newchatBtn) newchatBtn.addEventListener("click", startNewChat);

    // Загружаем список доступных моделей и строим панель выбора.
    fetch("/api/model")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d) return;
            if (d.available && d.available.length) {
                availableLabels = d.available.map(function (m) { return m.label; });
                buildModelControls(d.available);
            } else if (d.models && d.models.length) {
                availableLabels = d.models.slice();
                buildModelControls(d.models.map(function (l) {
                    return { label: l, cls: "" };
                }));
            }
            renderModelStats();
        })
        .catch(function () {});

    // Инициализация интерфейса.
    renderModelStats();
    renderModelsTitle();
    resetContextStats();
    // Показываем панели facts/веток согласно активной стратегии.
    syncStrategyUI();
    renderProfiles({ profiles: [], active: null });
    renderFacts({});
    renderBranches({ branches: [{ name: "main", size: 0 }], active_branch: 0 });
    renderMemory({ short: { items: 0, branches: 1 },
                   working: {}, longterm: {} });
    // Загружаем историю с сервера (если она есть на диске).
    // ------------------------------------------------------------------
    // Левая колонка: автоподгонка ширины под содержимое
    // ------------------------------------------------------------------
    // Панель слева сама подстраивается под самый широкий элемент (заголовки,
    // подписи, строки памяти и т.п.) в разумных пределах CSS (min/max-width).
    // Ширину задаём через inline-style, измеряя фактическую ширину контента.
    (function autoFitSide() {
        var side = document.querySelector(".side");
        if (!side) return;
        var MIN = 180, MAX = 380;

        function measure() {
            var widest = MIN;
            // Перебираем детей панели и берём максимум их natural-ширины.
            var kids = side.children;
            for (var i = 0; i < kids.length; i++) {
                var el = kids[i];
                // Временно переводим в режим «по содержимому», чтобы измерить.
                var prevWidth = el.style.width;
                var prevWhite = el.style.whiteSpace;
                el.style.width = "max-content";
                el.style.whiteSpace = "nowrap";
                var w = el.scrollWidth;
                el.style.width = prevWidth;
                el.style.whiteSpace = prevWhite;
                if (w > widest) widest = w;
            }
            // + внутренние отступы панели (слева/справа) и небольшой запас.
            var cs = window.getComputedStyle(side);
            var pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
            var target = Math.ceil(widest + pad + 4);
            if (target < MIN) target = MIN;
            if (target > MAX) target = MAX;
            side.style.width = target + "px";
        }

        measure();
        // Пересчитываем при изменении размера окна и после загрузки сессии
        // (когда в панель могли добавиться новые строки памяти/веток).
        window.addEventListener("resize", measure);
        window.addEventListener("load", measure);
        // Небольшая задержка — учесть данные, подтянутые с сервера.
        setTimeout(measure, 300);
        setTimeout(measure, 1000);
    })();

    loadSession();
})();
