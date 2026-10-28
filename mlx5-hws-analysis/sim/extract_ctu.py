#!/usr/bin/env python3
"""Extract unique IPv4 5-tuples from a CTU-13 binetflow file."""
import socket
import struct
import sys

PROTO = {
    "tcp": 6,
    "udp": 17,
    "icmp": 1,
    "igmp": 2,
    "gre": 47,
    "esp": 50,
    "ah": 51,
    "sctp": 132,
}


def mix64(x):
    x &= (1 << 64) - 1
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & ((1 << 64) - 1)
    x ^= x >> 31
    return x


def main():
    src_path, dst_path, meta_path = sys.argv[1], sys.argv[2], sys.argv[3]
    seen = set()
    bad = 0
    rows = 0
    with open(src_path, "r", errors="replace") as f:
        header = f.readline()
        cols = [c.strip() for c in header.split(",")]
        try:
            i_proto = cols.index("Proto")
            i_src = cols.index("SrcAddr")
            i_sport = cols.index("Sport")
            i_dst = cols.index("DstAddr")
            i_dport = cols.index("Dport")
        except ValueError:
            i_proto, i_src, i_sport, i_dst, i_dport = 2, 3, 4, 6, 7
        for line in f:
            rows += 1
            p = line.split(",")
            if len(p) <= i_dport:
                bad += 1
                continue
            proto = PROTO.get(p[i_proto].strip().lower(), 0)
            try:
                src = socket.inet_aton(p[i_src].strip())
                dst = socket.inet_aton(p[i_dst].strip())
            except OSError:
                bad += 1
                continue
            try:
                sport = int(float(p[i_sport])) if p[i_sport].strip() else 0
                dport = int(float(p[i_dport])) if p[i_dport].strip() else 0
            except ValueError:
                bad += 1
                continue
            sport &= 0xFFFF
            dport &= 0xFFFF
            raw = src + dst + struct.pack("!HHB", sport, dport, proto & 0xFF)
            seen.add(raw)
    keys = []
    seen_hash = set()
    for raw in seen:
        acc = 0
        for b in raw:
            acc = ((acc * 131) ^ b) & ((1 << 64) - 1)
        h = mix64(acc)
        if h in seen_hash or h == 0:
            continue
        seen_hash.add(h)
        keys.append(h)
    keys.sort()
    cap = keys[:10_000_000]
    with open(dst_path, "wb") as out:
        out.write(struct.pack("<Q", len(cap)))
        out.write(struct.pack("<" + "Q" * len(cap), *cap))
    with open(meta_path, "w") as m:
        m.write(f"rows {rows}\n")
        m.write(f"bad {bad}\n")
        m.write(f"unique {len(seen)}\n")
        m.write(f"written {len(cap)}\n")
        m.write("source CTU-13 capture20110810.binetflow\n")
        m.write("https://mcfp.felk.cvut.cz/publicDatasets/CTU-Malware-Capture-Botnet-42/\n")
    print(f"rows={rows} unique={len(seen)} written={len(cap)} bad={bad}")


if __name__ == "__main__":
    main()
