import serial
import time
import atexit


ser = serial.Serial('/dev/ttyAMA0', 57600)  # open serial port

print(ser.name)


txt = "ok T:{TCur:.4f} /{TDes:.4f} B:{BCur:.4f} /{TDes:.4f} P:0 /0.0000 @:0 B@:0\n"

# print(txt.format(TCur = 20, TDes=190, BCur=20, BDes = 60))

i = 0
while i < 10:
    
    print(ser.readline())
    
    ser.write(bytes(txt.format(txt.format(TCur = 20, TDes=190, BCur=20, BDes = 60))))
    
    i += 1
    time.sleep(1)




def exit_handler():
    if ser.is_open:
        ser.close()
    print("Serial closed")

atexit.register(exit_handler)