from estop_common import StopState, Button
from hal import EStopHal
import utils


class InputService:
    """Responsible for collecting inputs from buttons and then exporting a StopState object to the caller."""
    def __init__(self):
        self._stop_state = StopState()
    
    def update(self, inputs: list[Button]):
        """Updates the stop state given the button inputs. Could include debouncing,
        or rate limiting in the future."""
        # TODO: Rate-limit / debouncing?
        # TODO: Check for timeout? (Update being called to infrequently)

        # Non-latching: stopped tracks the live buttons and clears once every
        # button is released. stopped_ids lists whichever buttons are pressed
        # right now (rebuilt each cycle, so it never grows without bound).
        pressed_ids = [b.id for b in inputs if b.pressed]
        self._stop_state.stopped = len(pressed_ids) > 0
        self._stop_state.stopped_ids = pressed_ids

    def get_state(self) -> StopState:
        return self._stop_state

class DisplayService:
    """Updates the attached OLED display at a configurable interval with the current StopState."""
    def __init__(self, hal: EStopHal, display_interval_ms: float):
        self._hal = hal
        self._display_interval_ms = display_interval_ms
        self._last_update_time_ms = 0.0

    def update(self, stop_state: StopState):
        # It's the responsibility of the caller to rate-limit display updates
        now = utils.get_time_ms()
        if now - self._last_update_time_ms >= self._display_interval_ms:
            self._hal.write_display(stop_state)
            self._last_update_time_ms = now

class OutputService:
    """Responsible for performing an action with the StopState. If more logic was
    added to this system, you would edit this class to do more hardware 'things'
    as a response to the StopState."""
    def __init__(self, hal: EStopHal):
        self._hal = hal

    def update(self, stop_state: StopState):
        # It's the responsibility of the caller to rate-limit output updates
        self._hal.write_relay(stop_state)

class EStop:
    """The class you should consume. Combines all services together and provides
    a simple, extensible interface for calling code."""
    def __init__(self, hal: EStopHal, display_interval_ms: float):
        self._input_service = InputService()
        self._display_service = DisplayService(hal, display_interval_ms)
        self._output_service = OutputService(hal)

    def update(self, inputs: list[Button]):
        # Collect inputs
        self._input_service.update(inputs)
        stop_state = self._input_service.get_state()
        # Outputs
        self._display_service.update(stop_state)
        self._output_service.update(stop_state)

    def get_state(self) -> StopState:
        return self._input_service.get_state()