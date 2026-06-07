#!/usr/bin/env python3
"""Generate a realistic sample invoice image without external dependencies."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import struct
import zlib

# 5x7 bitmap font for uppercase letters, digits, and common punctuation.
FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00001", "00001", "00001", "00001", "10001", "10001", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "#": ["01010", "11111", "01010", "01010", "11111", "01010", "01010"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "/": ["00001", "00010", "00100", "01000", "10000", "00000", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    ",": ["00000", "00000", "00000", "00000", "00110", "00110", "00100"],
    "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "?": ["01110", "10001", "00001", "00010", "00100", "00000", "00100"],
}


def make_canvas(width: int, height: int, bg: int = 255) -> bytearray:
    return bytearray([bg] * (width * height))


def draw_rect(canvas: bytearray, width: int, x: int, y: int, w: int, h: int, value: int) -> None:
    if w <= 0 or h <= 0:
        return
    height = len(canvas) // width
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    for yy in range(y0, y1):
        offset = yy * width
        for xx in range(x0, x1):
            canvas[offset + xx] = value


def draw_line(canvas: bytearray, width: int, x0: int, y0: int, x1: int, y1: int, value: int = 0) -> None:
    if x0 == x1:
        draw_rect(canvas, width, x0, min(y0, y1), 1, abs(y1 - y0) + 1, value)
    elif y0 == y1:
        draw_rect(canvas, width, min(x0, x1), y0, abs(x1 - x0) + 1, 1, value)


def draw_text(canvas: bytearray, width: int, x: int, y: int, text: str, scale: int = 2, color: int = 0) -> None:
    cx = x
    for raw_ch in text:
        ch = raw_ch.upper()
        glyph = FONT.get(ch, FONT["?"])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    draw_rect(canvas, width, cx + gx * scale, y + gy * scale, scale, scale, color)
        cx += (5 * scale) + scale


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def write_grayscale_png(path: str, width: int, height: int, pixels: bytearray) -> None:
    rows = []
    for y in range(height):
        start = y * width
        end = start + width
        rows.append(b"\x00" + bytes(pixels[start:end]))

    raw = b"".join(rows)
    compressed = zlib.compress(raw, level=9)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(signature)
        f.write(png_chunk(b"IHDR", ihdr))
        f.write(png_chunk(b"IDAT", compressed))
        f.write(png_chunk(b"IEND", b""))


def build_invoice(path: str) -> None:
    width, height = 1600, 1100
    canvas = make_canvas(width, height, bg=252)

    # Header and frame
    draw_rect(canvas, width, 80, 70, 1440, 2, 40)
    draw_rect(canvas, width, 80, 1000, 1440, 2, 40)
    draw_rect(canvas, width, 80, 70, 2, 932, 40)
    draw_rect(canvas, width, 1518, 70, 2, 932, 40)

    today = dt.date.today()
    draw_text(canvas, width, 120, 120, "ACME SUPPLY SOLUTIONS LTD", scale=5)
    draw_text(canvas, width, 120, 210, "123 INDUSTRIAL AVENUE, LAGOS", scale=3)
    draw_text(canvas, width, 120, 255, "EMAIL: ACCOUNTS@ACME.LOCAL", scale=3)

    draw_text(canvas, width, 980, 140, "INVOICE", scale=6)
    draw_text(canvas, width, 980, 240, "INVOICE NO: INV-2026-0515", scale=3)
    draw_text(canvas, width, 980, 285, f"DATE: {today.strftime('%Y/%m/%d')}", scale=3)
    draw_text(canvas, width, 980, 330, "CURRENCY: USD", scale=3)

    draw_line(canvas, width, 100, 380, 1500, 380)
    draw_text(canvas, width, 120, 400, "BILL TO: INTELLIGENT DOCUMENT PROCESSING", scale=3)
    draw_text(canvas, width, 120, 445, "ATTN: FINANCE TEAM", scale=3)

    # Table headers
    draw_line(canvas, width, 100, 510, 1500, 510)
    draw_line(canvas, width, 100, 560, 1500, 560)
    draw_line(canvas, width, 100, 760, 1500, 760)
    draw_line(canvas, width, 100, 820, 1500, 820)
    draw_line(canvas, width, 100, 900, 1500, 900)

    col_x = [100, 180, 910, 1080, 1260, 1500]
    for x in col_x:
        draw_line(canvas, width, x, 510, x, 900)

    draw_text(canvas, width, 120, 525, "QTY", scale=3)
    draw_text(canvas, width, 270, 525, "DESCRIPTION", scale=3)
    draw_text(canvas, width, 940, 525, "UNIT PRICE", scale=3)
    draw_text(canvas, width, 1120, 525, "TAX", scale=3)
    draw_text(canvas, width, 1325, 525, "LINE TOTAL", scale=3)

    rows = [
        ("2", "DOC INGESTION CONNECTOR SETUP", "$450.00", "$45.00", "$945.00"),
        ("1", "OCR PIPELINE TUNING", "$1,200.00", "$120.00", "$1,320.00"),
        ("3", "MODEL EVALUATION BATCH", "$210.00", "$63.00", "$693.00"),
    ]

    y = 600
    for qty, desc, unit, tax, total in rows:
        draw_text(canvas, width, 130, y, qty, scale=3)
        draw_text(canvas, width, 210, y, desc, scale=3)
        draw_text(canvas, width, 940, y, unit, scale=3)
        draw_text(canvas, width, 1120, y, tax, scale=3)
        draw_text(canvas, width, 1300, y, total, scale=3)
        y += 70

    draw_text(canvas, width, 980, 840, "SUBTOTAL: $2,958.00", scale=3)
    draw_text(canvas, width, 980, 875, "TAX: $228.00", scale=3)
    draw_text(canvas, width, 980, 920, "TOTAL AMOUNT: $3,186.00", scale=4)

    draw_text(canvas, width, 120, 940, "PAYMENT TERMS: NET 15", scale=3)
    draw_text(canvas, width, 120, 980, "THANK YOU FOR YOUR BUSINESS", scale=3)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_grayscale_png(path, width, height, canvas)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a realistic sample invoice PNG for e2e testing")
    parser.add_argument(
        "--output",
        default="samples/documents/sample_invoice_001.png",
        help="Output PNG path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_invoice(args.output)
    print(f"Generated invoice sample at: {args.output}")


if __name__ == "__main__":
    main()
