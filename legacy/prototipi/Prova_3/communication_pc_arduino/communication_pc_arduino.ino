void setup() {
  Serial.begin(115200);   // usa 115200 per ridurre la latenza
  while (!Serial) { ; }   // attende la porta sulle schede con USB nativa
}

void loop() {
  if (Serial.available() > 0) {
    int c = Serial.read();
    Serial.write(c);      // eco immediato
  }
}