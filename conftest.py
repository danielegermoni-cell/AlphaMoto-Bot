"""
Stub dell'SDK alpaca_trade_api: permette di importare AlpacaBroker e main
senza rete e senza l'SDK reale (deprecato e con dipendenze fragili).
Deve essere caricato PRIMA di qualunque import del progetto.
"""
import sys
import types
import os

# --- Stub alpaca_trade_api -------------------------------------------------
fake_pkg  = types.ModuleType("alpaca_trade_api")
fake_rest = types.ModuleType("alpaca_trade_api.rest")


class _FakeAccount:
    status = "ACTIVE"
    equity = "100000"
    cash   = "100000"


class FakeREST:
    """Client minimale: i test sostituiscono i singoli metodi con dei fake."""
    def __init__(self, *args, **kwargs):
        pass

    def get_account(self):
        return _FakeAccount()


class TimeFrame:
    Day    = "1Day"
    Hour   = "1Hour"
    Minute = "1Min"


fake_pkg.REST       = FakeREST
fake_rest.TimeFrame = TimeFrame
fake_pkg.rest       = fake_rest

sys.modules.setdefault("alpaca_trade_api", fake_pkg)
sys.modules.setdefault("alpaca_trade_api.rest", fake_rest)

# --- Env: niente Telegram, niente token dashboard nei test ------------------
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("DASHBOARD_TOKEN", None)

# Impedisce a main.py di avviare il motore reale (loop di trading + polling
# Telegram) al semplice import del modulo durante la raccolta dei test.
# Senza questo, ogni `import main` nei test avvierebbe un thread reale in
# background che martella l'API (stubbata) di Alpaca in loop, con retry e
# sleep, interferendo con le asserzioni e rallentando/instabilendo la suite.
os.environ["ALPHAMOTO_DISABLE_ENGINE"] = "1"

# --- Path del progetto -----------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
