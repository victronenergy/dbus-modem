#! /usr/bin/python -u

import os
import sys
import threading
import serial
import gobject
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService

class Modem(threading.Thread):
    def __init__(self, dbussvc, dev, rate):
        threading.Thread.__init__(self)
        self.dbus = dbussvc
        self.lock = threading.Lock()
        self.ser = serial.Serial(dev, rate)
        self.cmds = []
        self.lastcmd = None
        self.ready = True
        self.roaming = False

    def send(self, cmd):
        global mainloop

        self.lastcmd = cmd
        self.ready = False

        try:
            self.ser.write('\r' + cmd + '\r')
        except serial.SerialException:
            print('Write error, quitting')
            mainloop.quit()

    def cmd(self, cmds):
        self.lock.acquire()
        if self.ready and not self.cmds:
            self.send(cmds.pop(0))
        self.cmds += cmds
        self.lock.release()

    def modem_init(self):
        self.cmd([
            'ATE0',
            'AT+CGMM',
            'AT+CGSN',
            'AT+CGPS=1',
        ])

    def modem_update(self):
        self.cmd([
            'AT+CREG?',
            'AT+COPS?',
            'AT*CNTI?',
            'AT+CSQ',
            'AT+CGACT?',
            'AT+CGPADDR',
        ])

    def handle_resp(self, cmd, resp):
        if cmd == '+CGMM':
            self.dbus['/Model'] = resp
            return

        if cmd == '+CGSN':
            self.dbus['/IMEI'] = resp
            return

        v = resp.split(',')

        if cmd == '*CNTI':
            self.dbus['/NetworkType'] = v[1]
            return

        if cmd == '+CREG':
            self.roaming = int(v[1]) == 5
            self.dbus['/Roaming'] = self.roaming
            return

        if cmd == '+COPS':
            net = v[2].strip('"')
            self.dbus['/NetworkName'] = net
            return

        if cmd == '+CSQ':
            self.dbus['/SignalStrength'] = int(v[0])
            return

        if cmd == '+CGACT':
            self.dbus['/Connected'] = int(v[1])
            return

        if cmd == '+CGPADDR':
            ip = v[1]
            if ip == '0.0.0.0':
                ip = None
            self.dbus['/IP'] = ip
            return

    def run(self):
        global mainloop

        self.modem_init()

        while True:
            self.lock.acquire()
            if self.ready and self.cmds:
                self.send(self.cmds.pop(0))
            self.lock.release()

            try:
                line = self.ser.readline().strip()
            except serial.SerialException:
                print('Read error, quitting')
                mainloop.quit()
                break

            if not line:
                continue

            if line in ['OK', 'ERROR', 'NO CARRIER']:
                self.ready = True
                continue

            p = line.split(': ', 1)

            if len(p) == 1:
                cmd = self.lastcmd.lstrip('AT')
                resp = p[0]
            else:
                cmd = p[0]
                resp = p[1]

            try:
                self.handle_resp(cmd, resp)
            except:
                pass

    def update_status(self):
        self.modem_update()
        return True

class ModemControl(dbus.service.Object):
    def __init__(self, modem, dbussvc, path):
        dbus.service.Object.__init__(self, dbussvc.dbusconn, path)
        self.modem = modem
        self.dbus = dbussvc

    @dbus.service.method('com.victronenergy.ModemControl', out_signature='b')
    def Connect(self):
        if self.modem.roaming and not self.dbus['/RoamingPermitted']:
            return False

        err = os.system('pon')

        return err == 0

    @dbus.service.method('com.victronenergy.ModemControl')
    def Disconnect(self):
        os.system('poff')

def main(argv):
    global mainloop

    if len(argv) != 1:
        exit(1)

    tty = argv[0]
    rate = 115200

    print('Starting dbus-modem on %s at %d bps' % (tty, rate))

    gobject.threads_init()
    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    svc = VeDbusService('com.victronenergy.modem')

    # status
    svc.add_path('/Model', None)
    svc.add_path('/IMEI', None)
    svc.add_path('/NetworkName', None)
    svc.add_path('/NetworkType', None)
    svc.add_path('/SignalStrength', None)
    svc.add_path('/Roaming', None)
    svc.add_path('/Connected', None)
    svc.add_path('/IP', None)

    # settings
    svc.add_path('/RoamingPermitted', False, writeable=True)

    modem = Modem(svc, tty, rate)
    modem.daemon = True
    modem.start()

    gobject.timeout_add(5000, modem.update_status)

    ModemControl(modem, svc, '/Control')

    mainloop = gobject.MainLoop()
    mainloop.run()

main(sys.argv[1:])
