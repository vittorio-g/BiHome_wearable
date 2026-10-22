import pyxdf
import csv

streams, header = pyxdf.load_xdf("recording.xdf")

s = streams[0]  # Assuming we want to convert the first stream
data=s['time_series']
t = s["time_stamps"]

with open("from_xdf.csv", mode='w', newline='') as f:
    writer = csv.writer(f)
    
    # Write header
    n_channels = data.shape[1]
    header = ['Timestamp_lsl'] + [f'Channel_{i+1}' for i in range(n_channels)]
    writer.writerow(header)
    
    # Write data rows
    for ts, row in zip(t, data):
        writer.writerow([ts] + list(row))