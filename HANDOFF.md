# BiHome Wearable — passaggio di consegne

> Scritto il **2026-10-22**, a fine borsa di Vittorio Guerrieri, che ha
> sviluppato il progetto da novembre 2025. Da questa data il PC di sviluppo
> (HP OMEN 30L) viene resettato: **tutto quello che serve sta su GitHub e
> nell'archivio Google Drive** descritto qui sotto.

---

## 1. In una frase

App Windows che acquisisce in parallelo ECG/accelerometro da **Polar H10**
(Bluetooth) e PPG/EDA/temperatura/IMU da **EmotiBit** (WiFi) per 1–6
partecipanti, li sincronizza su **Lab Streaming Layer** e li registra in
`.xdf`. È pensata per ricercatori in psicologia non tecnici: un wizard guida
il setup, un viewer mostra i segnali dal vivo, un pulsante REC registra.

---

## 2. Dove sta tutto

| Cosa | Dove |
|---|---|
| **Codice** (tutto, con la storia completa) | GitHub `vittorio-g/BiHome_wearable`, branch `main` — repository **pubblico** |
| Firmware Polar via Arduino non committato (mar 2026) | branch `archive/polar2-firmware-wip-2026-03` |
| **Dati, registrazioni di prova, prototipi originali, video, credenziali** | Google Drive di Vittorio → cartella **`BiHome Wearable - Handoff 2026-10`** (leggere il suo `00_LEGGIMI.md`). È privata: accessibile solo se Vittorio la condivide. |
| Backup offline del repository | stessa cartella Drive → `01_Codice/BiHome_wearable.bundle` |
| Exe pronto da usare | stessa cartella Drive → `01_Codice/` (zip) |

Il branch `validation` (pipeline di confronto col BIOPAC) è stato **unito in
`main`** il 2026-10-22: non serve più cambiare branch per vederlo.

---

## 3. Mappa della documentazione

| File | Per chi | Contenuto |
|---|---|---|
| `HANDOFF.md` | chi riprende il progetto | questo file |
| `README.md` | sviluppatori | architettura, setup da sorgente, elaborazione dei segnali |
| `DISTRIBUTION.md` | utenti finali | usare l'exe, aggirare SmartScreen / Smart App Control, trovare MAC e seriali |
| `VALIDATION.md` | chi valida le wearable | protocollo BIOPAC MP160, sincronizzazione TTL, metriche di agreement |
| `HTI.md` | revisori / articoli | analisi di usabilità per ricercatori in psicologia |
| `CHAT_CONTEXT.md` | sessioni Claude | stato dettagliato del progetto da incollare in una chat nuova |
| `legacy/README.md` | curiosi | prototipi nov 2025 – mar 2026 e come si è arrivati all'architettura attuale |

---

## 4. Stato al 2026-10-22

**Funziona (verificato):**
- Wizard, registro dispositivi, scansione BLE in-app per trovare il MAC del Polar.
- Avvio da sorgente fino al wizard e oltre, su due macchine diverse (Anaconda
  e venv). Demo con un solo Polar riuscita.
- Pipeline di validazione: 25 unit test passano.

**Non verificato — da fare per primo:**
1. **Flusso completo con registrazione**: wizard → dispositivi → viewer →
   REC → STOP → aprire davvero l'`.xdf` (con `pyxdf` o MNE) e controllare che
   ci siano tutti gli stream e i marker.
2. **Sessione multi-partecipante** (2 Polar + 2 EmotiBit insieme).
3. **EmotiBit non trovata** nell'ultimo tentativo (`BOARD_NOT_READY_ERROR:7`).
   Prima di cercare bug nel codice: EmotiBit accesa e sulla stessa rete WiFi
   del PC, regola firewall UDP 3131 applicata (`install_firewall_rule.bat`,
   come amministratore).
4. **Validazione contro il BIOPAC mai eseguita su hardware reale.**
   `VALIDATION.md` §3b ha una checklist pre-volo di 8 punti.
5. **L'exe nel Drive è compilato dal codice corretto ma non è stato provato
   con i dispositivi** (Smart App Control sul PC di sviluppo ne blocca
   l'avvio). Il primo test va fatto su un'altra macchina.

**Tre bug critici già risolti:**
- `8be4fd5` — `@dataclass` mancante: l'app si chiudeva subito dopo il wizard.
- `d83e7cf` — con i venv lo zombie-killer uccideva il proprio processo
  padre: stessa chiusura, ma *solo* da venv.
- 2026-10-22 — nessun exe aveva mai incluso le DLL native di BrainFlow:
  l'exe partiva e i Polar funzionavano, ma **le EmotiBit fallivano sempre**.
  Da sorgente il problema non c'era. Corretto in `build_exe.py`.

Tutti e tre dipendevano dalla modalità di lancio: **ogni modifica va provata
sia da sorgente (Anaconda e venv) sia dall'exe.**

---

## 5. Hardware

Dal registro dispositivi dell'app (`%APPDATA%\BiHome\devices.json`).
**Da verificare fisicamente** al momento della consegna.

| Nome nell'app | Dispositivo | Identificativo |
|---|---|---|
| Polar 1 | Polar H10 | MAC `24:AC:AC:04:96:A3`, ID `0496A33F` |
| Polar 2 | Polar H10 | MAC `24:AC:AC:04:93:ED`, ID `0493ED32` |
| EmotiBit 1 | EmotiBit MD-V6 | seriale `MD-V6-0000482` |
| EmotiBit 2 | EmotiBit MD-V6 | seriale `MD-V6-0000089` |

Altro materiale usato nel progetto: router WiFi dedicato (nome rete e
password nel Drive), Arduino MKR WiFi (sketch in `all_sensors/`), moduli UWB DWM3000EVB (sketch
`UWB_Ping`/`UWB_Pong`, datasheet nel Drive). Per la validazione: BIOPAC
MP160 con AcqKnowledge, interfaccia STP100D e modulo USB-TTL.

> Il Polar H10 **non ha LED né pulsante**: si accende solo se indossato con
> gli elettrodi della fascia inumiditi. È la causa più comune di "Polar non
> trovato".

Le credenziali WiFi del router e i `config.txt` delle EmotiBit (che
contengono la password della rete) sono nel Drive, cartella
`90_RISERVATO_credenziali/`, **non** nel repository. Visto che sono rimaste
scritte in vari file, **conviene cambiare la password del router** dopo il
passaggio di consegne e aggiornare il `config.txt` sulla SD di ogni EmotiBit.

---

## 6. Ripartire da zero su un PC nuovo

```bash
git clone https://github.com/vittorio-g/BiHome_wearable.git
cd BiHome_wearable
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

1. **Python 3.12 o 3.13** (python.org o Anaconda). Su Windows usare
   `python -m pip` / `python -m PyInstaller`, non gli `.exe` in `Scripts\`:
   con Smart App Control attivo vengono bloccati.
2. **LabRecorder**: i binari non sono nel repo. Scaricare la release Windows
   da `github.com/labstreaminglayer/App-LabRecorder/releases` (usate la
   1.16.4 e la 1.17.1) e copiarne il contenuto in `LabRecorder/`. In
   alternativa c'è uno zip pronto nel Drive (`01_Codice/`).
3. **Firewall**: eseguire `install_firewall_rule.bat` come amministratore
   (apre UDP 3131 per la scoperta delle EmotiBit).
4. **Avvio**: `.venv\Scripts\python.exe BiHome_wearable.py`, oppure
   `Avvia BiHome (da sorgente).bat` (cerca `pythonw` nel PATH).
5. **Exe**: `python build_exe.py` → `dist\BiHome Wearable\`. La cartella va
   distribuita intera (modalità `--onedir`).
6. **Validazione** (facoltativa):
   `pip install -r validation/requirements-validation.txt`, poi seguire
   `VALIDATION.md`. Meglio un venv separato: matplotlib compilato per
   NumPy 1.x non convive con NumPy 2 (problema riscontrato su Anaconda base).

I dati di runtime (registrazioni, log, registro dispositivi) vanno in
`%APPDATA%\BiHome\`, non nella cartella del programma.

---

## 7. Il repository è pubblico

È stato verificato il 2026-10-22: nessuna password o token nei file né nella
storia Git, `secrets.h` mai committato, credenziali oscurate in `legacy/`.
Restano due registrazioni di prova tracciate (`data/exp001/block_Prova_1.xdf`,
`data/Comparison_ecg/Comparison_ecg.xdf`), che contengono solo dati di
Vittorio registrati su se stesso. **Se un giorno il repository
dovesse contenere registrazioni di partecipanti reali, va reso privato
prima** (GDPR): i dati di ricerca stanno nel Drive o nello storage del
laboratorio, mai qui.

Il repository resta sull'account GitHub personale di Vittorio, **pubblico e
senza collaboratori**: chiunque può clonarlo o farne un fork, nessun altro
può modificarlo. Per continuare il progetto: pulsante **Fork** su GitHub,
oppure `git clone` e push in un repository vostro. Il codice è sotto
**licenza MIT** (`LICENSE`): si può riusare e modificare liberamente,
mantenendo la nota di copyright.
