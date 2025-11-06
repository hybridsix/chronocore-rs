import sys
from pythonosc.udp_client import SimpleUDPClient

addr  = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port  = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
path  = sys.argv[3] if len(sys.argv) > 3 else "/ccrs/flag/green"
value = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0

client = SimpleUDPClient(addr, port)
client.send_message(path, value)
print(f"sent {path} {value} -> {addr}:{port}")