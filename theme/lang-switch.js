/* Bilingual EN <-> 简体中文 switcher for mdBook.
 * Maps the current page cleanly between `<root>/<path>` and `<root>/zh/<path>`.
 */
(function () {
    "use strict";

    function getPathToRoot() {
        try {
            // mdBook declares `const path_to_root = "../";` at top-level (not on window).
            if (typeof path_to_root === "string") return path_to_root;
        } catch (_) {
            // ReferenceError if undeclared
        }
        return "";
    }

    function computeTargetHref() {
        const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
        const toRoot = getPathToRoot();
        const rootUrl = new URL(toRoot || "./", location.href);
        const curUrl = new URL(location.href);

        let rel = curUrl.pathname.startsWith(rootUrl.pathname)
            ? curUrl.pathname.slice(rootUrl.pathname.length)
            : "";
        if (!rel || rel.endsWith("/")) rel += "index.html";

        if (isZh) {
            // Current book root is `<site>/zh/`, so parent directory is `<site>/`.
            const enRoot = new URL("../", rootUrl);
            return enRoot.pathname + rel + curUrl.search + curUrl.hash;
        } else {
            // Current book root is `<site>/`, so Chinese book lives at `<site>/zh/`.
            return rootUrl.pathname + "zh/" + rel + curUrl.search + curUrl.hash;
        }
    }

    function init() {
        const bar = document.querySelector(".right-buttons");
        if (!bar || bar.querySelector(".goose-lang-switch")) return;

        const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
        const a = document.createElement("a");
        a.className = "goose-lang-switch";
        a.href = computeTargetHref();
        a.textContent = isZh ? "EN" : "中文";
        a.title = isZh ? "Switch to English edition" : "切换到简体中文版";
        a.setAttribute("aria-label", a.title);

        bar.insertBefore(a, bar.firstChild);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
