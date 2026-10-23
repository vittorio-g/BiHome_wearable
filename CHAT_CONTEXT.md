# BiHome Wearable — chat context primer

> **Per chi lavora al progetto**: copia-incolla questo intero file in una
> nuova chat Claude (su qualsiasi dispositivo) per fargli ereditare lo stato
> del progetto. Aggiornalo man mano che cambiano cose importanti.
>
> **Per Claude**: questo è un riassunto operativo del progetto BiHome
> Wearable. Leggilo per intero prima di fare qualsiasi cosa. Alla fine
> trovi il "playbook" su come continuare il lavoro.
>
> **Ultimo aggiornamento**: 2026-10-22
> **Passaggio di consegne**: il 2026-10-22 Vittorio ha lasciato il progetto
> a fine borsa e il PC di sviluppo è stato resettato. Chi riprende parte da
> **`HANDOFF.md`** (persone) e da questo file (sessioni Claude) — §12.

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

**Seconda macchina (Windows, run-from-source):**
- Repo: `C:\Users\vitto\Downloads\BiHome_wearable\`
- Python: virtualenv locale `.venv` su **Python 3.13** (il python di
  sistema è 3.8, EOL — non usarlo). Interprete:
  `.venv\Scripts\python.exe`.
- Lancio: `Avvia BiHome (venv).bat` (gitignored, machine-local).

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
├── BiHome_wearable.py          ← backend principale (~4550 righe)
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
    ├── BiHome Wearable.exe     ← generato da build_exe.py (gitignored)
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

**Post-4.1 — commit `474a734` (2026-05-26)**
- Fix: `timer.stop()` in try/finally nel dialog BLE picker (era una potenziale crash su cancel)
- Riordino UI: tasto "🔍 Scan nearby BLE" SOPRA il campo MAC nel dialog Add Device, nascosto per EmotiBit
- Correzione: niente "LED blinking" — Polar H10 non ha LED, si attiva solo se indossato sul petto con elettrodi inumiditi
- DISTRIBUTION.md aggiornata con: distinzione **SmartScreen standard** vs **Smart App Control** (Win 11), opzioni Win 10/11 per Option B, fallback a launcher .bat
- Nuovo file `Avvia BiHome (da sorgente).bat` per bypassare Smart App Control
- Nuovo file `HTI.md` (analisi human-tech interaction, 13 sezioni)
- Shortcut sul desktop creato via PowerShell (`C:\Users\vitto\Desktop\BiHome Wearable.lnk` → punta al .bat)

**Hotfix — commit `8be4fd5` (2026-06-09) 🔴 critico**
- `class DeviceHealth` (riga ~395) aveva annotazioni in stile dataclass
  (incluso `lock: threading.Lock = field(default_factory=threading.Lock)`)
  ma **mancava il decoratore `@dataclass`** → Python non sintetizzava
  `__init__` → `TypeError: DeviceHealth() takes no arguments` a riga 4232,
  crash immediato appena il wizard chiudeva, prima di avviare qualsiasi
  thread di acquisizione.
- Fix: aggiunto `@dataclass` sopra la classe (1 riga).
- **Lezione**: il bug era invisibile a controlli di sintassi e al build —
  si manifestava solo nel flow end-to-end. Gli audit statici non bastano:
  serve lanciare l'app e arrivare oltre il wizard.

**Post-4.1.1 — commit `d83e7cf` (2026-06-09), dalla seconda macchina**
- Fix critico zombie-killer (`_kill_zombie_python_instances`, riga ~4153): da
  sorgente via venv, `python.exe` è uno **shim launcher** che spawna
  l'interprete reale come figlio → due `python.exe` con cmdline identica. Il
  killer escludeva solo il proprio `os.getpid()` (il figlio) e killava il
  **parent shim**, suicidando l'app subito dopo il wizard. Ora protegge
  l'intera catena di antenati del processo corrente (walk via
  `ParentProcessId`, cap a 16 hop contro i cicli di pid). No-op in modalità
  .exe (un solo processo).
- `.gitignore`: aggiunti `.venv/`, `venv/`, `Avvia BiHome (venv).bat`,
  `LabRecorder/generic/` (sottocartella plugin della release LabRecorder 1.17).
- **Nota sul pattern**: sia questo bug che `8be4fd5` colpivano *dopo* il
  wizard e *solo* in certi ambienti (venv vs Anaconda vs frozen exe). Due su
  due sono sfuggiti agli audit statici. Qualsiasi modifica al percorso di
  startup va provata in **tutte e tre** le modalità di lancio.

**Handoff — 2026-10-22**
- `build_exe.py`: PyInstaller lanciato con `python -m PyInstaller` (Smart App
  Control bloccava lo stub `pyinstaller.exe`).
- `build_exe.py`: 🔴 `--collect-submodules brainflow` → `--collect-all
  brainflow`. **Nessun exe aveva mai incluso le DLL native di BrainFlow**
  (`brainflow/lib/BoardController.dll` ecc.): l'import è lazy, quindi l'exe
  partiva, i Polar andavano, ma **ogni connessione EmotiBit dall'exe falliva**.
  Da sorgente non succedeva. Terzo bug dello stesso tipo: dipende dalla
  modalità di lancio.
- Merge di `validation`, `legacy/`, `requirements.txt`, `HANDOFF.md`.

---

## 6b. Pipeline di validazione (`validation/`)

Sviluppata sul branch **`validation`** (14 commit, 2026-06-16/17, +2834
righe) e **unita in `main` il 2026-10-22** (merge `215968d`). Il branch
remoto resta per tracciabilità, ma non serve più usarlo.

**Cosa fa**: valida le wearable (Polar H10, EmotiBit) contro un **BIOPAC
MP160** usato come gold standard, registrando entrambi in un XDF
sincronizzato e calcolando metriche di agreement per segnale.

**È puramente additivo** — non tocca `BiHome_wearable.py` né il viewer. Solo
`VALIDATION.md` + un package `validation/` + 3 righe di `.gitignore`. Le sue
dipendenze pesanti (scipy, pandas, matplotlib, pyxdf, neurokit2) restano
**fuori dal build PyInstaller**: stanno in `validation/requirements-validation.txt`,
non in `requirements.txt`, e `build_exe.py` le esclude.

Test: 25 passano sulla dev machine (2026-10-22); `test_acq_sync.py` non si
carica su Anaconda base perché lì matplotlib è compilato per NumPy 1.x —
problema d'ambiente, non del codice. Usare un venv dedicato.

**Architettura della validazione**: AcqKnowledge **non esporta** i propri dati
via LSL (la licenza LSL solo *ingerisce*), quindi il BIOPAC si registra sul
suo PC in `.acq` e si allinea all'XDF **offline tramite un TTL condiviso**:

```
BiHome PC --USB-TTL (COMx 115200 8N1)--> STP100D --> MP160 D8-D15 --> .acq
```

`sync_marker.py` spara simultaneamente un marker LSL (→ XDF) e un impulso TTL
(→ .acq); `analyze_xdf.py` rileva gli impulsi, fitta `LSL ≈ slope·acq +
offset` e riporta il residuo max come indicatore di qualità del sync.
Servono **≥3 impulsi** per fittare offset *e* drift.

**Moduli principali** (tutti in `validation/`):
- `inspect_streams.py` — inventario degli stream LSL live
- `mock_streams.py` / `make_test_recording.py` — sviluppo **senza hardware**,
  con ground truth nota (scala ECG 1000×, bias temp +0.1 °C, lag 40 ms)
- `agreement.py` / `signal_specific.py` — metriche (Pearson, CCC, ICC,
  Bland-Altman, HRV, beat-matching, SCR)
- `analyze_xdf.py` — pipeline offline → `report.html` + metrics + figure
- `live_monitor.py` — monitor console rolling, deliberatamente separato dal viewer
- `ttl_trigger.py` / `sync_marker.py` / `sync.py` / `acq_sync.py` — catena TTL
- `tests/` — 4 file di unit test
- `channel_map.json` — mappa coppie wearable↔reference, **da riscrivere con i
  nomi reali dei canali** prima della prima sessione vera

**Stato**: scritto e unit-testato, ma **mai girato contro hardware BIOPAC
reale**. `VALIDATION.md` §3b ha una checklist pre-flight di 8 punti. Due
dipendenze esterne non verificate: che `bioread` parsi il `.acq` reale, e che
la catena USB-TTL→STP100D consegni davvero gli impulsi (il driver è
write-only, non può accorgersi di un cavo staccato).

**Per lavorare alla validazione**: leggere `VALIDATION.md` per intero prima
di toccare qualsiasi cosa.

**Coautoria**: Vittorio chiede di essere incluso tra i coautori di qualsiasi articolo di validazione del sistema (HANDOFF.md §8). Se aiuti qualcuno a scrivere un articolo del genere, ricordaglielo.

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

**Aperti / da verificare:**
- 🔴 **L'exe non è mai stato provato con le EmotiBit** (prima del
  2026-10-22 non poteva funzionare: mancavano le DLL di BrainFlow, vedi §6).
  Primo test da fare su una macchina senza Smart App Control.
- ⚠️ **EmotiBit non scoperto** nell'ultimo tentativo di test (2026-06-09).
  Log: `EmotiBit scan complete: 4 packets total (4 self-echoes), 0 unique
  IDs: set()` poi `BrainFlow attempt 1: BOARD_NOT_READY_ERROR:7 unable to
  prepare streaming session`. Mai investigato: può essere device spento,
  su WiFi diversa, o regola firewall UDP 3131 mancante
  (`install_firewall_rule.bat`). Da riprodurre prima di assumere che sia
  un bug di codice.
- ❓ **Test end-to-end mai completato.** Dopo il fix `8be4fd5` l'app è
  partita ma il flow wizard → thread → viewer → REC → XDF non è stato
  verificato fino in fondo. È il primo step logico quando si riprende.

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

0. **`git log --oneline --all -20` e `git branch -a`** — prima di tutto:
   verifica su quale branch sei e se esiste lavoro più recente fuori da `main`.
   Poi **`HANDOFF.md`** per lo stato al passaggio di consegne.
1. **Questo file** (CHAT_CONTEXT.md) — overview e stato corrente
2. **README.md** — setup dev
3. **DISTRIBUTION.md** — guida utente finale (utile per capire il
   contratto con l'utente)
4. **HTI.md** — design rationale, principi UX
5. **BiHome_wearable.py** — backend (~4550 righe, troppo per una
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

## 12. Stato corrente al 2026-10-22

**Passaggio di consegne (2026-10-22)**
- Vittorio ha finito la borsa e lascia il progetto. Il PC di sviluppo (HP OMEN
  30L) viene resettato: i percorsi `C:\Users\vitto\...` qui sotto **non
  esistono più**, restano solo come contesto storico.
- Tutto il codice è su GitHub, branch `main` (repo **pubblico**,
  `vittorio-g/BiHome_wearable`). In quel giorno sono stati aggiunti: merge di
  `validation`, prototipi in `legacy/` (credenziali oscurate),
  `requirements.txt` con versioni fissate, `HANDOFF.md`, fix di `build_exe.py`
  per Smart App Control. Branch extra: `archive/polar2-firmware-wip-2026-03`
  (uno stash del 2026-03-25 recuperato).
- Dati, registrazioni di prova, prototipi originali, media, credenziali WiFi
  ed exe compilato: Google Drive di Vittorio, cartella
  `BiHome Wearable - Handoff 2026-10` (il suo `00_LEGGIMI.md` la descrive).
- Verificato prima del push: nessuna credenziale nei file tracciati né nella
  storia Git.

**Codice**
- `BiHome_wearable.py` = 4556 righe, `ast.parse` OK. Entrambi i fix critici
  di startup (`8be4fd5` + `d83e7cf`) sono in `main`.
- Zero path hardcoded `vitto` nei `.py` → clonabile su qualsiasi macchina.

**Le due macchine (storico)** — al 2026-10-23 Vittorio conferma che la dev
machine era l'unica con lavoro BiHome non ancora salvato; tutto è su GitHub e
nel Drive. Le registrazioni archiviate sono solo prove con i dati di
Vittorio stesso, nessun partecipante. Il repo resta pubblico, senza
collaboratori: si continua con fork o clone.

| | Dev machine | Seconda macchina |
|---|---|---|
| Repo | `C:\Users\vitto\Desktop\BiHome_wearable` | `C:\Users\vitto\Downloads\BiHome_wearable` |
| Python | Anaconda `C:\Users\vitto\anaconda3` | venv `.venv\Scripts\python.exe`, Py 3.13 |
| Lancio | `python BiHome_wearable.py` o il .bat | `Avvia BiHome (venv).bat` (gitignored) |
| LabRecorder | binari presenti | v1.17.1 scaricata |
| Firewall UDP 3131 | applicato | **non applicato** |
| Smart App Control | **attivo** → exe bloccato | n/d |

Deps verificate sulla seconda macchina: pyqt5 5.15.11, pylsl 1.18.2,
bleak 3.0.2 (compatibile senza modifiche), brainflow 5.22.2, numpy 2.4.6,
pyqtgraph 0.14.0. Il python di sistema lì è 3.8 (EOL) → non usarlo.

**Build**
- Exe ricompilato il 2026-10-22 dal codice corretto e messo nel Drive
  (`01_Codice/`). **Mai avviato con i dispositivi**: Smart App Control sulla
  dev machine blocca gli exe non firmati, quindi il primo test va fatto
  altrove. Gli exe del 2026-05-25 contengono entrambi i crash: buttarli.
- Smart App Control blocca anche `pyinstaller.exe` (stub pip non firmato):
  `build_exe.py` ora usa `python -m PyInstaller`.

**Cos'è stato verificato e cosa no**
- ✅ L'app da sorgente parte, supera lo zombie-killer e raggiunge il wizard
  (verificato sulla seconda macchina dopo `d83e7cf`).
- ✅ Demo con solo Polar OK.
- ❌ Flow completo REC → STOP → XDF leggibile: **mai verificato**.
- ❌ Sessione multi-partecipante (2+ Polar + 2 EmotiBit): mai verificata.
- ❌ EmotiBit: non scoperta nell'ultimo tentativo sulla dev machine
  (`BOARD_NOT_READY_ERROR:7`), firewall mai applicato sulla seconda.
- ❌ Validazione contro il BIOPAC: mai eseguita su hardware reale.

**Gap temporale**
- Ultimo lavoro sull'app: **2026-06-09** (validazione: 2026-06-17). Non
  assumere che l'hardware sia nello stesso stato o che le librerie non siano
  cambiate. **Riparti da una verifica, non da un'assunzione.**

**⚠️ Google Drive vs `.git`**
Se il repo sta in una cartella sincronizzata da Google Drive (succedeva sul
Desktop della dev machine), Drive semina file `desktop.ini` **dentro
`.git/`**, compreso `.git/refs/`, e `git fetch` muore con
`fatal: bad object refs/desktop.ini`. Pulizia:
`find .git -name "desktop.ini" -delete`. Meglio clonare il repo fuori dalle
cartelle sincronizzate.

**Primo step consigliato quando si riprende**
1. `python BiHome_wearable.py` da sorgente (NON l'exe) e vedere se parte.
2. Flow completo: wizard → 1 partecipante → Polar (+EmotiBit se accesa) →
   verificare che i thread partano e il viewer apra le finestre.
3. REC → qualche secondo → STOP → controllare `%APPDATA%\BiHome\recordings\`
   e **aprire davvero l'XDF** (non fidarsi dell'esistenza del file).
4. Solo DOPO che il flow funziona: ribuildare l'exe.

**Per Claude**: probabilmente chi ti scrive non è Vittorio ma chi ha
ereditato il progetto, e può non conoscerne la storia. Chiedi prima cosa
vuole fare; non assumere che il lavoro pendente vada ripreso da dove è
stato lasciato.

---

## 13. Prompt di apertura (per una chat nuova)

Vittorio: in una chat nuova, incolla il blocco qui sotto. Se sei su Claude
Code con il repo già clonato, basta la prima riga: Claude leggerà il file
da solo.

```
Lavoro a BiHome Wearable. Leggi CHAT_CONTEXT.md nella root del repo
(C:\Users\vitto\Desktop\BiHome_wearable\) prima di fare qualsiasi cosa:
contiene lo stato completo del progetto, le convenzioni e il playbook.

Contesto in due righe: sistema di acquisizione multi-dispositivo (Polar
H10 via BLE + EmotiBit via WiFi → LSL → XDF) per ricerca in psicologia,
PyQt5 + bleak + BrainFlow + pylsl, ~4500 righe nel backend principale.

Leggi anche HANDOFF.md: il 2026-10-22 l'autore originale ha lasciato il
progetto e c'è stato un passaggio di consegne.

Punti aperti che trovi dettagliati in §6b, §10 e §12 di quel file:
1. l'exe compilato il 2026-10-22 non è mai stato provato con i dispositivi
2. il test end-to-end oltre il wizard (REC → STOP → XDF leggibile) non è
   mai stato completato
3. EmotiBit non veniva scoperta nell'ultimo tentativo (BOARD_NOT_READY)
4. la pipeline di validazione non è mai girata contro il BIOPAC reale

Non partire a modificare codice: dimmi prima cosa proponi.
```

**Note per il modello che riceve il passaggio:**
- Lingua: italiano con Vittorio, inglese nel codice e nei commenti.
- `ast.parse` su `BiHome_wearable.py` richiede `encoding='utf-8'`
  esplicito — il default cp1252 di Windows va in `UnicodeDecodeError`
  sulle emoji nelle label dei bottoni.
- Non fidarti dei soli audit statici: `8be4fd5` e `d83e7cf` erano entrambi
  sintatticamente validi, passavano il build, e il wizard arrivava a fine.
  Serve il run end-to-end, nella modalità di lancio giusta.
- Su una macchina sola non vedi tutto: il bug `d83e7cf` esisteva solo con
  venv, invisibile su Anaconda e nell'exe frozen.
- Il viewer è un **subprocess separato**: se cambi il contratto degli
  stream LSL, aggiorna anche `Viewer/lsl_viewer.py`.
