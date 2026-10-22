#include <SPI.h>
#include <WiFiNINA.h>

char ssid[] = "YOUR_WIFI_SSID";      // nome WiFi
char pass[] = "YOUR_WIFI_PASSWORD"; // password WiFi

int status = WL_IDLE_STATUS;
WiFiServer server(5000);      // porta TCP a tua scelta

void setup() {
  Serial.begin(115200);
  while (!Serial);  // aspetta la seriale per stampare i messaggi

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

void loop() {
  // Attende un client in ingresso
  WiFiClient client = server.available();

  if (client) {
    Serial.println("Client connesso");

    while (client.connected()) {
      if (client.available()) {
        // Legge una riga dal client
        String line = client.readStringUntil('\n');
        line.trim();
        Serial.print("Ricevuto dal PC: ");
        Serial.println(line);

        // Risponde
        client.print("Echo da MKR: ");
        client.println(line);
      }
    }

    client.stop();
    Serial.println("Client disconnesso");
  }
}
