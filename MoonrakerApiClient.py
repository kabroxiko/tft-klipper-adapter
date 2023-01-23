import requests  # instalation python3 -m pip install requests
import logging

class MoonrakerApiClient:

    def __init__(self,  address):
        self.TEMP_URL = address+"printer/objects/query?extruder=target,temperature&heater_bed=target,temperature"
        self.POSITION_URL = address+"printer/objects/query?gcode_move=gcode_position"
        self.SPEED_FACTOR_URL = address+"printer/objects/query?gcode_move=speed_factor"
        self.EXTRUDE_FACTOR_URL = address+"printer/objects/query?gcode_move=extrude_factor"
        self.gcode_url_template = address+"printer/gcode/script?script={g:s}"

    def __make_get_request(self, url):
        r = requests.get(url)
        if(r.status_code == 200):
            logging.info("Response (GET):{}".format(r.json()))
            return r.json()
        else:
            raise Exception("Can not get valid response from monraker (GET)")

    def __make_post_request(self, url):
        r = requests.post(url)
        if(r.status_code == 200):
            logging.info("Response (POST):{}".format(r.json()))
            return r.json()
        elif(r.status_code==400):
            message = r.json().get("error").get("message")
            # do not if 400, handle exception on bigger layer
            if message != "":
                logging.error(message)
                return None
            else:
                logging.error(r)
                raise Exception("Can not get valid response from monraker (POST)")

    def get_status(self):
        logging.debug("get status called")
        return self.__make_get_request(self.TEMP_URL)

    def get_current_position(self):
        logging.debug("get current position called")
        return self.__make_get_request(self.POSITION_URL)

    def get_speed_factor(self):
        logging.debug("get speed factor called")
        return self.__make_get_request(self.SPEED_FACTOR_URL)

    def get_extrude_factor(self):
        logging.debug("get exrude factor called")
        return self.__make_get_request(self.EXTRUDE_FACTOR_URL)
    
    def send_gcode_to_api(self, gcode):
        logging.debug("send gcode to API called")
        url = self.gcode_url_template.format(g=gcode);
        logging.info("URL:{}".format(url))
        r = self.__make_post_request(url)
        if(r != None):
            return r.get("result")
        else:
            return r
        
