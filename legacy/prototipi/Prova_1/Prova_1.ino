const int inputPin = A0;
const int yellowLed = 1; // questo led si accende quando il segnale della fotoresistenza super la soglia
const int whiteLed = 2;
const int soglia = 900;
int n = 1; //counter


void setup() {
Serial.begin(9600);
pinMode(yellowLed, OUTPUT);
pinMode(whiteLed, OUTPUT);
}

void loop() {

while (n<10000){

//read time at the start
int long timestamp_start = millis();

// reading sensor
int valore = analogRead(inputPin);
long timestamp_sensor = millis();

digitalWrite(whiteLed, LOW);

Serial.print(timestamp_sensor - timestamp_start);
Serial.print(",");

delay(10);

n = n+1;
}



}
     