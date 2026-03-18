/*
  ============================================================
  MKR (WiFiNINA) — ECG (analog) -> TCP server -> PC

  Protocollo line-based (ASCII):
    - Riceve:
        SYNC:<seq>   -> risponde T:<seq>,w2,t2_32,w3,t3_32
        led_on / led_off
    - Invia:
        ECG:wrap,us32,ecg_mV

  NOTE:
    - Niente moving average
    - Timestamp del campione stimato al centro di analogRead()
  ============================================================
*/

#include <WiFiNINA.h>
#include "secrets.h"  // credenziali WiFi — gitignored, non in repo

const uint16_t TCP_PORT = 5000;

// ------------------------------
// CONFIG ECG
// ------------------------------
const uint8_t CONNECTION_LED_PIN = 5;
const uint8_t ECG_PIN = A0;

const float ECG_FS_HZ = 250.0f;
const int ADC_BITS = 12;
const float ADC_MAX = (1 << ADC_BITS) - 1;
const float VREF = 3.3f;

inline float adcToMilliVolt(int raw) {
  return (raw * (VREF / ADC_MAX)) * 1000.0f;
}

// ------------------------------
// TCP
// ------------------------------
WiFiServer server(TCP_PORT);
WiFiClient client;

// ------------------------------
// Clock wrap micros()
// ------------------------------
static uint32_t last_us32 = 0;
static uint32_t wrap32 = 0;

uint64_t readMicros64() {
  uint32_t now = micros();
  if (now < last_us32) wrap32++;
  last_us32 = now;
  return (((uint64_t)wrap32) << 32) | (uint64_t)now;
}

void splitMicros64(uint64_t t, uint32_t &wrapOut, uint32_t &us32Out) {
  wrapOut = (uint32_t)(t >> 32);
  us32Out = (uint32_t)(t & 0xFFFFFFFFULL);
}

// ------------------------------
// Parsing comandi
// ------------------------------
String rxLine;

bool parseSyncSeq(const String &cmd, uint32_t &seqOut) {
  if (cmd == "SYNC") {
    seqOut = 0;
    return true;
  }
  if (cmd.startsWith("SYNC:")) {
    String tail = cmd.substring(5);
    tail.trim();
    if (tail.length() == 0) return false;
    seqOut = (uint32_t)tail.toInt();
    return true;
  }
  return false;
}

void sendSyncReply(uint32_t seq) {
  uint64_t t2_64 = readMicros64();
  uint64_t t3_64 = readMicros64();

  uint32_t w2, t2_32, w3, t3_32;
  splitMicros64(t2_64, w2, t2_32);
  splitMicros64(t3_64, w3, t3_32);

  client.print("T:");
  client.print(seq);
  client.print(",");
  client.print(w2); client.print(",");
  client.print(t2_32); client.print(",");
  client.print(w3); client.print(",");
  client.print(t3_32);
  client.print("\n");
}

void handleCommand(const String &cmd) {
  uint32_t seq = 0;
  if (parseSyncSeq(cmd, seq)) {
    sendSyncReply(seq);
    return;
  }

  if (cmd == "led_on") {
    digitalWrite(LED_BUILTIN, HIGH);
    client.print("ACK:led_on\n");
    return;
  }

  if (cmd == "led_off") {
    digitalWrite(LED_BUILTIN, LOW);
    client.print("ACK:led_off\n");
    return;
  }

  client.print("ERR:unknown_cmd\n");
}

void pollClientCommands() {
  if (!client || !client.connected()) return;

  while (client.available()) {
    char c = (char)client.read();
    if (c == '\n') {
      rxLine.trim();
      if (rxLine.length() > 0) handleCommand(rxLine);
      rxLine = "";
    } else if (c != '\r') {
      rxLine += c;
      if (rxLine.length() > 200) rxLine = "";
    }
  }
}

// ------------------------------
// Emit ECG sample
// ------------------------------
void sendEcgSample(uint64_t t_us64, float ecg_mV) {
  if (!client || !client.connected()) return;

  uint32_t w, us32;
  splitMicros64(t_us64, w, us32);

  client.print("ECG:");
  client.print(w); client.print(",");
  client.print(us32); client.print(",");
  client.print(ecg_mV, 6);
  client.print("\n");
}

// ------------------------------
// Setup
// ------------------------------
void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  pinMode(CONNECTION_LED_PIN, OUTPUT);
  digitalWrite(CONNECTION_LED_PIN, LOW);

  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && (millis() - t0) < 3000) { }

  analogReadResolution(ADC_BITS);

  Serial.println("[ECG_WIFI] Connecting WiFi...");
  int status = WL_IDLE_STATUS;
  while (status != WL_CONNECTED) {
    status = WiFi.begin(WIFI_SSID, WIFI_PASS);
    delay(1000);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("[ECG_WIFI] IP: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.print("[ECG_WIFI] TCP server listening on port ");
  Serial.println(TCP_PORT);

  digitalWrite(CONNECTION_LED_PIN, HIGH);
}

// ------------------------------
// Loop
// ------------------------------
unsigned long nextSampleMicros = 0;
const unsigned long samplePeriodUs = (unsigned long)(1000000.0f / ECG_FS_HZ);

void loop() {
  if (!client || !client.connected()) {
    WiFiClient newClient = server.available();
    if (newClient) {
      client = newClient;
      Serial.println("[ECG_WIFI] Client connected.");
      client.print("HELLO:ECG_WIFI\n");
      rxLine = "";
      nextSampleMicros = micros();
    }
  }

  pollClientCommands();

  if (client && client.connected()) {
    unsigned long nowUs = micros();
    if ((long)(nowUs - nextSampleMicros) >= 0) {
      nextSampleMicros += samplePeriodUs;

      uint64_t t0_64 = readMicros64();
      int raw = analogRead(ECG_PIN);
      uint64_t t1_64 = readMicros64();

      uint64_t t_mid_64 = t0_64 + ((t1_64 - t0_64) / 2ULL);
      float ecg_mV = adcToMilliVolt(raw);

      sendEcgSample(t_mid_64, ecg_mV);
    }
  }
}