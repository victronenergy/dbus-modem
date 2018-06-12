# dbus-modem

This python application manages a cellular modem. An externally powered modem based on the Simcom
SIM5360E module is used.

The Simcom is connected to the Venus device via USB. The module presents as multiple tty devices
in Linux: two functionally identical AT command / data interfaces and one for GPS NMEA messages.

In Venus, one AT interface is managed by pppd to provide the data connection. The other is used by
this dbus-modem application.

A fix in the kernel is required, see the commits titled `USB: serial: option: blacklist sendsetup on
SIM5218` in the various machine branches on https://github.com/victronenergy/linux. And see also 
[venus-private/wiki/kernel-config](https://github.com/victronenergy/venus-private/wiki/kernel-config).

## Hardware watchdog
To cope with lockups or other issues, there is a watchdog IC watching over the Simcom. Its configured
such that the startup delay is 60s. And normal delay is 10s. Its output is connected to the reset
pin on the Simcom.

The watchdog is connected to the modem gpio with two pins:
1. WD_SET0 to GPIO41
2. WD_WDI to GPIO44

For both pins we store a default config to the modem eeprom, see below.

To wait, or not to wait, for the first edge?
If anything goes wrong in between the modem powering up, and the script sending the first edge, the
mechanism will be stuck. Hence we decided to have the hardware watchdog active from the start. Not
waiting for the first edge. GPIO41 must be set high for that.

## D-Bus
The following read-only values are exported under the com.victronenergy.modem service:

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
/RegStatus | status code, see below

### SimStatus
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

### RegStatus
The RegStatus value is the status code returned by the +CREG command
defined by 3GPP TS 27.007 section 7.2.

RegStatus | Description
----------|------------
0 | not registered, not searching for operator
1 | registered, home network
2 | not registered, searching for operator
3 | registration denied
4 | unknown
5 | registered, roaming

### Settings
The following localsettings values are used. These are monitored and changes acted upon.

Setting | Description
--------|------------
/Settings/Modem/Connect | establish data connection (0/1)
/Settings/Modem/RoamingPermitted | connect when roaming (0/1)
/Settings/Modem/PIN | SIM PIN (string)
/Settings/Modem/APN | Access point name (string)

## Routing
When the data connection is active, it is configured with a high routing metric. This way, the Linux
kernel prioritises Ethernet or Wifi when these are available. A dnsmasq proxy forwards DNS lookups
to the correct name server on Ethernet/Wifi or mobile data. See various recipes in
[meta-victronenergy](https://github.com/victronenerygy/meta-victronenergy)
as well as serial-starter in meta-victronenergy-private, for details.

## Preparation in the factory
The Simcom modules on our GX GSM are preprogrammed after assembly.

```
# disable some special function (FUNC_WAKEUP_HOST) on pin 41. This is stored in
# eeprom. Note to *never* use AT+CRESET, as it will re-enable this special function.
at+cgfunc=13,0

# configure pin 41 as output and default value 1 (high) - stored to eeprom
at+cgdrt=41,1,1
at+cgsetv=41,1,1

# configure pin 44 as output and default value to 0 (low) - stored to eeprom
at+cgdrt=44,1,1
at+cgsetv=44,0,1
```

See the [modem chapter in the commandline manual](https://github.com/victronenergy/venus/wiki/commandline-introduction#modem) for how to run those commands from within the Venus shell.

To check the configuration:
```
at+cgfunc=13  (should respond with 0 = disabled)

at+cggetv=41 (should respond with 1 = high)

at+cggetv=44 (should respond with 0 = low; unless script has been running already)
```

Note that I did not find a way to check the direction (input/output) of a pin.
