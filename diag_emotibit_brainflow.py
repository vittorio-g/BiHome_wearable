"""Find EmotiBit serial numbers using BrainFlow.

Strategy: call BrainFlow prepare_session() multiple times.  Each time it
scans and connects to one EmotiBit.  We collect all unique device IDs
seen in the log across runs, releasing each session between runs.
"""
import re
import time

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds, LogLevels

BoardShim.set_log_level(LogLevels.LEVEL_TRACE.value)
BoardShim.enable_dev_board_logger()
LOG_PATH = "brainflow_discovery.log"
BoardShim.set_log_file(LOG_PATH)

# Regex to extract device IDs from discovery packets
PKT_RE = re.compile(r"package is\s+[^,]+,[^,]+,[^,]+,HH,[^,]+,[^,]+,[^,]+,[^,]+,DI,(\S+)")
CONN_RE = re.compile(r"adv_connection established, ip:\s*([\d.]+)")
FOUND_RE = re.compile(r"Found emotibit:\s*(\S+)")

def scan_once() -> tuple:
    """Try to connect to any EmotiBit. Returns (dev_id, ip, all_devs_seen)."""
    # Capture the current position of the log so we read only new data
    try:
        start_pos = 0
        if __import__('os').path.isfile(LOG_PATH):
            start_pos = __import__('os').path.getsize(LOG_PATH)
    except Exception:
        start_pos = 0

    params = BrainFlowInputParams()
    params.timeout = 15
    try:
        board = BoardShim(BoardIds.EMOTIBIT_BOARD.value, params)
        board.prepare_session()
    except Exception:
        pass
    time.sleep(0.5)
    try:
        with open(LOG_PATH, 'r') as f:
            f.seek(start_pos)
            txt = f.read()
    except Exception:
        txt = ""
    all_devs = set(PKT_RE.findall(txt))
    m_conn = CONN_RE.search(txt)
    ip = m_conn.group(1) if m_conn else None
    m_found = FOUND_RE.search(txt)
    dev_id = m_found.group(1) if m_found else None
    try:
        board.release_session()
    except Exception:
        pass
    return dev_id, ip, all_devs

def main():
    print("=== EmotiBit scanner ===\n")
    found = {}  # dev_id → ip

    # Run 1: initial scan
    print("Scan 1...")
    dev, ip, all_devs = scan_once()
    if dev:
        print(f"  Primary connection: {dev} @ {ip}")
        found[dev] = ip
    if all_devs:
        print(f"  All DI seen in log: {all_devs}")
    for d in all_devs:
        found.setdefault(d, "(log only)")

    time.sleep(2)
    print("\nScan 2...")
    dev, ip, all_devs = scan_once()
    if dev:
        print(f"  Primary connection: {dev} @ {ip}")
        found[dev] = ip
    if all_devs:
        print(f"  All DI seen in log: {all_devs}")
    for d in all_devs:
        found.setdefault(d, "(log only)")

    time.sleep(2)
    print("\nScan 3...")
    dev, ip, all_devs = scan_once()
    if dev:
        print(f"  Primary connection: {dev} @ {ip}")
        found[dev] = ip
    if all_devs:
        print(f"  All DI seen in log: {all_devs}")
    for d in all_devs:
        found.setdefault(d, "(log only)")

    print("\n=== Results ===")
    print(f"Found {len(found)} unique EmotiBit(s):\n")
    print("Add these to KNOWN_EMOTIBIT in BiHome_wearable.py:\n")
    for i, (dev_id, ip) in enumerate(sorted(found.items()), 1):
        print(f'    "EmotiBit {i} ({dev_id})": "{dev_id}",  # IP: {ip}')

if __name__ == "__main__":
    main()
