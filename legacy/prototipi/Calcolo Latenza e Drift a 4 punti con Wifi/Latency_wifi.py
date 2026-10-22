import time
from datetime import datetime
import socket
import threading
import sys

# Variabili di testing
N_MEASURES = 600 # Numero di misure da effettuare, contanto che verrà effettuata una misura ogni secondo
DIM_MESSAGE = 1 # Dimensione del messaggio inviato (in byte)

# Variabili esperimento
n_data = 2  # Numero di tipi di dati inviati da Arduino

# Variabili per sincronizzazione
last_sync_t1 = None
current_offset = 0.0
current_delay = None
offset_lock = threading.Lock()

# Configurazione della connessione wireless
ARDUINO_IP = "10.61.255.37"   # IP stampato da arduino su serial monitor
ARDUINO_PORT = 5000
READ_TIMEOUT = 1  # Timeout in secondi per la lettura da socket

# -----------------------------------------------------
# 1. Funzione di connessione con Arduino
# -----------------------------------------------------

def arduino_connection(ip: str, port: int):
  
  # Apriamo il socket
  soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  soc.settimeout(None)
  soc.connect((ip, port))
  soc.settimeout(READ_TIMEOUT)
  
  # Creaimo un oggetto file per la lettura e uno per gli invii (nota: non crea un file sul disco, è solo un wrapper)
  f_in = soc.makefile("r", encoding="utf-8", newline="\n")
  f_out = soc.makefile("w", encoding="utf-8", newline="\n")
  
  print("Connesso all'Arduino")
  
  return soc, f_in, f_out


# -----------------------------------------------------
# 2. Funzioni di gestione dei differenti tipi di dati e dizionario di dispatch
# -----------------------------------------------------

#Gestione di dati di luminosità
def handle_lux(value):
    # Splitto in timestamp di lux e il valore di lux
    tLux_string, vLux_string = value.split(",",1)
    
    # Converto il valore di tLux in float, poi secondi e poi lo aggiusto con l'offset corrente
    tLux_float = float(tLux_string)
    tLux_offset = tLux_float / 1e6
    with offset_lock:
        tLux = tLux_offset - current_offset
  
    with open("lux_log.txt", "a", encoding="utf-8") as f:
        #Timestamp ISO 8601
        ts = tLux  
        #riga con timestamp e valore, separati da virgola
        f.write(f"{ts},{vLux_string}\n")

#Gestione di dati di T2 e T3, registrati dentro Arduino
def handle_timestamps(value):

    global current_offset, current_delay, last_sync_t1
    
    # se è stata chiamata questa funzione vuol dire che sono arrivati i timestamp,
    # prima di tutto prendiamo t4, il tempo di ricezione sul PC
    t4 = time.perf_counter()
    
    #poi splitto i valori di T2 e T3, li converto in float e li salvo in variabili globali
    t2_str, t3_str = value.split(",",1)
    t2_us = float(t2_str)
    t3_us = float(t3_str)
    
     # Li porto in secondi
    t2 = t2_us / 1e6
    t3 = t3_us / 1e6
    
    #recuperiamo t1 dall'invio del comando SYNC, dalla variabile globale
    t1 = last_sync_t1
    if t1 is None:
        print("Avviso: t1 non disponibile per calcolo offset e delay")
        return
      
    delay = ((t4 - t1) - (t3 - t2)) / 2.0
    offset = (t2 - (t1 + delay))
    
    with offset_lock:
        current_offset = offset
        current_delay = delay
    
    
    return t2, t3, t4
    
# Dizionario di dispatch
handlers = {
    'LUX': handle_lux,
    'T': handle_timestamps,
}

# Funzione di processamento di una linea di messaggio
def process_line(line: str):
  messages = line.strip().split('\t')

  for msg in messages:
    msg = msg.strip()
    if not msg:
        continue
        
    #divido il messaggio in etichetta e contenuto
    parts =  msg.split(":",1)
    if len(parts) != 2:
        print(f"Avviso: messaggio malformato {msg!r}")
        continue
    label = parts[0]
    content = parts[1]
        
    #se l'etichetta è nel dizionario di dispatch, chiamo la funzione corrispondente
    if label in handlers:
      handlers[label](content)
            
      #se l'etichetta non è riconosciuta, stampo un avviso
    else:
      print(f"Avviso: etichetta non riconosciuta {label!r} con contenuto {content!r}")

# -----------------------------------------------------
# 3. Funzioni di invio e ricezione messaggi
# -----------------------------------------------------

def send_message(f_out, command: str):
    try:
      f_out.write(command + "\n")
      f_out.flush()
    except OSError as e:
      print(f"Errore invio messaggio {command!r}: {e}")

def read_message(f_in):
  try:
    line = f_in.readline()
    if line == "": #se il socket è chiuso, readline ritorna stringa vuota
      return None
    line = line.strip()
    if not line:
      return None
    return line
  except OSError as e:
    print(f"Errore lettura messaggio: {e}")
    return None

# -----------------------------------------------------
# 4. Creiamo una funzione da mettere in thread per la lettura continua dei messaggi
# -----------------------------------------------------

def reader_loop(f_in, f_out):
   while True:
     send_sync_request(f_out, "SYNC")
     line = read_message(f_in)
     if line is None:
            print("[READER_THREAD] Connessione chiusa da Arduino o linea vuota.")
            break

     process_line(line)

# -----------------------------------------------------
# Funzioni di sincronizzazione
# -----------------------------------------------------

def send_sync_request(f_out, command = "SYNC"):
    global last_sync_t1
    
    t1 = time.perf_counter()
    last_sync_t1 = t1
    send_message(f_out, command)
    
    

# -----------------------------------------------------
# Main
# -----------------------------------------------------

def main():
  soc, f_in, f_out = arduino_connection(ARDUINO_IP, ARDUINO_PORT)
    
  print("Apro la connessione su", ARDUINO_IP, "porta", ARDUINO_PORT )
  
  # Avvio il thread di lettura continua
  reader_thr = threading.Thread(target=reader_loop, args=(f_in, f_out), daemon=True)
  reader_thr.start()
  
  print("Thread di lettura avviato.")
  print("Puoi sccrivere comandi da inviare ad Arduino.")
  print("Comandi disponibili: led_on, led_off, quit")

  # Loop principale per inviare comandi
  try:
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue

            if cmd.lower() == "quit":
                print("Uscita richiesta. Chiudo la connessione...")
                break

            send_message(f_out, cmd)

            # piccola pausa giusto per non spammare
            time.sleep(0.01)

  finally:
        try:
            f_in.close()
            f_out.close()
            soc.close()

        except Exception as e:
            print(f"Errore durante la chiusura della connessione: {e}")


if __name__ == "__main__":
    main()  # Loop principale per inviare comandi
