#!/usr/bin/env python3
import tftadapter
import logging

logging.basicConfig(format='%(message)s',level=logging.INFO)
tft_device = '/dev/ttyS2'
moonraker_uri = 'ws://127.0.0.1:7125'
tft_baud = 115200

class Printer:
    def __init__(self):
        self.reactor = ''
    def get_reactor(self):
        return self.reactor
    def lookup_object(self, name):
        default = {"name": name}
        return default

class PrinterConfig:
    def __init__(self):
        self.printer = Printer()
    def get_printer(self):
        return self.printer
    def get(self, key):
        if key == 'tft_device':
            return tft_device
        elif key == 'moonraker_uri':
            return moonraker_uri
        else:
            exit(1)
    def getint(self, key):
        if key == 'tft_baud':
            return tft_baud

def run():
    config = PrinterConfig()
    tftadapter.load_config(config)

if __name__ == '__main__':
    run()
