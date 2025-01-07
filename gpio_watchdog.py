import RPi.GPIO as GPIO
import time
import os

GPIO.setmode(GPIO.BCM)
GPIO.setup(2, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def restart_service(channel):
    os.system("sudo systemctl restart tft-klipper-adapter.service")

GPIO.add_event_detect(2, GPIO.FALLING, callback=restart_service, bouncetime=200)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    GPIO.cleanup()
