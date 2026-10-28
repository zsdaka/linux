#!/usr/bin/env python3
"""Draw 10M unique 5-tuples from the CTU-13 per-field empirical distribution."""
import array
import collections
import socket
import struct
import sys

import numpy as np

PROTO = {
    "tcp": 6, "udp": 17, "icmp": 1, "igmp": 2, "gre": 47,
    "esp": 50, "ah": 51, "sctp": 132,
}


def mix64(x):
    x &= (1 << 64) - 1
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & ((1 << 64) - 1)
    x ^= x >> 31
    return x


def pack_key(src, dst, sport, dport, proto):
    acc = 0
    raw = src + dst + struct.pack("!HHB", sport & 0xFFFF, dport & 0xFFFF, proto & 0xFF)
    for b in raw:
        acc = ((acc * 131) ^ b) & ((1 << 64) - 1)
    return mix64(acc)


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    n_target = 10_000_000
    cs, cd, cp, csp, cdp = (collections.Counter() for _ in range(5))
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
            p = line.split(",")
            if len(p) <= i_dport:
                continue
            proto = PROTO.get(p[i_proto].strip().lower(), 0)
            try:
                src = socket.inet_aton(p[i_src].strip())
                dst = socket.inet_aton(p[i_dst].strip())
            except OSError:
                continue
            try:
                sport = int(float(p[i_sport])) if p[i_sport].strip() else 0
                dport = int(float(p[i_dport])) if p[i_dport].strip() else 0
            except ValueError:
                continue
            cs[src] += 1
            cd[dst] += 1
            cp[proto] += 1
            csp[sport & 0xFFFF] += 1
            cdp[dport & 0xFFFF] += 1

    def arrays(counter):
        keys = list(counter.keys())
        w = np.array(list(counter.values()), dtype=np.float64)
        w /= w.sum()
        return keys, w

    src_k, src_p = arrays(cs)
    dst_k, dst_p = arrays(cd)
    proto_k, proto_p = arrays(cp)
    sp_k, sp_p = arrays(csp)
    dp_k, dp_p = arrays(cdp)
    rng = np.random.default_rng(20261028)
    got = set()
    while len(got) < n_target:
        need = n_target - len(got)
        batch = max(need + need // 10, 1000)
        si = rng.choice(len(src_k), size=batch, p=src_p)
        di = rng.choice(len(dst_k), size=batch, p=dst_p)
        pi = rng.choice(len(proto_k), size=batch, p=proto_p)
        spi = rng.choice(len(sp_k), size=batch, p=sp_p)
        dpi = rng.choice(len(dp_k), size=batch, p=dp_p)
        for a, b, c, d, e in zip(si, di, pi, spi, dpi):
            got.add(pack_key(src_k[a], dst_k[b], sp_k[d], dp_k[e], proto_k[c]))
            if len(got) >= n_target:
                break
    keys = list(got)[:n_target]
    buf = array.array("Q", keys)
    if sys.byteorder != "little":
        buf.byteswap()
    with open(dst_path, "wb") as out:
        out.write(struct.pack("<Q", len(keys)))
        buf.tofile(out)
    print(f"wrote {len(keys)}")


if __name__ == "__main__":
    main()
