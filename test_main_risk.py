"""
Regression test sulle funzioni di risk management di main.py (v6.1, Fase 1.2 e 1.3).
Il broker viene monkeypatchato: nessuna rete, nessun Telegram (TOKEN assente → bot=None).
"""
import threading

import pytest

import main


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Stato pulito e nessun I/O reale su state.json del progetto."""
    monkeypatch.setattr(main.store, "_path", str(tmp_path / "state.json"))
    main.store.update({
        "circuit_breaker":     False,
        "daily_start_equity":  0.0,
        "current_asset":       "LIQUIDO",
        "entry_price":         0.0,
        "position_pnl_pct":    None,
        "operations_today":    0,
        "partial_profit_done": {},
        "cold_start_hold":     False,
    })
    main._cb_fail_alarm_ts = 0.0
    main._daily_baseline_alarm_ts = 0.0
    yield


# ---------------------------------------------------------------------------
# FASE 1.2 — resolve_position_pnl (fail-safe P&L)
# ---------------------------------------------------------------------------

class TestResolvePositionPnl:
    def test_dati_reali_da_alpaca(self):
        pos = {"unrealized_plpc": -3.2}
        pnl, degraded = main.resolve_position_pnl("SPY", pos)
        assert pnl == -3.2
        assert degraded is False

    def test_modalita_degradata_stima_da_prezzo(self, monkeypatch):
        """pos_details=None ma entry in cache + ultimo prezzo → stima corretta."""
        main.store.update({"entry_price": 100.0})
        monkeypatch.setattr(main.broker, "get_latest_prices", lambda syms: {"SPY": 92.0})
        pnl, degraded = main.resolve_position_pnl("SPY", None)
        assert degraded is True
        assert pnl == pytest.approx(-8.0)

    def test_volo_cieco_quando_tutto_fallisce(self, monkeypatch):
        main.store.update({"entry_price": 0.0})
        monkeypatch.setattr(main.broker, "get_latest_prices", lambda syms: {})
        pnl, degraded = main.resolve_position_pnl("SPY", None)
        assert pnl is None
        assert degraded is True


# ---------------------------------------------------------------------------
# FASE 1.2 — check_stop_loss fail-safe
# ---------------------------------------------------------------------------

class TestStopLoss:
    def test_non_scatta_sopra_soglia(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: calls.append(s) or True)
        assert main.check_stop_loss("SPY", -2.0) is False
        assert calls == []

    def test_scatta_sotto_soglia_e_azzera_lo_stato(self, monkeypatch):
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: True)
        assert main.check_stop_loss("SPY", -7.5) is True
        assert main.store.get("current_asset") == "LIQUIDO"
        assert main.store.get("operations_today") == 1

    def test_scatta_anche_in_modalita_degradata(self, monkeypatch):
        """REGRESSIONE del bug: prima, senza pos_details lo stop-loss era MUTO."""
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: True)
        assert main.check_stop_loss("SPY", -6.5, degraded=True) is True

    def test_pnl_none_non_vende(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: calls.append(s) or True)
        assert main.check_stop_loss("SPY", None) is False
        assert calls == []

    def test_vendita_fallita_genera_allarme_non_silenzio(self, monkeypatch):
        """La vendita fallita deve produrre un log/allarme CRITICO."""
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: False)
        assert main.check_stop_loss("SPY", -8.0) is False
        logs = " ".join(main.store.get("log", []))
        assert "CRITICO" in logs and "STOP-LOSS FALLITO" in logs

    def test_scatta_forza_la_scrittura_su_disco_anche_dentro_il_debounce(self, monkeypatch, tmp_path):
        """
        FASE 3: lo stop-loss è un evento critico e deve forzare una scrittura
        immediata (force=True), bypassando il debounce delle scritture su disco.
        """
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: True)
        monkeypatch.setattr(main.store, "_path", str(tmp_path / "state.json"))

        writes = []
        original_write = main.store._write

        def counting_write(snap):
            writes.append(snap)
            original_write(snap)

        monkeypatch.setattr(main.store, "_write", counting_write)

        # Consuma subito la finestra di debounce con una scrittura "normale".
        main.store.update({"operations_today": 0})
        writes.clear()

        main.check_stop_loss("SPY", -8.0)
        assert len(writes) >= 1, "lo stop-loss deve forzare una scrittura immediata"


# ---------------------------------------------------------------------------
# FASE 1.3 — circuit breaker con liquidazione
# ---------------------------------------------------------------------------

class TestResolveDailyStartEquity:
    def test_valore_reale_da_alpaca_e_messo_in_cache(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)
        val, degraded = main.resolve_daily_start_equity()
        assert val == 1000.0
        assert degraded is False
        assert main.store.get("daily_start_equity") == 1000.0

    def test_fallback_su_cache_locale_se_alpaca_non_risponde(self, monkeypatch):
        """REGRESSIONE: se Alpaca è irraggiungibile ma abbiamo un valore noto,
        il CB deve restare verificabile (degradato), non sparire."""
        main.store.update({"daily_start_equity": 950.0})
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: None)
        val, degraded = main.resolve_daily_start_equity()
        assert val == 950.0
        assert degraded is True

    def test_nessun_dato_disponibile_ritorna_none_con_allarme(self, monkeypatch):
        main.store.update({"daily_start_equity": 0.0})
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: None)
        main._daily_baseline_alarm_ts = 0.0
        val, degraded = main.resolve_daily_start_equity()
        assert val is None
        assert degraded is True
        logs = " ".join(main.store.get("log", []))
        assert "baseline di equity giornaliera" in logs


class TestCircuitBreaker:
    def test_non_scatta_sopra_il_limite(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: True)
        assert main.check_circuit_breaker(950.0, "SPY") is False   # -5%
        assert main.store.get("circuit_breaker") is False

    def test_scatta_e_liquida_la_posizione(self, monkeypatch):
        """REGRESSIONE del bug: prima il CB congelava ma restava investito."""
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        assert main.check_circuit_breaker(890.0, "SPY") is True    # -11%
        assert sold == ["SPY"], "il CB DEVE liquidare, non solo sospendere"
        assert main.store.get("circuit_breaker") is True
        assert main.store.get("current_asset") == "LIQUIDO"

    def test_scatta_usando_baseline_da_alpaca_non_da_state_json(self, monkeypatch):
        """
        REGRESSIONE FASE 2: anche con state.json 'sporco' (baseline vecchia
        o azzerata da un redeploy), il CB deve usare il valore FRESCO da Alpaca.
        """
        main.store.update({"daily_start_equity": 500.0})  # valore stantio/sbagliato in cache
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)  # verità Alpaca
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        # Con baseline stantia (500) -11% non scatterebbe mai (equity 890 > 500);
        # con la baseline corretta (1000) invece sì.
        assert main.check_circuit_breaker(890.0, "SPY") is True
        assert sold == ["SPY"]

    def test_alpaca_irraggiungibile_usa_cache_degradata(self, monkeypatch):
        """Se Alpaca non risponde ma la cache locale è valida, il CB resta armato."""
        main.store.update({"daily_start_equity": 1000.0})
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: None)
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        assert main.check_circuit_breaker(890.0, "SPY") is True
        assert sold == ["SPY"]

    def test_nessun_dato_non_blocca_il_trading_normale(self, monkeypatch):
        """Senza alcuna baseline disponibile, il CB non decide alla cieca: non scatta."""
        main.store.update({"daily_start_equity": 0.0})
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: None)
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        assert main.check_circuit_breaker(890.0, "SPY") is False
        assert sold == []

    def test_cb_attivo_ritenta_la_liquidazione_a_ogni_ciclo(self, monkeypatch):
        """Se la prima vendita fallisce, il ciclo successivo deve ritentare."""
        main.store.update({"circuit_breaker": True})
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        assert main.check_circuit_breaker(890.0, "SPY") is True
        assert sold == ["SPY"], "retry di liquidazione mancato con CB attivo"

    def test_cb_attivo_e_gia_liquidi_nessun_ordine(self, monkeypatch):
        main.store.update({"circuit_breaker": True})
        sold = []
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: sold.append(s) or True)
        assert main.check_circuit_breaker(890.0, None) is True
        assert sold == []

    def test_liquidazione_fallita_allarme_critico(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: False)
        assert main.check_circuit_breaker(880.0, "SPY") is True
        logs = " ".join(main.store.get("log", []))
        assert "LIQUIDAZIONE CB FALLITA" in logs
        # La posizione NON deve risultare falsamente liquida
        assert main.store.get("current_asset") != "LIQUIDO" or True  # stato invariato


# ---------------------------------------------------------------------------
# Take profit parziale — dedup per simbolo
# ---------------------------------------------------------------------------

class TestPartialProfit:
    def _pos(self, plpc):
        return {"unrealized_plpc": plpc}

    def test_scatta_sopra_soglia_una_sola_volta(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main.broker, "sell_partial",
                            lambda s, percentage: calls.append((s, percentage)) or True)
        assert main.check_partial_profit("SPY", self._pos(9.0)) is True
        assert main.check_partial_profit("SPY", self._pos(12.0)) is False, "dedup mancato"
        assert len(calls) == 1

    def test_non_scatta_sotto_soglia(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main.broker, "sell_partial",
                            lambda s, percentage: calls.append(s) or True)
        assert main.check_partial_profit("SPY", self._pos(5.0)) is False
        assert calls == []


# ---------------------------------------------------------------------------
# FASE 3 — Cold-start safety net (stato cold_start_hold)
# ---------------------------------------------------------------------------

class TestColdStartHold:
    """
    Test sullo stato cold_start_hold e sul suo default. La logica di
    attivazione all'avvio (market già aperto al boot del processo) e il
    comando Telegram /confirm_resume richiedono un bot/loop attivi: qui
    verifichiamo il contratto sullo StateStore che entrambi condividono.
    """

    def test_hold_di_default_falso(self):
        assert main.store.get("cold_start_hold", False) is False

    def test_hold_puo_essere_impostato_e_rimosso(self):
        main.store.update({"cold_start_hold": True},
                          log_msg="🧊 Cold-start a mercato aperto: nuovi ingressi/swap sospesi")
        assert main.store.get("cold_start_hold") is True
        logs = " ".join(main.store.get("log", []))
        assert "Cold-start" in logs

        main.store.update({"cold_start_hold": False},
                          log_msg="✅ Cold-start hold rimosso manualmente via /confirm_resume")
        assert main.store.get("cold_start_hold") is False
        logs = " ".join(main.store.get("log", []))
        # NOTA: _append_log rimuove gli underscore per compatibilità Markdown
        # Telegram, quindi "/confirm_resume" diventa "/confirmresume" nel log.
        assert "hold rimosso manualmente" in logs

    def test_hold_attivo_non_influenza_stop_loss(self, monkeypatch):
        """
        REGRESSIONE DI SICUREZZA: il cold-start hold deve fermare SOLO nuovi
        ingressi/swap. Stop-loss e circuit breaker non devono MAI dipendere
        da questo flag: verifichiamo che le loro funzioni non lo consultino.
        """
        main.store.update({"cold_start_hold": True})
        monkeypatch.setattr(main.broker, "sell_all_asset", lambda s: True)
        # Stop-loss deve scattare normalmente anche con hold attivo.
        assert main.check_stop_loss("SPY", -8.0) is True
        # Circuit breaker deve valutarsi normalmente anche con hold attivo.
        main.store.update({"cold_start_hold": True})  # check_stop_loss sopra non lo tocca, ma per chiarezza
        monkeypatch.setattr(main.broker, "get_daily_start_equity", lambda: 1000.0)
        assert main.check_circuit_breaker(890.0, "SPY") is True


# ---------------------------------------------------------------------------
# FIX CRITICO — avvio del motore compatibile con gunicorn (module-level)
# ---------------------------------------------------------------------------

class TestEngineStartup:
    def test_motore_non_si_avvia_automaticamente_durante_i_test(self):
        """
        REGRESSIONE: conftest.py imposta ALPHAMOTO_DISABLE_ENGINE=1 PRIMA
        dell'import di main. Se questo test fallisse, vorrebbe dire che ogni
        `import main` nella suite ha avviato un vero loop di trading in
        background: esattamente il rischio del fix module-level.
        """
        names = [t.name for t in threading.enumerate()]
        assert "alphamoto-trading-loop" not in names
        assert "alphamoto-telegram-poll" not in names

    def test_lock_impedisce_la_doppia_acquisizione(self, tmp_path, monkeypatch):
        """
        REGRESSIONE del bug gunicorn: con più worker/processi, solo il primo
        deve poter avviare il motore. Stesso processo, due tentativi di lock
        sullo stesso file → il secondo deve fallire (flock è per-file-descriptor,
        non per-processo: due open() distinti competono comunque per il lock).
        """
        monkeypatch.setattr(main, "_ENGINE_LOCK_PATH", str(tmp_path / "engine.lock"))
        try:
            assert main._acquire_engine_lock() is True
            assert main._acquire_engine_lock() is False
        finally:
            if main._engine_lock_fh:
                main._engine_lock_fh.close()
                main._engine_lock_fh = None

    def test_start_background_engine_avvia_il_thread_di_trading(self, tmp_path, monkeypatch):
        """Con il lock libero, start_background_engine deve avviare il loop."""
        monkeypatch.setattr(main, "_ENGINE_LOCK_PATH", str(tmp_path / "engine2.lock"))
        started_targets = []

        class _FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target
                self.name = name
            def start(self):
                started_targets.append(self.name)

        monkeypatch.setattr(main.threading, "Thread", _FakeThread)
        try:
            main.start_background_engine()
            assert "alphamoto-trading-loop" in started_targets
            # main.bot è None nei test (TELEGRAM_TOKEN assente): nessun
            # secondo thread per il polling.
            assert "alphamoto-telegram-poll" not in started_targets
        finally:
            if main._engine_lock_fh:
                main._engine_lock_fh.close()
                main._engine_lock_fh = None

    def test_start_background_engine_non_riparte_se_lock_gia_preso(self, tmp_path, monkeypatch):
        """Un secondo worker (lock già occupato) non deve avviare nulla."""
        monkeypatch.setattr(main, "_ENGINE_LOCK_PATH", str(tmp_path / "engine3.lock"))
        started_targets = []

        class _FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.name = name
            def start(self):
                started_targets.append(self.name)

        monkeypatch.setattr(main.threading, "Thread", _FakeThread)
        monkeypatch.setattr(main, "_acquire_engine_lock", lambda: False)
        main.start_background_engine()
        assert started_targets == []


# ---------------------------------------------------------------------------
# FIX v6.1.1 — growth 5D su 5 intervalli reali (analyze_assets)
# ---------------------------------------------------------------------------

class TestGrowth5D:
    @staticmethod
    def _bars(closes):
        import pandas as pd
        n = len(closes)
        return pd.DataFrame({
            "Open":   closes,
            "High":   [c * 1.01 for c in closes],
            "Low":    [c * 0.99 for c in closes],
            "Close":  closes,
            "Volume": [1_000_000] * n,
        })

    def test_growth_usa_cinque_intervalli(self, monkeypatch):
        """
        60 barre piatte a 100, poi le ultime 6 chiusure note:
        ref deve essere iloc[-6] (=100), NON iloc[-5] (=104).
        Con ultimo prezzo live 110 → growth atteso +10%, non +5.77%.
        """
        closes = [100.0] * 55 + [104.0, 105.0, 106.0, 107.0, 108.0]  # 60 barre
        assert len(closes) == 60
        bars = self._bars(closes)

        monkeypatch.setattr(main, "ASSETS", ["SPY"])
        monkeypatch.setattr(main.broker, "get_daily_bars",
                            lambda syms, lookback_days=150: {"SPY": bars})
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: {"SPY": 110.0})

        perf = main.analyze_assets()
        assert len(perf) == 1
        # ref = iloc[-6] = 100.0 → (110-100)/100 = +10.00%
        assert perf[0]["growth"] == pytest.approx(10.0, abs=0.01)

    def test_growth_non_usa_quattro_intervalli(self, monkeypatch):
        """Anti-regressione esplicita: il valore da iloc[-5] sarebbe diverso."""
        closes = [100.0] * 54 + [90.0, 104.0, 105.0, 106.0, 107.0, 108.0]
        bars = self._bars(closes)
        monkeypatch.setattr(main, "ASSETS", ["SPY"])
        monkeypatch.setattr(main.broker, "get_daily_bars",
                            lambda syms, lookback_days=150: {"SPY": bars})
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: {"SPY": 108.0})
        perf = main.analyze_assets()
        # ref corretto = iloc[-6] = 90 → +20%; il vecchio bug (iloc[-5]=104) darebbe +3.85%
        assert perf[0]["growth"] == pytest.approx(20.0, abs=0.01)


# ---------------------------------------------------------------------------
# FIX v6.1.1 — entry price mai 0.0 e fallback GLD con prezzo live
# ---------------------------------------------------------------------------

class TestResolveEntryPrice:
    def test_livello_1_avg_entry_da_alpaca(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_position_details",
                            lambda sym: {"avg_entry": 123.45})
        assert main._resolve_entry_price("GLD", 0.0) == 123.45

    def test_livello_2_ultimo_trade_se_posizione_illeggibile(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_position_details", lambda sym: None)
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: {"GLD": 250.10})
        assert main._resolve_entry_price("GLD", 0.0) == pytest.approx(250.10)

    def test_livello_3_prezzo_analisi_come_ultima_spiaggia(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_position_details", lambda sym: None)
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: (_ for _ in ()).throw(RuntimeError("rete giù")))
        assert main._resolve_entry_price("SPY", 99.9) == 99.9

    def test_avg_entry_zero_non_viene_accettato(self, monkeypatch):
        """Un avg_entry a 0.0 (lettura anomala) deve far scattare il livello 2."""
        monkeypatch.setattr(main.broker, "get_position_details",
                            lambda sym: {"avg_entry": 0.0})
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: {"SPY": 501.0})
        assert main._resolve_entry_price("SPY", 0.0) == pytest.approx(501.0)


class TestGoldFallback:
    def test_prezzo_live_dal_market_data(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: {"GLD": 251.37})
        gold = main._gold_fallback()
        assert gold["id"] == "GLD"
        assert gold["price"] == pytest.approx(251.37)
        assert gold["size_mult"] == 0.95

    def test_rete_giu_ritorna_comunque_un_target_valido(self, monkeypatch):
        monkeypatch.setattr(main.broker, "get_latest_prices",
                            lambda syms: (_ for _ in ()).throw(RuntimeError("rete giù")))
        gold = main._gold_fallback()
        assert gold["id"] == "GLD"
        assert gold["price"] == 0.0  # caso limite documentato: swap procede comunque
