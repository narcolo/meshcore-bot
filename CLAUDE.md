# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MeshCore Bot is a Python async bot that connects to MeshCore mesh networks via serial, BLE, or TCP. It processes messages, responds to keywords/commands, and provides data services (weather, solar, satellites, etc.). It uses a plugin-based architecture for both commands and background services.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt
# Or as editable with test deps
pip install -e ".[test]"

# Run the bot
python3 meshcore_bot.py --config config.ini

# Validate config without starting
python3 meshcore_bot.py --validate-config

# Run all tests
pytest

# Run unit or integration tests only
pytest tests/unit/
pytest tests/integration/

# Run a single test file or specific test
pytest tests/unit/test_mesh_graph_edges.py
pytest tests/unit/test_mesh_graph_edges.py::TestMeshGraphEdges::test_add_new_edge

# Run by marker
pytest -m unit
pytest -m integration

# Run with coverage
pytest --cov=modules --cov-report=html --cov-report=term-missing
```

## Architecture

### Entry Point & Core Loop
`meshcore_bot.py` → creates `MeshCoreBot` (from `modules/core.py`) → runs async event loop with signal handling. The bot connects to a MeshCore device, listens for messages, routes them through the message handler, and processes scheduled tasks.

### Plugin System (two types, both auto-discovered)

**Command Plugins** (`modules/commands/*_command.py`):
- Subclass `BaseCommand` from `modules/commands/base_command.py`
- Set class attributes: `name`, `keywords` (list of trigger words), `description`, `requires_dm`, `requires_internet`, `cooldown_seconds`, `category`
- Implement `async execute(self, message: MeshMessage) -> bool`
- Use `self.send_response(message, content)` to reply
- Use `self.get_config_value(section, key, fallback, value_type)` for config (handles legacy section name migrations)
- Use `self.translate(key, **kwargs)` for i18n
- `self.get_max_message_length(message)` returns max chars (150 for DM, ~148 for channel after username prefix)
- Loaded by `PluginLoader` in `modules/plugin_loader.py`

**Service Plugins** (`modules/service_plugins/*_service.py`):
- Subclass `BaseServicePlugin` from `modules/service_plugins/base_service.py`
- Implement `async start()` and `async stop()`
- Long-running background tasks (Discord bridge, packet capture, map uploader, weather alerts)
- Loaded by `ServicePluginLoader` in `modules/service_plugin_loader.py`

### Core Modules
- `modules/core.py` — `MeshCoreBot` class, initialization sequence, main loop
- `modules/message_handler.py` — Routes incoming messages to commands/keywords, enforces rate limits
- `modules/command_manager.py` — Command execution, routing, response capture
- `modules/channel_manager.py` — Channel configuration and monitoring
- `modules/repeater_manager.py` — Repeater/node tracking and topology (largest module)
- `modules/db_manager.py` — SQLite database operations
- `modules/rate_limiter.py` — Global, per-user, and TX rate limiting
- `modules/mesh_graph.py` — Graph-based mesh path analysis
- `modules/feed_manager.py` — RSS feed subscriptions
- `modules/scheduler.py` — Scheduled message handling
- `modules/models.py` — `MeshMessage` dataclass (the message type passed throughout)
- `modules/utils.py` — Shared utility functions
- `modules/i18n.py` — Translation system
- `modules/web_viewer/` — Flask-based web UI (`app.py`, `integration.py`)
- `modules/clients/` — External API clients (ESPN, NOAA aurora, weather sim parser)

### Data Model
`MeshMessage` (in `modules/models.py`) is the central dataclass passed through the system: `content`, `sender_id`, `sender_pubkey`, `channel`, `hops`, `path`, `is_dm`, `timestamp`, `snr`, `rssi`, `elapsed`.

### Configuration
INI-based (`config.ini`). Key sections: `[Connection]`, `[Bot]`, `[Channels]`, `[Keywords]`, `[External_Data]`, `[Logging]`, plus per-command sections like `[Wx_Command]`, `[Alert_Command]`, and per-service sections like `[Discord_Bridge]`, `[Packet_Capture]`. Templates: `config.ini.example` (full), `config.ini.minimal-example`, `config.ini.quickstart`.

## Testing Conventions

- Tests in `tests/` with `unit/` and `integration/` subdirectories
- Markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow`
- `asyncio_mode = auto` in pytest.ini (no need for `@pytest.mark.asyncio`)
- Fixtures in `tests/conftest.py`: `mock_logger`, `test_config`, `test_db`, `mock_bot`, `mesh_graph`, `populated_mesh_graph`
- Helper factories in `tests/helpers.py`: `create_test_repeater()`, `create_test_edge()`, `create_test_path()`
- Unit tests use in-memory SQLite; integration tests use real database
- CI runs `pytest tests/ -v --tb=short` on Python 3.11

## Development Workflow

- Main branch: `main`. Development branch: `dev`. PRs go to `dev`.
- CI: GitHub Actions runs tests on push to main/dev and PRs
- Config validation tool: `python3 validate_config.py`
- DB backup utility: `python3 backup_database.py`
- Docs built with MkDocs Material (`mkdocs.yml`), source in `docs/`
