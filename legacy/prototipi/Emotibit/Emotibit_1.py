import time
from datetime import datetime
import socket
import threading
import sys
import os
import select
from pylsl import StreamInfo, StreamOutlet, local_clock

# TODO
# Leggi anche stato del led su arduino e mettere nello stesso csv di lux

# -----------------------------------------------------
# Feature flags
# -----------------------------------------------------

ENABLE_ARDUINO = True
ENABLE_EMOTIBIT = True

ENABLE_LSL = False

if ENABLE_LSL:
    ARDUINO_ENABLE_LSL = True
    EMOTIBIT_ENABLE_LSL = True
else:
    ARDUINO_ENABLE_LSL = False
    EMOTIBIT_ENABLE_LSL = False
    

# ----------------------------------------------------
# Configurazione connessione Arduino
# ----------------------------------------------------

ARDUINO_IP = "172.21.131.37"   # SOSTITUIRE QUI IP 
ARDUINO_PORT = 5000

# -----------------------------------------------------
# Configurazione EmotiBit
# -----------------------------------------------------

EMOTIBIT_IP = "172.21.131.255"  # TODO: sostituire
EMOTIBIT_PORT = 0
EMOTIBIT_TIMEOUT = 5

EMOTIBIT_POLL_INTERVAL = 0.05
EMOTIBIT_LOG_FILE = "emotibit_log.csv"

# -----------------------------------------------------
# Variabili varie
# -----------------------------------------------------

# Variabili esperimento
n_data = 2  # Numero di tipi di dati inviati da Arduino (non usata qui, lasciata per coerenza)

# Dichiarazione variabili per sincronizzazione
last_sync_t1 = None
current_offset = 0.0
current_delay = None
offset_lock = threading.Lock()

# Lock per evitare scritture concorrenti su f_out (SYNC + comandi utente)
send_lock = threading.Lock()

# Evento per shutdown pulito
stop_event = threading.Event()



# -----------------------------------------------------
# Helper per ricostruire tempi corretti Arduino
# -----------------------------------------------------

US_WRAP = 2**32  # uguale a 4294967296, il valore di overflow a 32 bit

def arduino_us64(wrap: int, us32: int) -> int:
    return int(wrap) * US_WRAP + int(us32)

# -----------------------------------------------------
# LSL (import opzionale) + clock host coerente
# -----------------------------------------------------

PYLSL_AVAILABLE = False
try:
    from pylsl import StreamInfo, StreamOutlet, local_clock  # type: ignore
    PYLSL_AVAILABLE = True
except ImportError:
    StreamInfo = None
    StreamOutlet = None
    local_clock = None

def host_time() -> float:
    if ENABLE_LSL and PYLSL_AVAILABLE and local_clock is not None:
        return float(local_clock())
    return time.perf_counter()

# -----------------------------------------------------
# ARD1. Connessione Arduino
# -----------------------------------------------------

def arduino_connection(ip: str, port: int):
    soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    soc.connect((ip, port))
    # Lascia blocking. read_lines usa select, quindi non blocchi comunque.
    print("Connesso all'Arduino")
    return soc



# -----------------------------------------------------
# ARD2. Setup LSL per Arduino (stream separati)
# -----------------------------------------------------

def setup_arduino_lsl():
    """
    Crea outlet LSL separati per diversi tipi di messaggi Arduino.
    Ritorna (enabled, outlets_dict).
    """
    outlets = {}
    if not ARDUINO_ENABLE_LSL:
        return False, outlets

    if not PYLSL_AVAILABLE:
        print("[ARDUINO] pylsl non installato. LSL Arduino disattivato.")
        return False, outlets

    try:
        # Stream LUX: 1 canale (lux), srate irregolare => nominal_srate=0
        info_lux = StreamInfo(
            name="Arduino_LUX",
            type="AUX",
            channel_count=1,
            nominal_srate=0.0,
            channel_format="float32",
            source_id="arduino_1_lux",
        )
        try:
            chns = info_lux.desc().append_child("channels")
            ch = chns.append_child("channel")
            ch.append_child_value("label", "lux")
        except Exception:
            pass
        outlets["LUX"] = StreamOutlet(info_lux)

        # Stream SYNC: canali utili per debug sincronizzazione (t2, t3, t4, offset, delay)
        info_sync = StreamInfo(
            name="Arduino_SYNC",
            type="SYNC",
            channel_count=5,
            nominal_srate=0.0,
            channel_format="float32",
            source_id="arduino_1_sync",
        )
        try:
            chns = info_sync.desc().append_child("channels")
            for nm in ["t2", "t3", "t4", "offset", "delay"]:
                ch = chns.append_child("channel")
                ch.append_child_value("label", nm)
        except Exception:
            pass
        outlets["T"] = StreamOutlet(info_sync)

        print("[ARDUINO] Stream LSL creati: Arduino_LUX, Arduino_SYNC")
        return True, outlets

    except Exception as e:
        print(f"[ARDUINO] Errore creazione stream LSL: {e}. LSL Arduino disattivato.")
        return False, {}

# -----------------------------------------------------
# ARD3. Invio/Ricezione messaggi Arduino
# -----------------------------------------------------

send_lock = threading.Lock()

def send_message(sock: socket.socket, command: str):
    try:
        data = (command + "\n").encode("utf-8")
        with send_lock:
            sock.sendall(data)
    except OSError as e:
        print(f"Errore invio messaggio {command!r}: {e}")
        
def read_lines(sock: socket.socket, buffer: str, timeout: float = 0.1):
    """
    Ritorna: (buffer_aggiornato, lines, closed)
      - lines: lista di linee complete (senza '\n')
      - closed: True se socket chiuso (EOF)
    """
    try:
        r, _, _ = select.select([sock], [], [], timeout)
        if not r:
            return buffer, [], False

        chunk = sock.recv(4096)
        if not chunk:
            return buffer, [], True  # EOF

        buffer += chunk.decode("utf-8", errors="replace")
        parts = buffer.split("\n")
        buffer = parts[-1]              # resto incompleto
        lines = [p.strip() for p in parts[:-1] if p.strip()]
        return buffer, lines, False

    except OSError as e:
        print(f"Errore lettura socket: {e}")
        return buffer, [], False


# -----------------------------------------------------
# ARD4. Sincronizzazione Arduino
# -----------------------------------------------------

def send_sync_request(sock, command="SYNC"):
    global last_sync_t1
    last_sync_t1 = host_time()
    send_message(sock, command)


def compute_offset_delay(t2_us: float, t3_us: float, t4: float):
    """
    Calcolo offset/delay come nel tuo codice.
    """
    global current_offset, current_delay, last_sync_t1

    t2 = float(t2_us) / 1e6
    t3 = float(t3_us) / 1e6

    t1 = last_sync_t1
    if t1 is None:
        print("[ARDUINO] Avviso: t1 non disponibile per calcolo offset/delay")
        return None

    delay = ((t4 - t1) - (t3 - t2)) / 2.0
    offset = (t2 - (t1 + delay))

    with offset_lock:
        current_offset = offset
        current_delay = delay

    return t2, t3, t4, offset, delay

# -----------------------------------------------------
# ARD5. Handlers Arduino + LSL
# -----------------------------------------------------

# Outlets LSL Arduino (inizializzati in main)
ARDUINO_LSL_ENABLED_LOCAL = False
ARDUINO_LSL_OUTLETS = {}

def handle_lux(value: str):
    try:
        parts = value.split(",")
        if len(parts) == 2:
            # formato vecchio
            tLux_us = float(parts[0])
            lux_val = float(parts[1])
            tLux_s = tLux_us / 1e6
        elif len(parts) == 3:
            # formato nuovo wrap-aware
            wLux = int(parts[0]); tLux_32 = int(parts[1])
            lux_val = float(parts[2])
            tLux_us64 = arduino_us64(wLux, tLux_32)
            tLux_s = tLux_us64 / 1e6
        else:
            raise ValueError("numero campi LUX non valido")
    except Exception:
        print(f"[ARDUINO] LUX malformato: {value!r}")
        return

    with offset_lock:
        off = current_offset

    # mappa Arduino time -> host time stimato
    ts_host_est = (tLux_s - off) if off is not None else host_time()

    try:
        with open("lux_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{ts_host_est:.9f},{lux_val}\n")
    except Exception as e:
        print(f"[ARDUINO] Errore scrittura lux_log.txt: {e}")

    if ARDUINO_LSL_ENABLED_LOCAL and "LUX" in ARDUINO_LSL_OUTLETS:
        try:
            ARDUINO_LSL_OUTLETS["LUX"].push_sample([float(lux_val)], timestamp=float(ts_host_est))
        except Exception as e:
            print(f"[ARDUINO] Errore push LSL (LUX): {e}")


def handle_timestamps(value: str):
    try:
        parts = value.split(",")
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
        print(f"[ARDUINO] T malformato: {value!r}")
        return

    t4 = host_time()

    global current_offset, current_delay, last_sync_t1
    t1 = last_sync_t1
    if t1 is None:
        print("[ARDUINO] Avviso: t1 non disponibile per calcolo offset/delay")
        return

    delay = ((t4 - t1) - (t3_s - t2_s)) / 2.0
    offset = (t2_s - (t1 + delay))

    with offset_lock:
        current_offset = offset
        current_delay = delay

    # LSL stream SYNC
    if ARDUINO_LSL_ENABLED_LOCAL and "T" in ARDUINO_LSL_OUTLETS:
        try:
            ARDUINO_LSL_OUTLETS["T"].push_sample(
                [float(t2_s), float(t3_s), float(t4), float(offset), float(delay)],
                timestamp=float(t4),
            )
        except Exception as e:
            print(f"[ARDUINO] Errore push LSL (SYNC): {e}")


# Dizionario di dispatch
handlers = {
    "T": handle_timestamps,
    "LUX": handle_lux,
}

# -----------------------------------------------------
# ARD6. Parsing linee Arduino
# -----------------------------------------------------

def process_line(line: str):
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
            h(content)
        except Exception as e:
            # Non far collassare il thread Arduino per un singolo messaggio
            print(f"[ARDUINO] Errore handler {label!r} su {content!r}: {e}")

# -----------------------------------------------------
# ARD7. Thread lettura Arduino (con SYNC periodico)
# -----------------------------------------------------

def ard_reader_loop(sock: socket.socket, sync_interval=2.0):
    next_sync = 0.0
    buf = ""

    while not stop_event.is_set():
        now = host_time()
        if now >= next_sync:
            send_sync_request(sock, "SYNC")
            next_sync = now + float(sync_interval)

        buf, lines, closed = read_lines(sock, buf, timeout=0.1)
        if closed:
            print("[READER_THREAD] Connessione chiusa da Arduino (EOF).")
            break

        for line in lines:
            process_line(line)

    print("[READER_THREAD] Terminato.")

# ----------------------------------------------------
# EMOTIBIT. Thread EmotiBit (via BrainFlow) + LSL per preset
# ----------------------------------------------------

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
                        ts_host = host_time()  # coerente con LSL

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

    dispositivi_connessi = []
    soc = None

    # --- Arduino ---
    if ENABLE_ARDUINO:
        soc = arduino_connection(ARDUINO_IP, ARDUINO_PORT)
        print("Apro la connessione su", ARDUINO_IP, "porta", ARDUINO_PORT)

        # Setup LSL Arduino
        ARDUINO_LSL_ENABLED_LOCAL, ARDUINO_LSL_OUTLETS = setup_arduino_lsl()

        # Thread lettura (passo SOLO il socket)
        reader_thr = threading.Thread(target=ard_reader_loop, args=(soc,), daemon=True)
        reader_thr.start()

        dispositivi_connessi.append("Arduino")

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

    try:
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue

            if cmd.lower() == "quit":
                print("Uscita richiesta. Chiudo...")
                break

            if ENABLE_ARDUINO and soc is not None:
                send_message(soc, cmd)   # <-- socket, non f_out
            else:
                print("[MAIN] Arduino non attivo: comando ignorato.")

            time.sleep(0.01)

    finally:
        stop_event.set()
        try:
            if soc is not None:
                soc.close()
        except Exception as e:
            print(f"Errore durante la chiusura della connessione Arduino: {e}")

        print("Chiusura completata.")

if __name__ == "__main__":
    main()
