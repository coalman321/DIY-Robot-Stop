class Button:
    def __init__(self, id: int):
        self.id = id
        self.pressed = False

class StopState:
    def __init__(self):
        self.stopped = False
        self.stopped_ids = []
