void setup() {
Serial.begin(115200);
}

void loop() {

  // leggi sensori
 int sensore1 = analogRead(A0);
 int sensore2 = analogRead(A1);

// stampa su seriale in formato csv
Serial.print(sensore1);
Serial.print(",");
Serial.println(sensore2);

// attendi 10 ms
delay(10);

}
