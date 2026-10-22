#include <SPI.h>
#include <WiFiNINA.h>

// IMPOSTAZIONI WIFI --------------------------------------

char ssid[] = "YOUR_WIFI_SSID";      // nome WiFi
char pass[] = "YOUR_WIFI_PASSWORD"; // password WiFi

// IMPOSTAZIONI ARDUINO E ALTRE VARIABILI ----------------------------------

const int sensorPin = A1;
int lightValue = 0;
const int ledPin = 2;
unsigned long lightTime = 0;


// PARTE STANDARD PER INIZIO CONNESSIONE ARDUINO AL WIFI E APERTURA SERVER ----------------

int status = WL_IDLE_STATUS;
WiFiServer server(5000);      // porta TCP, una a caso

void setup() {
  // aspetta la seriale per stampare i messaggi
  Serial.begin(9600);
  while (!Serial) {
    ; // per alcune board (MKR, Nano 33 IoT, ecc.)
  }

  pinMode(ledPin, OUTPUT);
  digitalWrite(ledPin, LOW);

  // Controllo modulo WiFi
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("Modulo WiFi non trovato, controlla la scheda");
    while (true);
  }

  // Connessione al WiFi (solo 2.4 GHz, non 5 GHz)
  while (status != WL_CONNECTED) {
    Serial.print("Connessione a ");
    Serial.println(ssid);
    status = WiFi.begin(ssid, pass);
    delay(2000);
  }

  Serial.println("Connesso al WiFi");
  Serial.print("IP della scheda: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.println("Server TCP avviato sulla porta 5000");
}

// -----------------------------------------------------------------


void loop() {

  // STABILIRE UNA CONNESSIONE --------------------------------------------

  // Attende un client in ingresso (i.e. il pc che si collega)
  WiFiClient client = server.available();

  // Se esiste un client
  if (client) {
    Serial.println("Client connesso");

    // mentre il client è connesso
    while (client.connected()) {

      // COSE DA FARE NON APPENA STABILITA LA CONNESSIONE --------------------------------
      // Legge il tempo locale di Arduino in microsecondi prima di eseguire eventuale codice
      unsigned long t2 = micros();

      // se nel buffer c'è almeno un byte
      if (client.available()) {

        // Legge una linea dal client e la pulisce
        String line = client.readStringUntil('\n');
        line.trim();

        // SPAZIO PER FAR FAR COSE AD ARDUINO IN BASE A QUELLO CHE RICEVE DA PC ----------------
        // Ad ora facciamo semplicemente fare qualcosa ad Arduino per occuparla. 
        // Leggiamo un sensore e invertiamo lo stato di un led

        if (line == "led_on") {

          // accensione led
          digitalWrite(ledPin, HIGH);

        } else if (line == "led_off") {

          // spegnimento led
          digitalWrite(ledPin, LOW);

        } else if (line == "SYNC") {

          // Lettura fotoresistenza
          lightTime = micros();
          lightValue = analogRead(sensorPin);

          // Legge il tempo locale di Arduino in microsecondi dopo aver eseguito eventuale codice
          unsigned long t3 = micros();

          // LEGGIAMO IL TEMPO DI FINE CODICE E SPEDIAMO DATI INDIETRO AL PC ------

          // Invia indietro i timestamp in microsecondi come stringa + newline o altri valori rilevati
          // formato atteso da Python:
          //   T:t2,t3 \t LUX:lightTime,lightValue\n

          client.print("T:");
          client.print(t2);
          client.print(",");
          client.print(t3);
          client.print('\t');  // separatore fra messaggi sulla stessa riga

          client.print("LUX:");
          client.print(lightTime);
          client.print(",");
          client.println(lightValue);
        }

      } // end if (client.available())

    } // end while(client.connected())

    // Quanto il client non è più connesso chiudiamo la connessione
    client.stop();
    Serial.println("Client disconnesso");
  }

}
