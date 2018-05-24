# dbus-modem

This python application manages a cellular modem. An externally powered modem based on the Simcom
SIM5360E module is used.

To cope with lockups or other issues, there is a watchdog IC that can restart the Simcom. The
watchdog reset pin is connected to a GPIO on the Simcom module, and periodically reset from this
script. See modem hardware schematics for details of Watchdog IC configuration.

The Simcom is connected to the Venus device via USB. The module presents as multiple tty devices
in Linux: two functionally identical AT command / data interfaces and one for GPS NMEA messages.

In Venus, one AT interface is managed by pppd to provide the data connection. The other is used by
this dbus-modem application to retrieve status information and publish on the system dbus. The
following read-only values are exported under the com.victronenergy.modem service:

Path | Description
-----|-------------
/Model | modem model
/IMEI | International Mobile Equipment Identity
/NetworkName | name of registered mobile network
/NetworkType | type of mobile network (GSM, UMTS, ...)
/SignalStrength | signal strength (0-31)
/Roaming | currently roaming (0/1)
/Connected | data link active (0/1)
/IP | IP address (when connected)
/SimStatus | status code, see below

The SimStatus value is either (if less than 1000) an error code as
defined by 3GPP TS 27.007 section 9.2 or (1000 and higher) a status
code per the table below.

SimStatus | Description
----------|------------
10 | SIM not inserted
11 | SIM PIN required
12 | SIM PUK required
13 | SIM failure
14 | SIM busy
15 | SIM wrong
16 | incorrect password
1000 | ready
1001 | unknown error

The following localsettings values are used. These are monitored and changes acted upon.

Setting | Description
--------|------------
/Settings/Modem/Connect | establish data connection (0/1)
/Settings/Modem/RoamingPermitted | connect when roaming (0/1)
/Settings/Modem/PIN | SIM PIN (string)

When the data connection is active, it is configured with a high routing metric. This way, the Linux
kernel prioritises Ethernet or Wifi when these are available. A dnsmasq proxy forwards DNS lookups
to the correct name server on Ethernet/Wifi or mobile data. See various recipes in
[meta-victronenergy](https://github.com/victronenerygy/meta-victronenergy)
as well as serial-starter in meta-victronenergy-private, for details.

A fix in the kernel is required, see the commits titled `USB: serial: option: blacklist sendsetup on
SIM5218` in the various machine branches on https://github.com/victronenergy/linux. And see also 
[venus-private/wiki/kernel-config](https://github.com/victronenergy/venus-private/wiki/kernel-config).

### Preparation in the factory
The Simcom modules on our GX GSM are preprogrammed in the factory.

```
# change direction and default value of io pin, and store too eeprom
at+cgdrt=44,1,1
at+cgsetv=44,0,1

# disable some special function (FUNC_WAKEUP_HOST) on pin 41
at+cgfunc=13,0

# change direction and default value of io pin, and store too eeprom
at+cgdrt=41,1,1
at+cgsetv=41,0,1
```

See the [modem chapter in the commandline manual](https://github.com/victronenergy/venus/wiki/commandline-introduction#modem) for how to run those commands from within the Venus shell.

