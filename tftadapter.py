import serial
import atexit
import requests
import threading

class TFTAdapter:
    def __init__(self, config):
        ADDRESS = "http://127.0.0.1/"

        self.TEMP_URL = ADDRESS+"printer/objects/query?extruder=target,temperature&heater_bed=target,temperature"
        self.POSITION_URL = ADDRESS+"printer/objects/query?gcode_move=gcode_position"
        self.SPEED_FACTOR_URL = ADDRESS+"printer/objects/query?gcode_move=speed_factor"
        self.EXTRUDE_FACTOR_URL = ADDRESS+"printer/objects/query?gcode_move=extrude_factor"

        self.gcode_url_template = ADDRESS+"printer/gcode/script?script={g:s}"
        self.temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"
        self.position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_template = "FR:{fr:}%\nok\n"
        self.flow_rate_template = "E0 Flow: {er:}%\nok\n"

        self.tftSerial = serial.Serial('/dev/ttyS2', 115200)  # open serial port

        print(self.tftSerial.name)

        self.lock = threading.Lock()

        self.acceptable_gcode = ["M104", "M140", "M106", "M84"]

        self.start()

    # display is asking by M105 for reporting temps
    def auto_satus_repost(self):
        threading.Timer(5.0, auto_satus_repost).start()
        self.write_to_serial(self.get_status())

    def write_to_serial(self, data):
        if self.tftSerial.is_open:
            self.lock.acquire()
            try:
                self.tftSerial.write(bytes(data, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            print("serial port is not open")

    def get_status(self):
        r = requests.get(self.TEMP_URL)
        print(r.status_code)
        print(r.json())
        status = r.json().get("result").get("status")
        statusExtruder = status.get("extruder")
        statusBed = status.get("heater_bed")
        return self.temp_template.format(ETemp = statusExtruder.get("temperature"), ETarget= statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget = statusBed.get("target"))

    def get_current_position(self):
        r = requests.get(self.POSITION_URL)
        print(r.json())
        position = r.json().get("result").get("status").get("gcode_move").get("gcode_position")
        print(position)
        return self.position_template.format(x=position[0],y=position[1],z=position[2],e=position[3])

    def get_speed_factor(self):
        r = requests.get(self.SPEED_FACTOR_URL)
        print(r.json())
        speed_factor = r.json().get("result").get("status").get("gcode_move").get("speed_factor")
        return self.feed_rate_template.format(fr=speed_factor*100)

    def get_extrude_factor(self):
        r = requests.get(self.EXTRUDE_FACTOR_URL)
        print(r.json())
        extrude_factor = r.json().get("result").get("status").get("gcode_move").get("extrude_factor")
        return self.flow_rate_template.format(er=extrude_factor*100)

    #self.auto_satus_repost()
    def send_gcode_to_api(self, gcode):
        r = requests.post(self.gcode_url_template.format(g=gcode))
        print(r)
        return r.json().get("result")

    def check_is_basic_gcode(self, gcode):
        for g in self.acceptable_gcode:
            if g in gcode.capitalize():
                return True
        return False


    def exit_handler(self):
        if self.tftSerial.is_open:
            self.tftSerial.close()
        print("Serial closed")

    def start(self):
        while True:
            gcode = self.tftSerial.readline().decode("utf-8")
            print("data from serial:\n")
            print(gcode)

            if self.check_is_basic_gcode(gcode):
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
            elif "M105" in gcode.capitalize():
                self.write_to_serial(self.get_status())
            elif "M114" in gcode.capitalize():
                self.write_to_serial(self.get_current_position())
            elif "G28" in gcode.capitalize():
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
                self.write_to_serial(self.get_current_position())
            elif "G1" in gcode.capitalize():
                self.send_gcode_to_api("G91")
                self.send_gcode_to_api(gcode)
                self.send_gcode_to_api("G90")
                self.write_to_serial("ok\n")
                # self.write_to_serial(self.get_current_position())
            elif "M220" in gcode.capitalize():
                if "M220 S" in gcode.upper():
                    self.send_gcode_to_api(gcode)
                    self.write_to_serial("ok\n")
                else:
                    self.write_to_serial(self.get_speed_factor())
            elif "M221" in gcode.capitalize():
                if "M221 S" in gcode.upper():
                    self.send_gcode_to_api(gcode)
                    self.write_to_serial("ok\n")
                else:
                    self.write_to_serial(self.get_extrude_factor())
            else:
                print("default response to serial")
                self.write_to_serial(self.get_status())

        atexit.register(self.exit_handler)

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
