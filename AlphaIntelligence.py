import pandas as pd
import numpy as np


class AlphaIntelligence:
    """
    Motore decisionale istituzionale — versione aggressiva orientata al profitto.

    Indicatori usati:
    - RSI (14): momentum oscillator
    - MACD (12/26/9): trend following
    - Volume spike: conferma istituzionale
    - ATR (14): volatilità e sizing della posizione
    - Momentum 5D / 10D: trend a breve termine
    - Bollinger Bands: breakout detection
    """

    def __init__(self, hist_data: pd.DataFrame):
        """
        hist_data: DataFrame da yfinance con colonne Close, Volume, High, Low.
        Deve avere almeno 30 righe per calcoli affidabili.
        """
        self.data    = hist_data.copy()
        self.valid   = len(self.data) >= 15  # Minimo dati necessari
        self._score  = None  # Cache del punteggio composito
        self._errors = []

        if self.valid:
            self._compute_indicators()

    # ------------------------------------------------------------------
    # CALCOLO INDICATORI
    # ------------------------------------------------------------------

    def _compute_indicators(self):
        df = self.data

        # --- Prezzi e volumi ---
        close  = df['Close'].squeeze()
        volume = df['Volume'].squeeze() if 'Volume' in df.columns else None
        high   = df['High'].squeeze()   if 'High'   in df.columns else close
        low    = df['Low'].squeeze()    if 'Low'    in df.columns else close

        # --- RSI 14 ---
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        self.rsi = (100 - 100 / (1 + rs)).iloc[-1]

        # --- MACD (12, 26, 9) ---
        ema12      = close.ewm(span=12, adjust=False).mean()
        ema26      = close.ewm(span=26, adjust=False).mean()
        macd_line  = ema12 - ema26
        signal     = macd_line.ewm(span=9, adjust=False).mean()
        self.macd_hist    = (macd_line - signal).iloc[-1]
        self.macd_cross   = (macd_line.iloc[-1] > signal.iloc[-1]) and (macd_line.iloc[-2] <= signal.iloc[-2])
        self.macd_current = macd_line.iloc[-1]
        self.macd_signal  = signal.iloc[-1]

        # --- Momentum 5D e 10D ---
        self.momentum_5d  = ((close.iloc[-1] - close.iloc[-5])  / close.iloc[-5])  * 100 if len(close) >= 5  else 0
        self.momentum_10d = ((close.iloc[-1] - close.iloc[-10]) / close.iloc[-10]) * 100 if len(close) >= 10 else 0

        # --- ATR 14 (volatilità) ---
        tr = pd.DataFrame({
            'hl': high - low,
            'hc': (high - close.shift()).abs(),
            'lc': (low  - close.shift()).abs()
        }).max(axis=1)
        self.atr = tr.rolling(14).mean().iloc[-1]
        self.atr_pct = (self.atr / close.iloc[-1]) * 100  # ATR in % del prezzo

        # --- Bollinger Bands (20, 2σ) ---
        sma20    = close.rolling(20).mean()
        std20    = close.rolling(20).std()
        upper_bb = sma20 + 2 * std20
        lower_bb = sma20 - 2 * std20
        self.bb_position = (close.iloc[-1] - lower_bb.iloc[-1]) / (upper_bb.iloc[-1] - lower_bb.iloc[-1] + 1e-9)
        self.bb_breakout = close.iloc[-1] > upper_bb.iloc[-1]

        # --- Volume spike ---
        if volume is not None:
            avg_vol          = volume.rolling(20).mean().iloc[-1]
            self.volume_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1.0
        else:
            self.volume_ratio = 1.0

        # --- Prezzo vs SMA50 ---
        sma50 = close.rolling(50).mean()
        self.above_sma50 = close.iloc[-1] > sma50.iloc[-1] if not sma50.iloc[-1] != sma50.iloc[-1] else True

        self._score = self._compute_score()

    def _compute_score(self):
        """
        Calcola un punteggio composito da -100 a +100.
        Positivo = segnale long, negativo = segnale di uscita.
        """
        score = 0

        # RSI: zona di forza vs ipervenduto/ipercomprato
        if   self.rsi < 30:   score += 20   # ipervenduto: potenziale rimbalzo aggressivo
        elif self.rsi < 45:   score += 10   # momentum debole ma non esaurito
        elif self.rsi < 65:   score += 25   # zona ottimale di forza
        elif self.rsi < 75:   score += 5    # ipercomprato moderato
        else:                 score -= 15   # ipercomprato estremo: rischio correzione

        # MACD
        if self.macd_cross:           score += 25  # Golden cross: segnale forte
        elif self.macd_hist > 0:      score += 10  # Istogramma positivo
        elif self.macd_hist < -0.5:   score -= 20  # Divergenza negativa significativa

        # Momentum
        if   self.momentum_5d > 3:    score += 15
        elif self.momentum_5d > 1:    score += 8
        elif self.momentum_5d < -3:   score -= 20
        elif self.momentum_5d < -1:   score -= 10

        if   self.momentum_10d > 5:   score += 10
        elif self.momentum_10d < -5:  score -= 15

        # Volume spike (conferma istituzionale)
        if   self.volume_ratio > 2.0: score += 20  # Volume doppio: movimento istituzionale
        elif self.volume_ratio > 1.5: score += 10
        elif self.volume_ratio < 0.5: score -= 5   # Volume basso: poca convinzione

        # Bollinger Bands
        if   self.bb_breakout:            score += 15  # Breakout sopra la banda: momentum forte
        elif self.bb_position > 0.8:      score += 5
        elif self.bb_position < 0.2:      score += 10  # Rimbalzo da zona di supporto

        # Posizione vs SMA50
        if self.above_sma50: score += 5
        else:                score -= 10

        return max(-100, min(100, score))

    # ------------------------------------------------------------------
    # API PUBBLICA
    # ------------------------------------------------------------------

    def get_composite_score(self):
        """Ritorna il punteggio composito da -100 a +100."""
        return self._score if self._score is not None else 0

    def get_institutional_verdict(self):
        """
        Verdetto basato sul punteggio composito.
        Soglie aggressive per non perdere opportunità.
        """
        if not self.valid:
            return "NEUTRAL"

        s = self._score
        if   s >= 60:  return "STRONG_BUY"
        elif s >= 30:  return "BUY"
        elif s >= 10:  return "WEAK_BUY"
        elif s >= -10: return "NEUTRAL"
        elif s >= -30: return "WEAK_SELL"
        else:          return "SELL/SWAP_TO_GOLD"

    def get_bridgewater_risk(self):
        """
        Valutazione del rischio macroeconomico basata su volatilità e momentum.
        Versione aggressiva: HIGH_RISK solo in condizioni estreme.
        """
        if not self.valid:
            return "SAFE"

        # Solo HIGH_RISK se ATR molto elevato E momentum fortemente negativo
        if self.atr_pct > 4.0 and self.momentum_5d < -4:
            return "HIGH_RISK"

        # ELEVATED: avvertimento non bloccante
        if self.atr_pct > 2.5 or self.momentum_5d < -2:
            return "ELEVATED"

        return "SAFE"

    def get_position_size_multiplier(self):
        """
        Suggerisce un moltiplicatore per il sizing della posizione (0.5 - 1.0).
        In base alla volatilità (ATR%): più è alta, meno si investe.
        """
        if not self.valid:
            return 0.9

        if   self.atr_pct > 4.0: return 0.70
        elif self.atr_pct > 2.5: return 0.85
        elif self.atr_pct > 1.5: return 0.92
        else:                     return 1.00

    def get_summary(self):
        """Ritorna un dizionario con tutti gli indicatori calcolati."""
        if not self.valid:
            return {"error": "Dati insufficienti"}
        return {
            "score":          self._score,
            "verdict":        self.get_institutional_verdict(),
            "risk":           self.get_bridgewater_risk(),
            "rsi":            round(self.rsi, 1),
            "macd_hist":      round(self.macd_hist, 4),
            "macd_cross":     self.macd_cross,
            "momentum_5d":    round(self.momentum_5d, 2),
            "momentum_10d":   round(self.momentum_10d, 2),
            "atr_pct":        round(self.atr_pct, 2),
            "volume_ratio":   round(self.volume_ratio, 2),
            "bb_position":    round(self.bb_position, 2),
            "bb_breakout":    self.bb_breakout,
            "above_sma50":    self.above_sma50,
            "size_multiplier":self.get_position_size_multiplier(),
        }
