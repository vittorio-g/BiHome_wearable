import serial
from pylsl import StreamInfo, StreamOutlet

#apertura seriale con Arduino
ser = serial.Serial('COM3', 115200)

#Definizione stream LSL

n_channels = 2
fs = 100  # campioniamo 100 campioni al secondo

info = StreamInfo(
    name = "Arduino_sensors",
    type = "Other",
    channel_count = n_channels,
    nominal_srate = fs,
    channel_format = "float32",
    source_id = "arduino_001"
)

outlet = StreamOutlet(info) 

print("Inizio trasmissione dati...")

while True:
    #leggi una linea dalla seriale e decodificala
    line = ser.readline().decode('utf-8').strip()
    #se la linea è vuota, ripeti (i.e. passa al prossimo ciclo)
    if not line:
        continue
    
    parts = line.split(',')
    if len(parts) != n_channels:
        #vuol dire che la linea non è formattata correttamente
        continue
    
    try:
        sample = [float(p) for p in parts]
    except ValueError:
        continue  # se la conversione fallisce, salta questa linea
    
    outlet.push_sample(sample)
    
    