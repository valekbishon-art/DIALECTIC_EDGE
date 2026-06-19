"""Офлайн-тесты новых EDGE-фишек (без сети): подаём синтетические ряды в edge_signal.
Запуск: python tests/test_edge_enhancements.py  (или через pytest).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from halal_edge import edge_signal, DEFAULT_CFG

N = 260
I = N - 1


def build_market():
    btc = [100.0 * (1.004 ** t) for t in range(N)]          # стабильно растёт -> risk_on
    alt_cons = [(100.0 + t) if t <= 229 else (329.0 - (t - 229) * 0.6) for t in range(N)]
    alt_down = [400.0 - t for t in range(N)]                # нисходящий, ниже тренда
    return {"BTC": btc, "ALT_CONS": alt_cons, "ALT_DOWN": alt_down}


def build_regime():
    btc = [100.0] * 258 + [130.0, 140.0]                    # пересёк SMA200 только на последних барах
    alt = [50.0 + t for t in range(N)]
    return {"BTC": btc, "ALT": alt}


def test_default_unchanged_risk_on():
    s = build_market()
    sig = edge_signal(s, I, DEFAULT_CFG)
    assert sig["regime"] == "risk_on", sig["regime"]
    syms = {p["sym"] for p in sig["picks"]}
    assert "ALT_CONS" in syms, syms          # 30д минус, но среднее>0 -> проходит
    print("OK default risk_on + ALT_CONS included")


def test_consistency_excludes_mixed_momentum():
    s = build_market()
    cfg = dict(DEFAULT_CFG, mom_consistency=True)
    sig = edge_signal(s, I, cfg)
    syms = {p["sym"] for p in sig["picks"]}
    assert "ALT_CONS" not in syms, syms      # 30д импульс < 0 -> исключён
    print("OK consistency filter excludes mixed-momentum coin")


def test_breadth_gate_forces_cash():
    s = build_market()
    on = edge_signal(s, I, dict(DEFAULT_CFG, breadth_min=0.5))
    off = edge_signal(s, I, dict(DEFAULT_CFG, breadth_min=0.9))
    assert on["regime"] == "risk_on", on["regime"]    # 2/3 выше тренда >= 0.5
    assert off["regime"] == "risk_off", off["regime"]  # 2/3 < 0.9 -> стейбл
    print("OK breadth gate: 0.5->on, 0.9->off")


def test_regime_confirm_antiwhipsaw():
    s = build_regime()
    c1 = edge_signal(s, I, dict(DEFAULT_CFG, regime_confirm=1))
    c3 = edge_signal(s, I, dict(DEFAULT_CFG, regime_confirm=3))
    assert c1["regime"] == "risk_on", c1["regime"]    # свежий пробой засчитан
    assert c3["regime"] == "risk_off", c3["regime"]   # нет 3 баров подтверждения
    print("OK regime_confirm anti-whipsaw: 1->on, 3->off")


def test_skip_month_changes_score():
    s = build_market()
    base = edge_signal(s, I, DEFAULT_CFG)
    skip = edge_signal(s, I, dict(DEFAULT_CFG, mom_skip=7))
    b = next((p for p in base["picks"] if p["sym"] == "ALT_CONS"), None)
    assert b is not None
    sk = next((p for p in skip["picks"] if p["sym"] == "ALT_CONS"), None)
    if sk is not None:
        assert abs(sk["score"] - b["score"]) > 1e-9, (sk["score"], b["score"])
    print("OK skip-month changes momentum measurement")


if __name__ == "__main__":
    test_default_unchanged_risk_on()
    test_consistency_excludes_mixed_momentum()
    test_breadth_gate_forces_cash()
    test_regime_confirm_antiwhipsaw()
    test_skip_month_changes_score()
    print("\nALL ENHANCEMENT TESTS PASSED")
