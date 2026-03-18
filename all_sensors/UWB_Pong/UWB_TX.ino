#include <DW3000.h>

#define PIN_RSTN 9      // RSTn del DWM3000EVB (attivo basso)
#define PIN_CSN  10     // CSn / SS del DW3000 (deve combaciare con la libreria)
#define TX_SENT_DELAY_MS 500

void fatal(const char* msg) {
  Serial.println(msg);
  while (true) delay(100);
}
12345678910
void setup() {
  // put your setup code here, to run once:

}

void loop() {
  // put your main code here, to run repeatedly:

}



// Rilascia il reset (RSTn HIGH = chip attivo)
void releaseReset() {
  pinMode(PIN_RSTN, OUTPUT);
  digitalWrite(PIN_RSTN, HIGH);
}

// Reset hardware manuale (attivo basso)
void pulseReset() {
  pinMode(PIN_RSTN, OUTPUT);
  digitalWrite(PIN_RSTN, LOW);
  delay(5);
  digitalWrite(PIN_RSTN, HIGH);
  delay(20);
}

bool waitForIdle(uint32_t timeoutMs) {
  unsigned long t0 = millis();
  while (!DW3000.checkForIDLE()) {
    if (millis() - t0 > timeoutMs) return false;
    delay(20);
  }
  return true;
}

bool waitTxSent(uint32_t timeoutMs) {
  unsigned long t0 = millis();
  while (!DW3000.sentFrameSucc()) {
    if (millis() - t0 > timeoutMs) return false;
    delay(5);
  }
  return true;
}

void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && (millis() - t0 < 3000)) {}

  Serial.println("\n[TX] Avvio...");

  // Buona pratica: CS alto a riposo (chip non selezionato)
  pinMode(PIN_CSN, OUTPUT);
  digitalWrite(PIN_CSN, HIGH);

  // IMPORTANTISSIMO: rilascia il reset prima di parlare col chip
  releaseReset();
  delay(10);

  // Reset hardware pulito
  pulseReset();

  // Init SPI / libreria
  DW3000.begin();
  delay(50);

  // Test SPI (eventualmente ritenta una volta dopo reset)
  if (!DW3000.checkSPI()) {
    Serial.println("[TX] checkSPI fallito al primo tentativo, rifaccio reset...");
    pulseReset();
    delay(50);

    if (!DW3000.checkSPI()) {
      fatal("[TX][ERR] SPI KO: controlla CS/MOSI/MISO/SCK/GND + RSTn su D9");
    }
  }

  Serial.println("[TX] SPI OK");

  if (!waitForIdle(2000)) {
    fatal("[TX][ERR] Timeout: chip non entra in IDLE");
  }

  DW3000.softReset();
  delay(100);

  if (!waitForIdle(2000)) {
    fatal("[TX][ERR] IDLE dopo softReset KO");
  }

  DW3000.init();
  DW3000.setupGPIO();      // opzionale (LED interni DW3000)
  DW3000.configureAsTX();

  Serial.println("[TX] Pronto. Invio frame periodici...");
}

void loop() {
  // LED TX interno DW3000 (se supportato dalla tua config)
  DW3000.pullLEDHigh(2);

  // Payload demo
  DW3000.setTXFrame(507);
  DW3000.setFrameLength(9);   // come esempio della libreria

  // Avvia TX
  DW3000.standardTX();

  // Attendi conferma invio con timeout
  if (!waitTxSent(500)) {
    Serial.println("[TX][ERR] Timeout TX (frame non inviato)");
    DW3000.clearSystemStatus();
    DW3000.pullLEDLow(2);
    delay(100);
    return;
  }

  DW3000.clearSystemStatus();
  Serial.println("[TX] Frame inviato");

  DW3000.pullLEDLow(2);
  delay(TX_SENT_DELAY_MS);
}