import pandas as pd
import numpy as np

class AlphaIntelligence: # <--- QUESTA RIGA È FONDAMENTALE
    def __init__(self, ticker_data):
        self.data = ticker_data

    # --- MODULO CITADEL (Analisi Tecnica) ---
    def get_citadel_score(self):
        try:
            delta = self.data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            last_rsi = rsi.iloc[-1]
            return 1 if last_rsi < 40 else (-1 if last_rsi > 70 else 0)
        except:
            return 0

    # --- MODULO BRIDGEWATER (Rischio) ---
    def get_bridgewater_risk(self):
        try:
            volatility = self.data['Close'].pct_change().std() * np.sqrt(252)
            return "HIGH_RISK" if volatility > 0.35 else "SAFE"
        except:
            return "SAFE"

    # --- MODULO RENAISSANCE (Statistica) ---
    def get_renaissance_edge(self):
        try:
            monthly_return = (self.data['Close'].iloc[-1] / self.data['Close'].iloc[-20]) - 1
            return 1 if monthly_return > 0 else 0
        except:
            return 0

    # --- VERDETTO FINALE ---
    def get_institutional_verdict(self):
        score = self.get_citadel_score() + self.get_renaissance_edge()
        if score >= 1:
            return "STRONG_BUY"
        elif score <= -1:
            return "SELL/SWAP_TO_GOLD"
        else:
            return "NEUTRAL"
