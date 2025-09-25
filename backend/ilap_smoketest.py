import sys, time, serial

# Usage:
#   Windows: python ilap_smoketest.py COM7
#   Linux/RPi: python ilap_smoketest.py /dev/ttyUSB0

INIT_7DIGIT = bytes([1, 37, 13, 10])  # chr(001), '%', CR, LF

def main():
    if len(sys.argv) < 2:
        print('Usage: python ilap_smoketest.py <PORT>')
        sys.exit(1)

    port = sys.argv[1]
    print(f'Opening {port} @ 9600 8N1 (RTS OFF)…')
    s = serial.Serial(port, 9600, bytesize=8, parity='N', stopbits=1, timeout=2)
    try:
        s.rts = False  # Keep RTS off per I-Lap guidance
        time.sleep(0.2)

        print('Sending init for 7-digit mode… (also resets decoder clock)')
        s.write(INIT_7DIGIT)
        s.flush()

        print('Waiting for data (Ctrl+C to quit)…')
        while True:
            raw = s.readline()
            if not raw:
                continue

            txt = raw.decode(errors='replace').strip()
            print(f'RAW: {raw!r}  TXT: {txt}')

            # Typical pass format: \x01@\t<dec_id>\t<transponder>\t<secs.mss>\r\n
            if raw.startswith(b'\x01@') and b'\t' in raw:
                parts = txt.split('\t')
                if len(parts) >= 4:
                    decoder_id = parts[1]
                    tag = parts[2]
                    t = parts[3]
                    print(f'PASS → decoder={decoder_id} tag={tag} t={t}s')
    except KeyboardInterrupt:
        print('\nStopping.')
    finally:
        s.close()

if __name__ == '__main__':
    main()
