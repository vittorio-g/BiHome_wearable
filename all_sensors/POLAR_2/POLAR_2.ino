#include <ArduinoBLE.h>

// ==============================
// CONFIG SAMPLE RATE
// ==============================
// H10: ECG 130 Hz, ACC 25/50/100/200 Hz
const uint16_t ECG_SAMPLE_RATE_HZ = 50;
const uint16_t ACC_SAMPLE_RATE_HZ = 50;   // abbassato da 200 -> 100

const uint32_t ECG_PERIOD_US = (uint32_t)(1000000.0 / (double)ECG_SAMPLE_RATE_HZ + 0.5);
const uint32_t ACC_PERIOD_US = (uint32_t)(1000000.0 / (double)ACC_SAMPLE_RATE_HZ + 0.5);

// Output tick = frequenza più lenta
const uint32_t OUT_PERIOD_US = (ECG_PERIOD_US >= ACC_PERIOD_US) ? ECG_PERIOD_US : ACC_PERIOD_US;

// ==============================
// PIN / SERIAL
// ==============================
const uint8_t POLAR_CONN_LED_PIN = 1;
const uint32_t BAUD = 921600;

// ==============================
// Polar UUIDs (PMD)
// ==============================
#define PMD_SERVICE_UUID  "FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8"
#define PMD_CONTROL_UUID  "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
#define PMD_DATA_UUID     "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
#define HR_SERVICE_UUID   "180D"

// ECG start payload
const uint8_t ENABLE_ECG[] = { 0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00 };

// ==============================
// micros() 64-bit
// ==============================
static uint32_t last_us32 = 0;
static uint32_t wrap32 = 0;

// ==============================
// Polar sensor clock → Arduino micros64 calibration
//
// I pacchetti PMD della Polar H10 contengono nei byte 1-8 un timestamp
// a 64 bit in nanosecondi generato dall'orologio interno del sensore.
// Questo timestamp rappresenta il momento di campionamento dell'ULTIMO
// campione del batch. Usarlo come ancoraggio (invece del tempo di arrivo
// BLE su Arduino) elimina la latenza variabile del connection interval
// (~7.5–30 ms) che sfaserebbe la Polar rispetto all'ECG.
//
// Il clock della Polar (ns) è un dominio separato da Arduino micros64()
// (us). Manteniamo una stima EMA dell'offset per convertire i timestamp
// Polar nel dominio Arduino, in modo che il meccanismo NTP esistente
// (Arduino ↔ PC) continui a funzionare invariato.
// ==============================

static bool     polar_clk_init    = false;
static int64_t  polar_clk_off_us  = 0;   // EMA di (arduino_us - polar_ns/1000)
static const float POLAR_CLK_ALPHA = 0.05f;

// Aggiorna l'offset con la nuova misurazione arrivo BLE vs timestamp Polar.
// Chiamare una volta per pacchetto PMD, prima di usare polarNsToArduinoUs().
void updatePolarClock(uint64_t arrival_us64, uint64_t polar_ns) {
  int64_t polar_us  = (int64_t)(polar_ns / 1000ULL);
  int64_t sample    = (int64_t)arrival_us64 - polar_us;
  if (!polar_clk_init) {
    polar_clk_off_us = sample;
    polar_clk_init   = true;
  } else {
    polar_clk_off_us = (int64_t)(
      (1.0f - POLAR_CLK_ALPHA) * (float)polar_clk_off_us +
      POLAR_CLK_ALPHA           * (float)sample
    );
  }
}

// Converte un timestamp Polar (ns) nel dominio Arduino micros64() (us).
uint64_t polarNsToArduinoUs(uint64_t polar_ns) {
  return (uint64_t)((int64_t)(polar_ns / 1000ULL) + polar_clk_off_us);
}

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

// ==============================
// Ring buffers
// ==============================
template<int N>
struct RingECG {
  uint64_t t[N];
  float v[N];
  uint16_t head = 0, tail = 0, count = 0;

  void clear() {
    head = tail = count = 0;
  }

  void push(uint64_t ts, float val) {
    if (count == N) {
      tail = (tail + 1) % N;
      count--;
    }
    t[head] = ts;
    v[head] = val;
    head = (head + 1) % N;
    count++;
  }

  bool popUntil(uint64_t t_limit, float &outVal) {
    bool any = false;
    while (count > 0) {
      if (t[tail] > t_limit) break;
      outVal = v[tail];
      tail = (tail + 1) % N;
      count--;
      any = true;
    }
    return any;
  }
};

template<int N>
struct RingACC {
  uint64_t t[N];
  float x[N], y[N], z[N];
  uint16_t head = 0, tail = 0, count = 0;

  void clear() {
    head = tail = count = 0;
  }

  void push(uint64_t ts, float ax, float ay, float az) {
    if (count == N) {
      tail = (tail + 1) % N;
      count--;
    }
    t[head] = ts;
    x[head] = ax;
    y[head] = ay;
    z[head] = az;
    head = (head + 1) % N;
    count++;
  }

  bool popUntil(uint64_t t_limit, float &outX, float &outY, float &outZ) {
    bool any = false;
    while (count > 0) {
      if (t[tail] > t_limit) break;
      outX = x[tail];
      outY = y[tail];
      outZ = z[tail];
      tail = (tail + 1) % N;
      count--;
      any = true;
    }
    return any;
  }
};

RingECG<512> ecgBuf;
RingACC<512> accBuf;

// ==============================
// PC commands
// ==============================
String rxLine;

bool parseSyncSeq(const String &cmd, uint32_t &seqOut) {
  if (cmd.startsWith("SYNC:")) {
    String tail = cmd.substring(5);
    tail.trim();
    if (tail.length() == 0) return false;
    seqOut = (uint32_t)tail.toInt();
    return true;
  }
  if (cmd == "SYNC") {
    seqOut = 0;
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

  Serial.print("T:");
  Serial.print(seq);
  Serial.print(",");
  Serial.print(w2); Serial.print(",");
  Serial.print(t2_32); Serial.print(",");
  Serial.print(w3); Serial.print(",");
  Serial.print(t3_32);
  Serial.print("\n");
}

void handleCommand(const String &cmd) {
  uint32_t seq = 0;
  if (parseSyncSeq(cmd, seq)) { sendSyncReply(seq); return; }
  if (cmd == "led_on")  { digitalWrite(LED_BUILTIN, HIGH); Serial.print("ACK:led_on\n"); return; }
  if (cmd == "led_off") { digitalWrite(LED_BUILTIN, LOW);  Serial.print("ACK:led_off\n"); return; }
  Serial.print("ERR:unknown_cmd\n");
}

void pollSerialCommands() {
  while (Serial.available()) {
    char c = (char)Serial.read();
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

// ==============================
// BLE state
// ==============================
BLEDevice polar;
BLEService pmdService;
BLECharacteristic pmdControl;
BLECharacteristic pmdData;

bool connected = false;

bool ecgStartSent = false;
bool accStartSent = false;

bool haveEcgData = false;
bool haveAccData = false;

unsigned long lastNoDataWarnMs = 0;
unsigned long ecgStartMs = 0;

// ==============================
// Dashboard state
// ==============================
float lastEcg_uV = NAN;
float lastAx_mg = NAN, lastAy_mg = NAN, lastAz_mg = NAN;
uint64_t nextOutUs64 = 0;

// ==============================
// Helpers
// ==============================
int32_t int24_le(const uint8_t *p) {
  int32_t v = (int32_t)p[0] | ((int32_t)p[1] << 8) | ((int32_t)p[2] << 16);
  if (v & 0x00800000) v |= 0xFF000000;
  return v;
}

int16_t int16_le(const uint8_t *p) {
  return (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline int32_t sign_extend_u32(uint32_t x, uint8_t bits) {
  if (bits == 0 || bits >= 32) return (int32_t)x;
  uint32_t m = 1UL << (bits - 1);
  return (int32_t)((x ^ m) - m);
}

static int32_t read_bits_lsb_signed(const uint8_t* data, size_t nbytes, size_t bitpos, uint8_t nbits) {
  uint32_t v = 0;
  for (uint8_t b = 0; b < nbits; b++) {
    size_t byte_i = (bitpos + b) >> 3;
    uint8_t bit_i = (uint8_t)((bitpos + b) & 7);
    if (byte_i >= nbytes) break;
    if (data[byte_i] & (1U << bit_i)) v |= (1UL << b);
  }
  return sign_extend_u32(v, nbits);
}

// ==============================
// ACC payload builder
// ==============================
void buildAccPayload(uint8_t *buf, size_t &len, uint16_t sampleRateHz) {
  buf[0] = 0x02;
  buf[1] = 0x02;

  // setting: sample rate
  buf[2] = 0x00;
  buf[3] = 0x01;
  buf[4] = (uint8_t)(sampleRateHz & 0xFF);
  buf[5] = (uint8_t)((sampleRateHz >> 8) & 0xFF);

  // setting: resolution 16
  buf[6] = 0x01;
  buf[7] = 0x01;
  buf[8] = 0x10;
  buf[9] = 0x00;

  // setting: range 2G
  buf[10] = 0x02;
  buf[11] = 0x01;
  buf[12] = 0x02;
  buf[13] = 0x00;

  len = 14;
}

// ==============================
// Decode PMD -> buffers
// ==============================
void pushEcgSamples(const uint8_t *payload, int payloadN, uint64_t arrivalUs64) {
  const int step = 3;
  int nsamp = payloadN / step;
  if (nsamp <= 0) return;

  uint64_t firstTs = arrivalUs64 - (uint64_t)(nsamp - 1) * (uint64_t)ECG_PERIOD_US;
  for (int i = 0; i < nsamp; i++) {
    int32_t ecg = int24_le(payload + i * step);
    uint64_t ts = firstTs + (uint64_t)i * (uint64_t)ECG_PERIOD_US;
    ecgBuf.push(ts, (float)ecg);
  }

  if (!haveEcgData) {
    haveEcgData = true;
    nextOutUs64 = arrivalUs64;
    Serial.println("INFO:FIRST_ECG_DATA");
  }
}

void pushAccRaw(const uint8_t *payload, int payloadN, uint64_t arrivalUs64) {
  const int step = 6;
  int nsamp = payloadN / step;
  if (nsamp <= 0) return;

  uint64_t firstTs = arrivalUs64 - (uint64_t)(nsamp - 1) * (uint64_t)ACC_PERIOD_US;
  for (int i = 0; i < nsamp; i++) {
    const uint8_t* p = payload + i * step;
    int16_t ax = int16_le(p + 0);
    int16_t ay = int16_le(p + 2);
    int16_t az = int16_le(p + 4);
    uint64_t ts = firstTs + (uint64_t)i * (uint64_t)ACC_PERIOD_US;
    accBuf.push(ts, (float)ax, (float)ay, (float)az);
  }

  if (!haveAccData) {
    haveAccData = true;
    Serial.println("INFO:FIRST_ACC_DATA");
  }
}

void pushAccDelta(const uint8_t *payload, int payloadN, uint64_t arrivalUs64) {
  if (payloadN < 8) return;

  int32_t x = (int32_t)int16_le(payload + 0);
  int32_t y = (int32_t)int16_le(payload + 2);
  int32_t z = (int32_t)int16_le(payload + 4);

  const uint8_t deltaBits   = payload[6];
  const uint8_t sampleCount = payload[7];
  if (sampleCount == 0) return;

  const uint8_t* d = payload + 8;
  const size_t dlen = (size_t)(payloadN - 8);

  uint64_t firstTs = arrivalUs64 - (uint64_t)(sampleCount - 1) * (uint64_t)ACC_PERIOD_US;

  size_t bitpos = 0;
  for (uint8_t i = 0; i < sampleCount; i++) {
    const size_t needBits = (size_t)3 * (size_t)deltaBits;
    if (bitpos + needBits > dlen * 8) break;

    int32_t dx = read_bits_lsb_signed(d, dlen, bitpos, deltaBits); bitpos += deltaBits;
    int32_t dy = read_bits_lsb_signed(d, dlen, bitpos, deltaBits); bitpos += deltaBits;
    int32_t dz = read_bits_lsb_signed(d, dlen, bitpos, deltaBits); bitpos += deltaBits;

    x += dx; y += dy; z += dz;

    uint64_t ts = firstTs + (uint64_t)i * (uint64_t)ACC_PERIOD_US;
    accBuf.push(ts, (float)x, (float)y, (float)z);
  }

  if (!haveAccData) {
    haveAccData = true;
    Serial.println("INFO:FIRST_ACC_DATA");
  }
}

void handlePmdData(const uint8_t *buf, int n, uint64_t arrivalUs64) {
  if (n < 10) return;

  uint8_t measType = buf[0];

  // Byte 1-8: timestamp Polar (uint64 LE, nanosecondi dall'orologio interno
  // del sensore). Rappresenta il momento di campionamento dell'ULTIMO
  // campione del batch. Estraiamo questo valore e lo convertiamo nel
  // dominio Arduino micros64() tramite l'offset EMA calibrato.
  uint64_t polar_ts_ns = 0;
  for (uint8_t i = 0; i < 8; i++) {
    polar_ts_ns |= ((uint64_t)buf[1 + i]) << (8 * i);
  }

  uint8_t frameType = buf[9];

  const uint8_t *payload = buf + 10;
  int payloadN = n - 10;

  // Aggiorna la calibrazione clock Polar → Arduino con questo pacchetto,
  // poi converti il timestamp del sensore nel dominio Arduino.
  updatePolarClock(arrivalUs64, polar_ts_ns);
  uint64_t lastSampleUs64 = polarNsToArduinoUs(polar_ts_ns);

  if (measType == 0x00) { // ECG
    // lastSampleUs64 = timestamp Arduino dell'ULTIMO campione del batch
    pushEcgSamples(payload, payloadN, lastSampleUs64);
    return;
  }

  if (measType == 0x02) { // ACC
    bool isDelta = (frameType & 0x80) != 0;
    if (isDelta) pushAccDelta(payload, payloadN, lastSampleUs64);
    else         pushAccRaw(payload, payloadN, lastSampleUs64);
    return;
  }
}

// ==============================
// BLE connect/discover/start
// ==============================
bool connectAndDiscover() {
  BLE.scanForUuid(HR_SERVICE_UUID);

  unsigned long t0 = millis();
  while (millis() - t0 < 10000) {
    pollSerialCommands();
    BLE.poll();

    BLEDevice d = BLE.available();
    if (!d) continue;

    polar = d;
    BLE.stopScan();

    if (!polar.connect()) return false;
    if (!polar.discoverAttributes()) { polar.disconnect(); return false; }

    pmdService = polar.service(PMD_SERVICE_UUID);
    if (!pmdService) { polar.disconnect(); return false; }

    pmdControl = pmdService.characteristic(PMD_CONTROL_UUID);
    pmdData    = pmdService.characteristic(PMD_DATA_UUID);
    if (!pmdControl || !pmdData) { polar.disconnect(); return false; }

    pmdControl.subscribe();
    pmdData.subscribe();

    return true;
  }

  BLE.stopScan();
  return false;
}

bool sendStartECG() {
  if (!pmdControl.writeValue(ENABLE_ECG, sizeof(ENABLE_ECG))) {
    Serial.println("ERR:ECG_start_write_failed");
    return false;
  }

  ecgStartSent = true;
  ecgStartMs = millis();
  Serial.println("INFO:ECG_START_SENT");
  return true;
}

bool sendStartACC() {
  uint8_t accPayload[32];
  size_t accPayloadLen = 0;
  buildAccPayload(accPayload, accPayloadLen, ACC_SAMPLE_RATE_HZ);

  if (!pmdControl.writeValue(accPayload, accPayloadLen)) {
    Serial.println("ERR:ACC_start_write_failed");
    return false;
  }

  accStartSent = true;
  Serial.println("INFO:ACC_START_SENT");
  return true;
}

// ==============================
// Dashboard output
// ==============================
void emitDashboardRow(uint64_t t_tick) {
  ecgBuf.popUntil(t_tick, lastEcg_uV);
  accBuf.popUntil(t_tick, lastAx_mg, lastAy_mg, lastAz_mg);

  uint32_t w, us32;
  splitMicros64(t_tick, w, us32);

  Serial.print("Sens:");
  Serial.print(w); Serial.print(",");
  Serial.print(us32); Serial.print(",");

  if (isnan(lastEcg_uV)) Serial.print("nan"); else Serial.print(lastEcg_uV, 2);
  Serial.print(",");

  if (isnan(lastAx_mg)) Serial.print("nan"); else Serial.print(lastAx_mg, 2);
  Serial.print(",");

  if (isnan(lastAy_mg)) Serial.print("nan"); else Serial.print(lastAy_mg, 2);
  Serial.print(",");

  if (isnan(lastAz_mg)) Serial.print("nan"); else Serial.print(lastAz_mg, 2);

  Serial.print("\n");
}

void resetDataState() {
  ecgBuf.clear();
  accBuf.clear();

  lastEcg_uV = NAN;
  lastAx_mg = NAN;
  lastAy_mg = NAN;
  lastAz_mg = NAN;

  haveEcgData = false;
  haveAccData = false;

  ecgStartSent = false;
  accStartSent = false;

  nextOutUs64 = 0;

  // Reset calibrazione clock Polar: a ogni riconnessione il sensore può
  // avere un clock offset diverso, quindi ripartiamo da zero.
  polar_clk_init   = false;
  polar_clk_off_us = 0;
}

// ==============================
// Setup / Loop
// ==============================
void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  pinMode(POLAR_CONN_LED_PIN, OUTPUT);
  digitalWrite(POLAR_CONN_LED_PIN, LOW);

  Serial.begin(BAUD);
  while (!Serial) {}

  Serial.println("HELLO:POLAR_H10_USB");

  if (!BLE.begin()) {
    Serial.println("ERR:BLE.begin_failed");
    while (1) { delay(1000); }
  }

  BLE.setLocalName("MKR_POLAR_CENTRAL");
}

void loop() {
  pollSerialCommands();
  BLE.poll();

  if (!connected) {
    resetDataState();
    digitalWrite(POLAR_CONN_LED_PIN, LOW);

    if (connectAndDiscover()) {
      connected = true;
      digitalWrite(POLAR_CONN_LED_PIN, HIGH);
      Serial.println("INFO:POLAR_CONNECTED");
    } else {
      delay(200);
      return;
    }
  }

  if (connected && !polar.connected()) {
    connected = false;
    resetDataState();
    digitalWrite(POLAR_CONN_LED_PIN, LOW);
    Serial.println("INFO:POLAR_DISCONNECTED");
    delay(200);
    return;
  }

  // avvia ECG prima
  if (connected && !ecgStartSent) {
    if (!sendStartECG()) {
      delay(200);
      return;
    }
    delay(80);
  }

  // drena risposte control point
  for (int i = 0; i < 4; i++) {
    BLE.poll();
    if (pmdControl && pmdControl.valueUpdated()) {
      uint8_t buf[64];
      int n = pmdControl.readValue(buf, sizeof(buf));
      (void)n;
    }
  }

  // drena più notifiche PMD per loop
  for (int i = 0; i < 8; i++) {
    BLE.poll();
    if (!(pmdData && pmdData.valueUpdated())) break;

    uint64_t arrival = readMicros64();
    uint8_t buf[256];
    int n = pmdData.readValue(buf, sizeof(buf));
    if (n > 0) {
      handlePmdData(buf, n, arrival);
    }
  }

  // solo dopo il primo ECG, avvia ACC
  if (connected && ecgStartSent && haveEcgData && !accStartSent) {
    if (sendStartACC()) {
      delay(40);
    }
  }

  // warning se ECG non parte
  if (connected && ecgStartSent && !haveEcgData) {
    unsigned long nowMs = millis();

    if ((nowMs - ecgStartMs) > 1500 && !accStartSent) {
      Serial.println("WARN:NO_ECG_DATA_YET");
      // piccolo retry ECG
      ecgStartSent = false;
      delay(100);
    }

    if (nowMs - lastNoDataWarnMs > 3000) {
      Serial.println("WARN:WAITING_FOR_ECG");
      lastNoDataWarnMs = nowMs;
    }
  }

  // dashboard output: parte appena c'è ECG
  if (haveEcgData) {
    uint64_t now = readMicros64();
    while ((int64_t)(now - nextOutUs64) >= 0) {
      emitDashboardRow(nextOutUs64);
      nextOutUs64 += (uint64_t)OUT_PERIOD_US;
    }
  }

  delay(1);
}