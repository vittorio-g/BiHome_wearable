import serial
import time

PORT = 'COM3'
BAUDRATE = 9600

# Initialize serial connection
ser = serial.Serial(PORT, BAUDRATE, timeout=1)

# Allow time for the connection to establish
time.sleep(2)

print("Starting communication with the arduino... (Press Ctrl+C to stop)")

try:
    while True:
        riga = ser.readline()
        if riga:
            testo = riga.decode(errors='ignore').strip()
            print(testo)
except KeyboardInterrupt:
    print("Stopping communication.")
finally:
    ser.close()
    print("Serial connection closed.")
