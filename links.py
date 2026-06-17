"""links.py — диплинки на биржи, графики и котировки для тикеров.

Только спот-страницы (никакого фьючерса/маржи/деривативов). Чистый stdlib,
без сети. Возвращаем готовые Telegram-markdown ссылки `[label](url)`.

Использование:
    links.crypto_line("BTC")  -> "🔗 [Binance](…) · [Bybit](…) · [OKX](…) · [График](…)"
    links.stock_line("AAPL")  -> "🔗 [TradingView](…) · [Yahoo](…) · [Google](…)"

При отправке ставь disable_web_page_preview=True, чтобы ссылки не разворачивались.
"""
from __future__ import annotations

from urllib.parse import quote


def _md(label: str, url: str) -> str:
    return f"[{label}]({url})"


def crypto_links(symbol: str, quote_ccy: str = "USDT") -> list[tuple[str, str]]:
    """Спот-страницы крупных бирж + график для монеты (BTC, ETH, …)."""
    c = symbol.strip().upper()
    q = quote_ccy.upper()
    return [
        ("Binance", f"https://www.binance.com/en/trade/{c}_{q}?type=spot"),
        ("Bybit", f"https://www.bybit.com/en/trade/spot/{c}/{q}"),
        ("OKX", f"https://www.okx.com/trade-spot/{c.lower()}-{q.lower()}"),
        ("График", f"https://www.tradingview.com/chart/?symbol=BINANCE:{c}{q}"),
    ]


def stock_links(ticker: str) -> list[tuple[str, str]]:
    """График + котировки для акции (AAPL, MSFT, …)."""
    t = ticker.strip().upper()
    return [
        ("TradingView", f"https://www.tradingview.com/chart/?symbol={t}"),
        ("Yahoo", f"https://finance.yahoo.com/quote/{t}"),
        ("Google", f"https://www.google.com/finance/quote/{t}:NASDAQ"),
    ]


def line(pairs: list[tuple[str, str]], prefix: str = "🔗 ") -> str:
    """Список (label,url) → одна строка markdown-ссылок через ' · '."""
    return prefix + " · ".join(_md(lbl, url) for lbl, url in pairs)


def crypto_line(symbol: str, quote_ccy: str = "USDT", prefix: str = "🔗 ") -> str:
    return line(crypto_links(symbol, quote_ccy), prefix)


def stock_line(ticker: str, prefix: str = "🔗 ") -> str:
    return line(stock_links(ticker), prefix)
