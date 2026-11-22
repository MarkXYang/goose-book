/* Goose syntax highlighting for mdBook's bundled highlight.js */
(function () {
    "use strict";

    function registerGoose(hljs) {
        if (!hljs || typeof hljs.registerLanguage !== "function") {
            return;
        }

        hljs.registerLanguage("goose", function () {
            const KEYWORDS = {
                keyword:
                    "import namespace struct enum type fn thread_fn export extern " +
                    "simd recursive inline checkfree root const let var pad reusable " +
                    "if else while for in loop block guard defer match return break " +
                    "continue as from upto fields variants types has_default same",
                literal: "true false null self",
                type:
                    "bool varint i8 i16 i32 i64 u8 u16 u32 u64 f32 f64",
                built_in:
                    "print str format assert abort exit copy default " +
                    "hardware_threads thread_spawn thread_spawn_contained thread_wait " +
                    "qput qget qpoll qlimit to_bytes bytes_of from_bytes " +
                    "embed_file embed_shader src_file src_line"
            };

            const NESTED_BLOCK_COMMENT = {
                className: "comment",
                begin: /\/\*/,
                end: /\*\//,
                contains: ["self"]
            };

            const RAW_STRING = {
                className: "string",
                begin: /"""/,
                end: /"""/
            };

            const QUOTED_STRING = {
                className: "string",
                begin: /"/,
                end: /"/,
                contains: [
                    {
                        className: "subst",
                        begin: /\\(?:x[0-9a-fA-F]{2}|[ntr0\\"'"])/
                    }
                ]
            };

            const CHAR_LITERAL = {
                className: "string",
                begin: /'(?:\\(?:x[0-9a-fA-F]{2}|.)|[^'\\])'/
            };

            const NUMBER = {
                className: "number",
                variants: [
                    { begin: /\b0x[0-9a-fA-F][0-9a-fA-F_]*(?:\.[0-9a-fA-F][0-9a-fA-F_]*)?[pP][+-]?[0-9][0-9_]*\b/ },
                    { begin: /\b0x[0-9a-fA-F][0-9a-fA-F_]*\b/ },
                    { begin: /\b[0-9][0-9_]*(?:\.[0-9][0-9_]*)?[eE][+-]?[0-9][0-9_]*\b/ },
                    { begin: /\b[0-9][0-9_]*\.[0-9][0-9_]*\b/ },
                    { begin: /\b[0-9][0-9_]*\b/ }
                ]
            };

            return {
                name: "Goose",
                aliases: ["gs"],
                keywords: KEYWORDS,
                contains: [
                    hljs.C_LINE_COMMENT_MODE,
                    NESTED_BLOCK_COMMENT,
                    RAW_STRING,
                    QUOTED_STRING,
                    CHAR_LITERAL,
                    NUMBER,
                    {
                        className: "function",
                        beginKeywords: "fn thread_fn",
                        end: /[({<]/,
                        excludeEnd: true,
                        contains: [
                            {
                                className: "title",
                                begin: /(?:::|[A-Za-z_][A-Za-z0-9_]*::)?[A-Za-z_][A-Za-z0-9_]*/
                            }
                        ]
                    },
                    {
                        className: "class",
                        beginKeywords: "struct enum type",
                        end: /[{<=]/,
                        excludeEnd: true,
                        contains: [
                            {
                                className: "title",
                                begin: /[A-Za-z_][A-Za-z0-9_]*/
                            }
                        ]
                    }
                ]
            };
        });

        document.querySelectorAll("pre code.language-goose").forEach(function (block) {
            block.classList.remove("hljs");
            if (typeof hljs.highlightElement === "function") {
                hljs.highlightElement(block);
            } else if (typeof hljs.highlightBlock === "function") {
                hljs.highlightBlock(block);
            }
        });
    }

    if (window.hljs) {
        registerGoose(window.hljs);
    } else {
        window.addEventListener("DOMContentLoaded", function () {
            registerGoose(window.hljs);
        });
    }
})();
