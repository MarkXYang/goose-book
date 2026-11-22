/* Interactive Goose Playground for mdBook.
 *
 * Adds Run, Stacks, Edit, and Reset controls to ```goose code blocks and
 * executes them against a local (or configured remote) Goose runner backend:
 *   python3 scripts/serve_playground.py --open
 */
(function () {
    "use strict";

    let resolvedEndpoint = null;
    let endpointChecked = false;

    function candidateEndpoints() {
        const list = [];
        if (window.GOOSE_PLAYGROUND_URL) {
            list.push(window.GOOSE_PLAYGROUND_URL.replace(/\/+$/, ""));
        }
        if (location.protocol === "http:" || location.protocol === "https:") {
            list.push(""); // same origin
            if (location.hostname === "127.0.0.1" || location.hostname === "localhost") {
                if (location.port !== "3000") list.push("http://127.0.0.1:3000");
                if (location.port !== "3001") list.push("http://127.0.0.1:3001");
            }
        } else {
            list.push("http://127.0.0.1:3000");
            list.push("http://127.0.0.1:3001");
        }
        return list;
    }

    async function discoverEndpoint() {
        if (endpointChecked) return resolvedEndpoint;
        endpointChecked = true;
        for (const base of candidateEndpoints()) {
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 1200);
                const resp = await fetch(base + "/api/health", { signal: ctrl.signal });
                clearTimeout(timer);
                if (resp.ok) {
                    const data = await resp.json();
                    if (data && data.ok) {
                        resolvedEndpoint = base;
                        return resolvedEndpoint;
                    }
                }
            } catch (_) {
                // try next candidate
            }
        }
        return null;
    }

    function getMarkerBefore(pre) {
        let node = pre.previousSibling;
        while (node && node.nodeType === Node.TEXT_NODE && !node.textContent.trim()) {
            node = node.previousSibling;
        }
        if (node && node.nodeType === Node.COMMENT_NODE) {
            const m = node.textContent.trim().match(/^(fragment|check-only|build|compile-fail)$/);
            if (m) return m[1];
        }
        return "";
    }

    function iconSvg(id, fallbackText) {
        const tpl = document.getElementById(id);
        return tpl ? tpl.innerHTML : fallbackText;
    }

    function autoSizeTextarea(ta) {
        const lines = ta.value.split("\n").length;
        ta.rows = Math.max(3, lines + 1);
    }

    function highlightCode(codeEl) {
        if (window.hljs) {
            codeEl.classList.remove("hljs");
            if (typeof window.hljs.highlightElement === "function") {
                window.hljs.highlightElement(codeEl);
            } else if (typeof window.hljs.highlightBlock === "function") {
                window.hljs.highlightBlock(codeEl);
            }
        }
    }

    function setupBlock(codeEl) {
        const pre = codeEl.parentElement;
        if (!pre || pre.tagName !== "PRE" || pre.dataset.goosePlaygroundInit) {
            return;
        }
        const marker = getMarkerBefore(pre);
        if (marker === "fragment") {
            return;
        }
        pre.dataset.goosePlaygroundInit = "1";
        pre.classList.add("goose-playground");

        const originalCode = codeEl.textContent.replace(/\n$/, "");
        let currentCode = originalCode;
        let editorEl = null;
        let outputEl = null;

        let buttons = pre.querySelector(".buttons");
        if (!buttons) {
            buttons = document.createElement("div");
            buttons.className = "buttons";
            pre.insertBefore(buttons, pre.firstChild);
        }

        // Reset button (hidden until edited)
        const resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "goose-btn goose-reset-btn hidden";
        resetBtn.title = "Reset code to original";
        resetBtn.setAttribute("aria-label", "Reset code to original");
        resetBtn.innerHTML = iconSvg("fa-clock-rotate-left", "↺");

        // Edit toggle button
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "goose-btn goose-edit-btn";
        editBtn.title = "Edit code (Ctrl+Enter to run)";
        editBtn.setAttribute("aria-label", "Edit code");
        editBtn.textContent = "Edit";

        // Stacks button (`goose check --stacks`)
        const stacksBtn = document.createElement("button");
        stacksBtn.type = "button";
        stacksBtn.className = "goose-btn goose-stacks-btn";
        stacksBtn.title = "Inspect compiler data stacks (goose check --stacks)";
        stacksBtn.setAttribute("aria-label", "Inspect data stacks");
        stacksBtn.textContent = "Stacks";

        // Run button
        const runBtn = document.createElement("button");
        runBtn.type = "button";
        runBtn.className = "goose-btn goose-run-btn";
        runBtn.title = marker === "check-only" ? "Typecheck with goose check" : "Run with goose (Ctrl+Enter)";
        runBtn.setAttribute("aria-label", marker === "check-only" ? "Check code" : "Run code");
        runBtn.innerHTML = iconSvg("fa-play", "▶");

        buttons.insertBefore(runBtn, buttons.firstChild);
        buttons.insertBefore(stacksBtn, runBtn.nextSibling);
        buttons.insertBefore(editBtn, stacksBtn.nextSibling);
        buttons.insertBefore(resetBtn, editBtn.nextSibling);

        function ensureOutput() {
            if (!outputEl) {
                outputEl = document.createElement("div");
                outputEl.className = "goose-output";
                pre.appendChild(outputEl);
            }
            return outputEl;
        }

        function setEditedState(edited) {
            resetBtn.classList.toggle("hidden", !edited);
        }

        function enterEditMode() {
            if (editorEl) return;
            editorEl = document.createElement("textarea");
            editorEl.className = "goose-editor";
            editorEl.spellcheck = false;
            editorEl.autocapitalize = "off";
            editorEl.autocomplete = "off";
            editorEl.value = currentCode;
            autoSizeTextarea(editorEl);

            editorEl.addEventListener("input", function () {
                currentCode = editorEl.value;
                autoSizeTextarea(editorEl);
                setEditedState(currentCode !== originalCode);
            });

            editorEl.addEventListener("keydown", function (ev) {
                if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
                    ev.preventDefault();
                    execute(marker === "check-only" ? "check" : "run");
                    return;
                }
                if (ev.key === "Tab") {
                    ev.preventDefault();
                    const start = editorEl.selectionStart;
                    const end = editorEl.selectionEnd;
                    editorEl.value =
                        editorEl.value.slice(0, start) + "    " + editorEl.value.slice(end);
                    editorEl.selectionStart = editorEl.selectionEnd = start + 4;
                    currentCode = editorEl.value;
                    setEditedState(currentCode !== originalCode);
                }
            });

            codeEl.classList.add("hidden");
            pre.insertBefore(editorEl, outputEl || null);
            editBtn.textContent = "Done";
            editBtn.classList.add("active");
            editorEl.focus();
        }

        function exitEditMode() {
            if (!editorEl) return;
            currentCode = editorEl.value;
            editorEl.remove();
            editorEl = null;
            codeEl.textContent = currentCode + "\n";
            codeEl.classList.remove("hidden");
            highlightCode(codeEl);
            editBtn.textContent = "Edit";
            editBtn.classList.remove("active");
        }

        editBtn.addEventListener("click", function () {
            if (editorEl) {
                exitEditMode();
            } else {
                enterEditMode();
            }
        });

        resetBtn.addEventListener("click", function () {
            currentCode = originalCode;
            if (editorEl) {
                editorEl.value = originalCode;
                autoSizeTextarea(editorEl);
            } else {
                codeEl.textContent = originalCode + "\n";
                highlightCode(codeEl);
            }
            setEditedState(false);
            if (outputEl) {
                outputEl.remove();
                outputEl = null;
            }
        });

        async function execute(mode) {
            if (editorEl) {
                currentCode = editorEl.value;
            }
            const out = ensureOutput();
            out.className = "goose-output goose-output-running";
            out.innerHTML = '<div class="goose-output-bar">Running with goose...</div>';

            runBtn.disabled = true;
            stacksBtn.disabled = true;

            try {
                const base = await discoverEndpoint();
                if (base === null) {
                    out.className = "goose-output goose-output-info";
                    out.innerHTML =
                        '<div class="goose-output-bar">Local Goose Playground Offline</div>' +
                        '<pre class="goose-output-body">To run and edit Goose examples inside the book, start the local playground server:\n\n' +
                        '    python3 scripts/serve_playground.py --open</pre>';
                    return;
                }

                const resp = await fetch(base + "/api/run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code: currentCode, mode: mode }),
                });
                const data = await resp.json();

                const labelMap = {
                    run: "Ran",
                    build: "Built & ran",
                    check: "Checked",
                    stacks: "Data stacks",
                };
                const verb = labelMap[data.mode || mode] || "Ran";
                const statusText = data.ok
                    ? `✓ ${verb} in ${data.duration_ms} ms`
                    : `✗ Exit ${data.exit_code} (${data.duration_ms} ms)`;

                out.className =
                    "goose-output " + (data.ok ? "goose-output-ok" : "goose-output-err");

                const bar = document.createElement("div");
                bar.className = "goose-output-bar";
                const statusSpan = document.createElement("span");
                statusSpan.textContent = statusText;
                const closeBtn = document.createElement("button");
                closeBtn.type = "button";
                closeBtn.className = "goose-output-close";
                closeBtn.title = "Hide output";
                closeBtn.textContent = "×";
                closeBtn.addEventListener("click", function () {
                    out.remove();
                    outputEl = null;
                });
                bar.appendChild(statusSpan);
                bar.appendChild(closeBtn);

                const body = document.createElement("pre");
                body.className = "goose-output-body";
                const parts = [];
                if (data.stdout) parts.push(data.stdout.replace(/\n$/, ""));
                if (data.stderr) parts.push(data.stderr.replace(/\n$/, ""));
                body.textContent = parts.length ? parts.join("\n") : "(no output)";

                out.innerHTML = "";
                out.appendChild(bar);
                out.appendChild(body);
            } catch (err) {
                endpointChecked = false;
                resolvedEndpoint = null;
                out.className = "goose-output goose-output-err";
                out.innerHTML =
                    '<div class="goose-output-bar">Connection error</div>' +
                    '<pre class="goose-output-body"></pre>';
                out.querySelector(".goose-output-body").textContent = String(err);
            } finally {
                runBtn.disabled = false;
                stacksBtn.disabled = false;
            }
        }

        runBtn.addEventListener("click", function () {
            const defaultMode =
                marker === "check-only"
                    ? "check"
                    : marker === "build"
                    ? "build"
                    : "run";
            execute(defaultMode);
        });

        stacksBtn.addEventListener("click", function () {
            execute("stacks");
        });
    }

    function init() {
        document.querySelectorAll("pre > code.language-goose").forEach(setupBlock);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
