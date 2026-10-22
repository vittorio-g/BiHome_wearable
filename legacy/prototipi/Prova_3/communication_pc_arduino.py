import serial
import time
from statistics import mean, stdev

PORT = "COM3"
BAUDRATE = 115200
N = 1000           # numero di ping per statistica
payload_sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512] # numero di byte per payload da testare

for size in payload_sizes:

  PAYLOAD = b"X"*size     # numero di byte da inviare

  ser = serial.Serial(PORT, BAUDRATE, timeout=0.2)
  time.sleep(2)  # alcuni Arduino si resettano all'apertura

  # pulisci buffer
  ser.reset_input_buffer()
  ser.reset_output_buffer()

  rtts_ms = []

  for _ in range(N):
    t0 = time.perf_counter()
    ser.write(PAYLOAD)
    ser.flush()  # scarica il buffer verso la porta
    resp = ser.read(size)  # aspetta eco di 1 byte
    t1 = time.perf_counter()
    if resp == PAYLOAD:
      rtts_ms.append((t1 - t0) * 1000)
    else:
      # la risposta non è valida
      pass

  ser.close()


  if rtts_ms:
    print(f"Payload size: {size} bytes")
    print(f"Ping count: {len(rtts_ms)}/{N}")
    print(f"RTT avg: {mean(rtts_ms):.3f} ms, std: {stdev(rtts_ms):.3f} ms")
    print(f"Approx one-way latency: ~{mean(rtts_ms)/2:.3f} ms")
    print("--"*30)
  else:
    print("Nessuna risposta valida")