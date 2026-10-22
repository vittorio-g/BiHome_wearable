void setup() {
  Serial.begin(115200);
}

void loop() {
  // Se è arrivato almeno un byte dal PC
  if (Serial.available() > 0) {
    // Legge il byte ricevuto (contenuto non rilevante, poi da py mando 1B)
    char cmd = Serial.read();

    // Legge il tempo locale di Arduino in microsecondi prima di eseguire eventuale codice
    unsigned long t1_ard = micros();

    // Inserire eventuale codice
    // ...lettura emotibit...

    // Legge il tempo locale di Arduino in microsecondi dopo aver eseguito eventuale codice
    // unsigned long t2_ard = micros();


    // Invia indietro il timestamp in microsecondi come stringa + newline
    Serial.println(t1_ard);
  }
}