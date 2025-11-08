#!/usr/bin/env python3
"""Quick OSC send test to verify network connectivity to QLC+"""

from pythonosc.udp_client import SimpleUDPClient

# Send a test message to QLC+
client = SimpleUDPClient("10.0.0.25", 9000)

print("Sending test OSC message to 10.0.0.25:9000...")
print("OSC path: /ccrs/flag/green")
print("Value: 1.0 (ON)")

client.send_message("/ccrs/flag/green", 1.0)

print("Message sent! Check QLC+ to see if it received it.")
print("If QLC+ has a monitor/log window, you should see the message there.")
