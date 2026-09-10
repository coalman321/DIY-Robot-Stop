# Recovery hatch: hold the recovery button while powering on to skip the app and
# land in the REPL. Must be first calls
import recovery
recovery.check()

from time import sleep_ms
import gc
import sys
import machine
from estop import EStop, Button
from http_server import EStopHTTPServer
from hal import EStopHal
from network_ap import AccessPoint
from machine import Pin, I2C

# ==============================================================================
# CONSTANTS
# ==============================================================================
# It is encouraged to configure the following values for your hardware system.
# The following assume a setup with a relay, and an SSD1306-based display 
# connecting to a PIMORONI Pico Plus 2's QT port.
#
# To change button counts, add/remove constants below, then change the construction 
# of the EStopHal.
# 
# Pico......https://shop.pimoroni.com/products/pimoroni-pico-plus-2?variant=42092668289107
# Display...https://www.adafruit.com/product/938
# ==============================================================================

# Relay is active-high
PIN_RELAY_OUT = 11

# Display I2C
# These SCL/SDA pins correspond to the PIMORONI Pico Plus 2's QT port.
# 400kHz is needed: a full 128x64 frame is 1025 bytes, and at 100kHz that
# transfer (~90ms) exceeds the RP2350 I2C default timeout of 50ms. The explicit
# timeout adds headroom. If you see corruption on a long cable, drop to 100kHz
# AND keep timeout well above 100000.
DISPLAY_I2C = I2C(0, scl=Pin(5), sda=Pin(4), freq=400000, timeout=200000)

# I2C address of the SSD1306. The Adafruit #938 128x64 panel ships at 0x3D;
# many 128x32 panels use 0x3C. Run DISPLAY_I2C.scan() to confirm.
DISPLAY_ADDR = 0x3D

# Wi-Fi access point the device hosts. Clients connect straight to this network
# and reach the web server at http://<AP IP> (shown on the display).
# AP_PASSWORD must be 8+ chars for WPA2; leave "" for an open network.
AP_SSID = "obs-stop"
AP_PASSWORD = "some_ap_pw_here"

# ==============================================================================
# SRC
# ==============================================================================

def main():
    # First, construct the HAL with a mapping of buttons to pins. You can add
    # or remove from this mapping to change the number of buttons in your system.
    button_map = {
        Button(0): Pin(6, Pin.IN, Pin.PULL_DOWN),
        Button(1): Pin(7, Pin.IN, Pin.PULL_DOWN),
        Button(2): Pin(8, Pin.IN, Pin.PULL_UP),
        Button(3): Pin(9, Pin.IN, Pin.PULL_DOWN),
    }
    # Bring up the access point first so the display and web server have a
    # network to report. ap.ip is the address clients browse to.
    ap = AccessPoint(AP_SSID, AP_PASSWORD)
    ap.start()

    hal = EStopHal(button_map, PIN_RELAY_OUT, DISPLAY_I2C, display_addr=DISPLAY_ADDR,
                   net_ssid=ap.ssid, net_ip=ap.ip)

    # Pass in the HAL and configure the display update rate
    estop = EStop(hal, 1000)

    # Show the SSID / address immediately rather than waiting for the first
    # display interval.
    hal.write_display(estop.get_state())

    # Calling this constructor will attempt to create the socket and bind to it.
    http_server = EStopHTTPServer()

    loop_count = 0
    while True:
        # Get button states
        buttons = hal.get_button_states()

        estop.update(buttons)
        # Non-blocking: accepts any pending connections, pushes the current
        # state to the SSE stream(s), and returns. Only a static-file transfer
        # (index.html / css / js, loaded once per session) still blocks the
        # loop for the length of that transfer -- move poll_data() onto core 1
        # with _thread and hand it a snapshot of estop.get_state() if that
        # latency ever matters.
        http_server.poll_data(estop.get_state())

        # Keep GC pauses small and predictable (~1 s cadence) instead of letting
        # a full collection fire mid-request and stall the loop.
        loop_count += 1
        if loop_count % 50 == 0:
            gc.collect()

        # Fast loop so button -> relay latency stays low between requests.
        sleep_ms(20)


def run_supervised():
    """Run main(), and if it raises, log the traceback and reboot so a transient
    fault (I2C glitch, Wi-Fi hiccup) self-heals instead of leaving the device
    dead. Ctrl-C / a stop from the IDE falls through to the REPL instead."""
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        sys.print_exception(exc)
        # Give a connected console a few seconds to catch the traceback.
        sleep_ms(5000)
        machine.reset()


if __name__ == "__main__":
    run_supervised()