# BiHome Wearable — chat context primer

> **Per Vittorio**: copia-incolla questo intero file in una nuova chat
> Claude (su qualsiasi dispositivo) per fargli ereditare lo stato del
> progetto. Aggiornalo man mano che cambiano cose importanti.
>
> **Per Claude**: questo è un riassunto operativo del progetto BiHome
> Wearable. Leggilo per intero prima di fare qualsiasi cosa. Alla fine
> trovi il "playbook" su come continuare il lavoro.
>
> **Ultimo aggiornamento**: 2026-05-26

---

## 1. Cos'è BiHome Wearable

App Windows per acquisizione multi-dispositivo di dati fisiologici per
ricerca psicologica:
- **Polar H10** (ECG + ACC) via BLE diretto (libreria `bleak`)
- **EmotiBit** (PPG + EDA + temperatura + IMU) via WiFi (BrainFlow)
- Streaming via **LSL** (Lab Streaming Layer), 1-6 partecipanti
  simultanei
- Viewer in PyQt5: una finestra di plot per partecipante
- Registrazione XDF tramite **LabRecorderCLI** lanciato come subprocess
- Marker stream condiviso (eventi + stati, int32, timestamp click-time)

Utenti: ricercatori in psicologia (PI, PhD, RA), spesso non tecnici.
Vedere `HTI.md` per analisi human-technology interaction completa.

---

## 2. Percorsi e ambiente

**Dev machine (Windows, Vittorio):**
- Repo: `C:\Users\vitto\Desktop\BiHome_wearable\`
- Python: Anaconda (`C:\Users\vitto\anaconda3\`)
- App data dir runtime: `%APPDATA%\BiHome\` (devices.json, recordings,
  logs)
- Drive Google montato su `G:\Il mio Drive\` (per backup desktop)

**Su altre macchine (remoto):**
- Se hai il repo clonato, replica i comandi sostituendo il path.
- Se NON hai il repo, leggi questo file e chiedi a Claude di guidarti
  via descrizione (può ragionare sull'architettura senza vedere i file).

---

## 3. Stack tecnologico

| Layer | Libreria | Note |
|---|---|---|
| GUI | PyQt5 5.15.x | NO PyQt6 (incompatibile con build) |
| LSL | pylsl | StreamInfo/StreamOutlet, int32 per marker |
| BLE (Polar) | bleak 0.21+ | Windows: `WindowsProactorEventLoopPolicy` |
| WiFi (EmotiBit) | BrainFlow 5.x | Boards: EmotiBit (preset DEFAULT/AUXILIARY/ANCILLARY) |
| Plotting | pyqtgraph | NO matplotlib (escluso dal build) |
| Numerics | numpy 2.x | PyInstaller deve fare `--collect-all numpy` |
| Build | PyInstaller 6.x | `--windowed --onedir`, vedere `build_exe.py` |
| Registrazione | LabRecorderCLI.exe | Bundled in `LabRecorder/`, chiamato via subprocess |

---

## 4. Layout del repo

```
BiHome_wearable/
├── BiHome_wearable.py          ← backend principale (~4200 righe)
├── Viewer/
│   ├── lsl_viewer.py           ← viewer PyQt5, subprocess separato
│   ├── bihome.ico              ← icona app
│   └── (font Montserrat)
├── LabRecorder/
│   ├── LabRecorderCLI.exe      ← engine di registrazione XDF
│   └── (DLL Qt6, lsl.dll, etc.)
├── build_exe.py                ← script PyInstaller
├── diag_bleak.py               ← script di diagnostica BLE standalone
├── install_firewall_rule.bat   ← apre UDP 3131 per EmotiBit
├── README.md                   ← guida sviluppatore (setup da sorgente)
├── DISTRIBUTION.md             ← guida utente finale (exe + bat + troubleshooting)
├── HTI.md                      ← analisi human-tech interaction completa
├── CHAT_CONTEXT.md             ← questo file
├── Avvia BiHome (da sorgente).bat ← launcher Python (bypass Smart App Control)
└── dist/BiHome Wearable/
    ├── BiHome Wearable.exe     ← build corrente (28.5 MB)
    └── _internal/              ← dipendenze bundle
```

---

## 5. Architettura runtime

```
   [BiHome_wearable.py main process]
       │
       ├── Wizard PyQt5 (setup → device assignment)
       │
       ├── Per ogni Polar:  BleakPolarThread (asyncio in thread)
       │     └─ produce LSL stream P0N_PolarECG, P0N_PolarACC
       │
       ├── Per ogni EmotiBit: EmotiBitThread (BrainFlow)
       │     └─ produce LSL stream P0N_EmoPPG, P0N_EmoEDA, etc.
       │
       ├── MarkerStream (LSL int32, click-time timestamps)
       │
       ├── Scrive active_participants.json
       │
       ├── subprocess: Viewer/lsl_viewer.py
       │     └─ legge active_participants.json → apre N finestre plot
       │
       └── subprocess: LabRecorder/LabRecorderCLI.exe (su REC)
             └─ scrive .xdf + .csv in %APPDATA%/BiHome/recordings/
```

State files in `%APPDATA%/BiHome/`:
- `devices.json` — registro device (schema-versioned)
- `wizard_defaults.json` — ultime scelte wizard
- `viewer_settings.json` — posizioni finestre, layout canali
- `active_participants.json` — bridge backend ↔ viewer
- `acquisition.log` — log diagnostico (sovrascritto ogni sessione)
- `recordings/` — XDF + CSV delle sessioni

---

## 6. Sprint completati

**Sprint 1 — stabilità BLE/EmotiBit**
- `stop_notify` prima del disconnect Bluetooth
- Try/finally con flag `prepared`/`streaming` per EmotiBit
- Retry con backoff esponenziale

**Sprint 2 — portabilità / file system**
- `_resolve_app_data_dir()` → `%APPDATA%/BiHome/` per-utente
- Migrazione automatica da legacy paths
- `log()` helper basato su file (scrive in acquisition.log invece di stdout)
- `_safe_print()` per evitare crash in modalità --windowed

**Sprint 3 — qualità dati**
- NaN sentinels per gap di trasmissione (preserva asse temporale)
- Interpolazione per-sample dell'offset clock EMA
- `MonotonicGuard` per timestamp
- `ACC_MAX_AGE_S = 1.5s` per scartare campioni stantii

**Sprint 4 — pre-distribuzione**
- Default registry vuoti (no device hardcoded)
- Timeout BleakClient = 12s
- Contatore `consecutive_failures`
- Migrazione estesa a recordings/ e diag/
- DISTRIBUTION.md scritto da zero

**Sprint 4.1 — quick wins UX**
- Skip BLE scan se registry vuoto al primo avvio
- Validazione regex per seriale EmotiBit (`MD-V\d+-\d{4,}`)
- Funzione `_pick_ble_device_dialog()` (scanner BLE in-app)
- Hint sulla scoperta MAC in DISTRIBUTION.md

**Post-4.1 (questo turno):**
- Fix: `timer.stop()` in try/finally nel dialog BLE picker (era una potenziale crash su cancel)
- Riordino UI: tasto "🔍 Scan nearby BLE" SOPRA il campo MAC nel dialog Add Device, nascosto per EmotiBit
- Correzione: niente "LED blinking" — Polar H10 non ha LED, si attiva solo se indossato sul petto con elettrodi inumiditi
- DISTRIBUTION.md aggiornata con: distinzione **SmartScreen standard** vs **Smart App Control** (Win 11), opzioni Win 10/11 per Option B, fallback a launcher .bat
- Nuovo file `Avvia BiHome (da sorgente).bat` per bypassare Smart App Control
- Nuovo file `HTI.md` (analisi human-tech interaction, 13 sezioni)
- Shortcut sul desktop creato via PowerShell (`C:\Users\vitto\Desktop\BiHome Wearable.lnk` → punta al .bat)

---

## 7. Convenzioni / decisioni architetturali

**Naming stream LSL**: `P01_PolarECG`, `P01_PolarACC`, `P01_EmoPPG`, …
Il prefisso `P0N_` è enforced dal backend, parsato dal viewer.

**Marker stream**: una sola, condivisa fra tutti i partecipanti, int32,
timestamp = `pylsl.local_clock()` catturato nell'handler del bottone
(non al flush).

**Schema versioning**: ogni `.json` ha un `_schema_version` (oggi è 1).
Migrazioni in `_load_devices_registry` / `_load_wizard_defaults`.

**No automation surprises**: REC è sempre esplicito, mai automatico.
Markers stampati a click-time, mai a flush-time.

**File-based bridge backend↔viewer**: `active_participants.json` invece
di parsare nomi degli stream LSL.

**Build mode**: `--windowed` (no console), `--onedir` (no --onefile),
il viewer è un subprocess separato.

---

## 8. Build e test

**Sviluppo (run da sorgente):**
```cmd
cd C:\Users\vitto\Desktop\BiHome_wearable
python BiHome_wearable.py
```

**Build exe:**
```cmd
cd C:\Users\vitto\Desktop\BiHome_wearable
python build_exe.py
```
Note operative:
- Se il build fallisce con `PermissionError` su `build/` o `dist/`,
  rinomina la dir vecchia (`mv "dist/BiHome Wearable" "dist/BiHome Wearable.old.$$"`)
  invece di cancellarla — Windows ha lock per AV/Explorer.
- Tempo build ~3 min (PyInstaller + collect-all numpy/pylsl/pyqtgraph/bleak).

**Run exe:**
- Doppio click su `dist/BiHome Wearable/BiHome Wearable.exe`
- Su Win 11 con Smart App Control attivo: bloccato senza bypass. Usare
  `Avvia BiHome (da sorgente).bat` invece.

**Diagnostica BLE standalone:**
```cmd
python diag_bleak.py
```

---

## 9. Test plan corrente

Pre-distribuzione, da fare su almeno 2 macchine (dev + altra):
- [ ] First launch: wizard apre, "+ Add device…" funziona, scan BLE
      lista Polar, validazione seriale EmotiBit (regex)
- [ ] Sessione 2 Polar + 2 EmotiBit: tutti gli stream attivi, viewer
      apre 2 finestre, plot continui
- [ ] REC start/stop: file XDF + CSV in `%APPDATA%/BiHome/recordings/`
- [ ] Marker: click → timestamp coerente con local_clock
- [ ] Disconnessione Polar → riconnessione automatica
- [ ] Quit app → tutti i thread chiusi puliti (no orphan processes)
- [ ] Acquisition.log popolato senza errori non gestiti

---

## 10. TODO / open items

**Bug minori noti:**
- `_resolve_app_data_dir` ha un fallback wording leggermente diverso fra
  `BiHome_wearable.py` e `Viewer/lsl_viewer.py` (cosmetico, stesso
  risultato in pratica).

**Quality of life da valutare:**
- Tutorial in-app guidato al primo avvio (overlay)
- Template per-esperimento (`%APPDATA%/BiHome/templates/`)
- Keyboard shortcuts per REC / marker
- Tema alto contrasto / accessibility audit
- Code-signing dell'exe (~$200/anno, risolve Smart App Control)
- Port macOS / Linux (stack è già cross-platform, manca solo testarlo)

**Nessun TODO bloccante** al momento. App è pronta per uso interno.

---

## 11. Playbook per Claude (lettura prioritaria)

Quando ricevi questo file in una nuova chat e Vittorio chiede di
lavorare a BiHome, leggi nell'ordine:

1. **Questo file** (CHAT_CONTEXT.md) — overview e stato corrente
2. **README.md** — setup dev
3. **DISTRIBUTION.md** — guida utente finale (utile per capire il
   contratto con l'utente)
4. **HTI.md** — design rationale, principi UX
5. **BiHome_wearable.py** — backend (~4200 righe, troppo per una
   lettura completa: usa `Grep` per cercare la funzione/classe
   rilevante alla richiesta)
6. **Viewer/lsl_viewer.py** — viewer (anche questo grosso)
7. **build_exe.py** — solo se cambi packaging

**Regole operative:**
- NON modificare i file in `%APPDATA%/BiHome/` direttamente (sono runtime
  state, modifica solo il codice che li legge/scrive).
- Per modifiche al backend: ricorda che il viewer è un subprocess
  separato — se cambi il contratto (es. naming stream), aggiorna
  anche `lsl_viewer.py`.
- Quando ribuildi: se il vecchio `dist/BiHome Wearable/` è loccato,
  rinominalo (`*.old.$$`) invece di cancellarlo.
- NON ridurre la verbosità di `acquisition.log` — è il principale
  strumento diagnostico in caso di problemi sul campo.
- Lingua: rispondi a Vittorio in italiano. Codice e commenti in inglese.

**Come Vittorio lavora:**
- Sprint piccoli, audit fra uno sprint e l'altro
- Approva con "vai" / "perfetto" / "fai questo"
- Test in locale prima, poi su seconda macchina
- Preferisce vedere cosa cambierà PRIMA di applicare modifiche grosse;
  per fix piccoli (1-2 righe) è ok procedere e descrivere dopo

---

## 12. Stato corrente al 2026-05-26

- Exe buildato: `dist/BiHome Wearable/BiHome Wearable.exe` (13:08)
- Shortcut desktop creato (punta al .bat per bypass Smart App Control)
- HTI.md creato
- Tutti i fix Sprint 4.1 + post-4.1 applicati
- Vittorio ha Smart App Control attivo sulla sua macchina → usa il .bat
- Prossimo step: test in locale del flow completo (wizard → 2 Polar +
  2 EmotiBit → REC → STOP → verifica XDF), poi test su seconda macchina

**Per Claude**: se Vittorio chiede di continuare il lavoro, chiedi
prima cosa vuole fare. Non assumere che il lavoro pendente debba essere
ripreso da dove l'ho lasciato — possono essere passati giorni.
