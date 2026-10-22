import time
from datetime import datetime
import socket
import threading
import sys
import os
import select
from pylsl import StreamInfo, StreamOutlet, local_clock 

# TODO
# Leggi anche stato del led su arduino e mettere nello stesso csv di Sens
# LSL sempre attivo
# Le variabili varie verranno messe subito prima della funzione

# -----------------------------------------------------
# Feature flags
# -----------------------------------------------------

ENABLE_EMOTIBIT = True
ENABLE_ARDUINO = True

#mettiamo le caratteristiche delle arduino. I nomi per ora sono semplicemente A, B, C...
ARDUINO_INFO = {
    "qty": 2,  # numero di arduino collegati
    "nsensors": {"A":1, "B":3}, # numero di sensori per ogni arduino, es {arduino_id: num_sensori}
    "ip": {"A":"172.21.131.37", "B":"172.21.131.38"}, # ip per ogni arduino
    "port": {"A":5000, "B":5001}, # porta per ogni arduino
}

# Caratteristiche dell'emotibit
EMOTIBIT_IP = "172.21.131.255"  # SOSTITUIRE QUI IP
EMOTIBIT_PORT = 0
EMOTIBIT_TIMEOUT = 5
MOTIBIT_POLL_INTERVAL = 0.05 # Quanti secondi dorme emotibit tra una lettura e l'altra SE non arriva nulla

# -----------------------------------------------------
# (AGGIUNTO) Safe defaults per evitare NameError nel tuo snippet EmotiBit
# -----------------------------------------------------
# Nel codice EmotiBit ci sono riferimenti a variabili non definite nel frammento incollato.
# Questi default permettono di eseguire lo script senza crashare subito.
# Se nel tuo progetto reale le definisci altrove, puoi rimuovere queste righe.
PYLSL_AVAILABLE = True
EMOTIBIT_ENABLE_LSL = True
EMOTIBIT_LOG_FILE = "emotibit.csv"
EMOTIBIT_POLL_INTERVAL = MOTIBIT_POLL_INTERVAL

# -----------------------------------------------------
# (AGGIUNTO) stop_event globale usato da Arduino + EmotiBit
# -----------------------------------------------------
stop_event = threading.Event()

# -----------------------------------------------------
# (AGGIUNTO) Stato di sincronizzazione per ogni Arduino
# -----------------------------------------------------
# Perché serve: con più Arduino, offset/delay non possono essere globali,
# altrimenti A sovrascrive B e i timestamp diventano sbagliati.
arduino_sync_state = {}  # arduino_id -> dict(last_sync_t1, current_offset, current_delay, lock)

# -----------------------------------------------------
# Configurazione degli stream
# -----------------------------------------------------

# configurazione stream per ogni arduino. Si aprirà uno stream indipendendente per ogni arudino

outlets = {}  # dizionario globale per gli outlet LSL

# (AGGIUNTO) "type" dei canali nei metadati. Nel tuo snippet era stream_type ma non esisteva.
stream_type = "sensor"

for arduino_id in ARDUINO_INFO["ip"].keys(): # ciclo su ogni nome di arduino che ho dato prima
    
    # (AGGIUNTO) inizializzo lo stato di sync per questo Arduino
    arduino_sync_state[arduino_id] = {
        "last_sync_t1": None,
        "current_offset": None,
        "current_delay": None,
        "offset_lock": threading.Lock(),
    }

    # prendo il numero di canali, ip e port per questo arduino
    n_ch = ARDUINO_INFO["nsensors"][arduino_id]
    ip = ARDUINO_INFO["ip"][arduino_id]
    port = ARDUINO_INFO["port"][arduino_id]

    # Creo info stream LSL per questo arduino
    info = StreamInfo(
        name=f"Arduino_{arduino_id}", # nome dello stream es. Arduino_A
        type="AUX", # AUX per dati ausiliari, uguale per tutti
        channel_count=n_ch, # numero di canali per questo arduino
        nominal_srate=0.0, # 0 perché sono dati asincroni per avere timestamp precisi
        channel_format="float32", # formato dei dati, classico
        source_id=f"arduino_{arduino_id}_sensors", # id univoco per questo stream
    )
    
    # Creo i metadati per questo stream
    try:
        desc = info.desc()
        desc.append_child_value("arduino_id", str(arduino_id))
        desc.append_child_value("ip", str(ip))
        desc.append_child_value("port", str(port))
        desc.append_child_value("nsensors", str(n_ch))
    except Exception:
        pass
    
    # Creo i metadati per ogni canale
    try:
        chns = info.desc().append_child("channels")
        for i in range(n_ch):
            ch = chns.append_child("channel")
            ch.append_child_value("label", f"sensor_{i+1}")
            ch.append_child_value("type", stream_type)
    except Exception:
        pass
        
    # (MODIFICA) Nel tuo snippet originale c'era info_Sens (non definito).
    # Qui usiamo l'oggetto StreamInfo appena creato ("info").
    outlets[arduino_id] = StreamOutlet(info) # salvo i dettagli dell'outlet in un dizionario globale

print(f"Stream LSL creati: per arduino {list(ARDUINO_INFO['ip'].keys())}")


# ----------------------------------------------------
# HELPER per ricostruire tempi corretti Arduino
# ----------------------------------------------------

# Abbiamo un problema: arduino invia timestamp a 32 bit microsecondi, 
# che vanno in overflow ogni ~71 minuti.
# Per risolvere questo, Arduino invia anche un "wrap count" che indica
# quante volte il contatore a 32 bit è andato in overflow.
# Qui definiamo una funzione per ricostruire il timestamp a 64 bit.

US_WRAP = 2**32  # uguale a 4294967296, il valore di overflow a 32 bit

def arduino_us64(wrap: int, us32: int) -> int:
    return int(wrap) * US_WRAP + int(us32)

# -----------------------------------------------------
# HELPER per Connessione con Arduino
# -----------------------------------------------------

# questa funzione la useremo per connetterci ad ogni arduino
# ritorna il socket connesso

def arduino_connection(ip: str, port: int):
    soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    soc.connect((ip, port))
    # Lascia blocking. read_lines usa select, quindi non blocchi comunque.
    print("Connesso all'Arduino")
    return soc


# -----------------------------------------------------
# Invio/Ricezione messaggi Arduino
# -----------------------------------------------------

# serve a far sì che i due thread (main e reader) non scrivano contemporaneamente sul socket
send_lock = threading.Lock()

# funzione di invio messaggi ad arduino
def send_message(sock: socket.socket, command: str):
    try:
        
        # trasforma in bytes e aggiunge newline
        data = (command + "\n").encode("utf-8")
        
        #manda dati in modo thread-safe
        with send_lock:
            sock.sendall(data)
    
    except OSError as e:
        print(f"Errore invio messaggio {command!r}: {e}")

# funzione di lettura messaggi da arduino
def read_lines(sock: socket.socket, buffer: str, timeout: float = 0.1):

    # Ritorna: (buffer_aggiornato, lines, closed)
    #  - lines: lista di linee complete (senza '\n')
    #  - closed: True se socket chiuso (EOF)

    try:
        
        r, _, _ = select.select([sock], [], [], timeout)
        if not r:
            return buffer, [], False

        # Legge i dati disponibili. 4096 byte (dimensione massima) per volta
        chunk = sock.recv(4096)
        if not chunk:
            return buffer, [], True  # EOF

        buffer += chunk.decode("utf-8", errors="replace") # decodifica con replacement errori
        parts = buffer.split("\n") # spezza in linee
        buffer = parts[-1]              # Rimettiamo nel buffer l'eventuale parte incompleta
        lines = [p.strip() for p in parts[:-1] if p.strip()] # linee complete, senza spazi vuoti
        return buffer, lines, False

    except OSError as e:
        print(f"Errore lettura socket: {e}")
        return buffer, [], False


# -----------------------------------------------------
# ARD4. Sincronizzazione Arduino
# -----------------------------------------------------

def send_sync_request(arduino_id: str, sock, command="SYNC"):
    # (MODIFICA) last_sync_t1 non può essere globale con più Arduino
    arduino_sync_state[arduino_id]["last_sync_t1"] = local_clock()
    send_message(sock, command)

# Calcolo offset/delay
def compute_offset_delay(arduino_id: str, t2_us: float, t3_us: float, t4: float):

    # (MODIFICA) current_offset/current_delay/last_sync_t1 non possono essere globali con più Arduino

    t2 = float(t2_us) / 1e6
    t3 = float(t3_us) / 1e6

    t1 = arduino_sync_state[arduino_id]["last_sync_t1"]
    if t1 is None:
        print(f"[ARDUINO {arduino_id}] Avviso: t1 non disponibile per calcolo offset/delay")
        return None

    delay = ((t4 - t1) - (t3 - t2)) / 2.0
    offset = (t2 - (t1 + delay))

    with arduino_sync_state[arduino_id]["offset_lock"]:
        arduino_sync_state[arduino_id]["current_offset"] = offset
        arduino_sync_state[arduino_id]["current_delay"] = delay

    return t2, t3, t4, offset, delay

# -----------------------------------------------------
# ARD5. Handlers Arduino
# -----------------------------------------------------

# (AGGIUNTO) Helper: parsing Sens multi-canale
# Formato atteso (consigliato): Sens:wrap,us32,v1,v2,..     .,vN
def _parse_sens_multichannel(arduino_id: str, value: str):
    ns = ARDUINO_INFO["nsensors"][arduino_id]
    parts = [p.strip() for p in value.split(",") if p.strip()]
    # wrap + us32 + N valori
    if len(parts) != 2 + ns:
        raise ValueError(f"numero campi Sens non valido: attesi {2+ns}, ricevuti {len(parts)}")
    wrap = int(parts[0])
    us32 = int(parts[1])
    vals = [float(x) for x in parts[2:]]
    t_us64 = arduino_us64(wrap, us32)
    t_s = t_us64 / 1e6
    return t_s, vals

def handle_sensors(arduino_id: str, value: str):
    try:
        # prende il messaggio e lo splitta e converte
        tSens_s, sens_vals = _parse_sens_multichannel(arduino_id, value)
    except Exception:
        print(f"[ARDUINO {arduino_id}] Sens malformato: {value!r}")
        return

    with arduino_sync_state[arduino_id]["offset_lock"]:
        off = arduino_sync_state[arduino_id]["current_offset"]

    # mappa Arduino time -> host time stimato
    if off is None:
        print(f"[ARDUINO {arduino_id}] Avviso: offset non disponibile per Sens")
        return

    ts_host_est = float(tSens_s - off)

    try:
        # (MODIFICA CRITICA) push SOLO sullo stream dell'Arduino che ha generato il dato
        # Nel tuo snippet originale pushavi su tutti gli outlet -> dati duplicati e confusi.
        outlets[arduino_id].push_sample([float(v) for v in sens_vals], timestamp=ts_host_est)
    except Exception as e:
        print(f"[ARDUINO {arduino_id}] Errore push LSL (Sens): {e}")


def handle_timestamps(arduino_id: str, value: str):
    try:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) == 2:
            # formato vecchio
            t2_us = float(parts[0])
            t3_us = float(parts[1])
            t2_s = t2_us / 1e6
            t3_s = t3_us / 1e6
        elif len(parts) == 4:
            # formato nuovo wrap-aware
            w2 = int(parts[0]); t2_32 = int(parts[1])
            w3 = int(parts[2]); t3_32 = int(parts[3])
            t2_us64 = arduino_us64(w2, t2_32)
            t3_us64 = arduino_us64(w3, t3_32)
            t2_s = t2_us64 / 1e6
            t3_s = t3_us64 / 1e6
        else:
            raise ValueError("numero campi T non valido")
    except Exception:
        print(f"[ARDUINO {arduino_id}] T malformato: {value!r}")
        return

    t4 = local_clock()

    # (MODIFICA) usa t1 per-arduino
    t1 = arduino_sync_state[arduino_id]["last_sync_t1"]
    if t1 is None:
        print(f"[ARDUINO {arduino_id}] Avviso: t1 non disponibile per calcolo offset/delay")
        return

    delay = ((t4 - t1) - (t3_s - t2_s)) / 2.0
    offset = (t2_s - (t1 + delay))

    with arduino_sync_state[arduino_id]["offset_lock"]:
        arduino_sync_state[arduino_id]["current_offset"] = float(offset)
        arduino_sync_state[arduino_id]["current_delay"] = float(delay)

    # (MODIFICA) Stream SYNC rimosso come richiesto:
    # qui non facciamo push su LSL, aggiorniamo solo lo stato interno.


# Dizionario di dispatch
handlers = {
    "T": handle_timestamps,
    "Sens": handle_sensors,
}

# -----------------------------------------------------
# ARD6. Parsing linee Arduino
# -----------------------------------------------------

def process_line(arduino_id: str, line: str):
    """
    Gestisce una riga con potenziali messaggi separati da tab.
    Ogni messaggio: "LABEL:content"
    """
    messages = line.strip().split("\t")
    for msg in messages:
        msg = msg.strip()
        if not msg:
            continue

        parts = msg.split(":", 1)
        if len(parts) != 2:
            print(f"Avviso: messaggio malformato {msg!r}")
            continue

        label = parts[0].strip()
        content = parts[1].strip()

        h = handlers.get(label)
        if h is None:
            print(f"Avviso: etichetta non riconosciuta {label!r} con contenuto {content!r}")
            continue

        try:
            # (MODIFICA) passiamo sempre arduino_id all'handler
            # così può usare outlet/offset del device corretto.
            h(arduino_id, content)
        except Exception as e:
            # Non far collassare il thread Arduino per un singolo messaggio
            print(f"[ARDUINO {arduino_id}] Errore handler {label!r} su {content!r}: {e}")

# -----------------------------------------------------
# ARD7. Thread lettura Arduino (con SYNC periodico)
# -----------------------------------------------------

def ard_reader_loop(arduino_id: str, sock: socket.socket, sync_interval=2.0):
    next_sync = 0.0
    buf = ""

    while not stop_event.is_set():
        now = local_clock()
        if now >= next_sync:
            send_sync_request(arduino_id, sock, "SYNC")
            next_sync = now + float(sync_interval)

        buf, lines, closed = read_lines(sock, buf, timeout=0.1)
        if closed:
            print(f"[READER_THREAD {arduino_id}] Connessione chiusa da Arduino (EOF).")
            break

        for line in lines:
            process_line(arduino_id, line)

    print(f"[READER_THREAD {arduino_id}] Terminato.")

# ----------------------------------------------------
# EMOTIBIT. Thread EmotiBit (via BrainFlow) + LSL per preset
# ----------------------------------------------------
# (NON HO CAMBIATO QUESTA PARTE: è lunga e non è il focus multi-arduino.
#  Ho solo aggiunto safe defaults sopra per evitare NameError nello snippet.)

def emotibit_loop():
    if not ENABLE_EMOTIBIT:
        return

    from contextlib import ExitStack

    try:
        from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds, BrainFlowPresets
        from brainflow.exit_codes import BrainFlowError
    except ImportError:
        print("[EMOTIBIT] brainflow non installato. Esegui `pip install brainflow` per usare EmotiBit da Python.")
        return

    lsl_outlets = {}  # preset -> StreamOutlet (se LSL attivo)

    # LSL opzionale
    if EMOTIBIT_ENABLE_LSL and PYLSL_AVAILABLE:
        EMOTIBIT_ENABLE_LSL_LOCAL = True
    elif EMOTIBIT_ENABLE_LSL and not PYLSL_AVAILABLE:
        print("[EMOTIBIT] pylsl non installato. LSL disattivato.")
        EMOTIBIT_ENABLE_LSL_LOCAL = False
    else:
        EMOTIBIT_ENABLE_LSL_LOCAL = False

    def _with_suffix(path: str, suffix: str) -> str:
        root, ext = os.path.splitext(path)
        ext = ext if ext else ".csv"
        return f"{root}_{suffix}{ext}"

    def _get_board_descr(board_id: int, preset):
        try:
            return BoardShim.get_board_descr(board_id, preset)
        except TypeError:
            return BoardShim.get_board_descr(board_id)

    def _build_channel_names(descr: dict, num_rows: int):
        names = [f"ch_{i}" for i in range(num_rows)]

        if "timestamp_channel" in descr:
            try:
                names[int(descr["timestamp_channel"])] = "ts_board"
            except Exception:
                pass
        if "package_num_channel" in descr:
            try:
                names[int(descr["package_num_channel"])] = "package_num"
            except Exception:
                pass
        if "marker_channel" in descr:
            try:
                names[int(descr["marker_channel"])] = "marker"
            except Exception:
                pass

        for single_key, label in [("battery_channel", "battery")]:
            if single_key in descr and descr[single_key] is not None:
                try:
                    names[int(descr[single_key])] = label
                except Exception:
                    pass

        for key, val in descr.items():
            if not (isinstance(key, str) and key.endswith("_channels")):
                continue
            if not isinstance(val, (list, tuple)):
                continue

            base = key[:-len("_channels")]
            try:
                ch_list = [int(x) for x in val]
            except Exception:
                continue

            if base in ("accel", "gyro", "magnetometer") and len(ch_list) >= 3:
                axes = ["x", "y", "z"]
                for j, ch in enumerate(ch_list[:3]):
                    if 0 <= ch < num_rows:
                        names[ch] = f"{base}_{axes[j]}"
                for j, ch in enumerate(ch_list[3:], start=3):
                    if 0 <= ch < num_rows:
                        names[ch] = f"{base}_{j}"
            else:
                for j, ch in enumerate(ch_list):
                    if 0 <= ch < num_rows:
                        names[ch] = f"{base}_{j}"

        return names

    BoardShim.enable_dev_board_logger()
    params = BrainFlowInputParams()
    params.ip_address = EMOTIBIT_IP
    #params.ip_port = EMOTIBIT_PORT
    params.timeout = EMOTIBIT_TIMEOUT

    board_id = BoardIds.EMOTIBIT_BOARD.value
    board = BoardShim(board_id, params)

    try:
        print(f"[EMOTIBIT] Connessione a EmotiBit su {EMOTIBIT_IP}:{EMOTIBIT_PORT} ...")
        board.prepare_session()
        board.start_stream()
        print("[EMOTIBIT] Sessione BrainFlow avviata.")

        presets = BoardShim.get_board_presets(board_id)
        if not presets:
            presets = [BrainFlowPresets.DEFAULT_PRESET]

        preset_name_map = {
            BrainFlowPresets.DEFAULT_PRESET: "default",
            BrainFlowPresets.AUXILIARY_PRESET: "aux",
            BrainFlowPresets.ANCILLARY_PRESET: "anc",
        }

        preset_cfg = {}

        print(f"[EMOTIBIT] Preset disponibili: {presets}")
        for preset in presets:
            descr = _get_board_descr(board_id, preset)
            try:
                num_rows = int(descr.get("num_rows", BoardShim.get_num_rows(board_id, preset)))
            except Exception:
                num_rows = int(BoardShim.get_num_rows(board_id, preset))

            try:
                timestamp_ch = int(descr.get("timestamp_channel", BoardShim.get_timestamp_channel(board_id, preset)))
            except Exception:
                timestamp_ch = int(BoardShim.get_timestamp_channel(board_id, preset))

            try:
                sampling_rate = float(descr.get("sampling_rate", BoardShim.get_sampling_rate(board_id, preset)))
            except Exception:
                sampling_rate = float(BoardShim.get_sampling_rate(board_id, preset))

            payload_channels = [i for i in range(num_rows) if i != timestamp_ch]
            channel_names_all = _build_channel_names(descr, num_rows)
            payload_names = [channel_names_all[i] for i in payload_channels]

            suffix = preset_name_map.get(preset, f"preset_{int(preset)}")
            log_path = _with_suffix(EMOTIBIT_LOG_FILE, suffix)

            preset_cfg[preset] = {
                "descr": descr,
                "num_rows": num_rows,
                "timestamp_ch": timestamp_ch,
                "sampling_rate": sampling_rate,
                "payload_channels": payload_channels,
                "payload_names": payload_names,
                "log_path": log_path,
                "suffix": suffix,
            }

            print(f"[EMOTIBIT] Preset {suffix}: srate={sampling_rate}, rows={num_rows}, ts_ch={timestamp_ch}")

        # LSL: uno stream per preset
        if EMOTIBIT_ENABLE_LSL_LOCAL:
            try:
                for preset, cfg in preset_cfg.items():
                    ch_count = len(cfg["payload_channels"])
                    info = StreamInfo(
                        name=f"EmotiBit_{cfg['suffix']}",
                        type="BIO",
                        channel_count=ch_count,
                        nominal_srate=float(cfg["sampling_rate"]),
                        channel_format="float32",
                        source_id=f"emotibit_1_{cfg['suffix']}",
                    )

                    try:
                        chns = info.desc().append_child("channels")
                        for nm in cfg["payload_names"]:
                            ch = chns.append_child("channel")
                            ch.append_child_value("label", str(nm))
                    except Exception:
                        pass

                    lsl_outlets[preset] = StreamOutlet(info)

                print("[EMOTIBIT] Stream LSL creati (uno per preset).")
            except Exception as e:
                print(f"[EMOTIBIT] Errore creazione stream LSL: {e}. LSL disattivato.")
                lsl_outlets = {}

        # Log file: scrivi header solo se file nuovo/vuoto
        with ExitStack() as stack:
            log_files = {}
            for preset, cfg in preset_cfg.items():
                path = cfg["log_path"]
                must_write_header = (not os.path.exists(path)) or (os.path.getsize(path) == 0)

                f = stack.enter_context(open(path, "a", encoding="utf-8"))
                if must_write_header:
                    f.write("# ts_host,ts_board," + ",".join(cfg["payload_names"]) + "\n")
                log_files[preset] = f

            while not stop_event.is_set():
                any_data = False

                for preset, cfg in preset_cfg.items():
                    try:
                        data = board.get_board_data(preset=preset)
                    except BrainFlowError as e:
                        print(f"[EMOTIBIT] Errore BrainFlow (preset={cfg['suffix']}): {e}. Termino thread EmotiBit.")
                        return

                    if getattr(data, "size", 0) == 0:
                        continue

                    any_data = True
                    n_samples = data.shape[1]
                    timestamps_board = data[cfg["timestamp_ch"], :]

                    f = log_files[preset]
                    outlet = lsl_outlets.get(preset)

                    payload_channels = cfg["payload_channels"]

                    for i in range(n_samples):
                        ts_board = float(timestamps_board[i])
                        ts_host = local_clock()  # coerente con LSL

                        sample_vals = [float(data[ch, i]) for ch in payload_channels]

                        # log
                        f.write(
                            f"{ts_host:.9f},{ts_board:.9f}," +
                            ",".join(f"{v:.6f}" for v in sample_vals) +
                            "\n"
                        )

                        # LSL
                        if outlet is not None:
                            try:
                                outlet.push_sample(sample_vals, timestamp=float(ts_host))
                            except Exception as e:
                                print(f"[EMOTIBIT] Errore push LSL (preset={cfg['suffix']}): {e}")

                    f.flush()

                if not any_data:
                    time.sleep(EMOTIBIT_POLL_INTERVAL)

    except Exception as e:
        print(f"[EMOTIBIT] Errore nella sessione: {e}")
    finally:
        try:
            board.stop_stream()
        except Exception:
            pass
        try:
            board.release_session()
        except Exception:
            pass
        print("[EMOTIBIT] Sessione chiusa.")

# -----------------------------------------------------
# Main
# -----------------------------------------------------

def main():
    global ARDUINO_LSL_ENABLED_LOCAL, ARDUINO_LSL_OUTLETS
    # (AGGIUNTO) Questi global nel tuo snippet erano riferimenti a una versione precedente.
    # Li lasciamo definiti per compatibilità, ma non servono più nella versione multi-arduino.
    ARDUINO_LSL_ENABLED_LOCAL = False
    ARDUINO_LSL_OUTLETS = {}

    dispositivi_connessi = []
    soc = None

    # (AGGIUNTO) con più Arduino abbiamo più socket
    arduino_sockets = {}   # arduino_id -> socket

    # --- Arduino ---
    if ENABLE_ARDUINO:
        # (MODIFICA) invece di ARDUINO_IP/ARDUINO_PORT (non definiti), cicliamo su ARDUINO_INFO
        for arduino_id in ARDUINO_INFO["ip"].keys():
            ip = ARDUINO_INFO["ip"][arduino_id]
            port = ARDUINO_INFO["port"][arduino_id]

            try:
                soc_i = arduino_connection(ip, port)
            except Exception as e:
                print(f"[MAIN] Errore connessione Arduino {arduino_id} ({ip}:{port}): {e}")
                continue

            print("Apro la connessione su", ip, "porta", port, "per Arduino", arduino_id)
            arduino_sockets[arduino_id] = soc_i

            # Setup LSL Arduino
            # (MODIFICA) gli stream LSL per Arduino sono già stati creati sopra (outlets[arduino_id])

            # Thread lettura (passo SOLO il socket)
            # (MODIFICA) passiamo anche arduino_id, così attribuiamo i messaggi al device corretto
            reader_thr = threading.Thread(target=ard_reader_loop, args=(arduino_id, soc_i), daemon=True)
            reader_thr.start()

        if arduino_sockets:
            dispositivi_connessi.append(f"Arduino({','.join(arduino_sockets.keys())})")

    # --- EmotiBit ---
    if ENABLE_EMOTIBIT:
        emotibit_thr = threading.Thread(target=emotibit_loop, daemon=True)
        emotibit_thr.start()
        print("Thread EmotiBit avviato (BrainFlow).")
        dispositivi_connessi.append("EmotiBit")

    print("Thread di lettura avviati.")
    print("Dispositivi attivi:", ", ".join(dispositivi_connessi) if dispositivi_connessi else "(nessuno)")
    print("Puoi scrivere comandi da inviare ad Arduino.")
    print("Comandi disponibili (esempio): led_on, led_off, quit")

    # (AGGIUNTO) Sintassi comandi multi-arduino:
    # - "led_on"     -> broadcast a tutti gli Arduino connessi
    # - "A:led_on"   -> invia solo ad Arduino A
    # - "B:led_off"  -> invia solo ad Arduino B
    try:
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue

            if cmd.lower() == "quit":
                print("Uscita richiesta. Chiudo...")
                break

            if ENABLE_ARDUINO and arduino_sockets:
                target = None
                payload = cmd

                # (AGGIUNTO) parsing "A:comando"
                if ":" in cmd:
                    maybe_target, payload2 = cmd.split(":", 1)
                    maybe_target = maybe_target.strip()
                    payload2 = payload2.strip()

                    if maybe_target in arduino_sockets:
                        target = maybe_target
                        payload = payload2

                if target is None:
                    # broadcast
                    for _aid, _sock in arduino_sockets.items():
                        send_message(_sock, payload)
                else:
                    send_message(arduino_sockets[target], payload)

            else:
                print("[MAIN] Arduino non attivo: comando ignorato.")

            time.sleep(0.01)

    finally:
        stop_event.set()
        try:
            # (MODIFICA) chiudo tutti i socket Arduino
            for _aid, _sock in arduino_sockets.items():
                try:
                    _sock.close()
                except Exception:
                    pass

            # mantenuto per compatibilità con la tua variabile originale
            if soc is not None:
                soc.close()
        except Exception as e:
            print(f"Errore durante la chiusura della connessione Arduino: {e}")

        print("Chiusura completata.")

if __name__ == "__main__":
    main()
