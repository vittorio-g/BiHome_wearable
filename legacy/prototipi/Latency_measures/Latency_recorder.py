import time
import statistics
import serial

# Variabili di testing
N_MEASURES = 600 # Numero di misure da effettuare, contanto che verrà effettuata una misura ogni secondo
DIM_MESSAGE = 1 # Dimensione del messaggio inviato (in byte)



# Configurazione della seriale
SERIAL_PORT = 'COM5'
BAUDRATE = 115200


# Timeout in secondi per la lettura da seriale
READ_TIMEOUT = 1.0

# Funzione per aprire la seriale
def open_serial(port, baudrate):
  # Apriamo la porta seriale e aspettiamo che Arduino si resetti
  ser = serial.Serial(port, baudrate=baudrate, timeout=READ_TIMEOUT)
  # Aspettiamo un paio di secondi per sicurezza
  time.sleep(2.0)
  # Svuota eventuali dati rimasti nel buffer
  ser.reset_input_buffer()
  ser.reset_output_buffer()
  return ser

# Funzione per effettuare una singola misura di latenza
def measure_once(ser):
  """
  Effettua UNA misura di latenza in stile LSL

  Step:
    1) PC prende t0
    2) PC invia un byte
    3) Arduino riceve, legge micros e risponde
    4) PC legge la stringa t_ard e prende t1
    5) calcola Latenza e Offset

  Restituisce (latenza_s, offset_s)
  in secondi.
  """

  # t0: tempo sul PC subito prima di inviare il byte
  # perf_counter ha alta risoluzione e non è legato all orologio di sistema
  t0 = time.perf_counter()

  # Invia un singolo byte, per esempio 'S'
  ser.write(b'S'*DIM_MESSAGE)

  # Legge una linea dalla seriale, che dovrebbe contenere t_ard in microsecondi
  line = ser.readline()
  t1 = time.perf_counter()

  # Avvisa se la lettura è fallita (timeout)
  if not line:
    raise RuntimeError("Timeout in lettura dalla seriale")

  # La linea è tipo b'1234567\r\n', decodifichiamola
  line_str = line.decode('ascii').strip()

  # Converte la stringa in intero microsecondi
  try:
      t_ard_us = int(line_str)
  except ValueError:
      raise RuntimeError(f"Valore non valido ricevuto da Arduino: {line_str!r}")

  # Converte t_ard in secondi, per coerenza con perf_counter
  t_ard_s = t_ard_us / 1e6

  # Latenza metà round trip time
  latency = (t1 - t0) / 2.0

  # Offset tra clock PC e clock Arduino
  """
  # t0 + Latenza rappresenta il tempo medio
  # del viaggio PC → Arduino → PC, quindi la stima
  # di quando il pacchetto ha raggiunto Arduino
  """
  # Offset = tempo_PC_stimato_quando_Arduino_ha_letto_micros - tempo_Arduino
  offset = (t0 + latency) - t_ard_s

  return latency, offset


def main():
  print("Apro la seriale su", SERIAL_PORT)
  ser = open_serial(SERIAL_PORT, BAUDRATE)

  latencies = []
  offsets = []

  print(f"Eseguo {N_MEASURES} misure di latenza e offset")

  for i in range(N_MEASURES):
    try:
      latency, offset = measure_once(ser)
      latencies.append(latency)
      offsets.append(offset)
      print(
        f"Misura {i+1:3d}  "
        f"Latenza = {latency*1000:.3f} ms  "
        f"Offset = {offset*1000:.3f} ms"
      )
      # Piccola pausa per non inondare la seriale
      time.sleep(1)
    except Exception as e:
      print(f"Misura {i+1:3d} fallita: {e}")

  ser.close()

  if not latencies:
    print("Nessuna misura valida, controlla connessione e porta seriale")
    return

  # Rimuovi eventuali outlier molto grossi
  # per esempio misure con latenza > 5 volte la mediana
  median_lat = statistics.median(latencies)
  clean_latencies = [
    L for L in latencies
    if L < 5 * median_lat
  ]
  clean_offsets = [
    O for L, O in zip(latencies, offsets)
    if L < 5 * median_lat
  ]

  print("\nRisultati aggregati:")

  def stats(name, values, unit):
    mean_v = statistics.mean(values)
    std_v = statistics.pstdev(values)
    print(f"{name}: media = {mean_v*unit:.3f}, std = {std_v*unit:.3f}")

  stats("Latenza", clean_latencies, 1000.0)  # in millisecondi
  stats("Offset", clean_offsets, 1000.0)     # in millisecondi


# Esegui il main se questo script è eseguito direttamente
if __name__ == "__main__":
  main()
