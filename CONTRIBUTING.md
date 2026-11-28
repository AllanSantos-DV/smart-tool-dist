# Contributing

## Setup

Python 3 with the standard library only; the product has no runtime dependency to install for development. Clone the
repository and work on `develop`.

## Workflow

1. Branch from `develop`. Write the test first and see it fail, then make the change.
2. Run the module's tests: `python -m unittest discover -s tests -p test_<module>.py`; before merging, the full suite:
   `python tests/run.py`.
3. Commit with a message that says what changed and why. No co-author trailer and no generated-by line (the pre-push hook
   refuses them).
4. Merge into `develop`. Releases are cut by the maintainer from `main` (`release/README.md`); never tag from `develop`.

## Conventions

- Reuse before writing: search the repository for an existing function first.
- Comments only where something is hard to understand, and then why, never what. Document functions, classes and modules
  with docstrings.
- Failures are loud: raise with context or return a visible error; no fallback that hides one.
- No backward-compatibility aliases: an old format fails loudly.
- Text shown to users goes through `i18n` with the English text as the key and an entry in `locales/pt-BR.json`.
- Tests are silent: no audio, no windows, no touching a running daemon.
- The product collects no usage data; do not add analytics or network pings.
- `docs/BACKLOG.md` holds only items that change how the end user uses the product; technical fixes are done right away.
