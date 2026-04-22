"""Find ALL EmotiBits on the local network.

Reproduces the BrainFlow discovery protocol:
  1. Send HELLO broadcast: "0,0,0,HE,1,100" to 255.255.255.255:3131
  2. Listen for responses on port 3131
  3. Each EmotiBit responds with "<seq>,0,0,HH,1,100,DP,-1,DI,<device_id>"
"""
import socket
import time

ADVERT_PORT = 3131
HELLO_PKT = b"0,0,0,HE,1,100"
LISTEN_SEC = 15

def main():
    # Find broadcast address on our interface
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    local_ip = s.getsockname()[0]
    s.close()
    # Assume /24 subnet (most common home networks)
    broadcast = '.'.join(local_ip.split('.')[:3]) + '.255'
    print(f"Local IP: {local_ip}  |  Broadcast: {broadcast}")

    # Open RX socket (must bind before sending to receive replies on same port)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        rx.bind(('', ADVERT_PORT))
    except OSError as e:
        print(f"Cannot bind UDP {ADVERT_PORT}: {e}")
        return
    rx.settimeout(0.5)

    # Send HELLO broadcasts periodically
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    print(f"Scanning for {LISTEN_SEC}s, sending HELLO broadcasts every 1s...\n")

    found = {}  # ip → device_id
    t0 = time.time()
    last_bcast = 0.0
    while time.time() - t0 < LISTEN_SEC:
        now = time.time()
        if now - last_bcast > 1.0:
            try:
                tx.sendto(HELLO_PKT, (broadcast, ADVERT_PORT))
                tx.sendto(HELLO_PKT, ('255.255.255.255', ADVERT_PORT))
            except Exception as e:
                print(f"send err: {e}")
            last_bcast = now
        try:
            data, (src_ip, _) = rx.recvfrom(4096)
        except socket.timeout:
            continue
        txt = data.decode('utf-8', errors='replace')
        # Parse: look for ",DI,<device_id>"
        device_id = None
        parts = [p.strip() for p in txt.split(',')]
        for i, p in enumerate(parts):
            if p == "DI" and i + 1 < len(parts):
                device_id = parts[i + 1]
                break
        if device_id and src_ip not in found:
            found[src_ip] = device_id
            elapsed = now - t0
            print(f"[{elapsed:4.1f}s] {src_ip:15s} → {device_id}")
            print(f"          raw: {txt[:150]}")

    tx.close()
    rx.close()
    print(f"\n=== Results ===")
    if not found:
        print("No EmotiBits found.")
    else:
        print(f"Found {len(found)} device(s):")
        print()
        print("  Add these to KNOWN_EMOTIBIT in BiHome_wearable.py:")
        print()
        for i, (ip, dev_id) in enumerate(sorted(found.items()), 1):
            # Generate Python-safe friendly name suggestion
            print(f'      "EmotiBit {i} ({dev_id})": "{dev_id}",  # IP: {ip}')

if __name__ == "__main__":
    main()
