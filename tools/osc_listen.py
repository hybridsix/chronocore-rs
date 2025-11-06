from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

def dump(addr, *args):
    print(f"{addr} {args}")

disp = Dispatcher()
disp.set_default_handler(dump)

# listen on the feedback port you set in QLC (step 1)
server = BlockingOSCUDPServer(("127.0.0.1", 9010), disp)
print("listening on 127.0.0.1:9010 ...")
server.serve_forever()
