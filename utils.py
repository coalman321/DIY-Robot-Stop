import time

def get_time_ms():
    """Get current time in milliseconds since device boot"""
    return time.ticks_ms()