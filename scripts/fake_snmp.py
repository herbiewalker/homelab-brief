#!/usr/bin/env python3
"""A tiny SNMPv2c agent for scripts/e2e_devices.py: invented values, GET / GETNEXT / GETBULK, one community.

Just enough BER to answer the real net-snmp tools in the image, so the collector's command line and its
parsing of the tools' output are tested against the real programs.
"""

import socket
import sys

COMMUNITY = b"e2e-comm"
PORT = 1161
INTEGER, OCTETS, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
COUNTER32, GAUGE32, TIMETICKS = 0x41, 0x42, 0x43
GET, GETNEXT, GETBULK, RESPONSE = 0xA0, 0xA1, 0xA5, 0xA2
END_OF_MIB = 0x82


def oid_t(text):
    return tuple(int(x) for x in text.strip(".").split("."))


MIB = {
    oid_t("1.3.6.1.2.1.1.3.0"): (TIMETICKS, 8640000),
    **{oid_t(f"1.3.6.1.2.1.2.2.1.8.{i}"): (INTEGER, s) for i, s in enumerate((1, 1, 2, 1), 1)},
    **{oid_t(f"1.3.6.1.2.1.2.2.1.14.{i}"): (COUNTER32, e) for i, e in enumerate((10, 0, 5, 0), 1)},
    **{oid_t(f"1.3.6.1.2.1.2.2.1.20.{i}"): (COUNTER32, e) for i, e in enumerate((0, 0, 1, 0), 1)},
    oid_t("1.3.6.1.2.1.33.1.2.1.0"): (INTEGER, 2),
    oid_t("1.3.6.1.2.1.33.1.2.3.0"): (INTEGER, 45),
    oid_t("1.3.6.1.2.1.33.1.2.4.0"): (INTEGER, 100),
    oid_t("1.3.6.1.2.1.33.1.4.1.0"): (INTEGER, 3),
    oid_t("1.3.6.1.2.1.33.1.3.3.1.3.1"): (INTEGER, 231),
    oid_t("1.3.6.1.2.1.33.1.4.4.1.5.1"): (INTEGER, 18),
    oid_t("1.3.6.1.2.1.33.1.4.4.1.4.1"): (INTEGER, 120),
}
ORDER = sorted(MIB)


def length(n):
    if n < 128:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag, body):
    return bytes([tag]) + length(len(body)) + body


def enc_int(n):
    size = max(1, (n.bit_length() + 8) // 8)
    return tlv(INTEGER, n.to_bytes(size, "big", signed=True))


def enc_unsigned(tag, n):
    size = max(1, (n.bit_length() + 8) // 8)
    return tlv(tag, n.to_bytes(size, "big"))


def enc_oid(oid):
    body = bytes([40 * oid[0] + oid[1]])
    for arc in oid[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body += bytes(reversed(chunk))
    return tlv(OID, body)


def enc_value(entry):
    kind, value = entry
    if kind == INTEGER:
        return enc_int(value)
    return enc_unsigned(kind, value)


def read_tlv(data, pos):
    tag = data[pos]
    first = data[pos + 1]
    if first < 128:
        size, start = first, pos + 2
    else:
        count = first & 0x7F
        size, start = int.from_bytes(data[pos + 2 : pos + 2 + count], "big"), pos + 2 + count
    return tag, data[start : start + size], start + size


def dec_int(body):
    return int.from_bytes(body, "big", signed=True)


def dec_oid(body):
    arcs = [body[0] // 40, body[0] % 40]
    value = 0
    for byte in body[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return tuple(arcs)


def next_oid(oid):
    return next((o for o in ORDER if o > oid), None)


def varbind(oid, entry):
    return tlv(SEQUENCE, enc_oid(oid) + (enc_value(entry) if entry else tlv(END_OF_MIB, b"")))


def answer(packet):
    _, message, _ = read_tlv(packet, 0)
    _, version, pos = read_tlv(message, 0)
    _, community, pos = read_tlv(message, pos)
    if community != COMMUNITY:
        return None
    kind, pdu, _ = read_tlv(message, pos)
    _, request_id, p = read_tlv(pdu, 0)
    _, field1, p = read_tlv(pdu, p)
    _, field2, p = read_tlv(pdu, p)
    _, binds, _ = read_tlv(pdu, p)
    requested, q = [], 0
    while q < len(binds):
        _, one, q = read_tlv(binds, q)
        _, oid_body, _ = read_tlv(one, 0)
        requested.append(dec_oid(oid_body))
    out = []
    if kind == GET:
        for oid in requested:
            out.append(
                varbind(oid, MIB.get(oid)) if oid in MIB else tlv(SEQUENCE, enc_oid(oid) + tlv(0x80, b""))
            )
    elif kind == GETNEXT:
        for oid in requested:
            nxt = next_oid(oid)
            out.append(varbind(nxt, MIB[nxt]) if nxt else varbind(oid, None))
    elif kind == GETBULK:
        repeats = max(1, dec_int(field2))
        current = requested[0]
        for _ in range(repeats):
            nxt = next_oid(current)
            if nxt is None:
                out.append(varbind(current, None))
                break
            out.append(varbind(nxt, MIB[nxt]))
            current = nxt
    else:
        return None
    body = tlv(INTEGER, request_id) + enc_int(0) + enc_int(0) + tlv(SEQUENCE, b"".join(out))
    return tlv(SEQUENCE, enc_int(dec_int(version)) + tlv(OCTETS, community) + tlv(RESPONSE, body))


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print("listening", flush=True)
    while True:
        data, peer = sock.recvfrom(65535)
        try:
            reply = answer(data)
        except (IndexError, ValueError):
            reply = None
        print(f"request from the client: answered={reply is not None}", file=sys.stdout, flush=True)
        if reply:
            sock.sendto(reply, peer)


main()
