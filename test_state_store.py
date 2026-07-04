"""
Test sul debounce delle scritture su disco di StateStore (Fase 3).

Principio da verificare: la MEMORIA (letta da dashboard/decisioni) è sempre
aggiornata all'istante; il DISCO (solo cache best-effort per log/restart) è
scritto al massimo una volta ogni write_debounce_sec secondi, salvo force=True.
"""
import json
import time

import pytest

import main


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "state_test.json")
    return main.StateStore(path, write_debounce_sec=0.2)


def _read_disk(path):
    with open(path) as f:
        return json.load(f)


class TestWriteDebounce:
    def test_scritture_ravvicinate_producono_pochi_write_reali(self, store, monkeypatch):
        writes = []
        original_write = store._write

        def counting_write(snapshot):
            writes.append(snapshot)
            original_write(snapshot)

        monkeypatch.setattr(store, "_write", counting_write)

        for i in range(10):
            store.update({"operations_today": i})  # tutte entro la finestra di debounce

        assert len(writes) < 10, "il debounce deve coalescere scritture ravvicinate"
        assert len(writes) >= 1, "almeno la prima scrittura deve sempre passare"

    def test_memoria_sempre_aggiornata_anche_con_disco_debounced(self, store):
        for i in range(5):
            store.update({"operations_today": i})
        # store.get legge SEMPRE dalla memoria: mai stale, indipendentemente
        # dal debounce sul disco (è quello che usa /api/state per la dashboard).
        assert store.get("operations_today") == 4

    def test_dopo_la_finestra_di_debounce_la_scrittura_va_a_disco(self, store):
        store.update({"operations_today": 1})
        time.sleep(0.25)  # supera write_debounce_sec=0.2
        store.update({"operations_today": 2})
        assert _read_disk(store._path)["operations_today"] == 2

    def test_force_bypassa_il_debounce(self, store):
        store.update({"operations_today": 1})
        store.update({"operations_today": 2}, force=True)  # deve arrivare SUBITO su disco
        assert _read_disk(store._path)["operations_today"] == 2

    def test_flush_now_forza_la_scrittura_dell_ultimo_stato(self, store):
        store.update({"operations_today": 7})   # forse ancora in coda per il debounce
        store.flush_now()
        assert _read_disk(store._path)["operations_today"] == 7

    def test_mutate_rispetta_lo_stesso_debounce(self, store, monkeypatch):
        writes = []
        original_write = store._write

        def counting_write(snapshot):
            writes.append(snapshot)
            original_write(snapshot)

        monkeypatch.setattr(store, "_write", counting_write)
        for _ in range(10):
            store.mutate(lambda s: s.__setitem__("operations_today", s.get("operations_today", 0) + 1))
        assert len(writes) < 10

    def test_mutate_force_bypassa_il_debounce(self, store):
        store.mutate(lambda s: s.__setitem__("operations_today", 1))
        store.mutate(lambda s: s.__setitem__("operations_today", 2), force=True)
        assert _read_disk(store._path)["operations_today"] == 2
