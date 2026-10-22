import ipaddress
import platform
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_PORT = 3132
CTRL_PORT = 3133

PING_TIMEOUT_MS = 120
MAX_WORKERS = 256

def get_local_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def ip_sort_key(ip: str):
    return tuple(int(p) for p in ip.split("."))

def ping_host(ip: str) -> None:
    # Windows ping: -n 1 (1 echo), -w timeout(ms)
    try:
        subprocess.run(
            ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

def arp_table_windows():
    """
    Ritorna lista di tuple (ip, mac) lette da 'arp -a'
    """
    out = subprocess.check_output(["arp", "-a"], text=True, errors="replace")
    rows = []
    # pattern: "  172.21.131.50          aa-bb-cc-dd-ee-ff     dynamic"
    for line in out.splitlines():
        m = re.search(r"\b((?:\d{1,3}\.){3}\d{1,3})\s+([0-9a-fA-F\-]{17})\s+\w+", line)
        if m:
            ip = m.group(1)
            mac = m.group(2).lower()
            rows.append((ip, mac))
    return rows

def tcp_port_open(ip: str, port: int, timeout: float = 0.15) -> bool:
    """
    Test TCP "best effort": se CTRL_PORT fosse TCP lo vedresti.
    Se è UDP, tipicamente risulterà chiuso e non è un problema.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except OSError:
        return False

def main():
    if platform.system().lower() != "windows":
        print("[ERRORE] Questo script usa 'arp -a' formato Windows. Dimmi OS e lo adatto.")
        return

    local_ip = get_local_ipv4()
    network = ipaddress.ip_network(f"{local_ip}/24", strict=False)

    print(f"[INFO] IP locale: {local_ip}")
    print(f"[INFO] Subnet usata: {network}")
    print("[INFO] Suggerimento: tieni Oscilloscope aperto e con EmotiBit selezionato (così ARP si popola).")

    # 1) Ping sweep per popolare ARP cache
    ips = [str(ip) for ip in network.hosts() if str(ip) != local_ip]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        list(ex.map(ping_host, ips))

    # 2) Leggi ARP
    arp_rows = arp_table_windows()
    arp_rows = [(ip, mac) for (ip, mac) in arp_rows if ipaddress.ip_address(ip) in network]

    if not arp_rows:
        print("[RISULTATO] ARP table vuota per questa subnet.")
        print("Azioni:")
        print("- Assicurati che Oscilloscope stia ricevendo dati dall’EmotiBit.")
        print("- Riprova dopo qualche secondo.")
        return

    # 3) Opzionale: test TCP 3133 (solo per dare un indizio aggiuntivo)
    candidates = []
    for ip, mac in arp_rows:
        open_3133 = tcp_port_open(ip, CTRL_PORT)
        candidates.append((ip, mac, open_3133))

    candidates.sort(key=lambda x: ip_sort_key(x[0]))

    print("[RISULTATO] IP trovati nella subnet (da ARP):")
    for ip, mac, open_3133 in candidates:
        hint = " (TCP 3133 OPEN)" if open_3133 else ""
        print(f" - {ip:15s}  MAC={mac}{hint}")

    print("\n[COME USARLO]")
    print("1) Tra questi IP, quello dell’EmotiBit è tipicamente quello comparso di recente mentre Oscilloscope riceve.")
    print("2) Se hai il MAC dell’EmotiBit (da Serial Monitor 'i'), matchalo con la colonna MAC e hai certezza.")
    print("3) In alternativa: apri Wireshark e filtra 'udp.port==3132 || udp.port==3133' per vedere il Source IP.")

if __name__ == "__main__":
    main()
