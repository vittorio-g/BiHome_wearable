# legacy/ — prototipi e versioni precedenti

Codice scritto **prima** dell'architettura attuale (`BiHome_wearable.py` +
`Viewer/`), tra novembre 2025 e marzo 2026. Non è mantenuto e non è usato
dall'app: sta qui come documentazione storica di come ci si è arrivati e
come fonte di misure già fatte (latenze, drift).

> **Credenziali oscurate.** Gli sketch Arduino originali contenevano SSID e
> password WiFi in chiaro. Qui sono sostituite da `YOUR_WIFI_SSID` /
> `YOUR_WIFI_PASSWORD`. Gli originali, con i **dati** delle prove (`.xdf`,
> `.csv`, video), sono nell'archivio Google Drive dell'handoff
> (`03_Prototipi_originali/`) — vedere `HANDOFF.md` nella root.

## Evoluzione in breve

1. **Arduino via seriale** (nov 2025) — fotoresistenza/sensori su Arduino,
   letti dal PC via USB-seriale e pubblicati su LSL.
2. **Arduino via WiFi** (dic 2025) — stessi sensori ma su TCP, con misure
   di latenza e drift del clock.
3. **EmotiBit + Arduino** (dic 2025 – gen 2026) — primo script
   multi-dispositivo con BrainFlow per EmotiBit.
4. **`modified_acq.py`** (mar 2026) — backend multi-dispositivo monolitico
   con time-sync e LSL; diretto predecessore di `BiHome_wearable.py`.
5. **Polar via Arduino** → abbandonato a favore del BLE diretto dal PC
   (commit `59ef439`). Il lavoro firmware non committato è sul branch
   `archive/polar2-firmware-wip-2026-03`.

## Contenuto

| Percorso | Cosa fa |
|---|---|
| `modified_acq.py` | Acquisizione multi-device + time-sync + streaming LSL (1281 righe). Predecessore di `BiHome_wearable.py`. |
| `prototipi/Prova_1/` | Primo sketch: fotoresistenza su A0 con LED a soglia. |
| `prototipi/Prova_2/` | Lettura/scrittura seriale PC↔Arduino (COM3, 9600 baud). |
| `prototipi/Prova_3/` | Ping seriale con payload da 1 a 512 byte per misurare la latenza. |
| `prototipi/Latency_measures/` | 600 misure di latenza seriale (una al secondo). |
| `prototipi/2 sensori e lsl/` | Due canali Arduino → stream LSL, + convertitore XDF→CSV. |
| `prototipi/Connessione_wifi_arduino/` | Arduino come server TCP (porta 5000) + client Python. |
| `prototipi/Calcolo Latenza e Drift a 4 punti con Wifi/` | Latenza e drift del clock su WiFi, misurati a 4 punti. |
| `prototipi/Emotibit/` | `Emotibit_1.py`: EmotiBit (BrainFlow) + Arduino lux su LSL. `find_emotibit_ip.py`: scansione della subnet per trovare l'EmotiBit. `import pyxdf.py`: export XDF→CSV. |
| `prototipi/Emotibit_Arduino/` | Variante successiva di `Emotibit_1.py` con LSL sempre attivo. |
| `arduino_sketchbook/` | Versione dello sketch WiFi presa dallo sketchbook dell'Arduino IDE (diversa da quella in `prototipi/`). |

Le porte seriali (`COM3`) e gli IP (`10.61.x.x`) sono quelli della macchina
e della rete di sviluppo: vanno adattati.
