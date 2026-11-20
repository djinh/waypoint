# Contributing

Issues and pull requests are welcome: bug reports, questions, ideas and fixes. A security problem
goes privately, not in an issue: see [SECURITY.md](SECURITY.md).

## How this repository works

This repository receives releases only. Development happens in a private repository, and each
release is exported here as a single commit. So a pull request here is never merged as it is:

1. a maintainer reads it and discusses it with you on the pull request;
2. if it goes in, the change is re-applied by hand in the development repository, with credit to
   you in the commit message (your GitHub name and its no-reply address, unless you ask for
   something else);
3. it reaches this repository with the next release, and the pull request is closed with a note
   saying which one.

The same goes for a patch in an issue. By contributing you agree that your change is published
under the project's [MIT licence](LICENSE).

## Before you open a pull request

- Run the tests: `uv run --with-editable . --with pytest --with pyyaml python -m pytest -q` (or
  `pip install -e '.[dev]'` and `python -m pytest -q`). Tests use `127.0.0.1` on a free port and
  temporary folders, never a real deployment.
- A change to `src/` bumps `__version__` in `src/waypoint/__init__.py`; then run
  `python scripts/sync_openapi.py`, which copies it into the API contract.
- A change to the API's shape updates `docs/api/openapi.yaml`, `docs/API.md`,
  `src/waypoint/openapi.json` (`python scripts/sync_openapi.py`) and the examples
  (`python examples/make_examples.py`) together.
- Examples in code and docs use invented names (`agent-a`, `alice`, `example.org`) and the
  documentation address ranges.

## Please leave out

Tokens, host names, addresses and other details of your own deployment, in issues, logs and pull
requests alike: this repository is public.
