import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

class AlpacaBroker:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.base_url = "https://paper-api.alpaca.markets"
        
        try:
            self.api = tradeapi.REST(self.api_key, self.secret_key, self.base_url, api_version='v2')
            account = self.api.get_account()
            print(f"✅ Connessione Alpaca: {account.status}")
        except Exception as e:
            print(f"⚠️ Errore critico connessione Alpaca: {e}")

    def is_market_open(self):
        try:
            clock = self.api.get_clock()
            return clock.is_open
        except Exception as e:
            print(f"Errore orario: {e}")
            return False

    def get_real_portfolio_value(self):
        try:
            account = self.api.get_account()
            return float(account.equity), float(account.cash)
        except Exception as e:
            return 0.0, 0.0

    # --- NUOVO POTERE: LETTURA PORTAFOGLIO REALE ---
    def get_open_position(self):
        """
        Ritorna il simbolo dell'asset attualmente posseduto, o None se siamo liquidi.
        Questa è la VERA fonte di verità.
        """
        try:
            positions = self.api.list_positions()
            if len(positions) > 0:
                return positions[0].symbol # Prende il primo asset (la strategia prevede 1 asset alla volta)
            return None
        except Exception as e:
            print(f"Errore lettura posizioni: {e}")
            return None

    def buy_asset(self, symbol, notional_value):
        try:
            self.api.submit_order(
                symbol=symbol,
                notional=notional_value, 
                side='buy',
                type='market',
                time_in_force='day'
            )
            print(f"🛒 Ordine ACQUISTO: {symbol} per ${notional_value}")
        except Exception as e:
            print(f"❌ Errore acquisto {symbol}: {e}")

    def sell_all_asset(self, symbol):
        try:
            self.api.close_position(symbol)
            print(f"💰 VENDITA TOTALE: {symbol}")
        except Exception as e:
            print(f"❌ Errore vendita {symbol}: {e}")
