# All Sensors — Acquisizione multi-dispositivo con Lab Streaming Layer

Questo progetto raccoglie dati fisiologici e di movimento da più sensori eterogenei, li sincronizza temporalmente e li pubblica come stream LSL (Lab Streaming Layer) che possono essere monitorati in tempo reale e registrati in formato `.xdf` per analisi offline.

---

## Obiettivo scientifico

Il setup è progettato per un contesto sperimentale che prevede:

- **Validazione Polar H10**: confronto della frequenza cardiaca/ECG della Polar H10 con un ECG di riferimento (Arduino MKR + elettrodi) per valutarne l'affidabilità prima dell'uso sperimentale.
- **Acquisizione fisiologica completa**: EmotiBit come sensore principale (PPG, EDA, temperatura, IMU), sempre attivo durante le sessioni.
- **Tracciamento posizione UWB** *(in sviluppo)*: localizzazione del soggetto sperimentale tramite triangolazione con 3 ancore UWB fisse + 1 tag UWB sul soggetto.

---

## Struttura della cartella

```
All_sensors/
├── All_Sensors.py              # Script principale Python: acquisizione, sync, LSL
├── README.md                   # Questo file
│
├── all_sensors/                # Firmware Arduino
│   ├── ECG.ino/
│   │   └── ECG.ino.ino         # Arduino MKR WiFi — ECG analogico → TCP → PC
│   ├── POLAR_2/
│   │   └── POLAR_2.ino         # Arduino MKR BLE — bridge Polar H10 → USB Serial → PC
│   ├── UWB_Ping/
│   │   └── UWB_Ping.ino        # DW3000 Initiator (PING) — DS-TWR ranging
│   └── UWB_Pong/
│       ├── UWB_TX.ino          # DW3000 TX test (sviluppo)
│       └── UWB_Pong/
│           └── UWB_Pong.ino    # DW3000 Responder (PONG) — DS-TWR ranging
│
├── data/
│   ├── import pyxdf.py         # Script per esportare .xdf → CSV + JSON meta
│   ├── Data_Exploration.R      # Analisi esplorativa in R
│   ├── exp001/
│   │   └── block_Prova_1.xdf   # Registrazione di esempio
│   ├── csv_export/             # Output CSV dello script pyxdf
│   └── Comparison_ecg/
│       └── Comparison_ecg.xdf  # Registrazione per validazione ECG vs Polar
│
└── Viewer/
    └── How to.txt              # Istruzioni rapide per visualizzare gli stream
```

---

## Hardware

| Dispositivo | Ruolo | Connessione |
|---|---|---|
| **Arduino MKR WiFi 1010** | ECG analogico (elettrodi) | WiFi TCP → PC (IP `192.168.50.174:5000`) |
| **Arduino MKR WiFi 1010** | Bridge BLE → USB per Polar H10 | USB Serial `COM5` @ 921600 baud |
| **Polar H10** | ECG + accelerometro (BLE PMD) | BLE → Arduino bridge |
| **EmotiBit** | PPG, EDA, temperatura, IMU 9-axis | WiFi/UDP → BrainFlow → PC (IP `192.168.50.163`) |
| **DW3000 (UWB)** *(in pausa)* | Ranging 2-way per localizzazione | SPI su Arduino |

### Note hardware

- Il **PIN 1** dell'Arduino MKR bridge Polar è usato come LED di connessione BLE.
- Il **PIN A0** dell'Arduino MKR ECG acquisisce il segnale analogico a 12 bit, `VREF = 3.3V`.
- L'**EmotiBit** è il sensore principale: tutti i suoi canali vengono acquisiti in ogni sessione.
- L'**UWB** è attualmente in pausa (in attesa di acquisto dei moduli). L'architettura prevista è: 1 tag mobile sul soggetto + 3 ancore fisse per triangolazione 3D.

---

## Flusso dati

```
Polar H10 ──BLE──► Arduino MKR (POLAR_2.ino) ──USB Serial──►
                                                              All_Sensors.py ──► LSL Outlets ──► LabRecorder / lsl_viewer
Arduino MKR (ECG.ino) ──WiFi TCP──────────────────────────►
EmotiBit ──WiFi/UDP──► BrainFlow ─────────────────────────►
```

---

## Stream LSL pubblicati

| Nome stream | Tipo | Canali | Samplerate | Sorgente |
|---|---|---|---|---|
| `ArduinoWiFi_ECG` | BIO | `ecg` | 250 Hz | Arduino MKR WiFi |
| `PolarH10_Sens` | BIO | `ecg`, `ax`, `ay`, `az` | 50 Hz* | Arduino MKR → Polar H10 |
| `EmotiBit_IMU` | BIO | `acc_x/y/z`, `gyro_x/y/z`, `mag_x/y/z` | variabile | EmotiBit |
| `EmotiBit_PPG` | BIO | `ppg_0`, `ppg_1`, `ppg_2` | variabile | EmotiBit |
| `EmotiBit_EDA_TEMP` | BIO | `eda`, `temp` | variabile | EmotiBit |
| `Clock_ArduinoWiFi_ECG` | CLOCK | 10 canali sync | irregolare | interno |
| `Clock_ArduinoUSB_Polar` | CLOCK | 10 canali sync | irregolare | interno |
| `Clock_EmotiBit` | CLOCK | 4 canali sync | irregolare | interno |

\* ECG e ACC della Polar vengono campionati entrambi a 50 Hz (configurabile nel firmware).

---

## Sincronizzazione temporale

La sincronizzazione è critica per allineare segnali da dispositivi con clock indipendenti. Ogni dispositivo usa un meccanismo diverso:

### Arduino MKR (ECG WiFi + Polar bridge)

Si usa un protocollo **NTP-like** implementato manualmente:

1. Il PC invia `SYNC:<seq>` al dispositivo.
2. Il dispositivo risponde immediatamente con `T:<seq>,w2,t2_32,w3,t3_32` dove `t2/t3` sono timestamp `micros64()` dell'Arduino.
3. Il PC calcola RTT, delay e offset tra il clock Arduino e `pylsl.local_clock()`.
4. L'offset viene lisciatosi con un filtro EMA (`alpha = 0.15`).
5. Ogni campione viene timbrato con il timestamp del dispositivo e convertito in tempo LSL tramite `t_host = t_dev - offset`.

**Timestamp Polar H10 (sensor-level):** I pacchetti PMD della Polar H10 contengono nei bytes 1-8 un timestamp a 64 bit in nanosecondi generato dall'orologio interno del sensore. Questo timestamp rappresenta il momento di campionamento dell'**ultimo campione** del batch. Il firmware `POLAR_2.ino` estrae questo timestamp, lo converte in microsecondi e lo usa come ancoraggio per back-calcolare i timestamp di tutti i campioni precedenti nel batch. Il timestamp del sensore viene poi convertito nel dominio del clock Arduino tramite un offset EMA stimato continuamente, mantenendo la compatibilità con il meccanismo di sync NTP verso il PC.

> **Perché è importante**: usare `micros()` all'arrivo della notifica BLE introdurrebbe una latenza variabile (~7.5–30 ms per connection interval) che sfaserebbe la Polar rispetto all'ECG Arduino (che invece timbra ogni campione al momento esatto di `analogRead()`).

### EmotiBit (BrainFlow)

BrainFlow fornisce i propri timestamp che vengono correlati a `pylsl.local_clock()` tramite un offset EMA (`alpha = 0.05`), aggiornato ad ogni batch di dati ricevuti.

### Formato timestamp Arduino (`micros64`)

I timestamp a 32 bit di `micros()` si azzerano ogni ~71 minuti (overflow uint32). Il firmware estende questo a 64 bit tracciando i wrap-around:

```c
uint64_t readMicros64() {
  uint32_t now = micros();
  if (now < last_us32) wrap32++;
  last_us32 = now;
  return (((uint64_t)wrap32) << 32) | (uint64_t)now;
}
```

Sul PC questi vengono ricostruiti da `(wrap, us32)` inviati via seriale/TCP.

---

## Dipendenze Python

```bash
pip install pylsl pyserial brainflow numpy
```

Per la visualizzazione degli stream (ambiente separato):

```bash
conda activate streamviewer310
lsl_status      # lista gli stream attivi
lsl_viewer      # visualizza i segnali in tempo reale
```

---

## Come avviare l'acquisizione

1. **Alimentare e connettere i dispositivi**:
   - Arduino MKR ECG: connesso alla rete WiFi `BiHOME`, IP `192.168.50.174`
   - Arduino MKR Polar: collegato via USB su `COM5`
   - EmotiBit: connesso alla stessa rete WiFi, IP `192.168.50.163`

2. **Configurare i feature flags** in `All_Sensors.py`:
   ```python
   ENABLE_ARDUINO_WIFI_ECG = True   # ECG analogico WiFi
   ENABLE_ARDUINO_USB_POLAR = True  # Bridge Polar H10
   ENABLE_EMOTIBIT = True           # EmotiBit
   ```

3. **Avviare lo script**:
   ```bash
   python All_Sensors.py
   ```

4. **Registrare**: usare **LabRecorder** (o `pylsl`) per salvare tutti gli stream in formato `.xdf`.

5. **Monitorare** (opzionale, terminale separato):
   ```bash
   conda activate streamviewer310
   lsl_viewer
   ```

---

## Analisi post-hoc

Lo script `data/import pyxdf.py` carica un file `.xdf` ed esporta:
- Un **CSV per stream** con colonna `time_stamps` + una colonna per canale
- Un **JSON di metadati** per stream (nome, tipo, samplerate, info LSL)
- Il **header XDF globale** in JSON

```bash
cd data
python "import pyxdf.py"
# output in: data/csv_export/
```

---

## UWB — Localizzazione *(in sviluppo)*

Il sistema UWB usa il protocollo **DS-TWR (Double-Sided Two-Way Ranging)** con chip **DW3000** per stimare la distanza tra coppie di nodi con precisione centimetrica.

### Architettura prevista

```
[Tag UWB sul soggetto]
        │
        ├──► Ancora A (DW3000 PONG)
        ├──► Ancora B (DW3000 PONG)
        └──► Ancora C (DW3000 PONG)
```

La posizione 3D viene stimata per trilaterazione dalle 3 distanze misurate.

### Stato attuale

- `UWB_Ping.ino`: firmware Initiator (PING) con DS-TWR + filtro FIR 7 punti (ritardo 3 campioni, warmup 7 misure).
- `UWB_Pong.ino`: firmware Responder (PONG) con DS-TWR.
- `UWB_TX.ino`: sketch di test per TX semplice.
- **In attesa**: acquisto dei moduli UWB aggiuntivi dall'università.

> Da integrare in futuro: stream LSL `UWB_Ranging` con canali `dist_A`, `dist_B`, `dist_C` (cm) e stream `UWB_Position` con `x`, `y`, `z`.

---

## Note sui protocolli seriali/TCP

### Comandi (PC → Arduino)

| Comando | Risposta | Descrizione |
|---|---|---|
| `SYNC:<seq>` | `T:<seq>,w2,t2_32,w3,t3_32` | Round-trip per stima offset clock |
| `led_on` | `ACK:led_on` | Accende LED builtin |
| `led_off` | `ACK:led_off` | Spegne LED builtin |

### Messaggi dati (Arduino → PC)

| Label | Formato | Descrizione |
|---|---|---|
| `ECG:` | `wrap,us32,ecg_mV` | Campione ECG Arduino WiFi |
| `Sens:` | `wrap,us32,ecg,ax,ay,az` | Batch Polar H10 (ECG + ACC) |
| `T:` | vedi sopra | Risposta sync |
| `INFO:*` | stringa | Messaggi diagnostici |
| `WARN:*` | stringa | Avvisi |
| `ERR:*` | stringa | Errori |

---

## Troubleshooting

| Problema | Causa probabile | Soluzione |
|---|---|---|
| Nessun dato dalla Polar | Arduino IDE Serial Monitor aperto | Chiudere il Serial Monitor (la porta COM è esclusiva) |
| `WARN:NO_ECG_DATA_YET` | Polar H10 lenta a rispondere al CMD ECG | Atteso: il firmware fa retry automatico |
| EmotiBit non trovata | IP errato o WiFi diverso | Verificare `EMOTIBIT_IP` in `All_Sensors.py` |
| Stream non visibili in LabRecorder | Script non avviato o firewall | Verificare che lo script giri e che la rete sia la stessa |
| UWB: `checkSPI KO` | Cablaggio SPI o reset | Verificare CS/MOSI/MISO/SCK e pin RSTn su D9 |
