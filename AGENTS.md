# AGENTS.md — Voyager CLI

Commands, protocol releases and architecture: [CONTRIBUTING.md](CONTRIBUTING.md).

## Keep the extension thin

An extension change reaches people only after store review and their own update, and a store
build that lags the CLI must keep working. So new behavior lands as far from the extension as it
can, in this order:

1. The CLI, `voyager serve` and its plugins, composing the page ops the extension already has.
2. Voyager site data (`site.json`), which is published without a release. Add a new top-level
   section: older builds ignore an unknown section but drop a whole known section that gains a key.
3. A new extension op or protocol change, only for a page capability that 1 and 2 cannot reach.
   The PR says why they cannot.

## Small files

The pre-commit hook (`scripts/check-staged-size.mjs`) blocks any staged file over 5,000 tokens
(bytes / 4), existing files included. Lay out new code as small files by responsibility from the
start; when the hook blocks, its message says how to split.
