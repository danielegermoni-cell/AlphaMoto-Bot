import yfinance as yf
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import threading
import time
import json
import os
import telebot
from dotenv import load_dotenv
from AlphaIntelligence import AlphaIntelligence
from AlpacaBroker import AlpacaBroker

# ══════════════════════════════════════════════════════════════════════
# INIZIALIZZAZIONE
# ══════════════════════════════════════════════════════════════════════
load_dotenv()
TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
bot     = telebot.TeleBot(TOKEN)
app     = Flask(__name__, static_folder="static")
CORS(app)
broker  = AlpacaBroker()

# ══════════════════════════════════════════════════════════════════════
# PARAMETRI STRATEGICI — MODALITÀ AGGRESSIVA
# ══════════════════════════════════════════════════════════════════════

# Asset monitorati — aggiunto TQQQ (3x leveraged Nasdaq) e SOXX per semiconduttori
ASSETS = ['SPY', 'QQQ', 'SMH', 'GLD', 'VTI', 'SOXX', 'TQQQ']

# Soglia minima di vantaggio per eseguire uno swap (% di differenza di crescita)
SWAP_THRESHOLD = 1.5        # Abbassato da 2.0 per reagire più rapidamente

# Stop-loss: percentuale di perdita massima tollerata su una singola posizione
STOP_LOSS_PCT = -6.0        # Esce se la posizione perde più del 6%

# Take-profit parziale: vende il 40% della posizione se guadagna X%
PARTIAL_PROFIT_PCT = 8.0    # Realizza il 40% dei profitti a +8%
PARTIAL_PROFIT_DONE = {}    # Tiene traccia delle prese di profitto già eseguite

# Circuit breaker giornaliero: max perdita giornaliera tollerata
DAILY_LOSS_LIMIT_PCT = -10.0
STATE_FILE = "state.json"
state_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════════
# STATO PERSISTENTE
# ══════════════════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                print("✅ State file caricato correttamente.")
                return data
        except json.JSONDecodeError as e:
            # Backup del file corrotto prima di resettare
            backup = STATE_FILE + f".corrupt_{int(time.time())}"
            os.rename(STATE_FILE, backup)
            print(f"⚠️ State file corrotto — backup in {backup} — reset allo stato iniziale.")
        except Exception as e:
            print(f"⚠️ Errore generico lettura state: {e}")

    return {
        "log":                 ["🚀 AlphaMoto v5.0 Avviato"],
        "assets_data":         [],
        "peak_price":          0.0,
        "total_equity":        0.0,
        "cash":                0.0,
        "current_asset":       "LIQUIDO",
        "institutional_verdict":"NEUTRAL",
        "bridgewater_risk":    "SAFE",
        "ai_score":            0,
        "ai_details":          {},
        "daily_start_equity":  0.0,
        "circuit_breaker":     False,
        "operations_today":    0,
        "entry_price":         0.0,    # Prezzo medio di entrata posizione attiva
        "partial_profit_taken":False,
    }

state = load_state()

def save_state():
    """DEVE essere chiamata sempre dentro un blocco with state_lock."""
    try:
        # Scrittura atomica via file temporaneo per evitare corruzione
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"❌ Errore salvataggio state: {e}")

def _log(msg):
    """Aggiunge una voce al log di stato (chiamare DENTRO il lock)."""
    timestamp = time.strftime('%H:%M:%S')
    clean_msg = msg.replace('*', '').replace('_', '')
    state["log"].insert(0, f"[{timestamp}] {clean_msg}")
    if len(state["log"]) > 100:
        state["log"] = state["log"][:100]

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════

def send_telegram(msg):
    try:
        with state_lock:
            _log(msg)
            save_state()
        bot.send_message(CHAT_ID, f"🏍️ *AlphaMoto v5.0*\n\n{msg}", parse_mode='Markdown')
    except Exception as e:
        print(f"Errore Telegram: {e}")

def _check_auth(message):
    if str(message.chat.id) != str(CHAT_ID):
        try:
            # Notifica il proprietario del tentativo non autorizzato
            bot.send_message(
                CHAT_ID,
                f"🚨 *ACCESSO NON AUTORIZZATO*\n\nChat ID: `{message.chat.id}`\nUsername: @{message.from_user.username or 'sconosciuto'}\nComando: {message.text}",
                parse_mode='Markdown'
            )
        except Exception:
            pass
        return False
    return True

# --- Comandi ---

@bot.message_handler(commands=['start', 'status'])
def handle_status(message):
    if not _check_auth(message): return
    with state_lock:
        equity  = state.get("total_equity", 0)
        cash    = state.get("cash", 0)
        asset   = state.get("current_asset", "LIQUIDO")
        verdict = state.get("institutional_verdict", "N/A")
        risk    = state.get("bridgewater_risk", "N/A")
        score   = state.get("ai_score", 0)
        cb      = state.get("circuit_breaker", False)
        ops     = state.get("operations_today", 0)
        daily_start = state.get("daily_start_equity", equity)

    daily_pnl = equity - daily_start if daily_start > 0 else 0
    daily_pnl_pct = (daily_pnl / daily_start * 100) if daily_start > 0 else 0

    status_icon = "🔴 CIRCUIT BREAKER ATTIVO" if cb else ("🟢 Operativo" if broker.is_market_open() else "🌙 Standby")

    msg = (
        f"📊 *REPORT LIVE*\n\n"
        f"💰 Capitale: `${equity:,.2f}`\n"
        f"💵 Cash: `${cash:,.2f}`\n"
        f"📈 P&L Oggi: `{'+' if daily_pnl >= 0 else ''}{daily_pnl:,.2f}$ ({daily_pnl_pct:+.2f}%)`\n\n"
        f"🏦 Asset: `{asset}`\n"
        f"🧠 Score AI: `{score}/100`\n"
        f"⚡ Verdetto: `{verdict}`\n"
        f"⚠️ Rischio: `{risk}`\n\n"
        f"🔄 Operazioni oggi: `{ops}`\n"
        f"🕒 Stato: {status_icon}\n"
        f"⏰ Aggiornato: `{time.strftime('%H:%M:%S')}`"
    )
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['log'])
def handle_log(message):
    if not _check_auth(message): return
    with state_lock:
        logs = state.get("log", [])[:10]
    msg = "📋 *ULTIMI 10 LOG*\n\n" + "\n".join(f"`{l}`" for l in logs)
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['radar'])
def handle_radar(message):
    if not _check_auth(message): return
    with state_lock:
        assets = state.get("assets_data", [])
    if not assets:
        bot.reply_to(message, "⏳ Dati radar non ancora disponibili.", parse_mode='Markdown')
        return
    lines = ["📡 *RADAR STRATEGICO (5D)*\n"]
    for a in assets[:7]:
        icon = "🟢" if a['growth'] > 0 else "🔴"
        lines.append(f"{icon} `{a['id']:6s}` ${a['price']:>8.2f}  {a['growth']:>+.2f}%  Score:{a.get('score',0):>4}")
    bot.reply_to(message, "\n".join(lines), parse_mode='Markdown')

@bot.message_handler(commands=['panic'])
def handle_panic(message):
    """Comando di emergenza: liquida tutto immediatamente."""
    if not _check_auth(message): return
    with state_lock:
        asset = state.get("current_asset", "LIQUIDO")
    if asset != "LIQUIDO":
        bot.reply_to(message, f"🚨 *PANIC SELL* in esecuzione su `{asset}`...", parse_mode='Markdown')
        success = broker.sell_all_asset(asset)
        if success:
            with state_lock:
                state["current_asset"] = "LIQUIDO"
                state["circuit_breaker"] = True
                _log(f"🚨 PANIC SELL manuale: {asset} liquidato")
                save_state()
            send_telegram(f"🚨 *PANIC SELL ESEGUITO*\n\nAsset `{asset}` liquidato manualmente.\nCircuit breaker attivato.")
        else:
            bot.reply_to(message, "❌ Errore durante la vendita. Controlla Alpaca.", parse_mode='Markdown')
    else:
        bot.reply_to(message, "ℹ️ Nessuna posizione aperta da liquidare.", parse_mode='Markdown')

@bot.message_handler(commands=['reset_cb'])
def handle_reset_cb(message):
    """Reimposta il circuit breaker manualmente."""
    if not _check_auth(message): return
    with state_lock:
        state["circuit_breaker"] = False
        _log("🔓 Circuit breaker resettato manualmente")
        save_state()
    bot.reply_to(message, "✅ Circuit breaker disattivato. Trading ripreso.", parse_mode='Markdown')

# ══════════════════════════════════════════════════════════════════════
# MOTORE CENTRALE
# ══════════════════════════════════════════════════════════════════════

def _reset_daily_state(equity):
    """Chiamare all'apertura di ogni nuova sessione di trading."""
    global PARTIAL_PROFIT_DONE
    with state_lock:
        state["daily_start_equity"] = equity
        state["circuit_breaker"]    = False
        state["operations_today"]   = 0
        state["partial_profit_taken"] = False
        PARTIAL_PROFIT_DONE = {}
        _log(f"📅 Nuova sessione — Equity di partenza: ${equity:,.2f}")
        save_state()

def _check_circuit_breaker(equity):
    """Verifica se la perdita giornaliera ha superato il limite."""
    with state_lock:
        daily_start = state.get("daily_start_equity", equity)
        cb          = state.get("circuit_breaker", False)

    if cb:
        return True

    if daily_start > 0:
        daily_pnl_pct = ((equity - daily_start) / daily_start) * 100
        if daily_pnl_pct <= DAILY_LOSS_LIMIT_PCT:
            with state_lock:
                state["circuit_breaker"] = True
                _log(f"🔴 CIRCUIT BREAKER: perdita giornaliera {daily_pnl_pct:.2f}% — trading sospeso")
                save_state()
            send_telegram(
                f"🔴 *CIRCUIT BREAKER ATTIVATO*\n\n"
                f"Perdita giornaliera: `{daily_pnl_pct:.2f}%`\n"
                f"Limite: `{DAILY_LOSS_LIMIT_PCT}%`\n\n"
                f"Trading sospeso fino al /reset\\_cb manuale."
            )
            return True
    return False

def _check_stop_loss(active_ticker, equity):
    """
    Controlla se la posizione attiva ha superato lo stop-loss.
    Ritorna True se ha eseguito una vendita.
    """
    details = broker.get_position_details(active_ticker)
    if details is None:
        return False

    pnl_pct = details["unrealized_plpc"]

    if pnl_pct <= STOP_LOSS_PCT:
        with state_lock:
            _log(f"🛑 STOP-LOSS: {active_ticker} a {pnl_pct:.2f}%")
        send_telegram(
            f"🛑 *STOP-LOSS ESEGUITO*\n\n"
            f"Asset: `{active_ticker}`\n"
            f"Perdita posizione: `{pnl_pct:.2f}%`\n"
            f"Soglia: `{STOP_LOSS_PCT}%`\n\n"
            f"Liquidazione in corso..."
        )
        success = broker.sell_all_asset(active_ticker)
        if success:
            with state_lock:
                state["current_asset"] = "LIQUIDO"
                state["entry_price"] = 0.0
                state["partial_profit_taken"] = False
                save_state()
        return success

    return False

def _check_partial_profit(active_ticker):
    """
    Presa di profitto parziale (40%) se la posizione guadagna >= PARTIAL_PROFIT_PCT.
    Eseguita una sola volta per posizione.
    """
    if PARTIAL_PROFIT_DONE.get(active_ticker):
        return False

    details = broker.get_position_details(active_ticker)
    if details is None:
        return False

    pnl_pct = details["unrealized_plpc"]

    if pnl_pct >= PARTIAL_PROFIT_PCT:
        send_telegram(
            f"💰 *TAKE PROFIT PARZIALE*\n\n"
            f"Asset: `{active_ticker}`\n"
            f"Profitto posizione: `+{pnl_pct:.2f}%`\n"
            f"Vendita del 40% per realizzare parte del guadagno."
        )
        success = broker.sell_partial(active_ticker, percentage=0.40)
        if success:
            PARTIAL_PROFIT_DONE[active_ticker] = True
            with state_lock:
                state["partial_profit_taken"] = True
                _log(f"💰 Take profit parziale: {active_ticker} a +{pnl_pct:.2f}%")
                save_state()
        return success

    return False

def _analyze_assets():
    """
    Scarica i dati di mercato e calcola il ranking degli asset con AI score.
    Ritorna una lista ordinata per score composito.
    """
    raw = yf.download(
        tickers=ASSETS,
        period="3mo",     # 3 mesi per indicatori come MACD e SMA50
        interval="1d",
        progress=False,
        group_by="ticker"
    )

    perf_list = []

    for ticker in ASSETS:
        try:
            # Estrai dati del singolo ticker
            if len(ASSETS) > 1:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                hist = raw[ticker].dropna()
            else:
                hist = raw.dropna()

            if len(hist) < 5:
                continue

            curr  = float(hist['Close'].iloc[-1])
            start = float(hist['Close'].iloc[-5]) if len(hist) >= 5 else float(hist['Close'].iloc[0])
            growth_5d = ((curr - start) / start) * 100

            # Analisi AI
            brain   = AlphaIntelligence(hist)
            summary = brain.get_summary()

            perf_list.append({
                "id":       ticker,
                "price":    round(curr, 2),
                "growth":   round(growth_5d, 2),
                "score":    summary.get("score", 0),
                "verdict":  summary.get("verdict", "NEUTRAL"),
                "risk":     summary.get("risk", "SAFE"),
                "rsi":      summary.get("rsi", 50),
                "macd_cross": summary.get("macd_cross", False),
                "volume_ratio": summary.get("volume_ratio", 1.0),
                "size_mult":  summary.get("size_multiplier", 0.9),
                "momentum_5d": summary.get("momentum_5d", 0),
            })

        except Exception as e:
            print(f"[ANALISI] Errore su {ticker}: {e}")
            continue

    # Ordina per score composito AI (non solo per crescita grezza)
    perf_list.sort(key=lambda x: x['score'], reverse=True)
    return perf_list

def update_logic():
    send_telegram(
        "✅ *Motore v5.0 Online*\n\n"
        "🎯 Modalità: Aggressiva\n"
        "🛑 Stop-Loss attivo: -6%\n"
        "💰 Take Profit parziale: +8%\n"
        "🔴 Circuit Breaker: -10% giornaliero\n"
        "📡 Asset: SPY | QQQ | SMH | GLD | VTI | SOXX | TQQQ"
    )

    market_was_closed = not broker.is_market_open()
    last_report_time  = time.time()
    last_analysis_time = 0  # Forza analisi immediata al primo giro

    while True:
        try:
            # ── 1. Lettura portafoglio (sempre, anche a mercato chiuso) ──
            equity, cash = broker.get_real_portfolio_value()

            # Sicurezza: se equity è 0 probabilmente c'è un errore di rete
            if equity == 0.0:
                print("[WARNING] equity 0.0 — skip ciclo per sicurezza")
                time.sleep(30)
                continue

            active_ticker, is_anomaly = broker.get_open_position()

            if is_anomaly:
                send_telegram(
                    f"⚠️ *ANOMALIA PORTAFOGLIO*\n\n"
                    f"Trovate più posizioni aperte su Alpaca!\n"
                    f"Verifica manuale necessaria."
                )

            with state_lock:
                state["total_equity"]  = round(equity, 2)
                state["cash"]          = round(cash, 2)
                state["current_asset"] = active_ticker or "LIQUIDO"
                save_state()

            # ── 2. Controllo orari di mercato ──
            is_open = broker.is_market_open()

            if not is_open:
                if not market_was_closed:
                    send_telegram(
                        f"🔔 *CAMPANELLA DI CHIUSURA*\n\n"
                        f"Wall Street ha chiuso.\n"
                        f"Capitale finale: `${equity:,.2f}`\n"
                        f"Il bot entra in standby. A domani! 🌙"
                    )
                    market_was_closed = True
                time.sleep(60)
                continue

            # ── 3. Apertura mercato ──
            if market_was_closed:
                _reset_daily_state(equity)
                send_telegram(
                    f"🔔 *MARKET OPEN!*\n\n"
                    f"Capitale: `${equity:,.2f}`\n"
                    f"Cash: `${cash:,.2f}`\n"
                    f"Posizione: `{active_ticker or 'LIQUIDO'}`\n\n"
                    f"Avvio analisi mercato..."
                )
                market_was_closed = False
                last_report_time  = time.time()

            # ── 4. Circuit breaker ──
            if _check_circuit_breaker(equity):
                time.sleep(60)
                continue

            # ── 5. Report biorario ──
            now = time.time()
            if (now - last_report_time) >= 7200:
                with state_lock:
                    verdict = state.get("institutional_verdict", "N/A")
                    ops     = state.get("operations_today", 0)
                    daily_start = state.get("daily_start_equity", equity)
                daily_pnl = equity - daily_start
                send_telegram(
                    f"⏱️ *AGGIORNAMENTO BIORARIO*\n\n"
                    f"Capitale: `${equity:,.2f}` ({'+' if daily_pnl >= 0 else ''}{daily_pnl:,.2f}$)\n"
                    f"Posizione: `{active_ticker or 'LIQUIDO'}`\n"
                    f"Verdetto: `{verdict}`\n"
                    f"Operazioni oggi: `{ops}`"
                )
                last_report_time = now

            # ── 6. Stop-Loss e Take Profit (ogni minuto, per reattività) ──
            if active_ticker:
                if _check_stop_loss(active_ticker, equity):
                    with state_lock:
                        state["operations_today"] = state.get("operations_today", 0) + 1
                        save_state()
                    time.sleep(10)
                    continue

                _check_partial_profit(active_ticker)

            # ── 7. Analisi di mercato (ogni 5 minuti per non spammare le API) ──
            if (now - last_analysis_time) < 300 and last_analysis_time > 0:
                time.sleep(30)
                continue

            last_analysis_time = now
            perf_list = _analyze_assets()

            if not perf_list:
                print("[ANALISI] Lista asset vuota — skip")
                time.sleep(60)
                continue

            best = perf_list[0]

            # Re-fetch posizione aggiornata dopo l'analisi (idempotenza)
            active_ticker, _ = broker.get_open_position()

            with state_lock:
                state["assets_data"]          = perf_list
                state["institutional_verdict"] = best["verdict"]
                state["bridgewater_risk"]      = best["risk"]
                state["ai_score"]             = best["score"]
                state["ai_details"]           = {
                    "best_asset":  best["id"],
                    "rsi":         best.get("rsi"),
                    "volume_ratio":best.get("volume_ratio"),
                    "macd_cross":  best.get("macd_cross"),
                }
                save_state()

            # ── 8. Logica di trading aggressiva ──

            # CASO A: Nessuna posizione aperta → Entra se il segnale è positivo
            if active_ticker is None and cash > 50.0:
                # Entra anche su WEAK_BUY se il momentum è forte
                if best["verdict"] in ("STRONG_BUY", "BUY", "WEAK_BUY") and best["risk"] != "HIGH_RISK":
                    size_mult = best.get("size_mult", 0.90)
                    buy_power = cash * 0.95 * size_mult
                    success   = broker.buy_asset(best["id"], buy_power)
                    if success:
                        with state_lock:
                            state["entry_price"] = best["price"]
                            state["current_asset"] = best["id"]
                            state["operations_today"] = state.get("operations_today", 0) + 1
                            _log(f"🎯 ACQUISTO: {best['id']} Score:{best['score']} Verdict:{best['verdict']}")
                            save_state()
                        send_telegram(
                            f"🎯 *INGRESSO A MERCATO*\n\n"
                            f"Asset: `{best['id']}`\n"
                            f"Valore: `${buy_power:,.2f}`\n"
                            f"Score AI: `{best['score']}/100`\n"
                            f"Verdetto: `{best['verdict']}`\n"
                            f"RSI: `{best.get('rsi', 'N/A')}`\n"
                            f"Volume: `{best.get('volume_ratio', 1):.1f}x`\n"
                            f"MACD Cross: `{'✅' if best.get('macd_cross') else '❌'}`"
                        )

            # CASO B: Posizione aperta → Valuta swap aggressivo
            elif active_ticker:
                active_data = next((x for x in perf_list if x["id"] == active_ticker), None)

                if active_data:
                    score_gap   = best["score"] - active_data["score"]
                    growth_gap  = best["growth"] - active_data["growth"]

                    # Swap aggressivo: basta che il migliore abbia un vantaggio netto in score
                    # e che la differenza di crescita superi la soglia minima
                    should_swap = (
                        best["id"] != active_ticker
                        and score_gap >= 20            # Vantaggio significativo in score AI
                        and growth_gap > SWAP_THRESHOLD
                        and best["verdict"] in ("STRONG_BUY", "BUY")
                        and best["risk"] != "HIGH_RISK"
                        and not broker._order_pending
                    )

                    # Swap d'emergenza su GLD se il rischio è alto
                    emergency_gold = (
                        active_data.get("risk") == "HIGH_RISK"
                        or active_data["verdict"] == "SELL/SWAP_TO_GOLD"
                    ) and active_ticker != "GLD"

                    if emergency_gold:
                        broker.sell_all_asset(active_ticker)
                        time.sleep(4)
                        _, new_cash = broker.get_real_portfolio_value()
                        broker.buy_asset("GLD", new_cash * 0.95)
                        with state_lock:
                            state["current_asset"] = "GLD"
                            state["operations_today"] = state.get("operations_today", 0) + 1
                            state["partial_profit_taken"] = False
                            _log(f"⚠️ SWAP EMERGENZA → GLD (risk: {active_data.get('risk')})")
                            save_state()
                        send_telegram(
                            f"⚠️ *SWAP EMERGENZA → ORO*\n\n"
                            f"Venduto: `{active_ticker}`\n"
                            f"Motivo: `{active_data.get('risk', 'SELL signal')}`\n"
                            f"Cash protetto in GLD."
                        )

                    elif should_swap:
                        sell_ok = broker.sell_all_asset(active_ticker)
                        if sell_ok:
                            time.sleep(4)
                            _, new_cash = broker.get_real_portfolio_value()
                            size_mult = best.get("size_mult", 0.90)
                            buy_ok    = broker.buy_asset(best["id"], new_cash * 0.95 * size_mult)
                            if buy_ok:
                                with state_lock:
                                    state["current_asset"] = best["id"]
                                    state["entry_price"]   = best["price"]
                                    state["operations_today"] = state.get("operations_today", 0) + 1
                                    state["partial_profit_taken"] = False
                                    PARTIAL_PROFIT_DONE.pop(active_ticker, None)
                                    _log(f"🔄 SWAP: {active_ticker} → {best['id']} (ΔScore:{score_gap})")
                                    save_state()
                                send_telegram(
                                    f"🔄 *SWAP AGGRESSIVO*\n\n"
                                    f"Venduto: `{active_ticker}` (Score:{active_data['score']})\n"
                                    f"Acquistato: `{best['id']}` (Score:{best['score']})\n"
                                    f"ΔScore: `+{score_gap}`\n"
                                    f"ΔGrowth: `+{growth_gap:.2f}%`\n"
                                    f"MACD Cross: `{'✅' if best.get('macd_cross') else '❌'}`"
                                )

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Errore Critico nel loop: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(60)

# ══════════════════════════════════════════════════════════════════════
# API DASHBOARD WEB
# ══════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_dashboard():
    return send_from_directory('.', 'dashboard.html')

@app.route('/api/state')
def get_state():
    """Endpoint protetto da token statico."""
    token = os.getenv("DASHBOARD_TOKEN", "")
    if token:
        req_token = (
            __import__('flask').request.headers.get("X-API-Key") or
            __import__('flask').request.args.get("token", "")
        )
        if req_token != token:
            return __import__('flask').jsonify({"error": "Unauthorized"}), 401

    with state_lock:
        snapshot = dict(state)

    snapshot["wallet"]   = snapshot.get("total_equity", 0.0)
    snapshot["invested"] = snapshot.get("total_equity", 0.0) - snapshot.get("cash", 0.0)
    snapshot["market_open"] = broker.is_market_open()
    snapshot["minutes_to_close"] = broker.minutes_to_close()
    return jsonify(snapshot)

@app.route('/health')
def health():
    """Endpoint di ping per Cron-job.org (keep-alive)."""
    return jsonify({"status": "ok", "ts": int(time.time())}), 200

# ══════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    threading.Thread(target=update_logic, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    print("🚀 AlphaMoto Core v5.0 avviato su http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
