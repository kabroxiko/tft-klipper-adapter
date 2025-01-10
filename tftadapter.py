# TFT LCD display support
#
# Copyright (C) 2020  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import serial
import os
import time
import errno
import logging
import asyncio
from jinja2 import Template
from collections import deque
from ..utils import ServerError
from ..utils import json_wrapper as jsonw

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Deque,
    Any,
    Tuple,
    Optional,
    Dict,
    List,
    Callable,
    Coroutine,
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .klippy_connection import KlippyConnection
    from .klippy_apis import KlippyAPI as APIComp
    from .file_manager.file_manager import FileManager as FMComp
    FlexCallback = Callable[..., Optional[Coroutine]]

MIN_EST_TIME = 10.
INITIALIZE_TIMEOUT = 10.

class TFTError(ServerError):
    pass


RESTART_GCODES = ["RESTART", "FIRMWARE_RESTART"]

MACHINE_TYPE = "Artillery Genius Pro"

PRINT_STATUS_TEMPLATE = (
    "//action:notification Layer Left {{ (virtual_sdcard.file_position or 0) }}/{{ (virtual_sdcard.file_size or 0) }}"
)

TEMPERATURE_TEMPLATE = (
    "T:{{ extruder.temperature | round(2) }} /{{ extruder.target | round(2) }} "
    "B:{{ heater_bed.temperature | round(2) }} /{{ heater_bed.target | round(2) }} "
    "@:0 B@:0"
)

PROBE_OFFSET_TEMPLATE = (
    "M851 X{{ bltouch.x_offset | float - gcode_move.homing_origin[0] }} "
    "Y{{ bltouch.y_offset | float - gcode_move.homing_origin[1] }} "
    "Z{{ bltouch.z_offset | float - gcode_move.homing_origin[2] }}"
)

REPORT_SETTINGS_TEMPLATE = (
    "M203 X{{ toolhead.max_velocity }} Y{{ toolhead.max_velocity }} "
    "Z{{ printer.max_z_velocity }} E{{ extruder.max_extrude_only_velocity }}\n"
    "M201 X{{ toolhead.max_accel }} Y{{ toolhead.max_accel }} "
    "Z{{ printer.max_z_accel }} E{{ extruder.max_extrude_only_accel }}\n"
    "M206 X{{ gcode_move.homing_origin[0] }} Y{{ gcode_move.homing_origin[1] }} Z{{ gcode_move.homing_origin[2] }}\n"
    f"{PROBE_OFFSET_TEMPLATE}\n"
    "M420 S1 Z{{ bed_mesh.fade_end }}\n"
    "M106 S{{ fan.speed }}"
)

FIRMWARE_INFO_TEMPLATE = (
    # "FIRMWARE_NAME:Klipper {{ mcu.mcu_version }} "
    "FIRMWARE_NAME:Klipper"
    "SOURCE_CODE_URL:https://github.com/Klipper3d/klipper "
    "PROTOCOL_VERSION:1.0 "
    f"MACHINE_TYPE:{MACHINE_TYPE}\n"
    "Cap:EEPROM:1\n"
    "Cap:AUTOREPORT_TEMP:1\n"
    "Cap:AUTOREPORT_POS:1\n"
    "Cap:AUTOLEVEL:1\n"
    "Cap:Z_PROBE:1\n"
    "Cap:LEVELING_DATA:0\n"
    "Cap:SOFTWARE_POWER:0\n"
    "Cap:TOGGLE_LIGHTS:0\n"
    "Cap:CASE_LIGHT_BRIGHTNESS:0\n"
    "Cap:EMERGENCY_PARSER:1\n"
    "Cap:PROMPT_SUPPORT:0\n"
    "Cap:SDCARD:1\n"
    "Cap:MULTI_VOLUME:0\n"
    "Cap:AUTOREPORT_SD_STATUS:1\n"
    "Cap:LONG_FILENAME:1\n"
    "Cap:BABYSTEPPING:1\n"
    "Cap:BUILD_PERCENT:1\n"
    "Cap:CHAMBER_TEMPERATURE:0"
)

SOFTWARE_ENDSTOPS_TEMPLATE = (
    "Soft endstops: {{ state }}"
)

POSITION_TEMPLATE = (
    "X:{{ gcode_move.position[0] | round(2) }} "
    "Y:{{ gcode_move.position[1] | round(2) }} "
    "Z:{{ gcode_move.position[2] | round(2) }} "
    "E:{{ gcode_move.position[3] | round(2) }}"
)

FEED_RATE_TEMPLATE = (
    "FR:{{ gcode_move.speed_factor * 100 | int }}%"
)
FLOW_RATE_TEMPLATE = (
    "E0 Flow:{{ gcode_move.extrude_factor * 100 | int }}%"
)

FILE_LIST_TEMPLATE = (
    "Begin file list\n"
    "{% for file, size in files %}{{ file }} {{ size }}\n{% endfor %}"
    "End file list\nok"
)

FILE_SELECT_TEMPLATE = (
    "File opened:{{ filename }} Size:{{ size }}\n"
    "File selected"
)

class SerialConnection:
    def __init__(self,
                 config: ConfigHelper,
                 tft: TFT
                 ) -> None:
        self.event_loop = config.get_server().get_event_loop()
        self.tft = tft
        self.port: str = config.get('serial')
        self.baud = config.getint('baud', 57600)
        self.partial_input: bytes = b""
        self.ser: Optional[serial.Serial] = None
        self.fd: Optional[int] = None
        self.connected: bool = False
        self.send_busy: bool = False
        self.send_buffer: bytes = b""
        self.attempting_connect: bool = True

    def disconnect(self, reconnect: bool = False) -> None:
        if self.connected:
            if self.fd is not None:
                self.event_loop.remove_reader(self.fd)
                self.fd = None
            self.connected = False
            if self.ser is not None:
                self.ser.close()
            self.ser = None
            self.partial_input = b""
            self.send_buffer = b""
            self.tft.initialized = False
            logging.info("TFT Disconnected")
        if reconnect and not self.attempting_connect:
            self.attempting_connect = True
            self.event_loop.delay_callback(1., self.connect)

    async def connect(self) -> None:
        self.attempting_connect = True
        start_time = connect_time = time.time()
        while not self.connected:
            if connect_time > start_time + 30.:
                logging.info("Unable to connect, aborting")
                break
            logging.info(f"Attempting to connect to: {self.port}")
            try:
                # XXX - sometimes the port cannot be exclusively locked, this
                # would likely be due to a restart where the serial port was
                # not correctly closed.  Maybe don't use exclusive mode?
                self.ser = serial.Serial(
                    self.port, self.baud, timeout=0, exclusive=True)
            except (OSError, IOError, serial.SerialException):
                logging.exception(f"Unable to open port: {self.port}")
                await asyncio.sleep(2.)
                connect_time += time.time()
                continue
            self.fd = self.ser.fileno()
            fd = self.fd = self.ser.fileno()
            os.set_blocking(fd, False)
            self.event_loop.add_reader(fd, self._handle_incoming)
            self.connected = True
            logging.info("TFT Connected")
        self.attempting_connect = False

    def _handle_incoming(self) -> None:
        # Process incoming data using same method as gcode.py
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, 4096)
        except os.error:
            return

        if not data:
            # possibly an error, disconnect
            self.disconnect(reconnect=True)
            logging.info("serial_display: No data received, disconnecting")
            return

        # Remove null bytes, separate into lines
        data = data.strip(b'\x00')
        lines = data.split(b'\n')
        lines[0] = self.partial_input + lines[0]
        self.partial_input = lines.pop()
        for line in lines:
            try:
                decoded_line = line.strip().decode('utf-8', 'ignore')
                self.tft.process_line(decoded_line)
            except ServerError:
                logging.exception(
                    f"GCode Processing Error: {decoded_line}")
                self.tft.handle_gcode_response(
                    f"!! GCode Processing Error: {decoded_line}")
            except Exception:
                logging.exception("Error during gcode processing")

    def send(self, data: bytes) -> None:
        self.ser.write(data)

class TFTAdapter:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.event_loop = self.server.get_event_loop()
        self.file_manager: FMComp = self.server.lookup_component('file_manager')
        self.klippy_apis: APIComp = self.server.lookup_component('klippy_apis')
        self.kinematics: str = "none"
        self.machine_name = config.get('machine_name', "Klipper")
        self.firmware_name: str = "Repetier | Klipper"
        self.last_message: Optional[str] = None
        self.last_gcode_response: Optional[str] = None
        self.current_file: str = ""
        self.file_metadata: Dict[str, Any] = {}
        self.enable_checksum = config.getboolean('enable_checksum', True)
        self.debug_queue: Deque[str] = deque(maxlen=100)
        self.temperature_report_task: Optional[asyncio.Task] = None
        self.position_report_task: Optional[asyncio.Task] = None

        # Initialize tracked state.
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        self.printer_state: Dict[str, Dict[str, Any]] = kconn.get_subscription_cache()
        self.extruder_count: int = 0
        self.heaters: List[str] = []
        self.is_ready: bool = False
        self.is_shutdown: bool = False
        self.initialized: bool = False
        self.cq_busy: bool = False
        self.gq_busy: bool = False
        self.command_queue: List[Tuple[FlexCallback, Any, Any]] = []
        self.gc_queue: List[str] = []
        self.last_printer_state: str = 'O'
        self.last_update_time: float = 0.

        # Set up macros
        self.confirmed_gcode: str = ""
        self.mbox_sequence: int = 0
        self.available_macros: Dict[str, str] = {}
        self.confirmed_macros = {
            "RESTART": "RESTART",
            "FIRMWARE_RESTART": "FIRMWARE_RESTART"}
        macros = config.getlist('macros', None)
        if macros is not None:
            # The macro's configuration name is the key, whereas the full
            # command is the value
            self.available_macros = {m.split()[0]: m for m in macros}
        conf_macros = config.getlist('confirmed_macros', None)
        if conf_macros is not None:
            # The macro's configuration name is the key, whereas the full
            # command is the value
            self.confirmed_macros = {m.split()[0]: m for m in conf_macros}
        self.available_macros.update(self.confirmed_macros)
        self.non_trivial_keys = config.getlist('non_trivial_keys', ["Klipper state"])
        self.ser_conn = SerialConnection(config, self)
        logging.info("TFT Configured")

        # Register server events
        self.server.register_event_handler(
            "server:klippy_ready", self._process_klippy_ready
        )
        self.server.register_event_handler(
            "server:klippy_shutdown", self._process_klippy_shutdown
        )
        self.server.register_event_handler(
            "server:klippy_disconnect", self._process_klippy_disconnect
        )
        self.server.register_event_handler(
            "server:gcode_response", self.handle_gcode_response
        )
        self.server.register_remote_method("tft_beep", self.tft_beep)

        # These commands are directly executued on the server and do not to
        # make a request to Klippy
        self.direct_gcodes: Dict[str, FlexCallback] = {
            'M20': self._run_tft_M20,
            'M21': self._run_tft_M21,
            'M27': self._run_tft_M27,
            'G29': self._run_tft_G29,
            'M30': self._run_tft_M30,
            'M33': self._run_tft_M33,
            'M36': self._run_tft_M36,
            'M82': self._run_tft_ok,
            'M92': self._run_tft_ok,
            'M105': self._run_tft_M105,
            'M108': self._run_tft_M108,
            'M114': self._run_tft_M114,
            'M118': self._run_tft_M118,
            'M115': self._run_tft_M115,
            'M154': self._run_tft_M154,
            'M155': self._run_tft_M155,
            'M201': self._run_tft_M201,
            'M203': self._run_tft_M203,
            'M206': self._run_tft_M206,
            'M211': self._run_tft_M211,
            'M220': self._run_tft_M220,
            'M221': self._run_tft_M221,
            'M280': self._run_tft_M280,
            'M503': self._run_tft_M503,
            'M524': self._run_tft_M524
        }

        # These gcodes require special parsing or handling prior to being
        # sent via Klippy's "gcode/script" api command.
        self.special_gcodes: Dict[str, Callable[[List[str]], str]] = {
            'M0': lambda args: "CANCEL_PRINT",
            'M23': self._prepare_M23,
            'M24': self._prepare_M24,
            'M25': self._prepare_M25,
            'M32': self._prepare_M32,
            'M120': lambda args: "SAVE_GCODE_STATE STATE=TFT",
            'M150': self._prepare_M150,
            'M121': lambda args: "RESTORE_GCODE_STATE STATE=TFT",
            'M290': self._prepare_M290,
            'M701': self._prepare_M701_M702,
            'M702': self._prepare_M701_M702,
            'M999': lambda args: "FIRMWARE_RESTART",
        }

    async def component_init(self) -> None:
        await self.ser_conn.connect()

    async def _process_klippy_ready(self) -> None:
        # Request "info" and "configfile" status
        retries = 10
        printer_info: Dict[str, Any] = {}
        cfg_status: Dict[str, Any] = {}
        while retries:
            try:
                printer_info = await self.klippy_apis.get_klippy_info()
                cfg_status = await self.klippy_apis.query_objects({'configfile': None})
            except self.server.error:
                logging.exception("TFT initialization request failed")
                retries -= 1
                if not retries:
                    raise
                await asyncio.sleep(1.)
                continue
            break

        self.firmware_name = "Repetier | Klipper " + printer_info['software_version']
        config: Dict[str, Any] = cfg_status.get('configfile', {}).get('config', {})
        printer_cfg: Dict[str, Any] = config.get('printer', {})
        self.kinematics = printer_cfg.get('kinematics', "none")
        self.printer: Dict[str, Any] = config.get('printer', {})
        self.bltouch: Dict[str, Any] = config.get('bltouch', {})
        self.bed_mesh: Dict[str, Any] = config.get('bed_mesh', {})

        logging.info(
            f"TFT Config Received:\n"
            f"Firmware Name: {self.firmware_name}\n"
            f"Kinematics: {self.kinematics}\n"
            f"Printer Config: {config}\n")

        # Make subscription request
        sub_args: Dict[str, Optional[List[str]]] = {
            "motion_report": None,
            "gcode_move": None,
            "toolhead": None,
            "virtual_sdcard": None,
            "fan": None,
            "display_status": None,
            "print_stats": None,
            "idle_timeout": None,
            "filament_switch_sensor filament_sensor": None,
            "gcode_macro TFT_BEEP": None
        }
        self.extruder_count = 0
        self.heaters = []
        extruders = []
        for cfg in config:
            if cfg.startswith("extruder"):
                self.extruder_count += 1
                extruders.append(cfg)
                sub_args[cfg] = None
            elif cfg == "heater_bed":
                self.heaters.append(cfg)
                sub_args[cfg] = None
        extruders.sort()
        self.heaters.extend(extruders)
        try:
            await self.klippy_apis.subscribe_objects(sub_args)
        except self.server.error:
            logging.exception("Unable to complete subscription request")
        self.is_shutdown = False
        self.is_ready = True
        self.event_loop.create_task(self._monitor_print_status())

    async def _monitor_print_status(self) -> None:
        while True:
            await asyncio.sleep(1)
            print_stats = self.printer_state.get('print_stats', {})
            logging.info(f"print_stats: {print_stats.get('state')}")
            state = print_stats.get('state', 'standby')
            if state == 'standby' and self.last_printer_state != 'standby':
                self.write_response(action="print_start")
            elif state == 'paused' and self.last_printer_state != 'paused':
                self.write_response(action="pause")
            elif state == 'printing' and self.last_printer_state != 'printing':
                self.write_response(action="resume")
            elif state == 'cancelled' and self.last_printer_state != 'cancelled':
                self.write_response(action="cancel")
            self.last_printer_state = state

    def _process_klippy_shutdown(self) -> None:
        self.is_shutdown = True

    def _process_klippy_disconnect(self) -> None:
        # Tell the PD that the printer is "off"
        self.write_response({'status': 'O'})
        self.last_printer_state = 'O'
        self.is_ready = False
        self.is_shutdown = self.is_shutdown = False

    def tft_beep(self, frequency: int, duration: float) -> None:
        duration = int(duration * 1000.)
        self.write_response(
            {'beep_freq': frequency, 'beep_length': duration})

    def process_line(self, line: str) -> None:
        logging.info(f"line: {line}")
        self.debug_queue.append(line)
        # If we find M112 in the line then skip verification
        if "M112" in line.upper():
            self.event_loop.register_callback(self.klippy_apis.emergency_stop)
            return

        if self.enable_checksum:
            # Get line number
            line_index = line.find(' ')
            if line_index == -1:
                line_index = len(line)
            logging.info(f"line_index: {line_index}")
            try:
                line_no: Optional[int] = int(line[1:line_index])
            except Exception:
                line_index = -1
                line_no = None

            # Verify checksum
            logging.info("line: " + line)
            cs_index = line.rfind('*')
            logging.info(f"cs_index: {cs_index}")
            try:
                checksum = int(line[cs_index+1:])
                logging.info(f"checksum: {checksum}")
            except Exception:
                # Invalid checksum, do not process
                msg = "!! Invalid Checksum"
                logging.info(f"line_no: {line_no}")
                if line_no is not None:
                    msg += f" Line Number: {line_no}"
                logging.exception("TFT: " + msg)
                raise TFTError(msg)

            # Checksum is calculated by XORing every byte in the line other
            # than the checksum itself
            calculated_cs = 0
            for c in line[:cs_index]:
                calculated_cs ^= ord(c)
            if calculated_cs & 0xFF != checksum:
                msg = "!! Invalid Checksum"
                if line_no is not None:
                    msg += f" Line Number: {line_no}"
                logging.info("TFT: " + msg)
                raise TFTError(msg)

            script = line[line_index+1:cs_index]
        else:
            script = line
        # Execute the gcode.  Check for special RRF gcodes that
        # require special handling
        parts = script.split()
        cmd = parts[0].strip()
        if cmd in ["M23", "M30", "M32", "M36", "M37"]:
            arg = script[len(cmd):].strip()
            parts = [cmd, arg]

        # Check for commands that query state and require immediate response
        if cmd in self.direct_gcodes:
            params: Dict[str, Any] = {}
            for p in parts[1:]:
                if p[0] not in "PSR":
                    params["arg_p"] = p.strip(" \"\t\n")
                    continue
                arg = p[0].lower()
                try:
                    val = int(p[1:].strip()) if arg in "sr" \
                        else p[1:].trip(" \"\t\n")
                except Exception:
                    msg = f"tft: Error parsing direct gcode {script}"
                    self.handle_gcode_response("!! " + msg)
                    logging.exception(msg)
                    return
                params[f"arg_{arg}"] = val
            func = self.direct_gcodes[cmd]
            self.queue_command(func, **params)
            return

        # Prepare GCodes that require special handling
        if cmd in self.special_gcodes:
            sgc_func = self.special_gcodes[cmd]
            script = sgc_func(parts[1:])

        if not script:
            return
        self.queue_gcode(script)

    async def _handle_probe_test(self) -> None:
        await self.websocket_handler.send_moonraker_request("printer.gcode.script", {"script": "QUERY_PROBE"})
        response = f"{Template(PROBE_TEST_TEMPLATE).render(**self.websocket_handler.latest_values)}\nok"
        self.send_to_tft(message=response)

    async def _handle_servo_command(self, position: int) -> None:
        if "bltouch" in self.websocket_handler.latest_values.get("configfile", {}).get("settings", {}):
            value = {
                10: "pin_down",
                90: "pin_up",
                160: "reset"
            }.get(position)
            command = f"BLTOUCH_DEBUG COMMAND={value}"
        else:
            value = {
                10: "1",
                90: "0",
                160: "0"
            }.get(position)
            command = f"SET_PIN PIN=_probe_enable VALUE={value}"
        response = await self.websocket_handler.send_moonraker_request("printer.gcode.script", {"script": command})
        self.send_to_tft(message=response)

    def _run_tft_M280(self, arg_s: int) -> None:
        position = arg_s
        if position == 120:  # Test
            self.event_loop.create_task(self._handle_probe_test())
        else:
            self.event_loop.create_task(self._handle_servo_command(position))

    async def _process_gcode_queue(self) -> None:
        while self.gc_queue:
            script = self.gc_queue.pop(0)
            try:
                if script in RESTART_GCODES:
                    await self.klippy_apis.do_restart(script)
                else:
                    await self.klippy_apis.run_gcode(script)
            except self.server.error:
                msg = f"Error executing script {script}"
                self.handle_gcode_response("!! " + msg)
                logging.exception(msg)
        self.gq_busy = False

    def queue_command(self, cmd: FlexCallback, *args, **kwargs) -> None:
        self.command_queue.append((cmd, args, kwargs))
        if not self.cq_busy:
            self.cq_busy = True
            self.event_loop.register_callback(self._process_command_queue)

    async def _process_command_queue(self) -> None:
        while self.command_queue:
            cmd, args, kwargs = self.command_queue.pop(0)
            try:
                ret = cmd(*args, **kwargs)
                if ret is not None:
                    await ret
            except Exception:
                logging.exception("Error processing command")
        self.cq_busy = False

    def _clean_filename(self, filename: str) -> str:
        # Remove quotes and whitespace
        filename.strip(" \"\t\n")
        # Remove drive number
        if filename.startswith("0:/"):
            filename = filename[3:]
        # Remove initial "gcodes" folder.  This is necessary
        # due to the HACK in the tft_M20 gcode.
        if filename.startswith("gcodes/"):
            filename = filename[6:]
        elif filename.startswith("/gcodes/"):
            filename = filename[7:]
        # Start with a "/" so the gcode parser can correctly
        # handle files that begin with digits or special chars
        if filename[0] != "/":
            filename = "/" + filename
        return filename

    def _prepare_M23(self, args: List[str]) -> str:
        filename = self._clean_filename(args[0])
        self.current_file = filename
        file_metadata = self.file_manager.get_file_metadata(filename)
        size = file_metadata.get('size', 0)
        response = Template(FILE_SELECT_TEMPLATE).render(filename=filename, size=size)
        self.write_response(f"{response}\nok")
        return f"M23 {self.current_file}"

    def _prepare_M24(self, args: List[str]) -> str:
        if not self.current_file:
            raise TFTError("No file selected to print")
        sd_state = self.printer_state.get("print_stats", {}).get("state", "standby")
        if sd_state == "paused":
            return "RESUME"
        elif sd_state in ("standby", "cancelled"):
            return f"SDCARD_PRINT_FILE FILENAME=\"{self.current_file}\""
        else:
            raise TFTError("Cannot start printing, printer is not in a stopped state")

    def _prepare_M25(self, args: List[str]) -> str:
        sd_state = self.printer_state.get("print_stats", {}).get("state", "standby")
        if sd_state == "printing":
            return "PAUSE"
        else:
            raise TFTError("Cannot pause, printer is not printing")

    def _prepare_M32(self, args: List[str]) -> str:
        filename = self._clean_filename(args[0])
        # Escape existing double quotes in the file name
        filename = filename.replace("\"", "\\\"")
        return f"SDCARD_PRINT_FILE FILENAME=\"{filename}\""

    def _prepare_M150(self, args: List[str]) -> str:
        params = {arg[0]: int(arg[1:]) for arg in args}
        red = params.get('R', 0) / 255
        green = params.get('U', 0) / 255
        blue = params.get('B', 0) / 255
        white = params.get('W', 0) / 255
        brightness = params.get('P', 255) / 255
        cmd = (
            f"SET_LED LED=statusled "
            f"RED={red * brightness:.3f} "
            f"GREEN={green * brightness:.3f} "
            f"BLUE={blue * brightness:.3f} "
            f"WHITE={white * brightness:.3f} "
            "TRANSMIT=1 SYNC=1"
        )
        self.write_response("ok")
        return cmd

    def _prepare_M290(self, args: List[str]) -> str:
        # args should in in the format Z0.02
        offset = args[0][1:].strip()
        return f"SET_GCODE_OFFSET Z_ADJUST={offset} MOVE=1"

    def _prepare_M701_M702(self, args: List[str]) -> str:
        length = 25
        params_dict = {
            'L': length if args[0] == "M701" else -1 * length,
            'T': args[1] if len(args) > 1 else 0,
            'Z': args[2] if len(args) > 2 else 0
        }
        return self._handle_filament(params_dict)

    async def _handle_filament(self, params_dict: Dict[str, Any]) -> str:
        length = params_dict.get("L")
        extruder = params_dict.get("T")
        zmove = params_dict.get("Z")
        command = [
            "G91",               # Relative Positioning
            f"G92 E{extruder}",  # Reset Extruder
            f"G1 Z{zmove} E{length} F{3*60}",  # Extrude or Retract
            "G92 E0"              # Reset Extruder
        ]
        return await self.websocket_handler.send_moonraker_request("printer.gcode.script", [{"script": cmd} for cmd in command])

    def handle_gcode_response(self, response: str) -> None:
        # Only queue up "non-trivial" gcode responses.  At the
        # moment we'll handle state changes and errors
        if "Klipper state" in response \
                or response.startswith('!!'):
            self.last_gcode_response = response
        else:
            for key in self.non_trivial_keys:
                if key in response:
                    self.last_gcode_response = response
                    return

    def write_response(self, message=None, command=None, action=None, error=None) -> None:
        if message:
            msg = f'{message}'
        elif command:
            msg = f'{command}'
        elif action:
            msg = f'//action:{action}'
        elif error:
            msg = f'Error:{error}'

        message = msg.replace('\n', '\\n')
        logging.info(f"response: {message}")
        byte_resp = (msg + "\n").encode("utf-8")
        self.ser_conn.send(byte_resp)

    async def _report_temperature(self, interval: int) -> None:
        while self.ser_conn.connected and interval > 0:
            report = Template(TEMPERATURE_TEMPLATE).render(**self.printer_state)
            self.write_response(f"ok {report}")
            await asyncio.sleep(interval)

    async def _report_position(self, interval: int) -> None:
        while self.ser_conn.connected and interval > 0:
            report = Template(POSITION_TEMPLATE).render(**self.printer_state)
            self.write_response(f"{report}")
            await asyncio.sleep(interval)

    async def _report_print_status(self, interval: int) -> None:
        while self.ser_conn.connected and interval > 0:
            report = Template(PRINT_STATUS_TEMPLATE).render(**self.printer_state)
            self.write_response(f"{report}")
            await asyncio.sleep(interval)

    def _run_tft_M21(self) -> str:
        self.write_response(message="SD card ok\nok")

    def _run_tft_M20(self, arg_p: Optional[str] = None) -> None:
        response_type = 2
        if response_type != 2:
            logging.info(
                f"Cannot process response type {response_type} in M20")
            return
        path = "/"

        # Strip quotes if they exist
        path = path.strip('\"')

        # Path should come in as "0:/macros, or 0:/<gcode_folder>".  With
        # repetier compatibility enabled, the default folder is root,
        # ie. "0:/"
        if path.startswith("0:/"):
            path = path[2:]
        response: Dict[str, Any] = {'dir': path}
        response['files'] = []

        if path == "/macros":
            response['files'] = [(macro, 0) for macro in self.available_macros.keys()]
        else:
            # HACK: The TFT has a bug where it does not correctly detect
            # subdirectories if we return the root as "/".  Moonraker can
            # support a "gcodes" directory, however we must choose between this
            # support or disabling RRF specific gcodes (this is done by
            # identifying as Repetier).
            # The workaround below converts both "/" and "/gcodes" paths to
            # "gcodes".
            if path == "/":
                response['dir'] = "/gcodes"
                path = "gcodes"
            elif path.startswith("/gcodes"):
                path = path[1:]

            flist = self.file_manager.list_dir(path, simple_format=False)
            if flist:
                response['files'] = [(file['filename'], file['size']) for file in flist.get("files")]

        marlin_response = Template(FILE_LIST_TEMPLATE).render(files=response['files'])
        self.write_response(marlin_response)

    async def _run_tft_M30(self, arg_p: str = "") -> None:
        # Delete a file.  Clean up the file name and make sure
        # it is relative to the "gcodes" root.
        path = arg_p
        path = path.strip('\"')
        if path.startswith("0:/"):
            path = path[3:]
        elif path[0] == "/":
            path = path[1:]

        if not path.startswith("gcodes/"):
            path = "gcodes/" + path
        await self.file_manager.delete_file(path)

    def _run_tft_M36(self, arg_p: Optional[str] = None) -> None:
        response: Dict[str, Any] = {}
        filename: Optional[str] = arg_p
        sd_status = self.printer_state.get('virtual_sdcard', {})
        print_stats = self.printer_state.get('print_stats', {})
        if filename is None:
            # TFT is requesting file information on a
            # currently printed file
            active = False
            if sd_status and print_stats:
                filename = print_stats['filename']
                active = sd_status['is_active']
            if not filename or not active:
                # Either no file printing or no virtual_sdcard
                response['err'] = 1
                self.write_response(response)
                return
            else:
                response['fileName'] = filename.split("/")[-1]

        # For consistency make sure that the filename begins with the
        # "gcodes/" root.  The M20 HACK should add this in some cases.
        # Ideally we would add support to the TFT firmware that
        # indicates Moonraker supports a "gcodes" directory.
        if filename[0] == "/":
            filename = filename[1:]
        if not filename.startswith("gcodes/"):
            filename = "gcodes/" + filename

        metadata: Dict[str, Any] = \
            self.file_manager.get_file_metadata(filename)
        if metadata:
            response['err'] = 0
            response['size'] = metadata['size']
            # workaround for TFT replacing the first "T" found
            response['lastModified'] = "T" + time.ctime(metadata['modified'])
            slicer: Optional[str] = metadata.get('slicer')
            if slicer is not None:
                response['generatedBy'] = slicer
            height: Optional[float] = metadata.get('object_height')
            if height is not None:
                response['height'] = round(height, 2)
            layer_height: Optional[float] = metadata.get('layer_height')
            if layer_height is not None:
                response['layerHeight'] = round(layer_height, 2)
            filament: Optional[float] = metadata.get('filament_total')
            if filament is not None:
                response['filament'] = [round(filament, 1)]
            est_time: Optional[float] = metadata.get('estimated_time')
            if est_time is not None:
                response['printTime'] = int(est_time + .5)
        else:
            response['err'] = 1
        self.write_response(response)

    def _run_tft_M33(self, arg_p: Optional[str] = None) -> None:
        filename: Optional[str] = arg_p
        if filename is None:
            self.write_response("!! Missing filename\nok")
            return

        # Clean up the filename
        filename = filename.strip('\"')
        if filename.startswith("0:/"):
            filename = filename[3:]
        elif filename[0] == "/":
            filename[1:]

        if not filename.startswith("gcodes/"):
            filename = "gcodes/" + filename

        self.write_response(f"{filename}\nok")

    def _run_tft_M105(self) -> str:
        report = Template(TEMPERATURE_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _run_tft_M114(self) -> str:
        report = Template(POSITION_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _run_tft_M115(self) -> str:
        report = Template(FIRMWARE_INFO_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _run_tft_M155(self, arg_s: int) -> None:
        try:
            interval = arg_s
            if interval > 0:
                if self.temperature_report_task:
                    self.temperature_report_task.cancel()
                self.temperature_report_task = self.event_loop.create_task(
                    self._report_temperature(interval)
                )
            else:
                if self.temperature_report_task:
                    self.temperature_report_task.cancel()
                    self.temperature_report_task = None
                self.write_response("ok")
        except (IndexError, ValueError):
            self.write_response("!! Invalid M155 command")

    def _run_tft_M154(self, arg_s: int) -> None:
        try:
            interval = arg_s
            if interval > 0:
                if self.position_report_task:
                    self.position_report_task.cancel()
                self.position_report_task = self.event_loop.create_task(
                    self._report_position(interval)
                )
            else:
                if self.position_report_task:
                    self.position_report_task.cancel()
                    self.position_report_task = None
                self.write_response("ok")
        except (IndexError, ValueError):
            self.write_response("!! Invalid M154 command")

    def _run_tft_M27(self, arg_s: int) -> None:
        try:
            interval = arg_s
            if interval > 0:
                if self.position_report_task:
                    self.position_report_task.cancel()
                self.position_report_task = self.event_loop.create_task(
                    self._report_print_status(interval)
                )
            else:
                if self.position_report_task:
                    self.position_report_task.cancel()
                    self.position_report_task = None
                self.write_response("ok")
        except (IndexError, ValueError):
            self.write_response("!! Invalid M27 command")

    def _run_tft_M211(self) -> str:
        filament_sensor_enabled = self.printer_state.get("filament_switch_sensor filament_sensor", {}).get("enabled", False)
        state = {
            "state": "On" if filament_sensor_enabled else "Off"
        }
        report = f"{Template(SOFTWARE_ENDSTOPS_TEMPLATE).render(**state)}"
        self.write_response(f"{report}\nok")

    def _run_tft_M220(self) -> str:
        report = Template(FEED_RATE_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _run_tft_M221(self) -> str:
        report = Template(FLOW_RATE_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _run_tft_M503(self, arg_s: Optional[str] = None) -> str:
        report = Template(REPORT_SETTINGS_TEMPLATE).render(
            **(
                self.printer_state |
                {"printer": self.printer} |
                {"bltouch": self.bltouch} |
                {"bed_mesh": self.bed_mesh}
            )
        )
        self.write_response(f"{report}\nok")

    def _run_tft_ok(self, arg_p: Optional[str] = None, arg_s: Optional[str] = None) -> str:
        self.write_response("ok")

    def _run_tft_M524(self) -> str:
        self.queue_gcode("CANCEL_PRINT")
        self.write_response(message="ok")

    def _run_tft_M108(self) -> str:
        self.write_response(message="ok")

    def _run_tft_M118(self, arg_p: Optional[str] = None) -> str:
        if arg_p and "P0 A1 action:cancel" in arg_p:
            self.write_response(action="cancel")
        elif arg_p and "P0 A1 action:notification remote pause" in arg_p:
            self.write_response(action="notification remote pause")
        elif arg_p and "P0 A1 action:notification remote resume" in arg_p:
            self.write_response(action="notification remote resume")
        else:
            self.write_response(message=f"echo:{arg_p}\nok" if arg_p else "ok")

    def _run_tft_G29(self, *args: str) -> str:
        self.queue_gcode("BED_MESH_CLEAR")
        cmd = "BED_MESH_CALIBRATE"
        if args:
            cmd += " " + " ".join(args)
        self.queue_gcode(cmd)
        self.write_response(message="ok")

    def _run_tft_M201(self, **params_dict: Any) -> None:
        if any(key in params_dict for key in ("X", "Y")):
            acceleration = int(params_dict.get('X', params_dict.get('Y', 0)))
            command = f"SET_VELOCITY_LIMIT ACCEL={acceleration} ACCEL_TO_DECEL={acceleration / 2}"
            self.queue_gcode(command)
            self.write_response("ok")
        else:
            self.write_response("!! Invalid M201 command")

    def _run_tft_M203(self, **params_dict: Any) -> None:
        if any(key in params_dict for key in ("X", "Y")):
            velocity = int(params_dict.get('X', params_dict.get('Y', 0)))
            command = f"SET_VELOCITY_LIMIT VELOCITY={velocity}"
            self.queue_gcode(command)
            self.write_response("ok")
        else:
            self.write_response("!! Invalid M203 command")

    def _run_tft_M206(self, **params_dict: Any) -> None:
        if any(key in params_dict for key in ("X", "Y", "Z", "E")):
            offsets = " ".join(f"{axis}={value}" for axis, value in params_dict.items() if axis in ("X", "Y", "Z", "E"))
            command = f"SET_GCODE_OFFSET {offsets}"
            self.queue_gcode(command)
            self.write_response("ok")
        else:
            self.write_response("!! Invalid M206 command")

    def close(self) -> None:
        self.ser_conn.disconnect()
        if self.temperature_report_task:
            self.temperature_report_task.cancel()
        if self.position_report_task:
            self.position_report_task.cancel()
        msg = "\nTFT GCode Dump:"
        for i, gc in enumerate(self.debug_queue):
            msg += f"\nSequence {i}: {gc}"
        logging.debug(msg)

def load_component(config: ConfigHelper) -> TFT:
    return TFTAdapter(config)
