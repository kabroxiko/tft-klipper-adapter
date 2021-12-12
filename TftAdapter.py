import serial
import atexit
import requests  # instalation python -m pip install requests
import threading

TEMP_URL = "http://192.168.100.151/printer/objects/query?extruder=target,temperature&heater_bed=target,temperature"

ser = serial.Serial('/dev/ttyAMA0', 57600)  # open serial port

print(ser.name)

temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"

lock = threading.Lock()

# display is asking by M105 for reporting temps
def auto_satus_repost():
  threading.Timer(5.0, auto_satus_repost).start()
  write_to_serial(get_status())
  

def write_to_serial(data):
    if ser.is_open:
        lock.acquire()
        try:
            ser.write(bytes(data))
        finally:
            lock.release()
    else:
        print("serial port is not open")

def get_status():
    r = requests.get(TEMP_URL)
    print(r.status_code)
    print(r.json())
    status = r.json().get("result").get("status")
    statusExtruder = status.get("extruder")
    statusBed = status.get("heater_bed")
    return temp_template.format(ETemp = statusExtruder.get("temperature"), ETarget= statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget = statusBed.get("target"))

#auto_satus_repost()

while True:

    gcode = ser.readline()
    print("data from serial:\n")
    print(gcode)

    if "M105" in gcode.capitalize():
        write_to_serial(get_status())





def exit_handler():
    if ser.is_open:
        ser.close()
    print("Serial closed")

atexit.register(exit_handler)
