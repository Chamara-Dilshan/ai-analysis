import pytest

import standalone_monitor as m


@pytest.fixture(autouse=True)
def _no_exchange_info(monkeypatch):
    """main() refreshes Binance lot sizes over the network every run. Tests
    stay hermetic - and pass on US CI runners Binance refuses - by keeping the
    built-in fallbacks instead."""
    async def _noop():
        return None
    monkeypatch.setattr(m, "refresh_lot_steps", _noop)


@pytest.fixture(autouse=True)
def _no_telegram_files(monkeypatch):
    """No test may upload a file to the real chat, even where the bot token
    happens to be set in the environment."""
    monkeypatch.setattr(m, "send_document", lambda *a, **k: True)
