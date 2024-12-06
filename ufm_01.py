import logging, argparse, signal
from queue import Queue
import time, serial, threading

# Set up logging
'''
    Global Configs
'''
logging.basicConfig(filemode='w', format='%(levelname)s - %(message)s', level=logging.INFO)

START_OF_FRAME = [0xFE, 0xFE]
START_BYTE = [0x11]
STOP_BYTE = [0x16]
CLEAR_ACC_FLOW = [0x5A, 0xFD]
ACTIVE_MODE = [0x5C, 0x00]
PASSIVE_MODE = [0x5C, 0x01]
READ_DATA_INC_ID = [0x5B, 0xCB]
READ_DATA_NO_ID = [0x5B, 0x0F]
RESET_DEVICE = [0x5D, 0xFD]

DEVICE_ACK = [0xE5]
DATA_OUTPUT_ACTIVE_MODE_PREFIX = [0x3C, 0x32]
DATA_OUTPUT_PASSIVE_MODE_INC_ID_PREFIX = [0x3C, 0x96]
DATA_OUTPUT_PASSIVE_MODE_NO_ID_PREFIX = [0x3C, 0x64]

q = Queue()
alive = True

def serial_read_callback(serial_handler):
    serial_handler.flushInput()

    buffer = b''

    while alive:
        byte_rcv = serial_handler.read() # this is blocking
        if byte_rcv:
            buffer += byte_rcv
        else:
            if len(buffer) > 0:
                hex_dump = ' '.join(f'{byte:02x}' for byte in buffer)
                q.put(buffer) # add to the queue
                logging.debug("Received: {}".format(hex_dump))

            buffer = b''

def signal_handler(signum, frame):
    signal.signal(signum, signal.SIG_IGN)
    global alive
    print("CTRL+C detected...")
    alive = False

def calculateChecksum(data: bytes):
    res = 0x00
    for byte in data:
        res += byte
    return res & 0xFF

def getChangePassiveModeCmd(mode):
    cmd = []
    cmd += START_OF_FRAME
    cmd += START_BYTE
    if not mode:
        cmd += ACTIVE_MODE
    else:
        cmd += PASSIVE_MODE
    cmd += [calculateChecksum(cmd[3:])]
    cmd += STOP_BYTE
    return cmd

def getClearAccumulatedFlowCmd():
    cmd = []
    cmd += START_OF_FRAME
    cmd += START_BYTE
    cmd += CLEAR_ACC_FLOW
    cmd += [calculateChecksum(cmd[3:])]
    cmd += STOP_BYTE
    return cmd

def getResetModuleCmd():
    cmd = []
    cmd += START_OF_FRAME
    cmd += START_BYTE
    cmd += RESET_DEVICE
    cmd += [calculateChecksum(cmd[3:])]
    cmd += STOP_BYTE
    return cmd

def getReadCmd(withSN):
    cmd = []
    cmd += START_OF_FRAME
    cmd += START_BYTE
    if withSN:
        cmd += READ_DATA_INC_ID
    else:
        cmd += READ_DATA_NO_ID
    cmd += [calculateChecksum(cmd[3:])]
    cmd += STOP_BYTE
    return cmd

def sleepMs(delayMs):
    buckets = delayMs * 0.01
    while alive and buckets > 0:
        buckets -= 1
        time.sleep(0.1)

def main():

    # Create Parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', action='store', dest='baudrate',
                        help='Baudrate in bps', type=int, default=2400)
    parser.add_argument('-D',action='store', dest='serial_port',
                        help='Serial Port', default='/dev/tty.usbserial-01C789B8')           
    parser.add_argument('--version', action='version', version='%(prog)s 1.0')

    results = parser.parse_args()  

    try:
        serial_handler = serial.Serial(port=results.serial_port, baudrate=results.baudrate, parity='E', stopbits=1, timeout=0.01) #timeout 0 for non-blocking. Set to None for blocking.
    except:
        logging.error("Impossible to start serial port! Check configuration!")
        exit()

    serial_thread = threading.Thread(target=serial_read_callback, args=(serial_handler,))
    serial_thread.daemon = True
    serial_thread.start()
    
    # stop active mode
    cmd = getChangePassiveModeCmd(True)
    serial_handler.write(bytes(cmd))

    time.sleep(1)

    # delete accumulated values
    cmd = getClearAccumulatedFlowCmd()
    serial_handler.write(bytes(cmd))

    withSN = False

    while alive:
        cmd = getReadCmd(withSN)
        serial_handler.write(bytes(cmd))

        # demo
        withSN = not withSN

        if not q.empty():
            reply = q.get()

            # active mode report?
            if reply[0:len(DATA_OUTPUT_ACTIVE_MODE_PREFIX)] == bytes(DATA_OUTPUT_ACTIVE_MODE_PREFIX):
                logging.info("Received active mode output!")
                deviceId = int.from_bytes(reply[2:7],'little')
                data = deviceId >> 16
                serialNumber = deviceId & 0xFFFF
                accFlowFlag = reply[8]
                accFlow = int.from_bytes(reply[9:15],'little')
                instFlowFlag = reply[15]
                instFlow = int.from_bytes(reply[16:20],'little')
                flowSign = reply[20]

                tempFlag = reply[24]
                waterTemp = int.from_bytes(reply[25:27],'little')

                st1 = reply[28]
                st2 = reply[29]
                validChecksum = reply[30] == calculateChecksum(reply[:-2])
                validStopByte = reply[31] == int.from_bytes(STOP_BYTE,'little')
                
                if validChecksum and validStopByte:
                    logging.info("Device manufacturing data: {:06X}, serial number: {:04X}".format(data, serialNumber))

                    if accFlowFlag == 0x0A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}l".format((accFlow >> 12),accFlow & 0xFFF))
                    elif accFlowFlag == 0x1A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}m3".format((accFlow >> 12),accFlow & 0xFFF))
                    
                    if instFlowFlag == 0x0B:
                        logging.info("Instant Flow: {}{:06X}.{:02X}l/h".format('-' if flowSign == 0x80 else '', (instFlow >> 8),instFlow & 0xFF))

                    if tempFlag == 0x0D:
                        logging.info("Water temperature: {:02X}.{:02X}C".format((waterTemp >> 8),waterTemp & 0xFF))

                    if st1 & 0x20:
                        logging.info("Empty tube")
                    
                    if st2 & 0x20:
                        logging.info("UFC chip error")

                    if st2 & 0x08:
                        logging.info("Flow direction wrong")
                    
                    if st2 & 0x04:
                        logging.info("Flow rate out of range")
                
                else:
                    logging.info("Invalid checksum!")

            elif reply[0:len(DEVICE_ACK)] == bytes(DEVICE_ACK):
                logging.info("ACK received.")

            elif reply[0:len(DATA_OUTPUT_PASSIVE_MODE_INC_ID_PREFIX)] == bytes(DATA_OUTPUT_PASSIVE_MODE_INC_ID_PREFIX):
                logging.info("Received passive mode output with serial number!")
                deviceId = int.from_bytes(reply[2:7],'little')
                data = deviceId >> 16
                serialNumber = deviceId & 0xFFFF
                accFlowFlag = reply[8]
                accFlow = int.from_bytes(reply[9:15],'little')
                instFlowFlag = reply[22]
                instFlow = int.from_bytes(reply[23:27],'little')
                flowSign = reply[27]

                tempFlag = reply[31]
                waterTemp = int.from_bytes(reply[32:34],'little')

                st1 = reply[35]
                st2 = reply[36]
                validChecksum = reply[37] == calculateChecksum(reply[:-2])
                validStopByte = reply[38] == int.from_bytes(STOP_BYTE,'little')
                
                if validChecksum and validStopByte:
                    logging.info("Device manufacturing data: {:06X}, serial number: {:04X}".format(data, serialNumber))

                    if accFlowFlag == 0x0A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}l".format((accFlow >> 12),accFlow & 0xFFF))
                    elif accFlowFlag == 0x1A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}m3".format((accFlow >> 12),accFlow & 0xFFF))
                    
                    if instFlowFlag == 0x0B:
                        logging.info("Instant Flow: {}{:06X}.{:02X}l/h".format('-' if flowSign == 0x80 else '', (instFlow >> 8),instFlow & 0xFF))

                    if tempFlag == 0x0D:
                        logging.info("Water temperature: {:02X}.{:02X}C".format((waterTemp >> 8),waterTemp & 0xFF))

                    if st1 & 0x20:
                        logging.info("Empty tube")
                    
                    if st2 & 0x20:
                        logging.info("UFC chip error")

                    if st2 & 0x08:
                        logging.info("Flow direction wrong")
                    
                    if st2 & 0x04:
                        logging.info("Flow rate out of range")
                
                else:
                    logging.info("Invalid checksum!")
            
            elif reply[0:len(DATA_OUTPUT_PASSIVE_MODE_NO_ID_PREFIX)] == bytes(DATA_OUTPUT_PASSIVE_MODE_NO_ID_PREFIX):
                logging.info("Received passive mode output without serial number!")
                accFlowFlag = reply[2]
                accFlow = int.from_bytes(reply[3:9],'little')
                instFlowFlag = reply[9]
                instFlow = int.from_bytes(reply[10:14],'little')
                flowSign = reply[14]

                tempFlag = reply[15]
                waterTemp = int.from_bytes(reply[16:18],'little')

                st1 = reply[19]
                st2 = reply[20]

                validChecksum = reply[21] == calculateChecksum(reply[:-2])
                validStopByte = reply[22] == int.from_bytes(STOP_BYTE,'little')
                
                if validChecksum and validStopByte:
                    if accFlowFlag == 0x0A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}l".format((accFlow >> 12),accFlow & 0xFFF))
                    elif accFlowFlag == 0x1A:
                        logging.info("Accumulated Flow: {:09X}.{:03X}m3".format((accFlow >> 12),accFlow & 0xFFF))
                    
                    if instFlowFlag == 0x0B:
                        logging.info("Instant Flow: {}{:06X}.{:02X}l/h".format('-' if flowSign == 0x80 else '', (instFlow >> 8),instFlow & 0xFF))

                    if tempFlag == 0x0D:
                        logging.info("Water temperature: {:02X}.{:02X}C".format((waterTemp >> 8),waterTemp & 0xFF))

                    if st1 & 0x20:
                        logging.info("Empty tube")
                    
                    if st2 & 0x20:
                        logging.info("UFC chip error")

                    if st2 & 0x08:
                        logging.info("Flow direction wrong")
                    
                    if st2 & 0x04:
                        logging.info("Flow rate out of range")
                
                else:
                    logging.info("Invalid checksum!")

        sleepMs(1000)

if __name__ == '__main__':
    # redirect Ctrl+C signal to signal_handler
    signal.signal(signal.SIGINT, signal_handler)

    # start main process
    main()