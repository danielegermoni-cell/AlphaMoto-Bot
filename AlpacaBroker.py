import os
import time
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

def retry(max_attempts=3, backoff=2, fallback=None):
    """Decorator: riprova la chiamata fino a max_attempts volte con backoff esponenziale."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_attempts - 1:
                        wait = backoff ** attempt
                        print(f"[RETRY] {func.__name__} fallito (tentativo {attempt+1}/{max_attempts}): {e}. Riprovo tra {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"[ERRORE FATALE] {func.__name__} fallito dopo {max_attempts} tentativi: {e}")
                        if fallback is not None:
                            return fallback
                        raise
        return wrapper
    return decorator


class AlpacaBroker:
    def __init__(self):
        load_dotenv()
        self.api_key    = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.base_url   = "https://paper-api.alpaca.markets"  # Cambia in live quando pronto
        self._order_pending = False  # Flag idempotenza per evitare doppi acquisti

        try:
            self.api = tradeapi.REST(self.api_key, self.secret_key, self.base_url, api_version='v2')
            account  = self.api.get_account()
            print(f"✅ Connessione Alpaca OK — Status: {account.status} | Equity: ${account.equity}")
        except Exception as e:
            print(f"⚠️ Errore critico connessione Alpaca: {e}")
            self.api = None

    # ------------------------------------------------------------------
    # MERCATO
    # ------------------------------------------------------------------

    @retry(max_attempts=3, backoff=2, fallback=False)
    def is_market_open(self):
        clock = self.api.get_clock()
        return clock.is_open

    def minutes_to_close(self):
        """Restituisce i minuti alla chiusura del mercato (0 se chiuso)."""
        try:
            clock = self.api.get_clock()
            if not clock.is_open:
                return 0
            close_ts = clock.next_close.timestamp()
            now_ts   = time.time()
            return max(0, int((close_ts - now_ts) / 60))
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # PORTAFOGLIO
    # ------------------------------------------------------------------

    @retry(max_attempts=3, backoff=2, fallback=(0.0, 0.0))
    def get_real_portfolio_value(self):
        account = self.api.get_account()
        equity  = float(account.equity)
        cash    = float(account.cash)
        # Sicurezza: se equity è 0.0, quasi certamente è un errore di lettura
        if equity == 0.0:
            raise ValueError("Equity restituita 0.0 — probabile errore di rete, non trading state.")
        return equity, cash

    @retry(max_attempts=3, backoff=2, fallback=None)
    def get_open_position(self):
        """
        Ritorna il simbolo della posizione aperta, o None se siamo liquidi.
        Se ci sono più posizioni (situazione anomala), lancia un allarme.
        """
        positions = self.api.list_positions()

        if len(positions) > 1:
            symbols = [p.symbol for p in positions]
            print(f"⚠️ ANOMALIA: trovate {len(positions)} posizioni aperte: {symbols}")
            # Restituiamo la posizione di maggior valore
            positions.sort(key=lambda p: float(p.market_value), reverse=True)
            return positions[0].symbol, True  # (symbol, is_anomaly)

        if len(positions) == 1:
            return positions[0].symbol, False

        return None, False

    @retry(max_attempts=3, backoff=2, fallback=None)
    def get_position_details(self, symbol):
        """Restituisce dettagli completi di una posizione (avg entry price, qty, P&L%)."""
        try:
            pos = self.api.get_position(symbol)
            return {
                "symbol":       pos.symbol,
                "qty":          float(pos.qty),
                "avg_entry":    float(pos.avg_entry_price),
                "current_price":float(pos.current_price),
                "unrealized_pl":float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc) * 100,  # in percentuale
                "market_value": float(pos.market_value),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # ORDINI
    # ------------------------------------------------------------------

    def buy_asset(self, symbol, notional_value, slippage_buffer=0.98):
        """
        Acquista `symbol` per `notional_value` dollari (meno buffer slippage).
        Ritorna True se l'ordine è stato inviato, False altrimenti.
        """
        if self._order_pending:
            print(f"⚠️ Ordine già pendente — acquisto {symbol} bloccato per idempotenza.")
            return False

        safe_value = round(notional_value * slippage_buffer, 2)
        if safe_value < 1.0:
            print(f"⚠️ Valore ordine troppo basso ({safe_value}$) — acquisto {symbol} saltato.")
            return False

        try:
            self._order_pending = True
            self.api.submit_order(
                symbol=symbol,
                notional=safe_value,
                side='buy',
                type='market',
                time_in_force='day'
            )
            print(f"🛒 ACQUISTO inviato: {symbol} per ${safe_value}")
            time.sleep(3)  # Attendi che Alpaca registri la posizione
            self._order_pending = False
            return True
        except Exception as e:
            self._order_pending = False
            print(f"❌ Errore acquisto {symbol}: {e}")
            return False

    def sell_all_asset(self, symbol):
        """
        Chiude completamente la posizione su `symbol`.
        Ritorna True se l'ordine è stato inviato, False altrimenti.
        """
        try:
            self.api.close_position(symbol)
            print(f"💰 VENDITA TOTALE inviata: {symbol}")
            time.sleep(3)  # Attendi che Alpaca registri la liquidazione
            return True
        except Exception as e:
            print(f"❌ Errore vendita {symbol}: {e}")
            return False

    def sell_partial(self, symbol, percentage=0.5):
        """Vende una percentuale della posizione (utile per prese di profitto parziali)."""
        try:
            pos = self.api.get_position(symbol)
            qty = float(pos.qty)
            qty_to_sell = max(1, int(qty * percentage))
            self.api.submit_order(
                symbol=symbol,
                qty=qty_to_sell,
                side='sell',
                type='market',
                time_in_force='day'
            )
            print(f"📉 VENDITA PARZIALE ({percentage*100:.0f}%): {symbol} — {qty_to_sell} shares")
            return True
        except Exception as e:
            print(f"❌ Errore vendita parziale {symbol}: {e}")
            return False
