import serial
import atexit
import requests  # instalation python -m pip install requests
import threading

ADDRESS = "http://192.168.100.151/"

TEMP_URL = ADDRESS+"printer/objects/query?extruder=target,temperature&heater_bed=target,temperature"
POSITION_URL = ADDRESS+"printer/objects/query?gcode_move=gcode_position"
SPEED_FACTOR_URL = ADDRESS+"printer/objects/query?gcode_move=speed_factor"
EXTRUDE_FACTOR_URL = ADDRESS+"printer/objects/query?gcode_move=extrude_factor"

gcode_url_template = ADDRESS+"printer/gcode/script?script={g:s}"
temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"
position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
feed_rate_template = "FR:{fr:}%\nok\n"
flow_rate_template = "E0 Flow: {er:}%\nok\n"

ser = serial.Serial('/dev/ttyAMA0', 57600)  # open serial port

print(ser.name)

lock = threading.Lock()

acceptable_gcode = ["M104", "M140", "M106"]

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

def get_current_position():
    r = requests.get(POSITION_URL)
    print(r.json())
    position = r.json().get("result").get("status").get("gcode_move").get("gcode_position")
    print(position)
    return position_template.format(x=position[0],y=position[1],z=position[2],e=position[3])

def get_speed_factor():
    r = requests.get(SPEED_FACTOR_URL)
    print(r.json())
    speed_factor = r.json().get("result").get("status").get("gcode_move").get("speed_factor")
    return feed_rate_template.format(fr=speed_factor*100)

def get_extrude_factor():
    r = requests.get(EXTRUDE_FACTOR_URL)
    print(r.json())
    extrude_factor = r.json().get("result").get("status").get("gcode_move").get("extrude_factor")
    return flow_rate_template.format(er=extrude_factor*100)    

#auto_satus_repost()
def send_gcode_to_api(gcode):
    r = requests.post(gcode_url_template.format(g=gcode))
    print(r)
    return r.json().get("result")

def check_is_basic_gcode(gcode):
    for g in acceptable_gcode:
        if g in gcode.capitalize():
            return True
    return False

while True:

    gcode = ser.readline()
    print("data from serial:\n")
    print(gcode)
    
    if check_is_basic_gcode(gcode):
        send_gcode_to_api(gcode)
        write_to_serial("ok\n")
    elif "M105" in gcode.capitalize():
        write_to_serial(get_status())
    elif "M114" in gcode.capitalize():
        write_to_serial(get_current_position())
    elif "G28" in gcode.capitalize():
        send_gcode_to_api(gcode)
        write_to_serial("ok\n")
        write_to_serial(get_current_position())
    elif "G1" in gcode.capitalize():
        send_gcode_to_api("G91")
        send_gcode_to_api(gcode)
        send_gcode_to_api("G90")
        write_to_serial("ok\n")
        write_to_serial(get_current_position())
    elif "M220" in gcode.capitalize():    
        if "M220 S" in gcode.upper():
            send_gcode_to_api(gcode)
            write_to_serial("ok\n")
        else:
            write_to_serial(get_speed_factor())
    elif "M221" in gcode.capitalize():    
        if "M221 S" in gcode.upper():
            send_gcode_to_api(gcode)
            write_to_serial("ok\n")
        else:
            write_to_serial(get_extrude_factor())
    else:
        print("default response to serial")
        write_to_serial(get_status())
        

def exit_handler():
    if ser.is_open:
        ser.close()
    print("Serial closed")

atexit.register(exit_handler)
