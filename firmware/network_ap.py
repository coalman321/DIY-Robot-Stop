# ==============================================================================
# Wi-Fi Access Point helper.
#
# Brings up the Pico's on-board CYW43 radio as a self-contained access point so
# clients can connect directly to the e-stop's web server with no existing
# network. After start() returns, `ssid` and `ip` hold what should be shown on
# the display.
# ==============================================================================

import network
from time import sleep_ms


class AccessPoint:
    """Thin wrapper around network.WLAN(AP_IF).

    Usage:
        ap = AccessPoint("estop-net", "changeme123")
        ap.start()
        print(ap.ssid, ap.ip, ap.url)
    """

    def __init__(self, ssid: str, password: str = "", channel: int = 6):
        # WPA2 needs an 8+ char password; anything shorter is treated as "open".
        self.ssid = ssid
        self._password = password if len(password) >= 8 else ""
        self._channel = channel
        self._wlan = network.WLAN(network.AP_IF)
        self.ip = "0.0.0.0"

    @property
    def url(self) -> str:
        return "http://" + self.ip

    def start(self, timeout_ms: int = 5000):
        # The rp2 / CYW43 port takes essid + password and defaults to WPA2.
        # An open AP needs security=0, which not every build accepts, so try
        # it and fall back.
        if self._password:
            self._wlan.config(essid=self.ssid, password=self._password)
        else:
            try:
                self._wlan.config(essid=self.ssid, security=0)
            except (ValueError, OSError):
                self._wlan.config(essid=self.ssid)

        try:
            self._wlan.config(channel=self._channel)
        except (ValueError, OSError):
            pass

        self._wlan.active(True)

        # Wait for the interface to come up so ifconfig() reports a real address
        # (the AP default is 192.168.4.1).
        waited = 0
        while waited < timeout_ms:
            if self._wlan.active() and self._wlan.ifconfig()[0] != "0.0.0.0":
                break
            sleep_ms(100)
            waited += 100

        self.ip = self._wlan.ifconfig()[0]
        return self

    def is_active(self) -> bool:
        return bool(self._wlan.active())
