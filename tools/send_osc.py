import asyncio, sys
from pythonosc.asyncio import AsyncIOOSCUDPClient

async def main():
    addr  = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port  = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
    path  = sys.argv[3] if len(sys.argv) > 3 else "/ccrs/flag/green"
    value = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0

    client = AsyncIOOSCUDPClient(addr, port)
    await client.send_message(path, value)
    print(f"sent {path} {value} -> {addr}:{port}")

if __name__ == "__main__":
    asyncio.run(main())
