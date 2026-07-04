"""
Regression test sulle correzioni Fase 1 in AlpacaBroker (v6.1) + comportamenti chiave.
Nessuna rete: self.api viene sostituito con un fake controllabile.
"""
import types
import pytest

from AlpacaBroker import AlpacaBroker


# ---------------------------------------------------------------------------
# Fake API controllabile
# ---------------------------------------------------------------------------

class FakeOrder:
    def __init__(self, oid="ord-1", status="filled"):
        self.id = oid
        self.status = status


class FakePosition:
    def __init__(self, symbol="SPY", qty=10.0, price=100.0, market_value=None):
        self.symbol           = symbol
        self.qty              = str(qty)
        self.current_price    = str(price)
        self.avg_entry_price  = str(price * 0.95)
        self.unrealized_pl    = "10"
        self.unrealized_plpc  = "0.05"
        self.market_value     = str(market_value if market_value is not None else qty * price)


class FakePortfolioHistory:
    def __init__(self, base_value=1000.0):
        self.base_value = base_value


class FakeAPI:
    def __init__(self):
        self.submitted   = []   # kwargs degli submit_order
        self.closed      = []   # simboli passati a close_position
        self.position    = FakePosition()
        self.fill_status = "filled"
        self.fail_list_positions = False
        self._positions  = []
        self.portfolio_history = FakePortfolioHistory()
        self.fail_portfolio_history = False

    def get_portfolio_history(self, period=None, timeframe=None):
        if self.fail_portfolio_history:
            raise ConnectionError("rete giù")
        return self.portfolio_history

    def get_position(self, symbol):
        return self.position

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return FakeOrder("ord-buy")

    def close_position(self, symbol):
        self.closed.append(symbol)
        return FakeOrder("ord-close")

    def get_order(self, order_id):
        return FakeOrder(order_id, self.fill_status)

    def list_positions(self):
        if self.fail_list_positions:
            raise ConnectionError("rete giù")
        return self._positions


@pytest.fixture
def broker():
    b = AlpacaBroker()          # usa lo stub REST del conftest
    b.api = FakeAPI()
    return b


# ---------------------------------------------------------------------------
# FASE 1.4 — sell_partial frazionario
# ---------------------------------------------------------------------------

class TestSellPartialFrazionario:
    def test_qty_frazionaria_non_vende_piu_del_posseduto(self, broker):
        """
        REGRESSIONE del bug: qty=0.8 shares, pct=40%.
        Il vecchio max(1, int(0.32)) avrebbe venduto 1 share (> posseduto).
        Ora deve vendere 0.32 shares.
        """
        broker.api.position = FakePosition(qty=0.8, price=200.0)  # residuo 0.48*200=96$ ≥ 1$
        ok = broker.sell_partial("SPY", percentage=0.4)
        assert ok is True
        assert len(broker.api.submitted) == 1
        order = broker.api.submitted[0]
        assert float(order["qty"]) == pytest.approx(0.32)
        assert float(order["qty"]) < 0.8, "non deve MAI superare la qty posseduta"
        assert order["time_in_force"] == "day"

    def test_residuo_dust_promuove_a_vendita_totale(self, broker):
        """Se il residuo vale < 1$, meglio chiudere tutto (close_position)."""
        broker.api.position = FakePosition(qty=0.01, price=50.0)  # residuo 0.006*50=0.30$
        ok = broker.sell_partial("SPY", percentage=0.4)
        assert ok is True
        assert broker.api.closed == ["SPY"]
        assert broker.api.submitted == [], "nessun ordine parziale: chiusura totale"

    def test_qty_intera_normale(self, broker):
        broker.api.position = FakePosition(qty=10.0, price=100.0)
        ok = broker.sell_partial("SPY", percentage=0.4)
        assert ok is True
        assert float(broker.api.submitted[0]["qty"]) == pytest.approx(4.0)

    def test_lock_rilasciato_anche_su_eccezione(self, broker):
        """Il lock ordini deve essere rilasciato anche se get_position esplode."""
        def boom(symbol):
            raise RuntimeError("api ko")
        broker.api.get_position = boom
        assert broker.sell_partial("SPY", percentage=0.4) is False
        assert broker.order_in_progress is False, "lock non rilasciato dopo eccezione!"


# ---------------------------------------------------------------------------
# FASE 1.1 (già presente in v2, regression) — fallback tupla
# ---------------------------------------------------------------------------

class TestGetOpenPositionFallback:
    def test_fallimento_rete_ritorna_tupla_spacchettabile(self, broker, monkeypatch):
        """Il vecchio fallback=None crashava l'unpacking nel loop."""
        monkeypatch.setattr("time.sleep", lambda s: None)  # niente attesa nei retry
        broker.api.fail_list_positions = True
        result = broker.get_open_position()
        ticker, is_anomaly = result   # NON deve lanciare TypeError
        assert ticker is None
        assert is_anomaly is False

    def test_posizione_singola(self, broker):
        broker.api._positions = [FakePosition("QQQ")]
        assert broker.get_open_position() == ("QQQ", False)

    def test_anomalia_multi_posizione_sceglie_la_maggiore(self, broker):
        broker.api._positions = [FakePosition("QQQ", market_value=100),
                                 FakePosition("GLD", market_value=900)]
        ticker, anomaly = broker.get_open_position()
        assert (ticker, anomaly) == ("GLD", True)


# ---------------------------------------------------------------------------
# FASE 2 (già presente in v2, regression) — ciclo di vita ordini
# ---------------------------------------------------------------------------

class TestOrderLifecycle:
    def test_buy_ritorna_true_solo_su_fill(self, broker):
        broker.api.fill_status = "filled"
        assert broker.buy_asset("SPY", 1000.0) is True

    def test_buy_ritorna_false_su_rejected(self, broker):
        broker.api.fill_status = "rejected"
        assert broker.buy_asset("SPY", 1000.0) is False

    def test_buy_sotto_1_dollaro_saltato(self, broker):
        assert broker.buy_asset("SPY", 0.5) is False
        assert broker.api.submitted == []

    def test_slippage_buffer_applicato(self, broker):
        broker.buy_asset("SPY", 1000.0)
        assert broker.api.submitted[0]["notional"] == pytest.approx(980.0)

    def test_sell_all_su_fill(self, broker):
        assert broker.sell_all_asset("SPY") is True
        assert broker.api.closed == ["SPY"]


# ---------------------------------------------------------------------------
# FASE 2 — get_daily_start_equity (Source of Truth per il circuit breaker)
# ---------------------------------------------------------------------------

class TestDailyStartEquity:
    def test_valore_reale_da_alpaca(self, broker):
        broker.api.portfolio_history = FakePortfolioHistory(base_value=15234.56)
        assert broker.get_daily_start_equity() == pytest.approx(15234.56)

    def test_cache_evita_chiamate_ripetute(self, broker):
        """Entro il TTL, chiamate successive non devono toccare l'API."""
        calls = []
        original = broker.api.get_portfolio_history
        def counting(*a, **kw):
            calls.append(1)
            return original(*a, **kw)
        broker.api.get_portfolio_history = counting

        v1 = broker.get_daily_start_equity(max_age_sec=60)
        v2 = broker.get_daily_start_equity(max_age_sec=60)
        assert v1 == v2
        assert len(calls) == 1, "la seconda chiamata doveva usare la cache"

    def test_nessun_valore_se_alpaca_irraggiungibile_e_niente_cache(self, broker, monkeypatch):
        """REGRESSIONE: mai fidarsi di un default silenzioso — None esplicito."""
        monkeypatch.setattr("time.sleep", lambda s: None)
        broker.api.fail_portfolio_history = True
        assert broker.get_daily_start_equity() is None

    def test_base_value_mancante_ritorna_none(self, broker, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        broker.api.portfolio_history = FakePortfolioHistory(base_value=None)
        assert broker.get_daily_start_equity() is None
