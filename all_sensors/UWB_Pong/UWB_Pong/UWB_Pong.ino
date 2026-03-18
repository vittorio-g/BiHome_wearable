#include <DW3000.h>

// ====== PINOUT (tuo setup) ======
#define PIN_RSTN 9
#define PIN_CSN  10

static int rx_status = 0;
static int curr_stage = 0;
/*
  valid stages:
  0 - await ranging
  1 - ranging received; sending response
  2 - response sent; await second response
  3 - second response received; sending information frame
*/

static int t_roundB = 0;
static int t_replyB = 0;
static long long rx_ts = 0;
static long long tx_ts = 0;

// ---------- Utility ----------
void fatal(const char* msg) {
  Serial.println(msg);
  while (true) delay(100);
}

void releaseReset() {
  pinMode(PIN_RSTN, OUTPUT);
  digitalWrite(PIN_RSTN, HIGH);
}

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
    delay(10);
  }
  return true;
}

void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && (millis() - t0 < 3000)) {}

  Serial.println("\n[PONG] DS-TWR init...");

  pinMode(PIN_CSN, OUTPUT);
  digitalWrite(PIN_CSN, HIGH);

  releaseReset();
  delay(10);
  pulseReset();

  DW3000.begin();
  delay(50);

  if (!DW3000.checkSPI()) {
    Serial.println("[PONG] checkSPI fallito, retry dopo reset...");
    pulseReset();
    delay(50);
    if (!DW3000.checkSPI()) {
      fatal("[PONG][ERR] SPI KO");
    }
  }

  if (!waitForIdle(2000)) fatal("[PONG][ERR] IDLE1 FAILED");

  DW3000.softReset();
  delay(100);

  if (!waitForIdle(2000)) fatal("[PONG][ERR] IDLE2 FAILED");

  DW3000.init();
  DW3000.setupGPIO();

  // L'esempio configura TX e poi mette subito in RX
  DW3000.configureAsTX();
  DW3000.clearSystemStatus();
  DW3000.standardRX();

  Serial.println("[PONG] Ready. In ascolto per ranging...");
}

void loop() {
  switch (curr_stage) {
    case 0: {
      // Await initial ranging frame
      t_roundB = 0;
      t_replyB = 0;

      rx_status = DW3000.receivedFrameSucc();

      if (rx_status) {
        DW3000.clearSystemStatus();

        if (rx_status == 1) {
          if (DW3000.ds_isErrorFrame()) {
            Serial.println("[PONG][WARN] Error frame -> reset state");
            curr_stage = 0;
            DW3000.standardRX();
          } else if (DW3000.ds_getStage() != 1) {
            DW3000.ds_sendErrorFrame();
            DW3000.standardRX();
            curr_stage = 0;
          } else {
            curr_stage = 1;
          }
        } else {
          Serial.print("[PONG][ERR] RX status=");
          Serial.println(rx_status);
          DW3000.standardRX();
          curr_stage = 0;
        }
      }
      break;
    }

    case 1: {
      // Ranging received -> send response (stage 2)
      DW3000.ds_sendFrame(2);

      rx_ts = DW3000.readRXTimestamp();
      tx_ts = DW3000.readTXTimestamp();
      t_replyB = (int)(tx_ts - rx_ts);

      curr_stage = 2;
      break;
    }

    case 2: {
      // Await second ranging frame (stage 3)
      rx_status = DW3000.receivedFrameSucc();

      if (rx_status) {
        DW3000.clearSystemStatus();

        if (rx_status == 1) {
          if (DW3000.ds_isErrorFrame()) {
            Serial.println("[PONG][WARN] Error frame stage 3 -> reset state");
            curr_stage = 0;
            DW3000.standardRX();
          } else if (DW3000.ds_getStage() != 3) {
            DW3000.ds_sendErrorFrame();
            DW3000.standardRX();
            curr_stage = 0;
          } else {
            curr_stage = 3;
          }
        } else {
          Serial.print("[PONG][ERR] RX status (stage2)=");
          Serial.println(rx_status);
          DW3000.standardRX();
          curr_stage = 0;
        }
      }
      break;
    }

    case 3: {
      // Second response received -> send ranging timing info back
      rx_ts = DW3000.readRXTimestamp();
      t_roundB = (int)(rx_ts - tx_ts);

      DW3000.ds_sendRTInfo(t_roundB, t_replyB);

      // Torna subito in attesa
      curr_stage = 0;
      DW3000.standardRX();
      break;
    }

    default: {
      Serial.print("[PONG][ERR] Stage sconosciuto: ");
      Serial.println(curr_stage);
      curr_stage = 0;
      DW3000.standardRX();
      break;
    }
  }
}