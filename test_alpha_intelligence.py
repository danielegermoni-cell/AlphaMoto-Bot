"""
Test BASELINE su AlphaIntelligence.py (versione attuale, PRE-fix Fase 1.5).

Convenzione:
  - test_sanity_*   → comportamento che DEVE già essere corretto oggi
  - test_bug_*      → test che DOCUMENTANO un bug noto: passano se il bug
                      è presente. Dopo il fix 1.5 andranno invertiti e
                      diventeranno i regression test del comportamento corretto.
"""
import numpy as np
import pandas as pd
import pytest

from AlphaIntelligence import AlphaIntelligence


# ---------------------------------------------------------------------------
# Helper: generatori di DataFrame sintetici in formato Open/High/Low/Close/Volume
# ---------------------------------------------------------------------------

def make_df(closes, volumes=None, spread=0.5):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    return pd.DataFrame({
        "Open":   closes - 0.1,
        "High":   closes + spread,
        "Low":    closes - spread,
        "Close":  closes,
        "Volume": np.asarray(volumes, dtype=float),
    })


def uptrend(n=80, start=100.0, step=0.5):
    """Trend PURAMENTE rialzista: nessuna candela negativa."""
    return make_df(start + np.arange(n) * step)


def realistic(n=150, seed=42):
    """Serie realistica: random walk con rumore, sempre positiva."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    closes = np.maximum(closes, 5.0)
    vols   = rng.uniform(0.8e6, 1.5e6, n)
    return make_df(closes, vols)


# ---------------------------------------------------------------------------
# SANITY — comportamento atteso già oggi
# ---------------------------------------------------------------------------

class TestSanity:
    def test_dati_insufficienti_sotto_50_righe(self):
        ai = AlphaIntelligence(make_df([100] * 10))
        assert ai.valid is False
        assert ai.get_summary() == {"error": "Dati insufficienti"}
        assert ai.get_composite_score() == 0
        assert ai.get_institutional_verdict() == "NEUTRAL"
        assert ai.get_bridgewater_risk() == "SAFE"

    def test_summary_completo_con_dati_pieni(self):
        ai = AlphaIntelligence(realistic(150))
        s = ai.get_summary()
        for key in ("score", "verdict", "risk", "rsi", "macd_hist", "macd_cross",
                    "momentum_5d", "momentum_10d", "atr_pct", "volume_ratio",
                    "bb_position", "bb_breakout", "above_sma50", "size_multiplier"):
            assert key in s, f"chiave mancante: {key}"
        assert -100 <= s["score"] <= 100
        assert not np.isnan(s["rsi"]), "con 150 righe l'RSI deve essere calcolabile"

    def test_score_clampato(self):
        ai = AlphaIntelligence(realistic(150))
        assert -100 <= ai.get_composite_score() <= 100

    def test_verdetti_coerenti_con_le_soglie(self):
        ai = AlphaIntelligence(realistic(150))
        s, v = ai.get_composite_score(), ai.get_institutional_verdict()
        if s >= 60:      assert v == "STRONG_BUY"
        elif s >= 30:    assert v == "BUY"
        elif s >= 10:    assert v == "WEAK_BUY"
        elif s >= -10:   assert v == "NEUTRAL"
        elif s >= -30:   assert v == "WEAK_SELL"
        else:            assert v == "SELL/SWAP_TO_GOLD"

    def test_size_multiplier_range(self):
        ai = AlphaIntelligence(realistic(150))
        assert 0.5 <= ai.get_position_size_multiplier() <= 1.0

    def test_no_crash_senza_volume(self):
        df = realistic(150).drop(columns=["Volume"])
        ai = AlphaIntelligence(df)
        assert ai.volume_ratio == 1.0
        assert "score" in ai.get_summary()


# ---------------------------------------------------------------------------
# BUG NOTI — questi test PASSANO se il bug è presente (baseline pre-fix 1.5)
# ---------------------------------------------------------------------------

class TestFix15Regression:
    """
    Regression test del FIX 1.5. Sostituiscono i vecchi test_bug_* che
    documentavano i bug pre-fix (vedi git history / changelog per il baseline).
    """

    def test_fix_rsi_100_su_trend_puramente_rialzista(self):
        """RSI deve essere 100 (non NaN) quando non ci sono candele negative."""
        ai = AlphaIntelligence(uptrend(80))
        assert ai.rsi == pytest.approx(100.0)
        # Zona ottimale prevista dallo score per RSI alto ma non 'else': deve
        # finire nel ramo >=75 (score -15) SOLO se realmente ipercomprato;
        # qui verifichiamo solo che non sia più NaN.
        assert not pd.isna(ai.rsi)

    def test_fix_rsi_50_su_prezzo_piatto(self):
        """Prezzo perfettamente piatto: gain=loss=0 → RSI neutro (50)."""
        ai = AlphaIntelligence(make_df(np.full(80, 100.0), spread=0.1))
        assert ai.rsi == pytest.approx(50.0)

    def test_fix_above_sma50_false_con_dati_insufficienti(self):
        """
        Con <50 righe l'oggetto non è più 'valid' (soglia alzata a 50): il
        summary deve segnalare dati insufficienti, non calcolare un bonus.
        """
        ai = AlphaIntelligence(make_df(np.linspace(100, 90, 30)))
        assert ai.valid is False
        assert ai.get_summary() == {"error": "Dati insufficienti"}

    def test_fix_above_sma50_false_quando_sotto_sma_con_dati_pieni(self):
        """Con dati pieni (80 righe) ma prezzo sotto la SMA50, deve essere False."""
        closes = np.concatenate([np.full(60, 120.0), np.linspace(120, 90, 20)])
        ai = AlphaIntelligence(make_df(closes))
        assert ai.above_sma50 is False

    def test_fix_soglia_validita_alzata_a_50(self):
        """
        FIX: con 16-49 righe l'oggetto non è più 'valid' (evita indicatori
        NaN silenziosi come bb_position con <20 righe o above_sma50 con <50).
        """
        ai = AlphaIntelligence(make_df(100 + np.arange(16) * 0.1))
        assert ai.valid is False
        assert ai.get_summary() == {"error": "Dati insufficienti"}

    def test_fix_con_50_righe_indicatori_tutti_calcolabili(self):
        ai = AlphaIntelligence(realistic(50))
        assert ai.valid is True
        s = ai.get_summary()
        assert not np.isnan(s["bb_position"])
        assert isinstance(s["above_sma50"], bool)

    def test_fix_momentum_5d_copre_5_intervalli_reali(self):
        """FIX: '5D' deve confrontare oggi con 5 sedute fa (6 punti di serie), non 4."""
        closes = 100 * (1.01 ** np.arange(60))
        ai = AlphaIntelligence(make_df(closes))
        atteso_5_intervalli = ((closes[-1] - closes[-6]) / closes[-6]) * 100
        assert ai.momentum_5d == pytest.approx(atteso_5_intervalli)
        assert ai.momentum_5d == pytest.approx(5.101, abs=1e-3)


# ---------------------------------------------------------------------------
# Comportamenti di rischio — devono valere anche dopo il fix
# ---------------------------------------------------------------------------

class TestRisk:
    def test_high_risk_su_crollo_volatile(self):
        """ATR% alto + momentum 5d fortemente negativo → HIGH_RISK."""
        n = 60
        rng = np.random.default_rng(7)
        closes = np.concatenate([np.full(40, 100.0),
                                 100 - np.cumsum(rng.uniform(1.5, 3.0, 20))])
        df = make_df(closes, spread=5.0)  # spread ampio → ATR alto
        ai = AlphaIntelligence(df)
        assert ai.atr_pct > 4.0
        assert ai.momentum_5d < -4
        assert ai.get_bridgewater_risk() == "HIGH_RISK"

    def test_safe_su_mercato_piatto(self):
        ai = AlphaIntelligence(make_df(np.full(80, 100.0), spread=0.2))
        assert ai.get_bridgewater_risk() == "SAFE"
