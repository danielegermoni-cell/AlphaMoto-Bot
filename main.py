"""
AlphaMoto v6.0 — Refactor architetturale.

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
        "log":                   ["🚀 AlphaMoto v6.0 Avviato"],
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
        bot.send_message(CHAT_ID, f"🏍️ *AlphaMoto v6.0*\n\n{msg}", parse_mode='Markdown')
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
            f"🕒 Stato: {status_icon}\n"
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
            if hist is None or len(hist) < 15:
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
    store.update({
        "daily_start_equity":  equity,
        "circuit_breaker":     False,
        "operations_today":    0,
        "partial_profit_done": {},
    }, log_msg=f"📅 Nuova sessione — Equity di partenza: ${equity:,.2f}")


def check_circuit_breaker(equity):
    s = store.snapshot()
    if s["circuit_breaker"]:
        return True
    daily_start = s.get("daily_start_equity", 0)
    if daily_start > 0:
        daily_pct = ((equity - daily_start) / daily_start) * 100
        if daily_pct <= DAILY_LOSS_LIMIT_PCT:
            store.update({"circuit_breaker": True})
            send_telegram(
                f"🔴 *CIRCUIT BREAKER ATTIVATO*\n\n"
                f"Perdita giornaliera: `{daily_pct:.2f}%`\nLimite: `{DAILY_LOSS_LIMIT_PCT}%`\n\n"
                f"Trading sospeso fino al /reset\\_cb manuale."
            )
            return True
    return False


def check_stop_loss(active_ticker, pos_details):
    """P&L letto da Alpaca (pos_details). Ritorna True se ha venduto."""
    if pos_details is None:
        return False
    pnl_pct = pos_details["unrealized_plpc"]
    if pnl_pct > STOP_LOSS_PCT:
        return False

    send_telegram(
        f"🛑 *STOP-LOSS ESEGUITO*\n\nAsset: `{active_ticker}`\n"
        f"Perdita posizione: `{pnl_pct:.2f}%`\nSoglia: `{STOP_LOSS_PCT}%`\n\nLiquidazione in corso..."
    )
    with trade_lock:
        success = broker.sell_all_asset(active_ticker)
    if success:
        store.update({"current_asset": "LIQUIDO", "entry_price": 0.0, "position_pnl_pct": None})
        store.increment("operations_today")
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
        "✅ *Motore v6.0 Online*\n\n"
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
            elif store.get("daily_start_equity", 0.0) <= 0.0:
                # Riavvio a mercato aperto senza baseline giornaliera
                reset_daily_state(equity)

            # ── 4. CIRCUIT BREAKER ───────────────────────────────────────
            if check_circuit_breaker(equity):
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

            # ── 6. STOP-LOSS / TAKE PROFIT (ogni minuto, P&L da Alpaca) ──
            if active_ticker:
                if check_stop_loss(active_ticker, pos_details):
                    time.sleep(10)
                    continue
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

            # CASO A: liquidi → ingresso
            if active_ticker is None and cash > 50.0:
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
    return jsonify(snap)


@app.route('/health')
def health():
    return jsonify({"status": "ok", "ts": int(time.time())}), 200

# ══════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    threading.Thread(target=update_logic, daemon=True).start()
    if bot:
        threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    print("🚀 AlphaMoto Core v6.0 avviato su http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
