"""
AlphaMoto v6.1 — Refactor architetturale.

I 4 pilastri implementati:

1. LOCK CONTENTION → risolta con la classe StateStore: il lock in memoria
   (mem_lock) protegge SOLO letture/scritture del dizionario. L'I/O su disco
   avviene fuori dal mem_lock (su uno snapshot, serializzato da un io_lock
   separato). Nessuna chiamata di rete (Alpaca/Telegram) avviene mai mentre
   un lock è tenuto. Il vecchio schema (Lock non rientrante + send_telegram
   che riacquisiva lo stesso lock) era una trappola da deadlock.

2. ALPACA = SOURCE OF TRUTH → ogni ciclo il bot legge posizione, entry price
   e P&L direttamente da Alpaca (list_positions / get_position). Lo state.json
   è ora SOLO una cache per la dashboard e il log: nessuna decisione di
   trading legge dal JSON. Al riavvio dopo un crash il bot si ri-sincronizza
   da solo al primo ciclo.

3. MARKET DATA ALPACA → _analyze_assets() usa broker.get_daily_bars() e
   broker.get_latest_prices(). yfinance è eliminato: AlphaIntelligence riceve
   lo stesso DataFrame (Open/High/Low/Close/Volume) e non richiede modifiche.

4. TIMEFRAME COERENTI → growth 5D e indicatori AI ora derivano dalle STESSE
   barre giornaliere Alpaca (stessa fonte, stesso timeframe), con il prezzo
   corrente dall'ultimo trade per la reattività intraday.

Concorrenza degli ordini: `trade_lock` serializza le SEQUENZE multi-ordine
(stop-loss, swap sell→buy, panic da Telegram); dentro il broker, _order_lock
serializza i singoli ordini. Così un /panic non può incrociarsi con uno swap.
"""

import copy
import json
import os
import threading
import time
import traceback

import telebot
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from AlphaIntelligence import AlphaIntelligence
from AlpacaBroker import AlpacaBroker

# ══════════════════════════════════════════════════════════════════════
# INIZIALIZZAZIONE
# ══════════════════════════════════════════════════════════════════════
load_dotenv()
TOKEN    = os.getenv("TELEGRAM_TOKEN")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
bot      = telebot.TeleBot(TOKEN) if TOKEN else None
app      = Flask(__name__, static_folder="static")
CORS(app)
broker   = AlpacaBroker()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════════════════════════════
# PARAMETRI STRATEGICI
# ══════════════════════════════════════════════════════════════════════
ASSETS               = ['SPY', 'QQQ', 'SMH', 'GLD', 'VTI', 'SOXX', 'TQQQ']
SWAP_THRESHOLD       = 1.5     # % di vantaggio in growth per uno swap
SWAP_SCORE_GAP       = 20      # vantaggio minimo in score AI per uno swap
STOP_LOSS_PCT        = -6.0
PARTIAL_PROFIT_PCT   = 8.0
PARTIAL_PROFIT_FRAC  = 0.40
DAILY_LOSS_LIMIT_PCT = -10.0
ANALYSIS_INTERVAL    = 300     # secondi tra due analisi complete
REPORT_INTERVAL      = 7200    # report biorario
STATE_FILE           = os.path.join(BASE_DIR, "state.json")

# Serializza le sequenze di trading (loop + comandi Telegram).
trade_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════════
# STATE STORE — cache thread-safe per dashboard/log (NON source of truth)
# ══════════════════════════════════════════════════════════════════════

class StateStore:
    """
    Regole d'oro:
      - mem_lock protegge SOLO il dizionario in memoria (operazioni O(µs));
      - la scrittura su disco avviene su uno snapshot, FUORI dal mem_lock;
      - nessun metodo di questa classe fa mai chiamate di rete;
      - i dati di trading (posizione, entry, P&L) qui dentro sono una COPIA
        di ciò che dice Alpaca, mai l'originale.
    """

    DEFAULTS = {
        "log":                   ["🚀 AlphaMoto v6.1 Avviato"],
        "assets_data":           [],
        "total_equity":          0.0,
        "cash":                  0.0,
        "current_asset":         "LIQUIDO",
        "entry_price":           0.0,     # da Alpaca (avg_entry_price), solo display
        "position_pnl_pct":      None,    # da Alpaca (unrealized_plpc), solo display
        "institutional_verdict": "NEUTRAL",
        "bridgewater_risk":      "SAFE",
        "ai_score":              0,
        "ai_details":            {},
        "daily_start_equity":    0.0,
        "circuit_breaker":       False,
        "operations_today":      0,
        "partial_profit_done":   {},      # {symbol: True} — dedup prese di profitto
        "cold_start_hold":       False,   # Fase 3: sospensione nuovi ingressi/swap dopo cold-start mid-day
    }

    def __init__(self, path):
        self._path     = path
        self._mem_lock = threading.Lock()
        self._io_lock  = threading.Lock()
        self._state    = self._load()

    # ---- caricamento / persistenza -----------------------------------

    def _load(self):
        base = copy.deepcopy(self.DEFAULTS)
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    base.update(json.load(f))
                print("✅ State file caricato (cache dashboard/log).")
            except json.JSONDecodeError:
                backup = self._path + f".corrupt_{int(time.time())}"
                os.rename(self._path, backup)
                print(f"⚠️ State file corrotto — backup in {backup}, reset ai default.")
            except Exception as e:
                print(f"⚠️ Errore lettura state: {e}")
        return base

    def _serialize(self):
        """DA CHIAMARE dentro mem_lock: produce uno snapshot serializzabile."""
        return json.loads(json.dumps(self._state, default=str))

    def _write(self, snapshot):
        """Scrittura atomica su disco — FUORI dal mem_lock."""
        with self._io_lock:
            try:
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(snapshot, f, indent=2)
                os.replace(tmp, self._path)
            except Exception as e:
                print(f"❌ Errore salvataggio state: {e}")

    # ---- API pubblica --------------------------------------------------

    def snapshot(self):
        """Copia consistente e indipendente dell'intero stato."""
        with self._mem_lock:
            return self._serialize()

    def get(self, key, default=None):
        with self._mem_lock:
            return copy.deepcopy(self._state.get(key, default))

    def update(self, updates=None, log_msg=None, persist=True):
        """Aggiorna campi e/o aggiunge una voce di log, poi persiste lo snapshot."""
        with self._mem_lock:
            if updates:
                self._state.update(updates)
            if log_msg:
                self._append_log(log_msg)
            snap = self._serialize()
        if persist:
            self._write(snap)

    def mutate(self, fn, persist=True):
        """Modifica arbitraria dello stato sotto lock: fn(state_dict)."""
        with self._mem_lock:
            fn(self._state)
            snap = self._serialize()
        if persist:
            self._write(snap)

    def increment(self, key, amount=1):
        self.mutate(lambda s: s.__setitem__(key, s.get(key, 0) + amount))

    def _append_log(self, msg):
        """SOLO dentro mem_lock."""
        clean = str(msg).replace('*', '').replace('_', '')
        self._state["log"].insert(0, f"[{time.strftime('%H:%M:%S')}] {clean}")
        self._state["log"] = self._state["log"][:100]


store = StateStore(STATE_FILE)

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM — notifiche e comandi (nessun lock tenuto durante l'invio)
# ══════════════════════════════════════════════════════════════════════

def send_telegram(msg):
    """Prima aggiorna il log (lock brevissimo), POI invia sulla rete senza lock."""
    store.update(log_msg=msg)
    if bot is None:
        return
    try:
        bot.send_message(CHAT_ID, f"🏍️ *AlphaMoto v6.1*\n\n{msg}", parse_mode='Markdown')
    except Exception as e:
        print(f"Errore Telegram: {e}")


def _check_auth(message):
    if str(message.chat.id) != str(CHAT_ID):
        try:
            bot.send_message(
                CHAT_ID,
                f"🚨 *ACCESSO NON AUTORIZZATO*\n\nChat ID: `{message.chat.id}`\n"
                f"Username: @{message.from_user.username or 'sconosciuto'}\nComando: {message.text}",
                parse_mode='Markdown'
            )
        except Exception:
            pass
        return False
    return True


if bot:

    @bot.message_handler(commands=['start', 'status'])
    def handle_status(message):
        if not _check_auth(message):
            return
        s = store.snapshot()  # lettura consistente, lock rilasciato subito
        equity, cash = s["total_equity"], s["cash"]
        daily_start  = s.get("daily_start_equity") or equity
        daily_pnl    = equity - daily_start if daily_start > 0 else 0
        daily_pct    = (daily_pnl / daily_start * 100) if daily_start > 0 else 0
        # is_market_open è cachato nel broker: nessun burst di chiamate REST
        status_icon = ("🔴 CIRCUIT BREAKER ATTIVO" if s["circuit_breaker"]
                       else ("🟢 Operativo" if broker.is_market_open() else "🌙 Standby"))
        hold_note = ("\n🧊 *Nuovi ingressi/swap SOSPESI* (cold-start) — usa /confirm\\_resume"
                     if s.get("cold_start_hold") else "")
        msg = (
            f"📊 *REPORT LIVE*\n\n"
            f"💰 Capitale: `${equity:,.2f}`\n"
            f"💵 Cash: `${cash:,.2f}`\n"
            f"📈 P&L Oggi: `{'+' if daily_pnl >= 0 else ''}{daily_pnl:,.2f}$ ({daily_pct:+.2f}%)`\n\n"
            f"🏦 Asset: `{s['current_asset']}`\n"
            f"🧠 Score AI: `{s['ai_score']}/100`\n"
            f"⚡ Verdetto: `{s['institutional_verdict']}`\n"
            f"⚠️ Rischio: `{s['bridgewater_risk']}`\n\n"
            f"🔄 Operazioni oggi: `{s['operations_today']}`\n"
            f"🕒 Stato: {status_icon}{hold_note}\n"
            f"⏰ Aggiornato: `{time.strftime('%H:%M:%S')}`"
        )
        bot.reply_to(message, msg, parse_mode='Markdown')

    @bot.message_handler(commands=['log'])
    def handle_log(message):
        if not _check_auth(message):
            return
        logs = store.get("log", [])[:10]
        bot.reply_to(message, "📋 *ULTIMI 10 LOG*\n\n" + "\n".join(f"`{l}`" for l in logs),
                     parse_mode='Markdown')

    @bot.message_handler(commands=['radar'])
    def handle_radar(message):
        if not _check_auth(message):
            return
        assets = store.get("assets_data", [])
        if not assets:
            bot.reply_to(message, "⏳ Dati radar non ancora disponibili.", parse_mode='Markdown')
            return
        lines = ["📡 *RADAR STRATEGICO (5D)*\n"]
        for a in assets[:7]:
            icon = "🟢" if a['growth'] > 0 else "🔴"
            lines.append(f"{icon} `{a['id']:6s}` ${a['price']:>8.2f}  {a['growth']:>+.2f}%  Score:{a.get('score', 0):>4}")
        bot.reply_to(message, "\n".join(lines), parse_mode='Markdown')

    @bot.message_handler(commands=['panic'])
    def handle_panic(message):
        """Emergenza: liquida tutto. La posizione viene chiesta ad ALPACA, non al JSON."""
        if not _check_auth(message):
            return
        active_ticker, _ = broker.get_open_position()   # Source of Truth
        if active_ticker is None:
            bot.reply_to(message, "ℹ️ Nessuna posizione aperta da liquidare.", parse_mode='Markdown')
            return
        bot.reply_to(message, f"🚨 *PANIC SELL* in esecuzione su `{active_ticker}`...", parse_mode='Markdown')

        if not trade_lock.acquire(timeout=90):
            bot.reply_to(message, "❌ Trading engine occupato: riprova tra pochi secondi.", parse_mode='Markdown')
            return
        try:
            success = broker.sell_all_asset(active_ticker)
        finally:
            trade_lock.release()

        if success:
            store.update({"current_asset": "LIQUIDO", "entry_price": 0.0,
                          "position_pnl_pct": None, "circuit_breaker": True})
            send_telegram(f"🚨 *PANIC SELL ESEGUITO*\n\nAsset `{active_ticker}` liquidato.\nCircuit breaker attivato.")
        else:
            bot.reply_to(message, "❌ Errore durante la vendita. Controlla Alpaca.", parse_mode='Markdown')

    @bot.message_handler(commands=['reset_cb'])
    def handle_reset_cb(message):
        if not _check_auth(message):
            return
        store.update({"circuit_breaker": False}, log_msg="🔓 Circuit breaker resettato manualmente")
        bot.reply_to(message, "✅ Circuit breaker disattivato. Trading ripreso.", parse_mode='Markdown')

    @bot.message_handler(commands=['confirm_resume'])
    def handle_confirm_resume(message):
        """
        FASE 3 — Sblocca nuovi ingressi/swap dopo un cold-start a mercato
        aperto. Stop-loss e take-profit non sono mai stati toccati da questo
        hold: qui si conferma solo di aver verificato manualmente lo stato
        del portafoglio prima di lasciare che il bot apra nuove posizioni.
        """
        if not _check_auth(message):
            return
        if not store.get("cold_start_hold", False):
            bot.reply_to(message, "ℹ️ Nessuna sospensione da cold-start attualmente attiva.", parse_mode='Markdown')
            return
        store.update({"cold_start_hold": False},
                     log_msg="✅ Cold-start hold rimosso manualmente via /confirm_resume")
        bot.reply_to(message, "✅ Confermato — nuovi ingressi/swap riabilitati.", parse_mode='Markdown')

# ══════════════════════════════════════════════════════════════════════
# ANALISI DI MERCATO — dati nativi Alpaca (yfinance eliminato)
# ══════════════════════════════════════════════════════════════════════

def analyze_assets():
    """
    Un'unica fonte dati: barre GIORNALIERE Alpaca per growth 5D + indicatori AI
    (stesso timeframe → nessuna dissonanza), ultimo trade per il prezzo corrente.
    """
    bars_map = broker.get_daily_bars(ASSETS, lookback_days=150)
    if not bars_map:
        return []
    latest = broker.get_latest_prices(ASSETS) or {}

    perf_list = []
    for ticker in ASSETS:
        try:
            hist = bars_map.get(ticker)
            if hist is None or len(hist) < 50:   # allineato alla soglia AlphaIntelligence (SMA50)
                continue

            closes = hist['Close']
            curr   = float(latest.get(ticker, closes.iloc[-1]))
            ref    = float(closes.iloc[-5]) if len(closes) >= 5 else float(closes.iloc[0])
            growth_5d = ((curr - ref) / ref) * 100

            summary = AlphaIntelligence(hist).get_summary()
            if "error" in summary:
                continue

            perf_list.append({
                "id":           ticker,
                "price":        round(curr, 2),
                "growth":       round(growth_5d, 2),
                "score":        summary.get("score", 0),
                "verdict":      summary.get("verdict", "NEUTRAL"),
                "risk":         summary.get("risk", "SAFE"),
                "rsi":          summary.get("rsi", 50),
                "macd_cross":   summary.get("macd_cross", False),
                "volume_ratio": summary.get("volume_ratio", 1.0),
                "size_mult":    summary.get("size_multiplier", 0.9),
                "momentum_5d":  summary.get("momentum_5d", 0),
            })
        except Exception as e:
            print(f"[ANALISI] Errore su {ticker}: {e}")
            continue

    perf_list.sort(key=lambda x: x['score'], reverse=True)
    return perf_list

# ══════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT — decisioni da dati Alpaca, ordini sotto trade_lock
# ══════════════════════════════════════════════════════════════════════

def reset_daily_state(equity):
    """
    Reset a inizio giornata. NOTA (Fase 2): daily_start_equity qui è solo un
    seed/cache per il fallback degradato e la dashboard — l'unica fonte
    autorevole per il circuit breaker è resolve_daily_start_equity() (Alpaca).
    """
    store.update({
        "daily_start_equity":  equity,
        "circuit_breaker":     False,
        "operations_today":    0,
        "partial_profit_done": {},
    }, log_msg=f"📅 Nuova sessione — Equity di partenza (seed locale): ${equity:,.2f}")


# Throttle allarmi di liquidazione fallita: max 1 ogni 10 minuti (no spam).
_cb_fail_alarm_ts = 0.0


def _liquidate_for_circuit_breaker(active_ticker):
    """
    FASE 1.3 — Liquida la posizione sotto trade_lock quando il CB scatta.
    Ritorna True se siamo liquidi (o lo eravamo già).
    """
    global _cb_fail_alarm_ts
    if not active_ticker:
        return True

    ok = False
    if trade_lock.acquire(timeout=90):
        try:
            ok = broker.sell_all_asset(active_ticker)
        finally:
            trade_lock.release()

    if ok:
        store.update({"current_asset": "LIQUIDO", "entry_price": 0.0, "position_pnl_pct": None},
                     log_msg=f"🔴 CB: posizione {active_ticker} liquidata")
        store.increment("operations_today")
        send_telegram(
            f"🔴 *CIRCUIT BREAKER*\n\nPosizione `{active_ticker}` LIQUIDATA.\n"
            f"Portafoglio in sicurezza (LIQUIDO) fino al /reset\\_cb."
        )
    else:
        now = time.time()
        if now - _cb_fail_alarm_ts > 600:
            _cb_fail_alarm_ts = now
            send_telegram(
                f"🆘 *CRITICO: LIQUIDAZIONE CB FALLITA*\n\nCircuit breaker attivo ma la vendita di "
                f"`{active_ticker}` è FALLITA.\n⚠️ INTERVENTO MANUALE RICHIESTO su Alpaca.\n"
                f"Ritenterò a ogni ciclo."
            )
    return ok


# Throttle allarme "baseline giornaliera irraggiungibile" (no spam).
_daily_baseline_alarm_ts = 0.0


def resolve_daily_start_equity():
    """
    FASE 2 (estensione Source-of-Truth, pre-Fase 3) — la baseline per il
    circuit breaker giornaliero (-10%) viene SEMPRE richiesta ad Alpaca
    (get_portfolio_history → base_value), MAI fidandosi di state.json:
    un redeploy Render (o qualunque riavvio) cancella il filesystem effimero
    e un valore stantio o azzerato falserebbe silenziosamente il calcolo.

    Ritorna (valore, degraded):
      - (base_value reale da Alpaca, False)               → caso normale
      - (ultimo valore noto in cache locale, True)         → Alpaca irraggiungibile
      - (None, True)                                       → nessun dato disponibile
    """
    global _daily_baseline_alarm_ts
    val = broker.get_daily_start_equity()
    if val is not None and val > 0:
        store.update({"daily_start_equity": round(val, 2)})  # cache per il solo fallback/display
        return val, False

    cached = store.get("daily_start_equity", 0.0)
    if cached and cached > 0:
        return cached, True

    now = time.time()
    if now - _daily_baseline_alarm_ts > 600:
        _daily_baseline_alarm_ts = now
        send_telegram(
            "⚠️ *ATTENZIONE*\n\nImpossibile determinare la baseline di equity giornaliera "
            "(né da Alpaca né da cache locale).\nIl circuit breaker giornaliero NON è "
            "verificabile in questo ciclo — resterà silenzioso finché Alpaca non risponde."
        )
    return None, True


def check_circuit_breaker(equity, active_ticker):
    """
    Ritorna True se il trading è sospeso.
    FASE 1.3: la violazione del limite giornaliero LIQUIDA la posizione, non la
    lascia esposta al mercato. Se la vendita fallisce, ritenta a ogni ciclo.
    FASE 2: la baseline giornaliera è risolta da Alpaca (resolve_daily_start_equity),
    mai da uno state.json che un redeploy può azzerare.
    """
    s = store.snapshot()
    if s["circuit_breaker"]:
        # CB già attivo: se siamo ancora (o di nuovo) investiti, ritenta la liquidazione.
        _liquidate_for_circuit_breaker(active_ticker)
        return True

    daily_start, degraded = resolve_daily_start_equity()
    if daily_start is None:
        # Allarme già inviato da resolve_daily_start_equity: non possiamo
        # decidere in modo affidabile, non blocchiamo un trading altrimenti sano.
        return False

    daily_pct = ((equity - daily_start) / daily_start) * 100
    if daily_pct <= DAILY_LOSS_LIMIT_PCT:
        store.update({"circuit_breaker": True})
        degraded_note = "\n⚠️ _baseline da cache locale (Alpaca non raggiungibile)_" if degraded else ""
        send_telegram(
            f"🔴 *CIRCUIT BREAKER ATTIVATO*\n\n"
            f"Perdita giornaliera: `{daily_pct:.2f}%`\nLimite: `{DAILY_LOSS_LIMIT_PCT}%`{degraded_note}\n\n"
            f"Liquidazione posizione in corso — trading sospeso fino al /reset\\_cb manuale."
        )
        _liquidate_for_circuit_breaker(active_ticker)
        return True
    return False


def resolve_position_pnl(active_ticker, pos_details):
    """
    FASE 1.2 — Il P&L per lo stop-loss non deve MAI mancare in silenzio.
    Ritorna (pnl_pct, degraded):
      - pnl reale da Alpaca (pos_details)            → (pnl, False)
      - STIMA da ultimo prezzo + entry_price cache   → (pnl, True)
      - completamente ciechi                          → (None, True)
    """
    if pos_details is not None:
        return pos_details["unrealized_plpc"], False

    # Modalità degradata: get_position ha fallito, tentiamo una stima
    # indipendente (endpoint market-data diverso da quello posizioni).
    entry  = store.get("entry_price", 0.0)
    prices = broker.get_latest_prices([active_ticker]) or {}
    last   = prices.get(active_ticker)
    if entry and entry > 0 and last:
        return ((last - entry) / entry) * 100, True
    return None, True


def check_stop_loss(active_ticker, pnl_pct, degraded=False):
    """
    Stop-loss su P&L reale o stimato (degraded). Ritorna True se ha venduto.
    FASE 1.2: una vendita fallita genera un allarme CRITICO, mai silenzio.
    """
    if pnl_pct is None or pnl_pct > STOP_LOSS_PCT:
        return False

    degraded_note = "\n⚠️ _P&L STIMATO — endpoint posizioni Alpaca non disponibile_" if degraded else ""
    send_telegram(
        f"🛑 *STOP-LOSS ESEGUITO*\n\nAsset: `{active_ticker}`\n"
        f"Perdita posizione: `{pnl_pct:.2f}%`\nSoglia: `{STOP_LOSS_PCT}%`{degraded_note}\n\nLiquidazione in corso..."
    )
    with trade_lock:
        success = broker.sell_all_asset(active_ticker)
    if success:
        store.update({"current_asset": "LIQUIDO", "entry_price": 0.0, "position_pnl_pct": None})
        store.increment("operations_today")
    else:
        send_telegram(
            f"🆘 *CRITICO: STOP-LOSS FALLITO*\n\nLa vendita di `{active_ticker}` NON è riuscita.\n"
            f"⚠️ INTERVENTO MANUALE RICHIESTO su Alpaca (o usa /panic).\nRitenterò al prossimo ciclo."
        )
    return success


def check_partial_profit(active_ticker, pos_details):
    if pos_details is None:
        return False
    if store.get("partial_profit_done", {}).get(active_ticker):
        return False
    pnl_pct = pos_details["unrealized_plpc"]
    if pnl_pct < PARTIAL_PROFIT_PCT:
        return False

    send_telegram(
        f"💰 *TAKE PROFIT PARZIALE*\n\nAsset: `{active_ticker}`\n"
        f"Profitto posizione: `+{pnl_pct:.2f}%`\n"
        f"Vendita del {PARTIAL_PROFIT_FRAC*100:.0f}% per realizzare parte del guadagno."
    )
    with trade_lock:
        success = broker.sell_partial(active_ticker, percentage=PARTIAL_PROFIT_FRAC)
    if success:
        store.mutate(lambda s: s["partial_profit_done"].__setitem__(active_ticker, True))
        store.update(log_msg=f"💰 Take profit parziale: {active_ticker} a +{pnl_pct:.2f}%")
        store.increment("operations_today")
    return success

# ══════════════════════════════════════════════════════════════════════
# ESECUZIONE TRADE (sequenze atomiche sotto trade_lock)
# ══════════════════════════════════════════════════════════════════════

def execute_entry(best, cash):
    size_mult = best.get("size_mult", 0.90)
    buy_power = cash * 0.95 * size_mult
    with trade_lock:
        success = broker.buy_asset(best["id"], buy_power)
    if not success:
        return False
    # Entry price REALE da Alpaca, non il prezzo teorico dell'analisi
    details = broker.get_position_details(best["id"])
    store.update({
        "current_asset": best["id"],
        "entry_price":   details["avg_entry"] if details else best["price"],
    }, log_msg=f"🎯 ACQUISTO: {best['id']} Score:{best['score']} Verdict:{best['verdict']}")
    store.increment("operations_today")
    send_telegram(
        f"🎯 *INGRESSO A MERCATO*\n\nAsset: `{best['id']}`\nValore: `${buy_power:,.2f}`\n"
        f"Score AI: `{best['score']}/100`\nVerdetto: `{best['verdict']}`\n"
        f"RSI: `{best.get('rsi', 'N/A')}`\nVolume: `{best.get('volume_ratio', 1):.1f}x`\n"
        f"MACD Cross: `{'✅' if best.get('macd_cross') else '❌'}`"
    )
    return True


def execute_swap(active_ticker, target, reason_log, telegram_msg):
    """Sequenza sell→buy atomica rispetto a panic/stop-loss concorrenti."""
    with trade_lock:
        if not broker.sell_all_asset(active_ticker):
            return False
        _, new_cash = broker.get_real_portfolio_value()
        size_mult = target.get("size_mult", 0.90)
        buy_ok = broker.buy_asset(target["id"], new_cash * 0.95 * size_mult)

    if not buy_ok:
        # Vendita riuscita ma acquisto fallito: siamo liquidi, va detto.
        store.update({"current_asset": "LIQUIDO", "entry_price": 0.0, "position_pnl_pct": None})
        send_telegram(f"⚠️ Swap incompleto: `{active_ticker}` venduto ma acquisto `{target['id']}` fallito. Portafoglio LIQUIDO.")
        return False

    details = broker.get_position_details(target["id"])
    store.mutate(lambda s: s["partial_profit_done"].pop(active_ticker, None))
    store.update({
        "current_asset": target["id"],
        "entry_price":   details["avg_entry"] if details else target["price"],
    }, log_msg=reason_log)
    store.increment("operations_today")
    send_telegram(telegram_msg)
    return True

# ══════════════════════════════════════════════════════════════════════
# MOTORE CENTRALE
# ══════════════════════════════════════════════════════════════════════

def update_logic():
    send_telegram(
        "✅ *Motore v6.1 Online*\n\n"
        "🎯 Modalità: Aggressiva\n"
        f"🛑 Stop-Loss attivo: {STOP_LOSS_PCT}%\n"
        f"💰 Take Profit parziale: +{PARTIAL_PROFIT_PCT}%\n"
        f"🔴 Circuit Breaker: {DAILY_LOSS_LIMIT_PCT}% giornaliero\n"
        "📡 Dati: Alpaca Market Data (yfinance rimosso)\n"
        f"📡 Asset: {' | '.join(ASSETS)}"
    )

    market_was_closed  = not broker.is_market_open()
    last_report_time   = time.time()
    last_analysis_time = 0.0
    anomaly_notified   = False
    blind_alarm_sent    = False   # Fase 1.2: dedup allarme "volo cieco"
    degraded_alarm_sent = False   # Fase 1.2: dedup allarme P&L stimato

    # ── FASE 3 — Cold-start safety net ───────────────────────────────────
    # Se il processo (ri)parte con il mercato GIÀ aperto (tipico di un
    # redeploy Render mid-day), il ramo "APERTURA / AVVIO" più sotto non
    # scatterà mai (richiede una transizione chiuso→aperto), quindi
    # reset_daily_state() non viene chiamato: è proprio lo scenario in cui
    # state.json potrebbe essere stato azzerato dal filesystem effimero e
    # circuit_breaker potrebbe mentire (falso False). Non blocchiamo
    # stop-loss/take-profit (leggono Alpaca in tempo reale, sono affidabili
    # comunque): sospendiamo SOLO nuovi ingressi/swap finché l'operatore
    # non conferma manualmente con /confirm_resume.
    if not market_was_closed:
        store.update(
            {"cold_start_hold": True},
            log_msg="🧊 Cold-start a mercato aperto: nuovi ingressi/swap sospesi in attesa di /confirm_resume"
        )
        send_telegram(
            "🧊 *AVVIO A MERCATO APERTO*\n\n"
            "Il bot è ripartito con il mercato già aperto (probabile redeploy).\n"
            "Lo stato locale, circuit breaker incluso, potrebbe essere stato azzerato.\n\n"
            "🛡️ Stop-loss e take-profit restano ATTIVI (dati Alpaca in tempo reale).\n"
            "⛔ Nuovi ingressi/swap SOSPESI.\n\n"
            "Verifica lo stato del portafoglio (/status, controlla anche Alpaca "
            "direttamente) e poi invia /confirm\\_resume per riabilitare il trading."
        )
    else:
        store.update({"cold_start_hold": False})

    while True:
        try:
            # ── 1. SYNC DA ALPACA (Source of Truth, ogni ciclo) ──────────
            equity, cash = broker.get_real_portfolio_value()
            if equity == 0.0:
                print("[WARNING] equity 0.0 — skip ciclo per sicurezza")
                time.sleep(30)
                continue

            active_ticker, is_anomaly = broker.get_open_position()
            pos_details = broker.get_position_details(active_ticker) if active_ticker else None

            if is_anomaly and not anomaly_notified:
                anomaly_notified = True
                send_telegram("⚠️ *ANOMALIA PORTAFOGLIO*\n\nTrovate più posizioni aperte su Alpaca!\nVerifica manuale necessaria.")
            elif not is_anomaly:
                anomaly_notified = False

            # Cache per dashboard: aggiornamento in memoria, lock di microsecondi
            store.update({
                "total_equity":     round(equity, 2),
                "cash":             round(cash, 2),
                "current_asset":    active_ticker or "LIQUIDO",
                "entry_price":      pos_details["avg_entry"] if pos_details else 0.0,
                "position_pnl_pct": pos_details["unrealized_plpc"] if pos_details else None,
            })

            # ── 2. ORARI DI MERCATO ──────────────────────────────────────
            if not broker.is_market_open():
                if not market_was_closed:
                    send_telegram(
                        f"🔔 *CAMPANELLA DI CHIUSURA*\n\nWall Street ha chiuso.\n"
                        f"Capitale finale: `${equity:,.2f}`\nIl bot entra in standby. A domani! 🌙"
                    )
                    market_was_closed = True
                time.sleep(60)
                continue

            # ── 3. APERTURA / AVVIO A MERCATO GIÀ APERTO ─────────────────
            if market_was_closed:
                reset_daily_state(equity)
                send_telegram(
                    f"🔔 *MARKET OPEN!*\n\nCapitale: `${equity:,.2f}`\nCash: `${cash:,.2f}`\n"
                    f"Posizione: `{active_ticker or 'LIQUIDO'}`\n\nAvvio analisi mercato..."
                )
                market_was_closed = False
                last_report_time  = time.time()
            # NOTA (Fase 2): rimosso il vecchio ramo "riavvio senza baseline
            # giornaliera → reset_daily_state(equity)". Era una toppa per lo
            # stesso problema che risolviamo ora: azzerava anche circuit_breaker
            # ad ogni riavvio Render mid-day con state.json mancante. La baseline
            # ora arriva sempre da Alpaca via resolve_daily_start_equity(): un
            # riavvio non richiede più nessun reset locale a mercato aperto.

            # ── 4. CIRCUIT BREAKER (con liquidazione, Fase 1.3) ──────────
            if check_circuit_breaker(equity, active_ticker):
                time.sleep(60)
                continue

            # ── 5. REPORT BIORARIO ───────────────────────────────────────
            now = time.time()
            if (now - last_report_time) >= REPORT_INTERVAL:
                s = store.snapshot()
                daily_pnl = equity - (s.get("daily_start_equity") or equity)
                send_telegram(
                    f"⏱️ *AGGIORNAMENTO BIORARIO*\n\n"
                    f"Capitale: `${equity:,.2f}` ({'+' if daily_pnl >= 0 else ''}{daily_pnl:,.2f}$)\n"
                    f"Posizione: `{active_ticker or 'LIQUIDO'}`\n"
                    f"Verdetto: `{s['institutional_verdict']}`\n"
                    f"Operazioni oggi: `{s['operations_today']}`"
                )
                last_report_time = now

            # ── 6. STOP-LOSS / TAKE PROFIT (fail-safe, Fase 1.2) ─────────
            if active_ticker:
                pnl_pct, degraded = resolve_position_pnl(active_ticker, pos_details)

                if pnl_pct is None:
                    # VOLO CIECO: né P&L reale né stima disponibili.
                    if not blind_alarm_sent:
                        blind_alarm_sent = True
                        send_telegram(
                            f"🆘 *ALLARME CRITICO: VOLO CIECO*\n\nPosizione `{active_ticker}` aperta ma "
                            f"IMPOSSIBILE leggere il P&L da Alpaca (posizioni E market data ko).\n"
                            f"🛑 Lo stop-loss NON è verificabile.\n"
                            f"Retry aggressivo ogni 15s. Se persiste, valuta /panic."
                        )
                    time.sleep(15)   # retry aggressivo: salta analisi e trading
                    continue

                if blind_alarm_sent:
                    blind_alarm_sent = False
                    send_telegram(f"✅ Dati P&L ripristinati per `{active_ticker}`. Stop-loss di nuovo operativo.")

                if degraded:
                    # Stima disponibile: stop-loss resta armato, ma avvisa (una volta).
                    store.update({"position_pnl_pct": round(pnl_pct, 2)})
                    if not degraded_alarm_sent:
                        degraded_alarm_sent = True
                        send_telegram(
                            f"⚠️ P&L di `{active_ticker}` in modalità STIMATA "
                            f"(entry cache + ultimo prezzo). Stop-loss comunque attivo."
                        )
                else:
                    degraded_alarm_sent = False

                if check_stop_loss(active_ticker, pnl_pct, degraded):
                    time.sleep(10)
                    continue
                if not degraded:
                    # Il take-profit parziale richiede dati posizione REALI:
                    # in modalità stimata è più prudente non vendere frazioni.
                    check_partial_profit(active_ticker, pos_details)

            # ── 7. ANALISI (ogni ANALYSIS_INTERVAL) ──────────────────────
            if last_analysis_time > 0 and (now - last_analysis_time) < ANALYSIS_INTERVAL:
                time.sleep(30)
                continue
            last_analysis_time = now

            perf_list = analyze_assets()
            if not perf_list:
                print("[ANALISI] Lista asset vuota — skip")
                time.sleep(60)
                continue

            best = perf_list[0]

            # Re-fetch da Alpaca DOPO l'analisi (potrebbe essere passato del tempo)
            active_ticker, _ = broker.get_open_position()

            store.update({
                "assets_data":           perf_list,
                "institutional_verdict": best["verdict"],
                "bridgewater_risk":      best["risk"],
                "ai_score":              best["score"],
                "ai_details": {
                    "best_asset":   best["id"],
                    "rsi":          best.get("rsi"),
                    "volume_ratio": best.get("volume_ratio"),
                    "macd_cross":   best.get("macd_cross"),
                },
            })

            # ── 8. LOGICA DI TRADING ─────────────────────────────────────
            # FASE 3: durante un cold-start hold, l'analisi sopra continua
            # (dashboard/radar restano aggiornati) ma nessun nuovo ordine di
            # ingresso o swap parte finché l'operatore non conferma.
            cold_hold = store.get("cold_start_hold", False)

            # CASO A: liquidi → ingresso
            if cold_hold:
                pass
            elif active_ticker is None and cash > 50.0:
                if best["verdict"] in ("STRONG_BUY", "BUY", "WEAK_BUY") and best["risk"] != "HIGH_RISK":
                    execute_entry(best, cash)

            # CASO B: posizione aperta → swap
            elif active_ticker:
                active_data = next((x for x in perf_list if x["id"] == active_ticker), None)
                if active_data:
                    score_gap  = best["score"] - active_data["score"]
                    growth_gap = best["growth"] - active_data["growth"]

                    emergency_gold = (
                        (active_data.get("risk") == "HIGH_RISK"
                         or active_data["verdict"] == "SELL/SWAP_TO_GOLD")
                        and active_ticker != "GLD"
                    )

                    should_swap = (
                        best["id"] != active_ticker
                        and score_gap >= SWAP_SCORE_GAP
                        and growth_gap > SWAP_THRESHOLD
                        and best["verdict"] in ("STRONG_BUY", "BUY")
                        and best["risk"] != "HIGH_RISK"
                        and not broker.order_in_progress
                    )

                    if emergency_gold:
                        gold = next((x for x in perf_list if x["id"] == "GLD"),
                                    {"id": "GLD", "price": 0.0, "size_mult": 0.95})
                        execute_swap(
                            active_ticker, gold,
                            reason_log=f"⚠️ SWAP EMERGENZA → GLD (risk: {active_data.get('risk')})",
                            telegram_msg=(
                                f"⚠️ *SWAP EMERGENZA → ORO*\n\nVenduto: `{active_ticker}`\n"
                                f"Motivo: `{active_data.get('risk', 'SELL signal')}`\nCash protetto in GLD."
                            ),
                        )
                    elif should_swap:
                        execute_swap(
                            active_ticker, best,
                            reason_log=f"🔄 SWAP: {active_ticker} → {best['id']} (ΔScore:{score_gap})",
                            telegram_msg=(
                                f"🔄 *SWAP AGGRESSIVO*\n\n"
                                f"Venduto: `{active_ticker}` (Score:{active_data['score']})\n"
                                f"Acquistato: `{best['id']}` (Score:{best['score']})\n"
                                f"ΔScore: `+{score_gap}`\nΔGrowth: `+{growth_gap:.2f}%`\n"
                                f"MACD Cross: `{'✅' if best.get('macd_cross') else '❌'}`"
                            ),
                        )

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Errore Critico nel loop: {e}")
            traceback.print_exc()

        time.sleep(60)

# ══════════════════════════════════════════════════════════════════════
# API DASHBOARD WEB
# ══════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_dashboard():
    """La dashboard vive in dashboard.html: main.py resta leggibile."""
    path = os.path.join(BASE_DIR, "dashboard.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return Response(f.read(), mimetype='text/html')
    return Response("<h1>AlphaMoto</h1><p>dashboard.html non trovato.</p>", mimetype='text/html')


@app.route('/api/state')
def get_state():
    token = os.getenv("DASHBOARD_TOKEN", "")
    if token:
        req_token = request.headers.get("X-API-Key") or request.args.get("token", "")
        if req_token != token:
            return jsonify({"error": "Unauthorized"}), 401

    snap = store.snapshot()  # nessun lock tenuto durante la serializzazione HTTP
    snap["wallet"]           = snap.get("total_equity", 0.0)
    snap["invested"]         = snap["wallet"] - snap.get("cash", 0.0)
    snap["market_open"]      = broker.is_market_open()      # clock cachato 10s
    snap["minutes_to_close"] = broker.minutes_to_close()

    # Fase 2: baseline giornaliera risolta live (Alpaca, cache 60s) — mai il
    # valore grezzo di state.json, che un redeploy può aver azzerato.
    daily_start, _degraded = resolve_daily_start_equity()
    if daily_start:
        snap["daily_start_equity"] = round(daily_start, 2)

    return jsonify(snap)


@app.route('/health')
def health():
    return jsonify({"status": "ok", "ts": int(time.time())}), 200

# ══════════════════════════════════════════════════════════════════════
# AVVIO MOTORE (trading loop + Telegram) — a livello di modulo
# ══════════════════════════════════════════════════════════════════════
# FIX CRITICO: con `gunicorn main:app`, questo modulo viene IMPORTATO
# (non eseguito come script), quindi `__name__` non è mai '__main__'. Il
# vecchio codice avviava i thread di trading e Telegram SOLO dentro quel
# blocco: su un deploy gunicorn il bot serviva la dashboard ma non tradava
# né rispondeva su Telegram — senza nessun errore visibile nei log.
#
# Ora l'avvio del motore è una funzione a sé, richiamata SEMPRE al
# caricamento del modulo (sia sotto gunicorn sia con `python main.py`).
# Un lock su file (flock) garantisce che, anche con più worker/processi
# gunicorn, il motore parta una sola volta: gli altri worker servono solo
# richieste HTTP. Con un solo worker (oggi il default su Render) il lock
# è comunque innocuo — costa un file aperto in più.

try:
    import fcntl
    _HAS_FLOCK = True
except ImportError:
    # Piattaforme senza flock (es. sviluppo locale su Windows): nessuna
    # protezione multi-processo, ma il motore può comunque partire.
    _HAS_FLOCK = False

_ENGINE_LOCK_PATH = os.path.join(os.getenv("TMPDIR", "/tmp"), "alphamoto_engine.lock")
_engine_lock_fh   = None  # tenuto aperto per tutta la vita del processo: il lock si rilascia da solo alla sua chiusura


def _acquire_engine_lock():
    """True se QUESTO processo ha in mano il lock esclusivo del motore."""
    global _engine_lock_fh
    if not _HAS_FLOCK:
        return True
    try:
        fh = open(_ENGINE_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _engine_lock_fh = fh
        return True
    except (IOError, OSError):
        return False


def start_background_engine():
    """
    Avvia il loop di trading e il polling Telegram UNA sola volta per
    container, indipendentemente da come il processo è stato lanciato.
    """
    if not _acquire_engine_lock():
        print("ℹ️ Motore già avviato da un altro worker in questo container — questo processo serve solo la dashboard.")
        return
    threading.Thread(target=update_logic, daemon=True, name="alphamoto-trading-loop").start()
    if bot:
        threading.Thread(target=lambda: bot.infinity_polling(), daemon=True, name="alphamoto-telegram-poll").start()
    print("🚀 Motore di trading + bot Telegram avviati.")


# Interruttore esplicito per test/tooling che importano main.py senza voler
# avviare il motore reale (impostato da tests/conftest.py). In produzione
# questa variabile non è definita, quindi il motore parte sempre.
if os.getenv("ALPHAMOTO_DISABLE_ENGINE", "") != "1":
    start_background_engine()
else:
    print("⏸️ ALPHAMOTO_DISABLE_ENGINE=1 — motore non avviato (import per test/tooling).")

# ══════════════════════════════════════════════════════════════════════
# ENTRYPOINT — solo per l'esecuzione diretta (`python main.py`)
# ══════════════════════════════════════════════════════════════════════
# Con gunicorn questo blocco non gira mai: il server HTTP è gunicorn stesso.
# Con `python main.py` serve comunque un server: qui usiamo il dev server
# Flask, che per un solo utente (dashboard personale) è sufficiente.

if __name__ == '__main__':
    print("🚀 AlphaMoto Core v6.1 avviato su http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
