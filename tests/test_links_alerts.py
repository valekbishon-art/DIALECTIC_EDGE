"""Тесты диплинков (links.py) и логики автоалертов (halal_alerts.py)."""
import re

import links

try:  # halal_alerts тянет aiogram — нет в minimal-deps CI (unit-fast)
    import halal_alerts as ha
except ImportError:  # pragma: no cover - aiogram отсутствует
    ha = None

RELIGIOUS = re.compile(r"halal|haram|shari|riba|gharar|fiqh|fatwa|ислам|халя|харам|шариат", re.I)


# ─── links.py ────────────────────────────────────────────────────────────────
def test_crypto_links_spot_only_no_futures():
    pairs = links.crypto_links("BTC")
    labels = {l for l, _ in pairs}
    assert {"Binance", "Bybit", "OKX", "График"} <= labels
    for _, url in pairs:
        assert url.startswith("https://")
        # только спот — никаких фьючерс/деривативных страниц
        assert "futures" not in url.lower()
        assert "perpetual" not in url.lower()


def test_crypto_links_symbol_in_url():
    pairs = dict(links.crypto_links("ETH"))
    assert "ETH_USDT" in pairs["Binance"]
    assert "ETH/USDT" in pairs["Bybit"]
    assert "BINANCE:ETHUSDT" in pairs["График"]


def test_stock_links():
    pairs = dict(links.stock_links("AAPL"))
    assert "AAPL" in pairs["TradingView"]
    assert "AAPL" in pairs["Yahoo"]
    assert all(u.startswith("https://") for u in pairs.values())


def test_line_renders_markdown():
    line = links.crypto_line("SOL")
    assert line.startswith("🔗 ")
    assert "[Binance](" in line and " · " in line


def test_links_no_religious_terms():
    assert not RELIGIOUS.search(links.crypto_line("BTC"))
    assert not RELIGIOUS.search(links.stock_line("MSFT"))


# ─── halal_alerts.build_alert_text ───────────────────────────────────────────
def test_no_change_returns_none():
    s = {"BTC", "ETH"}
    assert ha.build_alert_text(s, s, {"AAPL"}, {"AAPL"}) is None


def test_crypto_entry_exit_in_text():
    txt = ha.build_alert_text({"BTC", "SOL"}, {"BTC"}, set(), set())
    assert txt is not None
    assert "SOL" in txt                 # вошёл в аптренд
    assert "аптренд" in txt.lower()
    assert "[Binance](" in txt          # есть диплинк
    assert not RELIGIOUS.search(txt)    # без религиозной терминологии


def test_crypto_exit_listed():
    txt = ha.build_alert_text({"BTC"}, {"BTC", "ADA"}, set(), set())
    assert "ADA" in txt
    assert "стейбл" in txt.lower()


def test_stock_changes_in_text():
    txt = ha.build_alert_text(set(), set(), {"NVDA"}, {"AMD"})
    assert "NVDA" in txt and "AMD" in txt
    assert "[TradingView](" in txt


def test_load_handles_garbage():
    assert ha._load(None) == set()
    assert ha._load("not json") == set()
    assert ha._load('["BTC","ETH"]') == {"BTC", "ETH"}


# ─── Inline chart-URL helpers (для кнопок под карточками) ─────────────────────
def test_crypto_chart_url():
    import links
    u = links.crypto_chart_url("btc")
    assert u.startswith("https://")
    assert "BINANCE:BTCUSDT" in u


def test_stock_chart_url():
    import links
    u = links.stock_chart_url("nvda")
    assert u.startswith("https://")
    assert "NVDA" in u
