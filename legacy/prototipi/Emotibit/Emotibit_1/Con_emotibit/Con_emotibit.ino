#include <SPI.h>
#include <WiFiNINA.h>
#include <stdint.h>

// IMPOSTAZIONI WIFI --------------------------------------

char ssid[] = "YOUR_WIFI_SSID";
char pass[] = "YOUR_WIFI_PASSWORD";

// IMPOSTAZIONI ARDUINO ------------------------------------

const int sensorPin = A1;
const int ledPin = 2;

int lightValue = 0;

// WRAP TRACKING per micros() ------------------------------
// micros() è 32-bit su molte board: wrap ~71.58 minuti.
// Usiamo un contatore di wrap per estendere a 64-bit lato PC.

static uint32_t prev_micros = 0;
static uint32_t wraps = 0;

static inline void update_wrap(uint32_t now_us) {
  if (now_us < prev_micros) {
    wraps++;
  }
  prev_micros = now_us;
}

static inline void capture_time(uint32_t &wrap_out, uint32_t &us_out) {
  uint32_t now = (uint32_t)micros();
  update_wrap(now);
  wrap_out = wraps;
  us_out = now;
}

// PARTE STANDARD WIFI + SERVER ----------------------------

int status = WL_IDLE_STATUS;
WiFiServer server(5000);

void setup() {
  Serial.begin(9600);
  while (!Serial) {;}

  pinMode(ledPin, OUTPUT);
  digitalWrite(ledPin, LOW);

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("Modulo WiFi non trovato, controlla la scheda");
    while (true);
  }

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

// ---------------------------------------------------------

void loop() {
  WiFiClient client = server.available();

  if (client) {
    Serial.println("Client connesso");
    client.setTimeout(50); // evita blocchi lunghi su readStringUntil

    // inizializza wrap tracking “bene” appena connesso
    prev_micros = (uint32_t)micros();
    wraps = 0;

    while (client.connected()) {
      if (!client.available()) {
        continue;
      }

      String line = client.readStringUntil('\n');
      line.trim();
      if (line.length() == 0) continue;

      if (line == "led_on") {
        digitalWrite(ledPin, HIGH);
        // opzionale: client.println("ACK:led_on");
      }
      else if (line == "led_off") {
        digitalWrite(ledPin, LOW);
        // opzionale: client.println("ACK:led_off");
      }
      else if (line == "SYNC") {
        // t2: inizio handling SYNC (wrap-aware)
        uint32_t w2, t2;
        capture_time(w2, t2);

        // lettura sensore + timestamp (wrap-aware)
        uint32_t wLux, tLux;
        capture_time(wLux, tLux);
        lightValue = analogRead(sensorPin);

        // t3: fine handling SYNC (wrap-aware)
        uint32_t w3, t3;
        capture_time(w3, t3);

        // Formato nuovo (wrap-aware):
        //   T:w2,t2,w3,t3 \t LUX:wLux,tLux,lightValue\n

        client.print("T:");
        client.print(w2);
        client.print(",");
        client.print(t2);
        client.print(",");
        client.print(w3);
        client.print(",");
        client.print(t3);
        client.print('\t');

        client.print("LUX:");
        client.print(wLux);
        client.print(",");
        client.print(tLux);
        client.print(",");
        client.println(lightValue);
      }
    }

    client.stop();
    Serial.println("Client disconnesso");
  }
}
