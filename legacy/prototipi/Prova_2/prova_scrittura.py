import serial
import time

PORTA = "COM3"
BAUDRATE = 9600

#open serial connection and allow time for it to establish
ser = serial.Serial(PORTA, BAUDRATE,timeout=1)
time.sleep(2)

print("Conesso. Digita 1 per accendere il LED, 0 per spegnerlo. Premi q per uscire.")

try:
    while True:
        #lettura
        riga = ser.readline()
        if riga:
            testo = riga.decode(errors='ignore').strip()
            print("Arduino:", testo)

        #richiesta input utente
        comando = input("Comando (1,0,q): ").strip()
        if comando == '1' or comando == '0':
            ser.write(comando.encode())
        elif comando.lower() == 'q':
            print("Uscita dal programma.")
            break
        else:
            print("Comando non valido. Usa 1, 0 o q.")
except KeyboardInterrupt:
    print("Interruzione da tastiera.")
finally:
    ser.close()
    print("Connessione seriale chiusa.")