import yfinance as yf
from flask import Flask, jsonify, send_file
from flask_cors import CORS
import threading
import time
import json
import os
import telebot
from dotenv import load_dotenv
from AlphaIntelligence import AlphaIntelligence
from AlpacaBroker import AlpacaBroker

# --- INIZIALIZZAZIONE ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
CORS(app)
broker = AlpacaBroker()

# --- PARAMETRI STRATEGICI ---
ASSETS = ['SPY', 'QQQ', 'SMH', 'GLD', 'VTI']
THRESHOLD = 2.0  
STATE_FILE = "state.json"
state_lock = threading.Lock()

# --- STATO DASHBOARD ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception: pass
    return {"log": ["AlphaMoto Avviato"], "assets_data": [], "peak_price": 0.0}

state = load_state()

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception: pass

def send_telegram(msg):
    try:
        with state_lock:
            state["log"].insert(0, f"[{time.strftime('%H:%M:%S')}] {msg.replace('*', '').replace('_', '')}")
            if len(state["log"]) > 50: state["log"].pop()
            save_state()
        bot.send_message(CHAT_ID, f"🚀 *AlphaMoto Intelligence*\n\n{msg}", parse_mode='Markdown')
    except Exception as e: print(f"Errore Telegram: {e}")

# --- COMANDI TELEGRAM INTERATTIVI ---
@bot.message_handler(commands=['info', 'report', 'stato', 'status'])
@bot.message_handler(func=lambda message: message.text and message.text.lower().strip() in ['info', 'report', 'stato', 'status'])
def send_realtime_report(message):
    try:
        # TEST DI DEBUG: Facciamo dire al bot il tuo ID
        mio_id = str(message.chat.id)
        id_salvato = str(os.getenv("TELEGRAM_CHAT_ID"))
        
        # Se gli ID non combaciano, facciamoglielo dire!
        if mio_id != id_salvato:
            bot.reply_to(message, f"⛔ Accesso Negato.\nIl tuo ID: {mio_id}\nID Autorizzato: {id_salvato}")
            return

        with state_lock:
            wallet = state.get("total_equity", 0.0)
            cash = state.get("cash", 0.0)
            invested = wallet - cash
            asset = state.get("current_asset", "Nessuno")
            verdict = state.get("ai_verdict", "Nessuno")
            status = state.get("status", "In attesa")

        testo_report = f"""
📊 *REPORT IN TEMPO REALE* 📊

💰 *Capitale Totale:* ${wallet:.2f}
💵 *Liquidità Disponibile:* ${cash:.2f}
📈 *Capitale Investito:* ${invested:.2f}

🎯 *Asset in Focus:* {asset}
🧠 *Verdetto AI:* {verdict}
🚦 *Stato Bot:* {status}
        """
        
        bot.send_message(message.chat.id, testo_report, parse_mode="Markdown")

    except Exception as e:
        # Se il codice va in crash, facciamoci mandare l'errore su Telegram!
        bot.reply_to(message, f"❌ Errore nel generare il report: {str(e)}")

# --- MOTORE CENTRALE ---
def update_logic():
    send_telegram("✅ *Motore v4.2 Online*\n\nLettura Portafoglio Sempre Attiva.\nTradingView integrato in Dashboard.")
    
    market_was_closed = not broker.is_market_open()
    last_report_time = time.time()

    while True:
        try:
            # FIX: LETTURA PORTAFOGLIO PRIMA DEL BLOCCO ORARIO
            # In questo modo la dashboard web avrà sempre i dati in tempo reale
            equity, cash = broker.get_real_portfolio_value()
            active_ticker = broker.get_open_position()

            with state_lock:
                state["total_equity"] = equity
                state["cash"] = cash
                state["current_asset"] = active_ticker or "LIQUIDO"
                save_state()

            # CONTROLLO ORARI
            is_open = broker.is_market_open()

            if not is_open:
                if not market_was_closed:
                    send_telegram("🔔 *CAMPANELLA DI CHIUSURA*\n\nWall Street ha chiuso. Il bot entra in Standby. A domani!")
                    market_was_closed = True
                time.sleep(60)
                continue # Se è chiuso, si ferma qui e non fa trading
            
            # SE IL MERCATO HA APPENA APERTO
            if market_was_closed:
                send_telegram(f"🔔 *MARKET OPEN!*\n\nInizia la sessione di trading.\nCapitale: ${round(equity, 2)}\nAsset: {state['current_asset']}")
                market_was_closed = False
                last_report_time = time.time()

            # TIMER BIORARIO (Ogni 2 ore = 7200 sec)
            current_time = time.time()
            if (current_time - last_report_time) >= 7200:
                send_telegram(f"⏱️ *AGGIORNAMENTO BIORARIO*\n\nCapitale: ${round(equity, 2)}\nPosizione: {state['current_asset']}")
                last_report_time = current_time

            # TRADING LOGIC
            data = yf.download(tickers=ASSETS, period="5d", interval="1d", progress=False)
            perf_list = []
            
            if not data.empty:
                for ticker in ASSETS:
                    try:
                        prices = data['Close'][ticker].dropna() if len(ASSETS) > 1 else data['Close'].dropna()
                        if prices.empty: continue
                        curr = float(prices.iloc[-1])
                        start = float(prices.iloc[0]) 
                        growth = ((curr - start) / start) * 100
                        perf_list.append({"id": ticker, "price": round(curr, 2), "growth": round(growth, 2)})
                    except Exception: pass

            if not perf_list:
                time.sleep(60)
                continue
                
            perf_list.sort(key=lambda x: x['growth'], reverse=True)
            best = perf_list[0]

            # INTELLIGENZA ISTITUZIONALE
            try:
                best_ticker_obj = yf.Ticker(best["id"])
                hist_data = best_ticker_obj.history(period="1mo")
                brain = AlphaIntelligence(hist_data)
                verdict = brain.get_institutional_verdict()
                risk_level = brain.get_bridgewater_risk()
            except Exception:
                verdict, risk_level = "NEUTRAL", "SAFE"

            # ESECUZIONE ORDINI
            if active_ticker is None and cash > 20.0:
                buy_power = cash * 0.95
                broker.buy_asset(best["id"], buy_power)
                send_telegram(f"🎯 *INGRESSO A MERCATO*\n\nAsset: {best['id']}\nValore: ${round(buy_power,2)}\nVerdetto: {verdict}")

            elif active_ticker:
                active_data = next((item for item in perf_list if item["id"] == active_ticker), None)
                if active_data:
                    if best["id"] != active_ticker and best["growth"] > (active_data["growth"] + THRESHOLD):
                        if verdict == "STRONG_BUY" and risk_level == "SAFE":
                            broker.sell_all_asset(active_ticker)
                            time.sleep(3) 
                            _, new_cash = broker.get_real_portfolio_value()
                            broker.buy_asset(best["id"], new_cash * 0.95)
                            send_telegram(f"🔄 *SWAP ISTITUZIONALE*\n\nVenduto: {active_ticker}\nAcquistato: {best['id']}\nVerdetto: {verdict}")

                    elif (verdict == "SELL/SWAP_TO_GOLD" or risk_level == "HIGH_RISK") and active_ticker != "GLD":
                        broker.sell_all_asset(active_ticker)
                        time.sleep(3)
                        _, new_cash = broker.get_real_portfolio_value()
                        broker.buy_asset("GLD", new_cash * 0.95)
                        send_telegram(f"⚠️ *RISCHIO ELEVATO*\n\nLiquidato portfolio e acquistato Oro (GLD).")

            with state_lock:
                state["assets_data"] = perf_list
                state["institutional_verdict"] = verdict
                state["bridgewater_risk"] = risk_level
                save_state()

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Errore Critico: {e}")
        
        time.sleep(60)

# --- API DASHBOARD WEB ---
# --- API DASHBOARD WEB ---
@app.route('/api/state')
def get_state():
    with state_lock:
        state_copy = state.copy()
        state_copy["wallet"] = state_copy.get("total_equity", 0.0) 
        state_copy["invested"] = state_copy.get("total_equity", 0.0) - state_copy.get("cash", 0.0)
        return jsonify(state_copy)

# --- INTERFACCIA WEB (IL PONTE) ---
@app.route('/')
def home():
    return send_file('Alphamoto.html')

if __name__ == '__main__':
    threading.Thread(target=update_logic, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    print("🚀 AlphaMoto Cloud Core Avviato!")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

