# CLAUDE.md — steampy (fork)

Fork of [bukson/steampy](https://github.com/bukson/steampy). The `feat/market-automation`
branch carries Gonzalo's extensions (async client/market layer, `RotatingProxySession`,
shared `utils_helpers`).

## CRITICAL — do not rewrite `feat/market-automation`

`dev/steam-toolkit` consumes this fork as a git dependency pinned to that branch
(`pyproject.toml`: `steampy @ git+...@feat/market-automation`). **Never rebase,
force-push, or delete `feat/market-automation`** — doing so breaks `uv sync` in
steam-toolkit. New work lands as normal forward commits on the branch (or a new branch),
never as history rewrites.

## Branching model

fork-PR (workspace standard): topic branches; contributions to upstream go via PR
following upstream's CONTRIBUTING.md; never push to the fork's `main` (keep it clean to
track upstream).
