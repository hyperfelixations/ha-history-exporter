# Contributing

Thank you for helping improve Home Assistant History Exporter.

## Development setup

Use Python 3.10 or newer in a virtual environment. Install the development
tools, then install HHE in editable mode with the optional Parquet dependency:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e ".[parquet]"
```

The project uses the standard `src` layout. Installing it is intentional: it
ensures tests import the package through the same packaging configuration that
users receive, rather than importing a same-named directory from the repository
root by accident.

## Required checks

Run these checks before submitting a change:

```bash
python -m pytest --cov=ha_history_exporter --cov-report=term-missing
python -m ruff check .
python -m mypy
python -m build
python -m twine check dist/*
```

The test suite disables network sockets, removes all supported Home Assistant
environment-variable names, redirects user and configuration paths to temporary
directories, and uses synthetic clients. Tests must remain fully runnable by a
developer who does not operate a Home Assistant instance.

## Privacy

Never add real Home Assistant URLs, tokens, IP addresses, user-specific paths,
email addresses, exported history, configuration, or other private data. Use
reserved example domains and synthetic fixtures. Run the repository privacy
audit for changes that affect packaging or repository-wide content:

```bash
python tools/privacy_audit.py --repository . --history
```

## Golden fixtures

`tests/golden/` records the output contract for a normal day and a daylight-
saving transition. If an intentional change affects these fixtures, inspect
every diff before refreshing them:

```bash
python tools/refresh_golden.py
```

Never refresh a golden merely to make a failing test pass. First establish why
the output changed and whether that change is part of the intended contract.
