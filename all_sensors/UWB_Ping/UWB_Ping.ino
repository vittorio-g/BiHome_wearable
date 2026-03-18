#include <DW3000.h>

// ====== PINOUT (tuo setup) ======
#define PIN_RSTN 9      // RSTn DW3000EVB (attivo basso)
#define PIN_CSN  10     // CSn/SS DW3000 (la libreria usa tipicamente 10)

// ====== PARAMETRI ======
#define ROUND_DELAY_MS 250   // raddoppiato: prima era 500 ms

// ====== FILTRO FIR SIMMETRICO (7 punti, centrato) ======
static const float FIR_W[7] = {0.2f, 0.4f, 0.6f, 1.0f, 0.6f, 0.4f, 0.2f};
static const float FIR_W_SUM = 3.4f;  // normalizzazione per mantenerne la scala

static float dist_hist_cm[7] = {0};
static uint8_t dist_hist_count = 0;

// Inserisce il nuovo campione e, quando possibile, restituisce il filtrato.
// Il filtrato corrisponde al campione centrale della finestra (ritardo di 3 campioni).
bool pushDistanceAndFilter(float new_sample_cm, float &filtered_cm_out) {
  if (dist_hist_count < 7) {
    dist_hist_cm[dist_hist_count++] = new_sample_cm;
  } else {
    // shift a sinistra (costo trascurabile a questa frequenza)
    for (uint8_t i = 0; i < 6; i++) {
      dist_hist_cm[i] = dist_hist_cm[i + 1];
    }
    dist_hist_cm[6] = new_sample_cm;
  }

  if (dist_hist_count < 7) {
    return false; // warmup: non ho ancora 3 punti prima e 3 dopo
  }

  float acc = 0.0f;
  for (uint8_t i = 0; i < 7; i++) {
    acc += dist_hist_cm[i] * FIR_W[i];
  }
  filtered_cm_out = acc / FIR_W_SUM;
  return true;
}

// ====== STATO RANGING (come esempio libreria, ma ripulito) ======
static int rx_status = 0;
static int curr_stage = 0;
/*
  valid stages:
  0 - default; starts ranging
  1 - ranging sent; awaiting response
  2 - response received; sending second range
  3 - second ranging sent; awaiting final answer
  4 - final answer received; calculating results
*/

static int t_roundA = 0;
static int t_replyA = 0;
static long long rx_ts = 0;
static long long tx_ts = 0;
static int clock_offset = 0;
static int ranging_time = 0;
static float distance_cm = 0.0f;
static float distance_m = 0.0f;

// opzionali: valori filtrati
static float distance_cm_filt = 0.0f;
static float distance_m_filt  = 0.0f;

// ---------- Utility ----------
void fatal(const char* msg) {
  Serial.println(msg);
  while (true) delay(100);
}

void releaseReset() {
  pinMode(PIN_RSTN, OUTPUT);
  digitalWrite(PIN_RSTN, HIGH);  // RSTn HIGH = chip attivo
}

void pulseReset() {
  pinMode(PIN_RSTN, OUTPUT);
  digitalWrite(PIN_RSTN, LOW);   // reset attivo
  delay(5);
  digitalWrite(PIN_RSTN, HIGH);  // rilascio reset
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

  Serial.println("\n[PING] DS-TWR init...");
  Serial.println("[PING] FIR 7pt attivo (0.2,0.4,0.6,1,0.6,0.4,0.2) - output filtrato con ritardo 3 campioni");
  Serial.println("[PING] Campionamento raddoppiato (delay round = 250 ms)");

  // CS alto a riposo
  pinMode(PIN_CSN, OUTPUT);
  digitalWrite(PIN_CSN, HIGH);

  // Reset manuale (così non dipendi dal RST_PIN hardcoded nella libreria)
  releaseReset();
  delay(10);
  pulseReset();

  // Init libreria / SPI
  DW3000.begin();
  delay(50);

  if (!DW3000.checkSPI()) {
    Serial.println("[PING] checkSPI fallito, retry dopo reset...");
    pulseReset();
    delay(50);
    if (!DW3000.checkSPI()) {
      fatal("[PING][ERR] SPI KO");
    }
  }

  if (!waitForIdle(2000)) fatal("[PING][ERR] IDLE1 FAILED");

  DW3000.softReset();
  delay(100);

  if (!waitForIdle(2000)) fatal("[PING][ERR] IDLE2 FAILED");

  DW3000.init();
  DW3000.setupGPIO();
  DW3000.configureAsTX();   // come esempio libreria DS-TWR
  DW3000.clearSystemStatus();

  Serial.println("[PING] Ready. Distanza filtrata sul PING.");
}

void loop() {
  switch (curr_stage) {
    case 0: {
      // Start ranging
      t_roundA = 0;
      t_replyA = 0;

      DW3000.ds_sendFrame(1);             // stage 1 message
      tx_ts = DW3000.readTXTimestamp();
      curr_stage = 1;
      break;
    }

    case 1: {
      // Await first response
      rx_status = DW3000.receivedFrameSucc();

      if (rx_status) {
        DW3000.clearSystemStatus();

        if (rx_status == 1) {
          if (DW3000.ds_isErrorFrame()) {
            Serial.println("[PING][WARN] Error frame ricevuto -> reset state");
            curr_stage = 0;
          } else if (DW3000.ds_getStage() != 2) {
            DW3000.ds_sendErrorFrame();
            curr_stage = 0;
          } else {
            curr_stage = 2;
          }
        } else {
          // errore RX
          Serial.print("[PING][ERR] RX status=");
          Serial.println(rx_status);
          curr_stage = 0;
        }
      }
      break;
    }

    case 2: {
      // Response received -> send second ranging frame
      rx_ts = DW3000.readRXTimestamp();
      DW3000.ds_sendFrame(3);

      t_roundA = (int)(rx_ts - tx_ts);
      tx_ts = DW3000.readTXTimestamp();
      t_replyA = (int)(tx_ts - rx_ts);

      curr_stage = 3;
      break;
    }

    case 3: {
      // Await final answer
      rx_status = DW3000.receivedFrameSucc();

      if (rx_status) {
        DW3000.clearSystemStatus();

        if (rx_status == 1) {
          if (DW3000.ds_isErrorFrame()) {
            Serial.println("[PING][WARN] Error frame finale -> reset state");
            curr_stage = 0;
          } else {
            clock_offset = DW3000.getRawClockOffset();
            curr_stage = 4;
          }
        } else {
          Serial.print("[PING][ERR] RX status finale=");
          Serial.println(rx_status);
          curr_stage = 0;
        }
      }
      break;
    }

    case 4: {
      // Final answer received -> calculate distance
      ranging_time = DW3000.ds_processRTInfo(
        t_roundA,
        t_replyA,
        DW3000.read(0x12, 0x04),  // t_roundB dal PONG
        DW3000.read(0x12, 0x08),  // t_replyB dal PONG
        clock_offset
      );

      distance_cm = DW3000.convertToCM(ranging_time);
      distance_m  = distance_cm / 100.0f;

      // Filtro FIR centrato: stampa solo quando ho finestra completa (7 campioni)
      if (pushDistanceAndFilter(distance_cm, distance_cm_filt)) {
        distance_m_filt = distance_cm_filt / 100.0f;

        Serial.print("[PING] Dist:");
        Serial.println(distance_cm_filt, 1);   // output filtrato in cm
      } else {
        // Warmup iniziale del filtro (prime 6 misure)
        Serial.print("[PING] Filter warmup ");
        Serial.print(dist_hist_count);
        Serial.println("/7");
      }

      curr_stage = 0;
      delay(ROUND_DELAY_MS);
      break;
    }

    default: {
      Serial.print("[PING][ERR] Stage sconosciuto: ");
      Serial.println(curr_stage);
      curr_stage = 0;
      break;
    }
  }
}