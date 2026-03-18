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
| **EmotiBit** | PPG, EDA, temperatura, IMU 9-axis | WiFi/UDP → BrainFlow → PC (auto-discovery via UDP broadcast) |
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
| `PolarH10_Sens` | BIO | `ecg`, `ax`, `ay`, `az`, `beat` | 130 Hz | Arduino MKR → Polar H10 |
| `EmotiBit_IMU` | BIO | `acc_x/y/z`, `gyro_x/y/z`, `mag_x/y/z` | variabile | EmotiBit |
| `EmotiBit_PPG` | BIO | `ppg_0`, `ppg_1`, `ppg_2` | variabile | EmotiBit |
| `EmotiBit_EDA_TEMP` | BIO | `eda`, `temp` | variabile | EmotiBit |
| `Clock_ArduinoWiFi_ECG` | CLOCK | 10 canali sync | irregolare | interno |
| `Clock_ArduinoUSB_Polar` | CLOCK | 10 canali sync | irregolare | interno |
| `Clock_EmotiBit` | CLOCK | 4 canali sync | irregolare | interno |

Il canale `beat` in `PolarH10_Sens` vale **1.0** nel campione in cui viene confermato un R-peak, **0.0** altrimenti. È calcolato interamente in Python (non arriva dal firmware). Vedi sezione [R-peak detection](#rilevamento-r-peak-e-canale-beat).

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

## Rilevamento R-peak e canale `beat`

Il canale `beat` nello stream `PolarH10_Sens` è calcolato in tempo reale in Python dalla classe `PolarECGImputer`. Vale **1.0** per il campione in cui viene confermato un R-peak, **0.0** per tutti gli altri.

### Pipeline di detection

La detection è ritardata di `POST_S = 0.40 s`: quando `beat=1.0` appare al campione con timestamp `T`, il vero R-peak è avvenuto a circa `T − 0.40 s`. Questo ritardo è necessario per verificare la finestra di conferma post-picco e non può essere eliminato senza perdere accuratezza.

**Passi per ogni campione:**

1. **Local-max/min check** (±`LOCAL_WIN = 5` campioni, ~38 ms a 130 Hz)
   Il campione candidato deve essere il massimo assoluto (o il minimo, per ECG invertito) nella sua finestra locale ristretta — non della finestra intera PRE+POST (0.65 s), che a frequenze cardiache normali contiene due R-peak e renderebbe il confronto fuorviante.

2. **Sharpness discriminator** (`SHARP_FRAC = 0.40`)
   Il segnale deve variare di almeno il 40 % dell'ampiezza locale nei `LOCAL_WIN` campioni che precedono il candidato. I picchi R hanno un'upstroke di 20–40 ms → superano la soglia. Le onde T hanno un'upstroke di 80–150 ms → vengono rigettate.

3. **Soglia adattiva sull'ampiezza** (`AMP_FRAC = 0.55`)
   L'ampiezza del candidato deve raggiungere almeno il 55 % della media mobile esponenziale (EMA, α = 0.2) dei picchi accettati in precedenza. Prima dell'inizializzazione dell'EMA si usa la soglia fissa `MIN_AMP_UV = 60 µV` (calcolata sulla finestra locale di 38 ms, non sull'intera finestra).

4. **Periodo refrattario** (`REFRACT_S = 0.35 s = 45 campioni`)
   Dopo ogni R-peak confermato, la detection è disabilitata per 350 ms — impedisce doppie detezioni sul versante discendente o sull'onda S.

5. **Guard MIN_RR in `_ingest`**
   Prima di confermare un picco, viene verificato che `peak_ts − last_peak_ts ≥ MIN_RR_S = 0.35 s`. Questo controllo copre anche il caso in cui `last_peak_ts` sia stato avanzato dalla *gap prediction* (vedi sotto), prevenendo doppie detezioni successive a una previsione.

### Gap prediction

Se non viene rilevato nessun R-peak per più di `1.8 × RR_medio` (dove `RR_medio` è la media degli ultimi 10 intervalli validi) e si hanno almeno 2 intervalli RR in storia, il sistema **predice** un battito mancante:

```
_last_peak_ts  +=  RR_medio
is_beat = True
```

Questo segnala nell'output che "un battito avrebbe dovuto esserci qui" senza modificare il segnale ECG.

### Output guard anti-doppio

Gap prediction e detection reale possono sparare entrambi entro pochi campioni (la previsione è retroattiva ma compare nel timestamp corrente). Per evitare due `beat=1.0` a distanza <`MIN_RR_S` nel tempo di stream, un secondo livello di guardia nell'output di `push()` sopprime il secondo evento se `ts_corrente − ts_ultimo_beat < MIN_RR_S`.

```
Scenario senza guard:
  t=6.00  is_beat=True  (gap prediction → beat previsto a 4.8 s)
  t=6.05  is_beat=True  (detection reale → R-peak a 5.65 s)   ← doppio

Scenario con guard:
  t=6.00  is_beat=True  ✓
  t=6.05  is_beat=False ✓  (soppresso: 0.05 s < MIN_RR_S = 0.35 s)
```

### Imputazione dei gap BLE (ECG sintetico)

Quando il BLE perde pacchetti, il firmware marca i campioni mancanti come `NaN`. Con `ENABLE_ECG_IMPUTATION = True`, il filler:

1. Calcola la fase nel ciclo RR al momento del gap
2. Piazza il template del battito (media degli ultimi `MAX_BEATS = 8` battiti, baseline sottratta) a ogni posizione di battito predetta dentro il gap
3. Restituisce campioni sintetici con timestamp corretti e `beat=1.0` sui picchi imputati

```
Segnale reale:    ____/\____/\____[  NaN gap  ]____/\____
Segnale imputato: ____/\____/\____[/\__synth__]____/\____
                                   ↑ beat previsto dal template + RR
```

I campioni imputati hanno ECG sintetico e `ax/ay/az = NaN` (nessun dato accelerometro per il gap).

### Parametri di riferimento

| Parametro | Valore | Significato |
|---|---|---|
| `FS` | 130 Hz | Frequenza campionamento (da `POLAR_2.ino`) |
| `PRE_S` / `POST_S` | 0.25 s / 0.40 s | Finestra attorno al picco |
| `LOCAL_WIN` | 5 campioni (~38 ms) | Half-width per local-max check e sharpness |
| `MIN_RR_S` / `MAX_RR_S` | 0.35 s / 1.60 s | Range fisiologico RR (170–37 bpm) |
| `REFRACT_S` | 0.35 s | Periodo refrattario post-picco |
| `AMP_FRAC` | 0.55 | Soglia adattiva (55 % dell'EMA) |
| `MIN_AMP_UV` | 60 µV | Soglia fissa per bootstrap dell'EMA |
| `SHARP_FRAC` | 0.40 | Soglia sharpness (upstroke/ampiezza) |
| `MAX_BEATS` | 8 | Beat nel template |
| `IMPUTER_MAX_GAP_S` | 4.0 s | Gap oltre il quale non si imputa |

### Attivazione / disattivazione

```python
ENABLE_ECG_IMPUTATION = True    # True = imputa i gap BLE, False = NaN puri
IMPUTER_MAX_GAP_S     = 4.0     # gap più lunghi non vengono imputati
# La detection R-peak (canale beat) è sempre attiva indipendentemente da questo flag
```

---

## Filtro lineare sui segnali

I segnali ECG (WiFi e Polar H10) e PPG (EmotiBit) vengono opzionalmente filtrati in Python prima dell'invio all'outlet LSL tramite una **media mobile pesata causale** (no dipendenze esterne, puro Python).

### Kernel usato

```
ECG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]   # finestra triangolare simmetrica a 5 tap
PPG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]   # stesso kernel
```

La finestra triangolare assegna peso massimo al campione corrente (3) e pesi decrescenti ai campioni precedenti (2, 1), ottenendo un'attenuazione delle alte frequenze senza annullare i picchi del segnale.

**Ritardo di gruppo:** ~2 campioni (causale).

| Segnale | Frequenza | Ritardo introdotto |
|---|---|---|
| ECG WiFi | 250 Hz | ~8 ms |
| ECG Polar H10 | ~130 Hz | ~15 ms |
| PPG EmotiBit | ~25 Hz | ~80 ms |

> Il ritardo è identico su tutti i campioni dello stesso stream, quindi **non altera l'allineamento temporale tra stream diversi** — i timestamp di ogni campione non vengono modificati, viene filtrato solo il valore numerico.

### Canali filtrati

- **ECG WiFi** (`ArduinoWiFi_ECG`): canale `ecg` — tutti i campioni passano per il filtro.
- **Polar H10** (`PolarH10_Sens`): solo il canale `ecg` (indice 0) — i canali `ax`, `ay`, `az` vengono trasmessi **senza filtro**.
- **EmotiBit PPG** (`EmotiBit_PPG`): tutti e tre i canali `ppg_0`, `ppg_1`, `ppg_2`.
- **EmotiBit IMU / EDA / TEMP**: non filtrati.

### Attivazione / disattivazione

Basta cambiare il flag in cima a `BiHome_wearable.py`:

```python
ENABLE_SIGNAL_FILTER = True   # True = filtra, False = segnale grezzo
```

### Personalizzazione del kernel

I pesi si cambiano direttamente in cima allo script (nella sezione `USER CONFIG`):

```python
ECG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]   # 5-tap triangolare (default)
PPG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]

# Esempi alternativi:
# media mobile semplice 7 tap:  [1, 1, 1, 1, 1, 1, 1]
# finestra più stretta 3 tap:   [1, 2, 1]
```

Qualunque lista di pesi positivi funziona — la classe `SignalFilter` normalizza automaticamente.

---

## Come avviare l'acquisizione

1. **Alimentare e connettere i dispositivi**:
   - Arduino MKR ECG: connesso alla rete WiFi `BiHOME`, IP `192.168.50.174`
   - Arduino MKR Polar: collegato via USB su `COM5`
   - EmotiBit: connesso alla stessa rete WiFi (auto-discovery via UDP broadcast, nessun IP da configurare)

2. **Configurare i feature flags** in `BiHome_wearable.py`:
   ```python
   ENABLE_ARDUINO_WIFI_ECG = True   # ECG analogico WiFi
   ENABLE_ARDUINO_USB_POLAR = True  # Bridge Polar H10
   ENABLE_EMOTIBIT = True           # EmotiBit
   ENABLE_SIGNAL_FILTER = True      # Filtro media mobile pesata su ECG e PPG
   ENABLE_ECG_IMPUTATION = True     # Imputazione gap BLE con ECG sintetico
   ```

3. **Avviare lo script**:
   ```bash
   python BiHome_wearable.py
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
| `ECG:` | `wrap:<v>,us32:<v>,ecg_mV:<v>` | Campione ECG Arduino WiFi |
| `Sens:` | `wrap:<v>,us32:<v>,ecg:<v>,ax:<v>,ay:<v>,az:<v>` | Batch Polar H10 (ECG + ACC) |
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
| EmotiBit non trovata | Dispositivo non visibile sulla rete | Verificare che EmotiBit sia sulla stessa rete WiFi; BrainFlow fa auto-discovery via UDP broadcast (nessun IP da configurare) |
| Stream non visibili in LabRecorder | Script non avviato o firewall | Verificare che lo script giri e che la rete sia la stessa |
| UWB: `checkSPI KO` | Cablaggio SPI o reset | Verificare CS/MOSI/MISO/SCK e pin RSTn su D9 |
