"""
AlpacaBroker v2 — Interfaccia thread-safe verso Alpaca (trading + market data).

Novità rispetto alla v1:
  1. MARKET DATA NATIVO: get_daily_bars() e get_latest_prices() sostituiscono
     yfinance. Una sola fonte dati (Alpaca) per analisi ed esecuzione.
  2. ORDINI SERIALIZZATI: threading.Lock (_order_lock) al posto del flag
     booleano _order_pending (che non era atomico e veniva letto da main.py
     come attributo privato).
  3. FILL REALE: dopo submit_order si attende lo stato 'filled' via polling
     (_wait_for_fill) invece di un time.sleep(3) cieco.
  4. CLOCK CACHE: get_clock() è cachato 10s — Flask, Telegram e il loop
     possono chiederlo in parallelo senza moltiplicare le chiamate REST.
  5. FALLBACK COERENTI: get_open_position ritorna sempre una tupla
     (symbol|None, is_anomaly) anche in caso di fallimento di rete.
"""

import os
import time
import threading
from datetime import datetime, timedelta, timezone

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame
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
                        print(f"[RETRY] {func.__name__} fallito ({attempt+1}/{max_attempts}): {e}. Riprovo tra {wait}s...")
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
        self.base_url   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        # 'iex' è il feed incluso nel piano gratuito; 'sip' richiede abbonamento dati.
        self.data_feed  = os.getenv("ALPACA_DATA_FEED", "iex")

        # Serializza l'invio di ordini: un solo ordine in volo alla volta.
        self._order_lock = threading.Lock()

        # Cache del clock (evita una chiamata REST per ogni richiesta dashboard).
        self._clock_cache_lock = threading.Lock()
        self._clock_cache = (0.0, None)  # (timestamp, clock)

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

    def _get_clock(self, max_age_sec=10):
        """Clock di mercato con cache breve, thread-safe."""
        with self._clock_cache_lock:
            ts, clock = self._clock_cache
            if clock is not None and (time.time() - ts) < max_age_sec:
                return clock
        clock = self.api.get_clock()  # chiamata di rete FUORI dal lock della cache
        with self._clock_cache_lock:
            self._clock_cache = (time.time(), clock)
        return clock

    @retry(max_attempts=3, backoff=2, fallback=False)
    def is_market_open(self):
        return self._get_clock().is_open

    def minutes_to_close(self):
        """Minuti alla chiusura del mercato (0 se chiuso o in caso di errore)."""
        try:
            clock = self._get_clock()
            if not clock.is_open:
                return 0
            close_ts = clock.next_close.timestamp()
            return max(0, int((close_ts - time.time()) / 60))
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # MARKET DATA (sostituisce yfinance)
    # ------------------------------------------------------------------

    @retry(max_attempts=3, backoff=2, fallback={})
    def get_daily_bars(self, symbols, lookback_days=150):
        """
        Barre giornaliere per una lista di simboli via Alpaca Market Data v2.
        Ritorna: dict {symbol: DataFrame con colonne Open/High/Low/Close/Volume},
        formato compatibile con AlphaIntelligence (nessuna modifica necessaria lì).
        """
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
        bars = self.api.get_bars(
            symbols,
            TimeFrame.Day,
            start=start,
            adjustment='split',
            feed=self.data_feed,
        ).df

        result = {}
        if bars is None or bars.empty:
            return result

        rename = {'open': 'Open', 'high': 'High', 'low': 'Low',
                  'close': 'Close', 'volume': 'Volume'}

        if 'symbol' in bars.columns:
            for sym in symbols:
                df = bars[bars['symbol'] == sym]
                if df.empty:
                    continue
                result[sym] = df.rename(columns=rename)[list(rename.values())].copy()
        else:
            # Caso simbolo singolo: il df non ha la colonna 'symbol'
            result[symbols[0] if isinstance(symbols, (list, tuple)) else symbols] = \
                bars.rename(columns=rename)[list(rename.values())].copy()

        return result

    @retry(max_attempts=2, backoff=2, fallback={})
    def get_latest_prices(self, symbols):
        """Ultimo prezzo scambiato per ogni simbolo. Ritorna {symbol: float}."""
        trades = self.api.get_latest_trades(symbols, feed=self.data_feed)
        return {sym: float(t.price) for sym, t in trades.items()}

    # ------------------------------------------------------------------
    # PORTAFOGLIO (Source of Truth)
    # ------------------------------------------------------------------

    @retry(max_attempts=3, backoff=2, fallback=(0.0, 0.0))
    def get_real_portfolio_value(self):
        account = self.api.get_account()
        equity  = float(account.equity)
        cash    = float(account.cash)
        if equity == 0.0:
            raise ValueError("Equity restituita 0.0 — probabile errore di rete, non trading state.")
        return equity, cash

    @retry(max_attempts=3, backoff=2, fallback=(None, False))
    def get_open_position(self):
        """
        Ritorna SEMPRE una tupla (symbol|None, is_anomaly).
        FIX: il fallback ora è (None, False) — il vecchio fallback=None faceva
        crashare l'unpacking nel loop in caso di fallimento di rete totale.
        """
        positions = self.api.list_positions()

        if len(positions) > 1:
            symbols = [p.symbol for p in positions]
            print(f"⚠️ ANOMALIA: trovate {len(positions)} posizioni aperte: {symbols}")
            positions.sort(key=lambda p: float(p.market_value), reverse=True)
            return positions[0].symbol, True

        if len(positions) == 1:
            return positions[0].symbol, False

        return None, False

    @retry(max_attempts=3, backoff=2, fallback=None)
    def get_position_details(self, symbol):
        """Dettagli completi di una posizione. Fonte: Alpaca, non lo state locale."""
        try:
            pos = self.api.get_position(symbol)
            return {
                "symbol":          pos.symbol,
                "qty":             float(pos.qty),
                "avg_entry":       float(pos.avg_entry_price),
                "current_price":   float(pos.current_price),
                "unrealized_pl":   float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc) * 100,
                "market_value":    float(pos.market_value),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # ORDINI
    # ------------------------------------------------------------------

    @property
    def order_in_progress(self):
        """API pubblica al posto della lettura di _order_pending da main.py."""
        return self._order_lock.locked()

    def _wait_for_fill(self, order_id, timeout=45):
        """Polling sullo stato dell'ordine finché non è filled (o fallito/timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = self.api.get_order(order_id)
            except Exception as e:
                print(f"[FILL-CHECK] Errore lettura ordine {order_id}: {e}")
                time.sleep(2)
                continue
            if order.status == 'filled':
                return True
            if order.status in ('canceled', 'expired', 'rejected'):
                print(f"❌ Ordine {order_id} terminato con stato: {order.status}")
                return False
            time.sleep(1)
        print(f"⏱️ Timeout attesa fill ordine {order_id}")
        return False

    def buy_asset(self, symbol, notional_value, slippage_buffer=0.98):
        """
        Acquista `symbol` per `notional_value` dollari (meno buffer slippage).
        Ritorna True solo se l'ordine risulta FILLED su Alpaca.
        """
        if not self._order_lock.acquire(blocking=False):
            print(f"⚠️ Ordine già in corso — acquisto {symbol} bloccato per idempotenza.")
            return False
        try:
            safe_value = round(notional_value * slippage_buffer, 2)
            if safe_value < 1.0:
                print(f"⚠️ Valore ordine troppo basso ({safe_value}$) — acquisto {symbol} saltato.")
                return False

            order = self.api.submit_order(
                symbol=symbol,
                notional=safe_value,
                side='buy',
                type='market',
                time_in_force='day'
            )
            print(f"🛒 ACQUISTO inviato: {symbol} per ${safe_value} (id: {order.id})")
            return self._wait_for_fill(order.id)
        except Exception as e:
            print(f"❌ Errore acquisto {symbol}: {e}")
            return False
        finally:
            self._order_lock.release()

    def sell_all_asset(self, symbol):
        """
        Chiude completamente la posizione su `symbol`.
        Le vendite usano acquire bloccante con timeout: uno stop-loss non deve
        essere scartato solo perché c'è un altro ordine in coda.
        """
        if not self._order_lock.acquire(timeout=60):
            print(f"❌ Impossibile acquisire order lock per vendita {symbol} (timeout).")
            return False
        try:
            order = self.api.close_position(symbol)
            print(f"💰 VENDITA TOTALE inviata: {symbol} (id: {order.id})")
            return self._wait_for_fill(order.id)
        except Exception as e:
            print(f"❌ Errore vendita {symbol}: {e}")
            return False
        finally:
            self._order_lock.release()

    def sell_partial(self, symbol, percentage=0.5):
        """Vende una percentuale della posizione (prese di profitto parziali)."""
        if not self._order_lock.acquire(timeout=60):
            print(f"❌ Impossibile acquisire order lock per vendita parziale {symbol}.")
            return False
        try:
            pos = self.api.get_position(symbol)
            qty = float(pos.qty)
            qty_to_sell = max(1, int(qty * percentage))
            order = self.api.submit_order(
                symbol=symbol,
                qty=qty_to_sell,
                side='sell',
                type='market',
                time_in_force='day'
            )
            print(f"📉 VENDITA PARZIALE ({percentage*100:.0f}%): {symbol} — {qty_to_sell} shares")
            return self._wait_for_fill(order.id)
        except Exception as e:
            print(f"❌ Errore vendita parziale {symbol}: {e}")
            return False
        finally:
            self._order_lock.release()
