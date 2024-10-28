import atexit
import threading
import logging
from MoonrakerApiClient import MoonrakerApiClient
from TFTSerial import TFTSerial

logging.basicConfig(level=logging.INFO)

# This should be moved to config
ADDRESS = "http://127.0.0.1/"
PORT = '/dev/ttyS2'

client = MoonrakerApiClient(ADDRESS)
tft_serial = TFTSerial(PORT)
tft_serial.open()


temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"
position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
feed_rate_template = "FR:{fr:}%\nok\n"
flow_rate_template = "E0 Flow: {er:}%\nok\n"

acceptable_gcode = ["M104", "M140", "M106", "M84"]

# display is asking by M105 for reporting temps
def auto_satus_repost():
    threading.Timer(5.0, auto_satus_repost).start()
    tft_serial.write(get_status())

def get_status():
    r = client.get_status()
    status = r.get("result").get("status")
    statusExtruder = status.get("extruder")
    statusBed = status.get("heater_bed")
    return temp_template.format(ETemp=statusExtruder.get("temperature"), ETarget=statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget=statusBed.get("target"))


def get_current_position():
    r = client.get_current_position()
    position = r.get("result").get("status").get("gcode_move").get("gcode_position")
    logging.info("Position:{}".format(position))
    return position_template.format(x=position[0], y=position[1], z=position[2], e=position[3])


def get_speed_factor():
    r = client.get_speed_factor()
    speed_factor = r.get("result").get("status").get("gcode_move").get("speed_factor")
    return feed_rate_template.format(fr=speed_factor*100)


def get_extrude_factor():
    r = client.get_extrude_factor()
    extrude_factor = r.get("result").get("status").get("gcode_move").get("extrude_factor")
    return flow_rate_template.format(er=extrude_factor*100)

def check_is_basic_gcode(gcode):
    for g in acceptable_gcode:
        if g in gcode.capitalize():
            return True
    return False

def exit_handler():
    tft_serial.close()
    logging.info("Serial closed")

atexit.register(exit_handler)

while True:

    gcode = tft_serial.read()

    if check_is_basic_gcode(gcode):
        client.send_gcode_to_api(gcode)
        tft_serial.send_ok()
    elif "M105" in gcode.capitalize():
        tft_serial.write(get_status())
    elif "M114" in gcode.capitalize():
        tft_serial.write(get_current_position())
    elif "G28" in gcode.capitalize():
        client.send_gcode_to_api(gcode)
        tft_serial.send_ok()
        tft_serial.write(get_current_position())
    elif "G1" in gcode.capitalize():
        client.send_gcode_to_api("G91")
        client.send_gcode_to_api(gcode)
        client.send_gcode_to_api("G90")
        tft_serial.send_ok()
        # write_to_serial(get_current_position())
    elif "M220" in gcode.capitalize():
        if "M220 S" in gcode.upper():
            client.send_gcode_to_api(gcode)
            tft_serial.send_ok()
        else:
            tft_serial.write(get_speed_factor())
    elif "M221" in gcode.capitalize():
        if "M221 S" in gcode.upper():
            client.send_gcode_to_api(gcode)
            tft_serial.send_ok()
        else:
            tft_serial.write(get_extrude_factor())
    else:
        logging.info("Default response to serial for gcode {}".format(gcode))
        tft_serial.write(get_status())
