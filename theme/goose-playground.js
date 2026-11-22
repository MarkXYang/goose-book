/* Interactive Goose Playground for mdBook (Hybrid Remote + Precomputed + Local).
 *
 * Adds Run, Stacks, Edit, and Reset controls to ```goose code blocks.
 * Execution priority:
 *   1. Live runner backend (configured via ?runner=..., localStorage,
 *      theme/goose-config.js window.GOOSE_PLAYGROUND_URL, same-origin /api/health,
 *      or local serve_playground.py on 127.0.0.1:3000/3001).
 *   2. Instant verified static snapshot (theme/goose-precomputed.js) for any
 *      unedited book listing—works everywhere, including GitHub Pages and file://.
 *   3. Inline runner configuration panel if the reader edits code with no live
 *      backend connected yet.
 */
(function () {
    "use strict";

    const STORAGE_KEY = "goose_playground_url";
    let resolvedEndpoint = null;
    let resolvedMeta = null;
    let endpointChecked = false;

    /* Deterministic 64-bit hex hash matching scripts/build_precomputed.py. */
    function hashCode(code) {
        const bytes = new TextEncoder().encode(code);
        let h1 = 0x811c9dc5;
        let h2 = 0x9e3779b9;
        for (let i = 0; i < bytes.length; i++) {
            const b = bytes[i];
            h1 ^= b;
            h1 = Math.imul(h1, 0x01000193) >>> 0;
            h2 ^= b;
            h2 = Math.imul(h2, 0x01000193) >>> 0;
            h2 = ((h2 << 7) | (h2 >>> 25)) >>> 0;
        }
        return h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0");
    }

    function normalizeUrl(url) {
        if (!url || typeof url !== "string") return "";
        return url.trim().replace(/\/+$/, "");
    }

    function getQueryRunner() {
        try {
            const params = new URLSearchParams(location.search);
            return normalizeUrl(params.get("runner") || "");
        } catch (_) {
            return "";
        }
    }

    function getSavedRunner() {
        try {
            return normalizeUrl(localStorage.getItem(STORAGE_KEY) || "");
        } catch (_) {
            return "";
        }
    }

    function saveRunnerUrl(url) {
        const clean = normalizeUrl(url);
        try {
            if (clean) localStorage.setItem(STORAGE_KEY, clean);
            else localStorage.removeItem(STORAGE_KEY);
        } catch (_) {
            // ignore storage errors in restricted contexts
        }
        resetDiscovery();
        return clean;
    }

    function resetDiscovery() {
        resolvedEndpoint = null;
        resolvedMeta = null;
        endpointChecked = false;
    }

    function candidateEndpoints() {
        const list = [];
        const add = (u) => {
            if (list.indexOf(u) === -1) list.push(u);
        };

        const q = getQueryRunner();
        if (q) add(q);

        const saved = getSavedRunner();
        if (saved) add(saved);

        const cfg = normalizeUrl(window.GOOSE_PLAYGROUND_URL || "");
        if (cfg) add(cfg);

        if (location.protocol === "http:" || location.protocol === "https:") {
            // Only probe same-origin if we're not on a known static-only host,
            // or probe quickly with a short timeout.
            if (
                location.hostname === "127.0.0.1" ||
                location.hostname === "localhost"
            ) {
                add("");
                if (location.port !== "3000") add("http://127.0.0.1:3000");
                if (location.port !== "3001") add("http://127.0.0.1:3001");
            } else if (!location.hostname.endsWith(".github.io")) {
                add("");
            }
        } else {
            add("http://127.0.0.1:3000");
            add("http://127.0.0.1:3001");
        }
        return list;
    }

    async function probeEndpoint(base, timeoutMs) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), timeoutMs);
        try {
            const resp = await fetch(base + "/api/health", { signal: ctrl.signal });
            clearTimeout(timer);
            if (!resp.ok) return null;
            const data = await resp.json();
            if (data && data.ok) return data;
        } catch (_) {
            clearTimeout(timer);
        }
        return null;
    }

    async function discoverEndpoint() {
        if (endpointChecked) return resolvedEndpoint;
        endpointChecked = true;
        const candidates = candidateEndpoints();
        for (const base of candidates) {
            const isRemote = base.startsWith("https://") || (base.startsWith("http://") && !base.includes("127.0.0.1") && !base.includes("localhost"));
            const meta = await probeEndpoint(base, isRemote ? 3500 : 800);
            if (meta) {
                resolvedEndpoint = base;
                resolvedMeta = meta;
                return resolvedEndpoint;
            }
        }
        return null;
    }

    function lookupPrecomputed(code, mode) {
        const store = window.GOOSE_PRECOMPUTED;
        if (!store || !store.entries) return null;
        const entry = store.entries[hashCode(code)];
        if (!entry) return null;
        const slot = mode === "stacks" ? "stacks" : "run";
        const res = entry[slot];
        if (!res) return null;
        return {
            ok: res.ok,
            exit_code: res.exit_code,
            stdout: res.stdout || "",
            stderr: res.stderr || "",
            duration_ms: res.duration_ms || 0,
            mode: res.mode || mode,
            cached: true,
            version: store.version || "goose",
        };
    }

    function getMarkerBefore(pre) {
        let node = pre.previousSibling;
        while (node && node.nodeType === Node.TEXT_NODE && !node.textContent.trim()) {
            node = node.previousSibling;
        }
        if (node && node.nodeType === Node.COMMENT_NODE) {
            const m = node.textContent
                .trim()
                .match(/^(fragment|check-only|build|compile-fail)$/);
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
        runBtn.title =
            marker === "check-only"
                ? "Typecheck with goose check"
                : "Run with goose (Ctrl+Enter)";
        runBtn.setAttribute(
            "aria-label",
            marker === "check-only" ? "Check code" : "Run code"
        );
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

        function defaultRunMode() {
            return marker === "check-only"
                ? "check"
                : marker === "build"
                ? "build"
                : "run";
        }

        function renderRunnerConfig(out, reason, retryMode) {
            out.className = "goose-output goose-output-info";
            out.innerHTML = "";

            const bar = document.createElement("div");
            bar.className = "goose-output-bar";
            const title = document.createElement("span");
            title.textContent = reason || "Connect a Live Goose Runner";
            const closeBtn = document.createElement("button");
            closeBtn.type = "button";
            closeBtn.className = "goose-output-close";
            closeBtn.title = "Hide output";
            closeBtn.textContent = "×";
            closeBtn.addEventListener("click", function () {
                out.remove();
                outputEl = null;
            });
            bar.appendChild(title);
            bar.appendChild(closeBtn);
            out.appendChild(bar);

            const panel = document.createElement("div");
            panel.className = "goose-config-panel";

            const desc = document.createElement("p");
            desc.textContent =
                "Unedited book listings run anywhere from the built-in verified snapshot. To execute custom edits live, connect a remote Goose runner (Fly.io / Cloud Run / Render / Docker) or start the local server:";
            panel.appendChild(desc);

            const form = document.createElement("form");
            form.className = "goose-config-form";

            const input = document.createElement("input");
            input.type = "url";
            input.className = "goose-config-input";
            input.placeholder = "https://your-goose-runner.fly.dev";
            input.value =
                getSavedRunner() ||
                getQueryRunner() ||
                normalizeUrl(window.GOOSE_PLAYGROUND_URL || "");

            const saveBtn = document.createElement("button");
            saveBtn.type = "submit";
            saveBtn.className = "goose-config-save";
            saveBtn.textContent = "Connect & Run";

            const clearBtn = document.createElement("button");
            clearBtn.type = "button";
            clearBtn.className = "goose-config-clear";
            clearBtn.textContent = "Reset URL";
            clearBtn.addEventListener("click", function () {
                saveRunnerUrl("");
                input.value = "";
            });

            form.appendChild(input);
            form.appendChild(saveBtn);
            form.appendChild(clearBtn);

            form.addEventListener("submit", function (ev) {
                ev.preventDefault();
                saveRunnerUrl(input.value);
                execute(retryMode || defaultRunMode());
            });
            panel.appendChild(form);

            const hint = document.createElement("pre");
            hint.className = "goose-output-body goose-config-hint";
            hint.textContent =
                "Deploy your own public HTTPS runner (see playground-server/README.md):\n" +
                "  cd playground-server && fly deploy\n" +
                "Or run locally:\n" +
                "  python3 scripts/serve_playground.py --open";
            panel.appendChild(hint);

            out.appendChild(panel);
        }

        function renderResult(out, data, mode) {
            const labelMap = {
                run: "Ran",
                build: "Built & ran",
                check: "Checked",
                stacks: "Data stacks",
            };
            const verb = labelMap[data.mode || mode] || "Ran";

            let statusText;
            if (data.cached) {
                statusText = data.ok
                    ? `✓ ${verb} (verified snapshot · ${data.duration_ms} ms)`
                    : `✗ Exit ${data.exit_code} (verified snapshot)`;
            } else {
                const originTag =
                    resolvedMeta && resolvedMeta.mode === "remote"
                        ? "remote runner"
                        : "live";
                statusText = data.ok
                    ? `✓ ${verb} in ${data.duration_ms} ms (${originTag})`
                    : `✗ Exit ${data.exit_code} (${data.duration_ms} ms · ${originTag})`;
            }

            out.className =
                "goose-output " + (data.ok ? "goose-output-ok" : "goose-output-err");

            const bar = document.createElement("div");
            bar.className = "goose-output-bar";

            const statusSpan = document.createElement("span");
            statusSpan.textContent = statusText;

            const actions = document.createElement("span");
            actions.className = "goose-output-actions";

            if (data.cached) {
                const cfgLink = document.createElement("button");
                cfgLink.type = "button";
                cfgLink.className = "goose-output-link";
                cfgLink.textContent = "Connect live runner";
                cfgLink.title = "Configure a remote or local Goose runner for live edits";
                cfgLink.addEventListener("click", function () {
                    renderRunnerConfig(out, "Connect a Live Goose Runner", mode);
                });
                actions.appendChild(cfgLink);
            }

            const closeBtn = document.createElement("button");
            closeBtn.type = "button";
            closeBtn.className = "goose-output-close";
            closeBtn.title = "Hide output";
            closeBtn.textContent = "×";
            closeBtn.addEventListener("click", function () {
                out.remove();
                outputEl = null;
            });
            actions.appendChild(closeBtn);

            bar.appendChild(statusSpan);
            bar.appendChild(actions);

            const body = document.createElement("pre");
            body.className = "goose-output-body";
            const parts = [];
            if (data.stdout) parts.push(data.stdout.replace(/\n$/, ""));
            if (data.stderr) parts.push(data.stderr.replace(/\n$/, ""));
            body.textContent = parts.length ? parts.join("\n") : "(no output)";

            out.innerHTML = "";
            out.appendChild(bar);
            out.appendChild(body);
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
                    execute(defaultRunMode());
                    return;
                }
                if (ev.key === "Tab") {
                    ev.preventDefault();
                    const start = editorEl.selectionStart;
                    const end = editorEl.selectionEnd;
                    editorEl.value =
                        editorEl.value.slice(0, start) +
                        "    " +
                        editorEl.value.slice(end);
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
            if (editorEl) exitEditMode();
            else enterEditMode();
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
            const cached = lookupPrecomputed(currentCode, mode);

            out.className = "goose-output goose-output-running";
            out.innerHTML =
                '<div class="goose-output-bar">Running with goose...</div>';

            runBtn.disabled = true;
            stacksBtn.disabled = true;

            try {
                const base = await discoverEndpoint();
                if (base !== null) {
                    const resp = await fetch(base + "/api/run", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ code: currentCode, mode: mode }),
                    });
                    const data = await resp.json();
                    renderResult(out, data, mode);
                    return;
                }

                // No live runner reachable: use verified static snapshot if available.
                if (cached) {
                    renderResult(out, cached, mode);
                    return;
                }

                renderRunnerConfig(
                    out,
                    "Live Goose Runner Required for Edited Code",
                    mode
                );
            } catch (err) {
                resetDiscovery();
                if (cached) {
                    renderResult(out, cached, mode);
                    return;
                }
                renderRunnerConfig(
                    out,
                    "Could not reach live runner (" + String(err) + ")",
                    mode
                );
            } finally {
                runBtn.disabled = false;
                stacksBtn.disabled = false;
            }
        }

        runBtn.addEventListener("click", function () {
            execute(defaultRunMode());
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
