import yfinance as yf
from flask import Flask, jsonify, Response
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

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaMoto v5.0</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#020509;
  --bg2:#060d14;
  --panel:#091220;
  --panel2:#0c1a2e;
  --border:rgba(0,200,255,0.12);
  --border-glow:rgba(0,200,255,0.35);
  --cyan:#00c8ff;
  --cyan2:#00f0ff;
  --green:#00ff9d;
  --red:#ff3a5c;
  --gold:#ffb700;
  --purple:#b060ff;
  --orange:#ff7e2e;
  --text:#c8e6ff;
  --muted:#3a6080;
  --cyan-bg:rgba(0,200,255,0.08);
  --green-bg:rgba(0,255,157,0.08);
  --red-bg:rgba(255,58,92,0.08);
  --gold-bg:rgba(255,183,0,0.08);
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;overflow-x:hidden}

/* grid noise texture */
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(0,200,255,0.03) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,200,255,0.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0
}

/* scanlines */
body::after{
  content:'';position:fixed;inset:0;
  background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,0.07) 3px,rgba(0,0,0,0.07) 4px);
  pointer-events:none;z-index:1
}

.shell{position:relative;z-index:2;max-width:1700px;margin:0 auto;padding:14px;display:flex;flex-direction:column;gap:10px;min-height:100vh}

/* ── HEADER ── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 22px;
  background:linear-gradient(135deg,rgba(0,20,40,0.95),rgba(0,30,60,0.9));
  border:1px solid var(--border-glow);
  border-radius:4px;
  box-shadow:0 0 30px rgba(0,200,255,0.08),inset 0 1px 0 rgba(0,200,255,0.15);
  position:relative;overflow:hidden
}
header::before{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--cyan),var(--cyan2),var(--cyan),transparent)
}
.logo{
  font-family:'Orbitron',sans-serif;font-size:20px;font-weight:900;
  color:#fff;letter-spacing:3px;text-transform:uppercase;
  text-shadow:0 0 20px var(--cyan),0 0 40px rgba(0,200,255,0.4)
}
.logo em{color:var(--cyan);font-style:normal}
.logo-ver{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;letter-spacing:2px;margin-left:10px;vertical-align:middle}
.hdr-right{display:flex;align-items:center;gap:16px}
.pill{
  display:flex;align-items:center;gap:6px;
  padding:5px 12px;border-radius:2px;font-size:10px;font-family:'JetBrains Mono',monospace;font-weight:700;letter-spacing:1px;
  border:1px solid;text-transform:uppercase
}
.pill.online{background:rgba(0,255,157,0.08);border-color:rgba(0,255,157,0.3);color:var(--green)}
.pill.offline{background:var(--red-bg);border-color:rgba(255,58,92,0.3);color:var(--red)}
.pill.mkt-open{background:rgba(0,255,157,0.08);border-color:rgba(0,255,157,0.3);color:var(--green)}
.pill.mkt-closed{background:rgba(58,96,128,0.15);border-color:var(--border);color:var(--muted)}
.pill.cb-pill{background:var(--red-bg);border-color:rgba(255,58,92,0.5);color:var(--red);display:none;animation:cb-flash 1s infinite}
.pill.cb-pill.active{display:flex}
@keyframes cb-flash{0%,100%{opacity:1;box-shadow:0 0 8px var(--red)}50%{opacity:.5;box-shadow:none}}
.dot-live{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:blink-dot 2s infinite}
@keyframes blink-dot{0%,100%{opacity:1}50%{opacity:.2}}
.mtc{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace}

/* ── KPI ROW ── */
.kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.kcard{
  background:linear-gradient(135deg,var(--panel),var(--panel2));
  border:1px solid var(--border);border-radius:4px;padding:14px 16px;
  position:relative;overflow:hidden;transition:border-color .3s
}
.kcard:hover{border-color:var(--border-glow)}
.kcard::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:.4}
.kcard-label{font-size:9px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;font-family:'JetBrains Mono',monospace}
.kcard-val{font-family:'Orbitron',sans-serif;font-size:20px;font-weight:700;color:#fff;line-height:1;text-shadow:0 0 15px rgba(0,200,255,0.4)}
.kcard-val.cyan{color:var(--cyan);text-shadow:0 0 15px var(--cyan)}
.kcard-sub{font-size:10px;color:var(--muted);margin-top:6px;font-family:'JetBrains Mono',monospace}
.delta{font-size:10px;margin-top:6px;font-weight:600;font-family:'JetBrains Mono',monospace}
.delta.pos{color:var(--green)}.delta.neg{color:var(--red)}.delta.neu{color:var(--muted)}

/* corner decoration */
.kcard::before{
  content:'';position:absolute;bottom:0;right:0;
  width:30px;height:30px;
  border-top:1px solid var(--border-glow);border-left:1px solid var(--border-glow);
  border-radius:0 0 0 4px;opacity:.3
}

/* ── MAIN GRID ── */
.main-grid{display:grid;grid-template-columns:1fr 1fr 1fr 300px;grid-template-rows:auto 1fr;gap:10px}

/* ── PANEL BASE ── */
.panel{
  background:linear-gradient(135deg,var(--panel),var(--panel2));
  border:1px solid var(--border);border-radius:4px;padding:16px;
  position:relative;overflow:hidden
}
.panel::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:.3}
.panel-title{
  font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;
  color:var(--cyan);margin-bottom:12px;font-family:'JetBrains Mono',monospace;
  display:flex;align-items:center;gap:8px
}
.panel-title::before{content:'';width:3px;height:10px;background:var(--cyan);box-shadow:0 0 6px var(--cyan);border-radius:1px}

/* ── CHART ── */
.chart-panel{grid-column:1/4;grid-row:1}
.chart-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:12px}
.asset-display{display:flex;align-items:center;gap:10px}
.asset-sym{font-family:'Orbitron',sans-serif;font-size:22px;font-weight:900;color:var(--cyan);text-shadow:0 0 20px var(--cyan),0 0 40px rgba(0,200,255,0.3);letter-spacing:3px}
.risk-badge{font-size:9px;padding:3px 8px;border-radius:2px;font-weight:700;font-family:'JetBrains Mono',monospace;letter-spacing:1px;text-transform:uppercase}
.risk-badge.safe{background:var(--green-bg);color:var(--green);border:1px solid rgba(0,255,157,0.3)}
.risk-badge.elevated{background:var(--gold-bg);color:var(--gold);border:1px solid rgba(255,183,0,0.3)}
.risk-badge.high{background:var(--red-bg);color:var(--red);border:1px solid rgba(255,58,92,0.3);animation:cb-flash 1s infinite}

/* stop-loss bar */
.sl-wrap{min-width:220px}
.sl-top{display:flex;justify-content:space-between;font-size:9px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-bottom:4px}
.sl-track{height:4px;background:rgba(255,255,255,0.05);border-radius:2px;position:relative;border:1px solid var(--border)}
.sl-fill{height:100%;border-radius:2px;transition:width .6s,background .4s;box-shadow:0 0 6px currentColor}
.sl-marks{display:flex;justify-content:space-between;font-size:8px;color:var(--muted);margin-top:3px;font-family:'JetBrains Mono',monospace}

/* tv iframe */
.tv-frame{height:340px;border-radius:3px;overflow:hidden;margin-top:0;border:1px solid var(--border);box-shadow:0 0 20px rgba(0,200,255,0.05)}
.tv-frame iframe{width:100%;height:100%;border:none}

/* ── SCORE PANEL ── */
.score-panel{grid-column:4;grid-row:1/3;display:flex;flex-direction:column;gap:12px}
.ring-wrap{display:flex;flex-direction:column;align-items:center;gap:10px;padding:16px 0}
svg.ring{width:140px;height:140px;filter:drop-shadow(0 0 12px rgba(0,200,255,0.3))}
.ring-track{fill:none;stroke:rgba(255,255,255,0.05);stroke-width:8}
.ring-bg-glow{fill:none;stroke:rgba(0,200,255,0.06);stroke-width:16}
.ring-fill{fill:none;stroke-width:8;stroke-linecap:round;transition:stroke-dashoffset 1s cubic-bezier(.4,0,.2,1),stroke .4s}
.ring-num{text-anchor:middle;dominant-baseline:middle;font-family:'Orbitron',sans-serif;font-size:30px;font-weight:900;fill:#fff}
.ring-lbl{text-anchor:middle;font-size:8px;fill:var(--muted);letter-spacing:2px;font-family:'JetBrains Mono',monospace}
.verdict-pill{
  font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;letter-spacing:2px;
  text-align:center;padding:8px 16px;border-radius:3px;text-transform:uppercase;width:100%;
  border:1px solid
}
.verdict-pill.buy{background:var(--green-bg);color:var(--green);border-color:rgba(0,255,157,0.3);box-shadow:0 0 15px rgba(0,255,157,0.1)}
.verdict-pill.sell{background:var(--red-bg);color:var(--red);border-color:rgba(255,58,92,0.3);box-shadow:0 0 15px rgba(255,58,92,0.1)}
.verdict-pill.gold{background:var(--gold-bg);color:var(--gold);border-color:rgba(255,183,0,0.3);box-shadow:0 0 15px rgba(255,183,0,0.1)}
.verdict-pill.neutral{background:var(--cyan-bg);color:var(--muted);border-color:var(--border)}
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.ai-cell{background:rgba(0,0,0,0.4);border:1px solid var(--border);border-radius:3px;padding:7px 9px}
.ai-cell-lbl{font-size:8px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.ai-cell-val{font-size:13px;font-weight:700;color:#fff;margin-top:3px;font-family:'JetBrains Mono',monospace}

/* ── RADAR TABLE ── */
.radar-panel{grid-column:1/4;grid-row:2;display:flex;flex-direction:column}
table{width:100%;border-collapse:collapse;margin-top:4px}
thead th{
  font-size:8px;font-weight:700;letter-spacing:2px;color:var(--muted);
  text-transform:uppercase;padding:0 0 10px;text-align:left;
  border-bottom:1px solid var(--border);font-family:'JetBrains Mono',monospace
}
tbody tr{border-bottom:1px solid rgba(0,200,255,0.05);transition:background .15s}
tbody tr:hover{background:rgba(0,200,255,0.04)}
tbody td{padding:9px 0;font-size:12px;vertical-align:middle}
.tcell{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:#fff;letter-spacing:1px}
.tcell.active{color:var(--cyan);text-shadow:0 0 10px var(--cyan)}
.act-indicator{display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--cyan);box-shadow:0 0 6px var(--cyan);margin-right:6px;vertical-align:middle}
.pcell{color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:11px}
.gpill{display:inline-block;padding:2px 8px;border-radius:2px;font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace}
.gpill.up{background:var(--green-bg);color:var(--green);border:1px solid rgba(0,255,157,0.2)}
.gpill.dn{background:var(--red-bg);color:var(--red);border:1px solid rgba(255,58,92,0.2)}
.sbar-wrap{display:flex;align-items:center;gap:6px}
.sbar-track{width:60px;height:3px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden}
.sbar-fill{height:100%;border-radius:2px}
.sval{font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace}
.vcell{text-align:right;font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;letter-spacing:.5px}
.v-sb{color:var(--green)}.v-b{color:#00e0c0}.v-wb,.v-n{color:var(--muted)}.v-s{color:var(--red)}

/* ── LOG ── */
.log-scroll{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:3px;margin-top:8px;max-height:340px}
.log-scroll::-webkit-scrollbar{width:3px}
.log-scroll::-webkit-scrollbar-thumb{background:rgba(0,200,255,0.2);border-radius:2px}
.log-entry{background:rgba(0,0,0,0.5);border:1px solid rgba(0,200,255,0.06);border-radius:2px;padding:5px 8px;font-size:9px;line-height:1.6;font-family:'JetBrains Mono',monospace}
.log-t{color:var(--cyan);margin-right:6px}
.log-m{color:rgba(200,230,255,0.5)}

/* ── GLOWS & DECORATIONS ── */
.corner-tl,.corner-br{position:absolute;width:12px;height:12px}
.corner-tl{top:6px;left:6px;border-top:1px solid var(--cyan);border-left:1px solid var(--cyan);opacity:.6}
.corner-br{bottom:6px;right:6px;border-bottom:1px solid var(--cyan);border-right:1px solid var(--cyan);opacity:.6}

@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.blink{animation:blink 1.2s step-start infinite}

/* ── RESPONSIVE ── */
@media(max-width:1200px){
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  .main-grid{grid-template-columns:1fr 300px;grid-template-rows:auto auto 1fr}
  .chart-panel{grid-column:1}
  .score-panel{grid-column:2;grid-row:1/4}
  .radar-panel{grid-column:1;grid-row:2}
}
</style>
</head>
<body>
<div class="shell">

<!-- HEADER -->
<header>
  <div>
    <span class="logo">Alpha<em>Moto</em></span>
    <span class="logo-ver">v5.0 // AGGRESSIVE MODE</span>
  </div>
  <div class="hdr-right">
    <span class="pill cb-pill" id="cb-pill">&#x26D4; CIRCUIT BREAKER</span>
    <span class="pill mkt-closed" id="mkt-pill">MKT CHIUSO</span>
    <span class="mtc" id="mtc"></span>
    <div class="pill online" id="conn-pill">
      <div class="dot-live" id="conn-dot"></div>
      <span id="conn-txt">ALPACA API</span>
    </div>
  </div>
</header>

<!-- KPI ROW -->
<div class="kpi-row">
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">Patrimonio</div><div class="kcard-val" id="kv-equity">$0.00</div><div class="delta neu" id="kv-dpnl">+0.00% oggi</div></div>
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">Cash Libero</div><div class="kcard-val" id="kv-cash">$0.00</div><div class="kcard-sub" id="kv-cashp">-% portafoglio</div></div>
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">Investito</div><div class="kcard-val" id="kv-inv">$0.00</div><div class="kcard-sub" id="kv-invp">-% portafoglio</div></div>
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">Asset Attivo</div><div class="kcard-val cyan" id="kv-asset">LIQUIDO</div><div class="kcard-sub" id="kv-entry">Entry: -</div></div>
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">Operazioni Oggi</div><div class="kcard-val" id="kv-ops">0</div><div class="kcard-sub">sessione corrente</div></div>
  <div class="kcard"><div class="corner-tl"></div><div class="corner-br"></div><div class="kcard-label">P&amp;L Posizione</div><div class="kcard-val" id="kv-pnl">-</div><div class="kcard-sub" id="kv-partial"></div></div>
</div>

<!-- MAIN GRID -->
<div class="main-grid">

  <!-- CHART PANEL -->
  <div class="panel chart-panel">
    <div class="corner-tl"></div><div class="corner-br"></div>
    <div class="chart-header">
      <div class="asset-display">
        <span class="asset-sym" id="ch-sym">LIQUIDO</span>
        <span class="risk-badge safe" id="ch-risk">SAFE</span>
      </div>
      <div class="sl-wrap">
        <div class="sl-top"><span>STOP-LOSS <span id="sl-val" style="color:var(--text)">-</span></span><span>LIMITE: -6%</span></div>
        <div class="sl-track"><div class="sl-fill" id="sl-fill" style="width:50%;background:var(--green)"></div></div>
        <div class="sl-marks"><span>-6%</span><span>0%</span><span>+8%</span><span>+&#x221E;</span></div>
      </div>
    </div>
    <div class="tv-frame" id="tv-frame"></div>
  </div>

  <!-- AI SCORE PANEL -->
  <div class="panel score-panel">
    <div class="panel-title">Score AI</div>
    <div class="ring-wrap">
      <svg class="ring" viewBox="0 0 140 140">
        <circle class="ring-bg-glow" cx="70" cy="70" r="56"/>
        <circle class="ring-track" cx="70" cy="70" r="56"/>
        <circle class="ring-fill" id="ring-arc" cx="70" cy="70" r="56"
          stroke-dasharray="351.86" stroke-dashoffset="351.86"
          stroke="var(--cyan)" transform="rotate(-90 70 70)"/>
        <text class="ring-num" id="ring-n" x="70" y="66">0</text>
        <text class="ring-lbl" x="70" y="84">/100</text>
      </svg>
      <div class="verdict-pill neutral" id="verd-pill">ANALISI...</div>
      <div class="ai-grid">
        <div class="ai-cell"><div class="ai-cell-lbl">RSI</div><div class="ai-cell-val" id="ai-rsi">-</div></div>
        <div class="ai-cell"><div class="ai-cell-lbl">Volume</div><div class="ai-cell-val" id="ai-vol">-</div></div>
        <div class="ai-cell"><div class="ai-cell-lbl">MACD Cross</div><div class="ai-cell-val" id="ai-macd">-</div></div>
        <div class="ai-cell"><div class="ai-cell-lbl">Best Asset</div><div class="ai-cell-val" id="ai-best">-</div></div>
      </div>
    </div>

    <div class="panel-title" style="margin-top:4px">System Log <span class="blink" style="color:var(--green);margin-left:4px">&#x2588;</span></div>
    <div class="log-scroll" id="log-box"></div>
  </div>

  <!-- RADAR TABLE -->
  <div class="panel radar-panel">
    <div class="panel-title">Radar Strategico // Score AI Composito</div>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Prezzo</th>
        <th>Score</th><th>5D%</th><th>RSI</th>
        <th style="text-align:right">Verdetto</th>
      </tr></thead>
      <tbody id="radar-tb"></tbody>
    </table>
  </div>

</div><!-- /main-grid -->
</div><!-- /shell -->

<script>
let curSym='';
const $=id=>document.getElementById(id);

function money(n){
  if(n==null||isNaN(n))return'$-';
  return'$'+parseFloat(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function pct(n,force){
  if(n==null||isNaN(n))return'-';
  return(n>=0?'+':'')+parseFloat(n).toFixed(2)+'%';
}

function setRing(score){
  const s=Math.max(-100,Math.min(100,score||0));
  const norm=(s+100)/200;
  const arc=$('ring-arc');
  arc.style.strokeDashoffset=351.86*(1-norm);
  arc.style.stroke=s>=50?'var(--green)':s>=20?'var(--cyan)':s>=-10?'var(--muted)':'var(--red)';
  arc.style.filter=`drop-shadow(0 0 6px ${s>=50?'var(--green)':s>=20?'var(--cyan)':'var(--red)'})`;
  $('ring-n').textContent=s;
}

function setVerdict(v){
  const el=$('verd-pill');
  el.textContent=v||'NEUTRAL';
  el.className='verdict-pill';
  if(!v||v==='NEUTRAL'||v==='WEAK_BUY'||v==='WEAK_SELL')el.classList.add('neutral');
  else if(v.includes('BUY'))el.classList.add('buy');
  else if(v.includes('GOLD'))el.classList.add('gold');
  else el.classList.add('sell');
}

function updateChart(sym){
  const s=(!sym||sym==='LIQUIDO')?'SPY':sym;
  if(s===curSym)return;curSym=s;
  $('tv-frame').innerHTML=`<iframe src="https://s.tradingview.com/widgetembed/?symbol=${s}&interval=D&theme=dark&style=1&locale=it&timezone=Europe%2FRome&hide_side_toolbar=1&allow_symbol_change=0&save_image=0&withdateranges=1" allowtransparency="true" scrolling="no" allowfullscreen></iframe>`;
}

function slBar(p){
  const f=$('sl-fill'),lbl=$('sl-val');
  if(p==null||isNaN(p)){f.style.width='50%';f.style.background='var(--muted)';lbl.textContent='-';return}
  lbl.textContent=pct(p);
  const w=Math.max(0,Math.min(100,((p+10)/30)*100));
  f.style.width=w+'%';
  f.style.background=p<=-4?'var(--red)':p<=0?'var(--gold)':p>=8?'var(--purple)':'var(--green)';
  f.style.boxShadow=`0 0 8px ${p<=-4?'var(--red)':p<=0?'var(--gold)':p>=8?'var(--purple)':'var(--green)'}`;
}

function vcls(v){
  if(!v)return'v-n';const u=v.toUpperCase();
  if(u.includes('STRONG'))return'v-sb';if(u==='BUY')return'v-b';
  if(u.includes('WEAK'))return'v-wb';if(u.includes('SELL'))return'v-s';return'v-n';
}
function scCol(s){return s>=50?'var(--green)':s>=20?'var(--cyan)':s>=-10?'var(--muted)':'var(--red)'}

function renderRadar(assets,active){
  const tb=$('radar-tb');
  if(!assets||!assets.length){
    tb.innerHTML=`<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:2px">// MERCATO CHIUSO — STANDBY //</td></tr>`;
    return;
  }
  tb.innerHTML=assets.map(a=>{
    const iA=a.id===active,sc=a.score||0,bw=Math.max(0,Math.min(100,(sc+100)/2));
    return`<tr>
      <td><span class="tcell ${iA?'active':''}">${iA?'<span class="act-indicator"></span>':''}${a.id}</span></td>
      <td class="pcell">$${(a.price||0).toFixed(2)}</td>
      <td><div class="sbar-wrap"><div class="sbar-track"><div class="sbar-fill" style="width:${bw}%;background:${scCol(sc)};box-shadow:0 0 4px ${scCol(sc)}"></div></div><span class="sval" style="color:${scCol(sc)}">${sc}</span></div></td>
      <td><span class="gpill ${a.growth>=0?'up':'dn'}">${pct(a.growth)}</span></td>
      <td class="pcell">${a.rsi?a.rsi.toFixed(0):'-'}</td>
      <td class="vcell"><span class="${vcls(a.verdict)}">${a.verdict||'-'}</span></td>
    </tr>`;
  }).join('');
}

function renderLog(logs){
  const box=$('log-box');
  if(!logs||!logs.length)return;
  box.innerHTML=logs.map(msg=>{
    const m=msg.match(/^\[(\d{2}:\d{2}:\d{2})\] (.*)/);
    if(m)return`<div class="log-entry"><span class="log-t">${m[1]}</span><span class="log-m">${m[2]}</span></div>`;
    return`<div class="log-entry"><span class="log-m">${msg}</span></div>`;
  }).join('');
}

async function tick(){
  try{
    const r=await fetch('/api/state');
    if(!r.ok)throw new Error(r.status);
    const d=await r.json();

    // conn
    $('conn-dot').style.background='var(--green)';$('conn-txt').textContent='ALPACA API';
    $('conn-pill').className='pill online';

    // market
    const io=d.market_open;
    const mp=$('mkt-pill');mp.textContent=io?'MKT APERTO':'MKT CHIUSO';
    mp.className='pill '+(io?'mkt-open':'mkt-closed');
    $('mtc').textContent=(io&&d.minutes_to_close>0)?`chiude tra ${d.minutes_to_close}m`:'';

    // circuit breaker
    $('cb-pill').classList.toggle('active',!!d.circuit_breaker);

    // kpi
    const eq=d.wallet||0,ca=d.cash||0,inv=d.invested||0;
    $('kv-equity').textContent=money(eq);
    $('kv-cash').textContent=money(ca);
    $('kv-inv').textContent=money(inv);
    $('kv-ops').textContent=d.operations_today||0;
    if(eq>0){$('kv-cashp').textContent=((ca/eq)*100).toFixed(1)+'% portafoglio';$('kv-invp').textContent=((inv/eq)*100).toFixed(1)+'% portafoglio'}
    const ds=d.daily_start_equity||eq,dp=eq-ds,dpp=ds>0?(dp/ds*100):0;
    const dpel=$('kv-dpnl');dpel.textContent=pct(dpp)+' oggi ('+money(dp)+')';dpel.className='delta '+(dp>=0?'pos':'neg');

    // asset
    const sym=d.current_asset||'LIQUIDO';
    $('kv-asset').textContent=sym;$('ch-sym').textContent=sym;
    $('kv-entry').textContent=d.entry_price?'Entry: '+money(d.entry_price):'Entry: -';
    updateChart(sym);

    // pnl posizione
    const pp=d.position_pnl_pct??null;
    $('kv-pnl').textContent=pp!=null?pct(pp):'-';
    $('kv-pnl').style.color=pp==null?'var(--muted)':pp>=0?'var(--green)':'var(--red)';
    $('kv-partial').textContent=d.partial_profit_taken?'&#x2713; Take profit parziale':'';
    $('kv-partial').style.color='var(--purple)';
    slBar(pp);

    // risk
    const rk=d.bridgewater_risk||'SAFE';
    const rb=$('ch-risk');rb.textContent=rk;
    rb.className='risk-badge '+(rk==='HIGH_RISK'?'high':rk==='ELEVATED'?'elevated':'safe');

    // score ring
    setRing(d.ai_score||0);setVerdict(d.institutional_verdict);
    const det=d.ai_details||{};
    $('ai-rsi').textContent=det.rsi!=null?det.rsi.toFixed(1)+' RSI':'-';
    $('ai-vol').textContent=det.volume_ratio!=null?det.volume_ratio.toFixed(2)+'x':'-';
    $('ai-macd').textContent=det.macd_cross!=null?(det.macd_cross?'YES &#x2191;':'NO'):'-';
    $('ai-best').textContent=det.best_asset||'-';

    renderRadar(d.assets_data,sym);
    renderLog(d.log);

  }catch(e){
    $('conn-dot').style.background='var(--red)';$('conn-txt').textContent='OFFLINE';
    $('conn-pill').className='pill offline';
  }
}
tick();setInterval(tick,5000);
</script>
</body>
</html>"""

@app.route('/')
def serve_dashboard():
    return Response(DASHBOARD_HTML, mimetype='text/html')

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
