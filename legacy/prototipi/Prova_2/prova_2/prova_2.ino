const int sensorPin = A0;
const int ledPin = 1;


void setup() {
Serial.begin(9600);
pinMode(ledPin, OUTPUT);
}

void loop() {
  int value = analogRead(sensorPin);

  Serial.println(value);

  if (Serial.available()>0)1     {
    char c = Serial.read();
    if (c == '1'){
      digitalWrite(ledPin,HIGH);
    } else if (c == '0'){
      digitalWrite(ledPin, LOW);
    }
  }

delay(1000);

}
