import os
import re
import json
import numpy as np
import pandas as pd
import pyxdf
from datetime import datetime

# INFORMAZIONI DA INSERIRE
xdf_path = "./exp001/block_Prova_1.xdf"
out_dir = "csv_export"


def json_fallback(o):
    """Rende serializzabili oggetti comuni non gestiti da json."""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    return str(o)


def _safe_filename(s: str, max_len: int = 120) -> str:
    s = s or "file"
    s = re.sub(r"[^\w\-\.]+", "_", s.strip())
    root, ext = os.path.splitext(s)
    max_root = max_len - len(ext)
    if max_root < 1:
        return (root + ext)[:max_len]
    return root[:max_root] + ext


def _get_info_str(info, key, default=""):
    v = info.get(key, default)
    if isinstance(v, list) and len(v) > 0:
        v = v[0]
    return v if isinstance(v, str) else default


def _extract_channel_labels(stream):
    """
    Prova a leggere le etichette canali dai metadati XDF/LSL.
    Se non ci sono, ritorna None.
    """
    info = stream.get("info", {})
    desc = info.get("desc")
    if not desc:
        return None

    try:
        channels = desc[0].get("channels", [None])[0]
        if not channels:
            return None
        ch_list = channels.get("channel")
        if not ch_list:
            return None

        labels = []
        for ch in ch_list:
            lab = ch.get("label", [""])
            if isinstance(lab, list) and lab:
                labels.append(lab[0])
            elif isinstance(lab, str):
                labels.append(lab)
            else:
                labels.append("")
        if all(l == "" for l in labels):
            return None
        return labels
    except Exception:
        return None


# --- controlli base ---
if not os.path.isfile(xdf_path):
    raise FileNotFoundError(f"File XDF non trovato: {os.path.abspath(xdf_path)}")

os.makedirs(out_dir, exist_ok=True)

streams, header = pyxdf.load_xdf(xdf_path)

# salviamo header e metadati generali (con fallback!)
with open(os.path.join(out_dir, "xdf_header.json"), "w", encoding="utf-8") as f:
    json.dump(header, f, ensure_ascii=False, indent=2, default=json_fallback)

run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

for i, st in enumerate(streams, start=1):
    info = st.get("info", {})
    name = _get_info_str(info, "name", f"stream_{i}")
    stype = _get_info_str(info, "type", "")
    srate = _get_info_str(info, "nominal_srate", "")

    ts = np.asarray(st.get("time_stamps", []), dtype=float)

    x = st.get("time_series", [])
    x = np.asarray(x, dtype=object)

    # Normalizza in matrice (N x C)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    elif x.ndim == 0:
        x = x.reshape(0, 1)

    # Se numerico, prova a castare a float (se fallisce resta object -> utile per marker testuali)
    try:
        x = x.astype(float)
    except Exception:
        pass

    # Allinea lunghezze time_stamps e time_series (robustezza)
    n_x = x.shape[0]
    n_ts = ts.shape[0]
    if n_x != n_ts:
        n = min(n_x, n_ts)
        print(f"[WARN] Stream {i}: mismatch campioni (X={n_x}, TS={n_ts}). Taglio a {n}.")
        x = x[:n, :]
        ts = ts[:n]

    labels = _extract_channel_labels(st)
    n_ch = x.shape[1] if x.ndim == 2 else 1
    if (not labels) or (len(labels) != n_ch):
        labels = [f"ch{c+1}" for c in range(n_ch)]

    df = pd.DataFrame(x, columns=labels)
    df.insert(0, "time_stamps", ts)

    csv_name = _safe_filename(f"{i:02d}_{name}_{stype}_{run_stamp}.csv")
    out_path = os.path.join(out_dir, csv_name)
    df.to_csv(out_path, index=False)

    # Meta PER STREAM (dentro al loop!)
    meta_name = _safe_filename(f"{i:02d}_{name}_{stype}_{run_stamp}_meta.json")
    meta_path = os.path.join(out_dir, meta_name)

    meta = {
        "stream_index": i,
        "name": name,
        "type": stype,
        "nominal_srate": srate,
        "n_samples": int(df.shape[0]),
        "n_channels": int(n_ch),
        "csv_file": csv_name,
        "info": info,
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=json_fallback)

print(f"Fatto. Export in: {os.path.abspath(out_dir)}")
