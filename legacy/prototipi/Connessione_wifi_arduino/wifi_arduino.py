import socket

ARDUINO_IP = "10.61.255.37"   # IP stampato da arduino su serial monitor
ARDUINO_PORT = 5000

def main():
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((ARDUINO_IP, ARDUINO_PORT))
  f = s.makefile("r",encoding="utf-8",newline="\n")
  print("Connesso all'Arduino")

  try:
    while True:
      msg = input("Messaggio da inviare (Enter per uscire): ")
      if msg == "":
        break
      
      #inviamo una singola riga
      s.sendall((msg + "\n").encode("utf-8"))
      
      #leggiamo la risposta, ovvero esattamente una riga
      risposta = f.readline()
      
      #non dovessimo leggerla significa che la connessione è stata chiusa
      if not risposta:
        print("Connessione chiusa dal server")
        break
      
      print("risposta:", risposta.strip())

  finally:
    s.close()
    print("Connessione chiusa")

if __name__ == "__main__":
  main()