#!/usr/bin/env python3
"""
OBLIVION Instant Answers — 20 useful instant answer tools
Port 3063 | oblivionsearch.com/instant
"""

import ast
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import string
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Optional

import qrcode
from fastapi import FastAPI, Query, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from simpleeval import simple_eval
import uvicorn

app = FastAPI(title="OBLIVION Instant Answers", docs_url=None, redoc_url=None)

# ─── Tool Registry ───────────────────────────────────────────────────────────

TOOLS = {
    "calculator": {
        "name": "Calculator",
        "desc": "Evaluate math expressions safely",
        "icon": "&#128290;",
        "placeholder": "e.g. (2+3)*5 or sqrt(144)",
        "example": "(2+3)*5",
    },
    "unit-converter": {
        "name": "Unit Converter",
        "desc": "Convert between common units (km↔mi, kg↔lbs, C↔F, etc.)",
        "icon": "&#128207;",
        "placeholder": "e.g. 100 km to miles",
        "example": "100 km to miles",
    },
    "color-converter": {
        "name": "Color Converter",
        "desc": "Convert between hex, RGB, and HSL color codes",
        "icon": "&#127912;",
        "placeholder": "e.g. #00d4ff or rgb(0,212,255)",
        "example": "#00d4ff",
    },
    "base64": {
        "name": "Base64 Encode/Decode",
        "desc": "Encode or decode Base64 strings",
        "icon": "&#128272;",
        "placeholder": "e.g. Hello World or SGVsbG8gV29ybGQ=",
        "example": "Hello World",
    },
    "url-encode": {
        "name": "URL Encode/Decode",
        "desc": "Percent-encode or decode URLs",
        "icon": "&#128279;",
        "placeholder": "e.g. hello world! or hello%20world%21",
        "example": "hello world!",
    },
    "timestamp": {
        "name": "Unix Timestamp Converter",
        "desc": "Convert Unix timestamps to human dates and vice versa",
        "icon": "&#128336;",
        "placeholder": "e.g. 1711584000 or 2024-03-28",
        "example": "now",
    },
    "password-generator": {
        "name": "Password Generator",
        "desc": "Generate secure random passwords",
        "icon": "&#128274;",
        "placeholder": "e.g. 16 or 24 strong",
        "example": "16",
    },
    "uuid-generator": {
        "name": "UUID Generator",
        "desc": "Generate random UUIDs (v4)",
        "icon": "&#128196;",
        "placeholder": "e.g. 1 or 5",
        "example": "1",
    },
    "hash-generator": {
        "name": "Hash Generator",
        "desc": "Generate MD5, SHA1, SHA256, SHA512 hashes",
        "icon": "&#128737;",
        "placeholder": "e.g. Hello World",
        "example": "Hello World",
    },
    "lorem-ipsum": {
        "name": "Lorem Ipsum Generator",
        "desc": "Generate placeholder text paragraphs",
        "icon": "&#128220;",
        "placeholder": "e.g. 3 (number of paragraphs)",
        "example": "3",
    },
    "dice-roller": {
        "name": "Dice Roller",
        "desc": "Roll dice in standard notation",
        "icon": "&#127922;",
        "placeholder": "e.g. 2d6 or 1d20+5",
        "example": "2d6",
    },
    "coin-flip": {
        "name": "Coin Flip",
        "desc": "Flip one or more coins",
        "icon": "&#129689;",
        "placeholder": "e.g. 1 or 10 (number of flips)",
        "example": "3",
    },
    "roman-numeral": {
        "name": "Roman Numeral Converter",
        "desc": "Convert between Roman numerals and decimal",
        "icon": "&#127963;",
        "placeholder": "e.g. 42 or XIV",
        "example": "42",
    },
    "number-base": {
        "name": "Number Base Converter",
        "desc": "Convert between binary, decimal, hex, and octal",
        "icon": "&#128290;",
        "placeholder": "e.g. 0xFF or 0b1010 or 255",
        "example": "255",
    },
    "bmi-calculator": {
        "name": "BMI Calculator",
        "desc": "Calculate Body Mass Index from weight and height",
        "icon": "&#9878;",
        "placeholder": "e.g. 70kg 175cm or 154lbs 5ft10in",
        "example": "70kg 175cm",
    },
    "tip-calculator": {
        "name": "Tip Calculator",
        "desc": "Calculate tip amounts and split bills",
        "icon": "&#128176;",
        "placeholder": "e.g. 85.50 18% 4people",
        "example": "85.50 18%",
    },
    "word-counter": {
        "name": "Word & Character Counter",
        "desc": "Count words, characters, sentences, and paragraphs",
        "icon": "&#128221;",
        "placeholder": "Paste your text here",
        "example": "The quick brown fox jumps over the lazy dog.",
    },
    "json-formatter": {
        "name": "JSON Formatter & Validator",
        "desc": "Format, validate, and prettify JSON data",
        "icon": "&#128203;",
        "placeholder": 'e.g. {"name":"test","value":123}',
        "example": '{"name":"OBLIVION","version":1,"features":["search","privacy"]}',
    },
    "regex-tester": {
        "name": "Regex Tester",
        "desc": "Test regular expressions against text",
        "icon": "&#128270;",
        "placeholder": "e.g. pattern|||test string",
        "example": r"\b\w+@\w+\.\w+\b|||Contact us at hello@oblivion.com or support@oblivion.com",
    },
    "qr-code": {
        "name": "QR Code Generator",
        "desc": "Generate QR codes from text or URLs",
        "icon": "&#9641;",
        "placeholder": "e.g. https://oblivionsearch.com",
        "example": "https://oblivionsearch.com",
    },
}

# ─── Tool Implementations ────────────────────────────────────────────────────

def tool_calculator(inp: str) -> dict:
    expr = inp.strip()
    # Support common math functions
    expr_clean = expr.replace('^', '**')
    try:
        result = simple_eval(expr_clean, functions={
            "sqrt": math.sqrt, "abs": abs, "round": round,
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "log": math.log, "log10": math.log10, "log2": math.log2,
            "pi": math.pi, "e": math.e, "pow": pow,
            "floor": math.floor, "ceil": math.ceil,
        })
        return {"expression": expr, "result": result}
    except Exception as e:
        return {"expression": expr, "error": str(e)}


UNIT_CONVERSIONS = {
    ("km", "miles"): lambda x: x * 0.621371,
    ("miles", "km"): lambda x: x * 1.60934,
    ("mi", "km"): lambda x: x * 1.60934,
    ("km", "mi"): lambda x: x * 0.621371,
    ("kg", "lbs"): lambda x: x * 2.20462,
    ("lbs", "kg"): lambda x: x * 0.453592,
    ("lb", "kg"): lambda x: x * 0.453592,
    ("kg", "lb"): lambda x: x * 2.20462,
    ("c", "f"): lambda x: x * 9/5 + 32,
    ("f", "c"): lambda x: (x - 32) * 5/9,
    ("celsius", "fahrenheit"): lambda x: x * 9/5 + 32,
    ("fahrenheit", "celsius"): lambda x: (x - 32) * 5/9,
    ("m", "ft"): lambda x: x * 3.28084,
    ("ft", "m"): lambda x: x * 0.3048,
    ("meters", "feet"): lambda x: x * 3.28084,
    ("feet", "meters"): lambda x: x * 0.3048,
    ("cm", "in"): lambda x: x * 0.393701,
    ("in", "cm"): lambda x: x * 2.54,
    ("inches", "cm"): lambda x: x * 2.54,
    ("cm", "inches"): lambda x: x * 0.393701,
    ("l", "gal"): lambda x: x * 0.264172,
    ("gal", "l"): lambda x: x * 3.78541,
    ("liters", "gallons"): lambda x: x * 0.264172,
    ("gallons", "liters"): lambda x: x * 3.78541,
    ("oz", "g"): lambda x: x * 28.3495,
    ("g", "oz"): lambda x: x * 0.035274,
    ("mph", "kph"): lambda x: x * 1.60934,
    ("kph", "mph"): lambda x: x * 0.621371,
    ("mm", "in"): lambda x: x * 0.0393701,
    ("in", "mm"): lambda x: x * 25.4,
}

def tool_unit_converter(inp: str) -> dict:
    # Parse: "100 km to miles" or "100km miles"
    m = re.match(r'([\d.]+)\s*([a-zA-Z°]+)\s+(?:to\s+|in\s+)?([a-zA-Z°]+)', inp.strip(), re.I)
    if not m:
        return {"error": "Format: <value> <from_unit> to <to_unit>. Example: 100 km to miles"}
    val, from_u, to_u = float(m.group(1)), m.group(2).lower(), m.group(3).lower()
    key = (from_u, to_u)
    if key in UNIT_CONVERSIONS:
        result = UNIT_CONVERSIONS[key](val)
        return {"input": val, "from": from_u, "to": to_u, "result": round(result, 6)}
    return {"error": f"Unknown conversion: {from_u} to {to_u}",
            "supported": "km↔miles, kg↔lbs, C↔F, m↔ft, cm↔in, L↔gal, oz↔g, mph↔kph"}


def hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c*2 for c in h)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hsl(r, g, b):
    r, g, b = r/255, g/255, b/255
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        h = s = 0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r: h = (g - b) / d + (6 if g < b else 0)
        elif mx == g: h = (b - r) / d + 2
        else: h = (r - g) / d + 4
        h /= 6
    return round(h*360, 1), round(s*100, 1), round(l*100, 1)

def tool_color_converter(inp: str) -> dict:
    inp = inp.strip()
    # Hex input
    if re.match(r'^#?[0-9a-fA-F]{3,6}$', inp):
        h = inp if inp.startswith('#') else '#' + inp
        r, g, b = hex_to_rgb(h)
        hue, sat, lig = rgb_to_hsl(r, g, b)
        return {"hex": h, "rgb": f"rgb({r},{g},{b})", "hsl": f"hsl({hue},{sat}%,{lig}%)",
                "r": r, "g": g, "b": b}
    # RGB input
    m = re.match(r'rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', inp, re.I)
    if not m:
        m = re.match(r'(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', inp)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h = f"#{r:02x}{g:02x}{b:02x}"
        hue, sat, lig = rgb_to_hsl(r, g, b)
        return {"hex": h, "rgb": f"rgb({r},{g},{b})", "hsl": f"hsl({hue},{sat}%,{lig}%)",
                "r": r, "g": g, "b": b}
    return {"error": "Provide a hex color (#00d4ff) or RGB (rgb(0,212,255))"}


def tool_base64(inp: str) -> dict:
    # Try decode first
    try:
        decoded = base64.b64decode(inp.strip()).decode('utf-8')
        # If it decodes cleanly, show both
        encoded = base64.b64encode(inp.strip().encode()).decode()
        return {"input": inp.strip(), "encoded": encoded, "decoded": decoded,
                "note": "Input appears to be valid base64; showing decode result"}
    except Exception:
        pass
    encoded = base64.b64encode(inp.strip().encode()).decode()
    return {"input": inp.strip(), "encoded": encoded}


def tool_url_encode(inp: str) -> dict:
    encoded = urllib.parse.quote(inp.strip(), safe='')
    decoded = urllib.parse.unquote(inp.strip())
    return {"input": inp.strip(), "encoded": encoded, "decoded": decoded}


def tool_timestamp(inp: str) -> dict:
    inp = inp.strip()
    now = datetime.now(timezone.utc)
    if inp.lower() == 'now' or inp == '':
        ts = int(now.timestamp())
        return {"unix": ts, "utc": now.strftime('%Y-%m-%d %H:%M:%S UTC'),
                "iso": now.isoformat(), "note": "Current time"}
    # Try as unix timestamp
    try:
        ts = int(inp)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return {"unix": ts, "utc": dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
                "iso": dt.isoformat(), "relative": _relative_time(ts)}
    except (ValueError, OSError):
        pass
    # Try as date string
    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                '%m/%d/%Y', '%d/%m/%Y', '%B %d, %Y']:
        try:
            dt = datetime.strptime(inp, fmt).replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            return {"unix": ts, "utc": dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
                    "iso": dt.isoformat(), "input_parsed": inp}
        except ValueError:
            continue
    return {"error": f"Cannot parse: {inp}. Use a Unix timestamp or date like 2024-03-28"}

def _relative_time(ts):
    diff = time.time() - ts
    if abs(diff) < 60: return "just now"
    if abs(diff) < 3600: return f"{int(abs(diff)/60)} minutes {'ago' if diff > 0 else 'from now'}"
    if abs(diff) < 86400: return f"{int(abs(diff)/3600)} hours {'ago' if diff > 0 else 'from now'}"
    return f"{int(abs(diff)/86400)} days {'ago' if diff > 0 else 'from now'}"


def tool_password_generator(inp: str) -> dict:
    parts = inp.strip().lower().split()
    length = 16
    for p in parts:
        try:
            length = int(p)
            break
        except ValueError:
            pass
    length = max(4, min(128, length))
    use_special = 'simple' not in inp.lower()
    chars = string.ascii_letters + string.digits
    if use_special:
        chars += "!@#$%&*-_=+"
    passwords = []
    for _ in range(3):
        pw = ''.join(random.SystemRandom().choice(chars) for _ in range(length))
        passwords.append(pw)
    return {"passwords": passwords, "length": length, "includes_special": use_special}


def tool_uuid_generator(inp: str) -> dict:
    try:
        count = max(1, min(20, int(inp.strip() or '1')))
    except ValueError:
        count = 1
    uuids = [str(uuid.uuid4()) for _ in range(count)]
    return {"uuids": uuids, "version": 4, "count": count}


def tool_hash_generator(inp: str) -> dict:
    data = inp.strip().encode('utf-8')
    return {
        "input": inp.strip(),
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha512": hashlib.sha512(data).hexdigest(),
    }


LOREM_SENTENCES = [
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.",
    "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore.",
    "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia.",
    "Nulla facilisi morbi tempus iaculis urna id volutpat lacus.",
    "Viverra accumsan in nisl nisi scelerisque eu ultrices vitae auctor.",
    "Eget nulla facilisi etiam dignissim diam quis enim lobortis.",
    "Amet consectetur adipiscing elit pellentesque habitant morbi tristique senectus.",
    "Turpis egestas pretium aenean pharetra magna ac placerat vestibulum.",
]

def tool_lorem_ipsum(inp: str) -> dict:
    try:
        count = max(1, min(20, int(inp.strip() or '3')))
    except ValueError:
        count = 3
    paragraphs = []
    for _ in range(count):
        n = random.randint(3, 6)
        sentences = random.sample(LOREM_SENTENCES, min(n, len(LOREM_SENTENCES)))
        paragraphs.append(' '.join(sentences))
    return {"paragraphs": paragraphs, "count": count}


def tool_dice_roller(inp: str) -> dict:
    inp = inp.strip() or '1d6'
    m = re.match(r'(\d*)d(\d+)([+-]\d+)?', inp, re.I)
    if not m:
        return {"error": "Use dice notation: 2d6, 1d20+5, 3d8-2"}
    num = int(m.group(1) or 1)
    sides = int(m.group(2))
    mod = int(m.group(3) or 0)
    num = max(1, min(100, num))
    sides = max(2, min(1000, sides))
    rolls = [random.randint(1, sides) for _ in range(num)]
    total = sum(rolls) + mod
    return {"notation": inp, "rolls": rolls, "modifier": mod, "total": total}


def tool_coin_flip(inp: str) -> dict:
    try:
        count = max(1, min(1000, int(inp.strip() or '1')))
    except ValueError:
        count = 1
    flips = [random.choice(["Heads", "Tails"]) for _ in range(count)]
    heads = flips.count("Heads")
    tails = flips.count("Tails")
    return {"flips": flips if count <= 20 else flips[:20] + [f"...and {count-20} more"],
            "heads": heads, "tails": tails, "total": count}


ROMAN_MAP = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),
             (50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]

def int_to_roman(n):
    result = ''
    for val, sym in ROMAN_MAP:
        while n >= val:
            result += sym
            n -= val
    return result

def roman_to_int(s):
    rom = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    result = 0
    for i, c in enumerate(s.upper()):
        if c not in rom:
            raise ValueError(f"Invalid Roman numeral character: {c}")
        if i + 1 < len(s) and rom.get(s[i+1].upper(), 0) > rom[c]:
            result -= rom[c]
        else:
            result += rom[c]
    return result

def tool_roman_numeral(inp: str) -> dict:
    inp = inp.strip()
    if re.match(r'^\d+$', inp):
        n = int(inp)
        if n < 1 or n > 3999:
            return {"error": "Number must be between 1 and 3999"}
        return {"decimal": n, "roman": int_to_roman(n)}
    if re.match(r'^[IVXLCDM]+$', inp.upper()):
        n = roman_to_int(inp)
        return {"roman": inp.upper(), "decimal": n}
    return {"error": "Provide a number (1-3999) or Roman numeral (e.g. XIV)"}


def tool_number_base(inp: str) -> dict:
    inp = inp.strip()
    try:
        if inp.startswith('0x') or inp.startswith('0X'):
            n = int(inp, 16)
        elif inp.startswith('0b') or inp.startswith('0B'):
            n = int(inp, 2)
        elif inp.startswith('0o') or inp.startswith('0O'):
            n = int(inp, 8)
        else:
            n = int(inp)
    except ValueError:
        return {"error": f"Cannot parse number: {inp}. Use decimal, 0x hex, 0b binary, or 0o octal."}
    return {"decimal": n, "binary": bin(n), "octal": oct(n), "hexadecimal": hex(n),
            "binary_raw": bin(n)[2:], "hex_raw": hex(n)[2:].upper()}


def tool_bmi_calculator(inp: str) -> dict:
    inp = inp.strip().lower()
    weight_kg = None
    height_m = None
    # kg and cm
    m = re.search(r'([\d.]+)\s*kg', inp)
    if m:
        weight_kg = float(m.group(1))
    m = re.search(r'([\d.]+)\s*(?:cm|centimeters)', inp)
    if m:
        height_m = float(m.group(1)) / 100
    # lbs
    m = re.search(r'([\d.]+)\s*(?:lbs?|pounds)', inp)
    if m:
        weight_kg = float(m.group(1)) * 0.453592
    # feet/inches
    m = re.search(r'(\d+)\s*(?:ft|feet|\')\s*(\d+)?\s*(?:in|inches|")?', inp)
    if m:
        feet = int(m.group(1))
        inches = int(m.group(2) or 0)
        height_m = (feet * 12 + inches) * 0.0254
    # meters
    m2 = re.search(r'([\d.]+)\s*m(?:eters?)?\b', inp)
    if m2 and height_m is None:
        height_m = float(m2.group(1))

    if weight_kg is None or height_m is None or height_m == 0:
        return {"error": "Provide weight and height. Example: 70kg 175cm or 154lbs 5ft10in"}
    bmi = weight_kg / (height_m ** 2)
    if bmi < 18.5: category = "Underweight"
    elif bmi < 25: category = "Normal weight"
    elif bmi < 30: category = "Overweight"
    else: category = "Obese"
    return {"bmi": round(bmi, 1), "category": category,
            "weight_kg": round(weight_kg, 1), "height_m": round(height_m, 2)}


def tool_tip_calculator(inp: str) -> dict:
    inp = inp.strip()
    # Parse bill amount
    m = re.search(r'[\$]?([\d.]+)', inp)
    if not m:
        return {"error": "Provide bill amount. Example: 85.50 18% 4people"}
    bill = float(m.group(1))
    # Parse tip percentage
    m2 = re.search(r'(\d+)\s*%', inp)
    tip_pct = float(m2.group(1)) if m2 else 18
    # Parse split
    m3 = re.search(r'(\d+)\s*(?:people|split|way)', inp)
    split = int(m3.group(1)) if m3 else 1
    tip = bill * tip_pct / 100
    total = bill + tip
    per_person = total / max(1, split)
    return {"bill": round(bill, 2), "tip_percent": tip_pct, "tip_amount": round(tip, 2),
            "total": round(total, 2), "split": split, "per_person": round(per_person, 2)}


def tool_word_counter(inp: str) -> dict:
    text = inp.strip()
    words = len(text.split()) if text else 0
    chars = len(text)
    chars_no_space = len(text.replace(' ', ''))
    sentences = len(re.split(r'[.!?]+', text)) - 1 if text else 0
    paragraphs = len([p for p in text.split('\n') if p.strip()]) if text else 0
    return {"words": words, "characters": chars, "characters_no_spaces": chars_no_space,
            "sentences": max(sentences, 0), "paragraphs": max(paragraphs, 1) if text else 0}


def tool_json_formatter(inp: str) -> dict:
    try:
        parsed = json.loads(inp.strip())
        formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
        return {"valid": True, "formatted": formatted, "type": type(parsed).__name__,
                "keys": list(parsed.keys()) if isinstance(parsed, dict) else None}
    except json.JSONDecodeError as e:
        return {"valid": False, "error": str(e), "position": e.pos}


def tool_regex_tester(inp: str) -> dict:
    # Split on ||| separator
    parts = inp.split('|||')
    if len(parts) < 2:
        # Try pipe separator
        parts = inp.split('|', 1)
    if len(parts) < 2:
        return {"error": "Format: pattern|||test string. Example: \\d+|||There are 42 cats"}
    pattern = parts[0].strip()
    test_str = parts[1].strip()
    try:
        matches = []
        for m in re.finditer(pattern, test_str):
            matches.append({"match": m.group(), "start": m.start(), "end": m.end(),
                           "groups": list(m.groups()) if m.groups() else None})
        return {"pattern": pattern, "test_string": test_str, "match_count": len(matches),
                "matches": matches[:50]}
    except re.error as e:
        return {"error": f"Invalid regex: {e}", "pattern": pattern}


def tool_qr_code(inp: str) -> dict:
    text = inp.strip() or "https://oblivionsearch.com"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"text": text, "qr_base64_png": b64, "length": len(text)}


TOOL_FUNCS = {
    "calculator": tool_calculator,
    "unit-converter": tool_unit_converter,
    "color-converter": tool_color_converter,
    "base64": tool_base64,
    "url-encode": tool_url_encode,
    "timestamp": tool_timestamp,
    "password-generator": tool_password_generator,
    "uuid-generator": tool_uuid_generator,
    "hash-generator": tool_hash_generator,
    "lorem-ipsum": tool_lorem_ipsum,
    "dice-roller": tool_dice_roller,
    "coin-flip": tool_coin_flip,
    "roman-numeral": tool_roman_numeral,
    "number-base": tool_number_base,
    "bmi-calculator": tool_bmi_calculator,
    "tip-calculator": tool_tip_calculator,
    "word-counter": tool_word_counter,
    "json-formatter": tool_json_formatter,
    "regex-tester": tool_regex_tester,
    "qr-code": tool_qr_code,
}

# ─── Auto-detect ──────────────────────────────────────────────────────────────

DETECT_PATTERNS = [
    (r'^\s*[\d.]+\s*[+\-*/^%]\s*[\d.]', 'calculator'),
    (r'(?:sqrt|sin|cos|tan|log)\s*\(', 'calculator'),
    (r'\d+\.?\d*\s*(?:km|miles?|mi|kg|lbs?|lb|celsius|fahrenheit|[cf]|meters?|feet|ft|cm|in|inches|gal|liters?|oz|mph|kph)\s+(?:to|in)\s+', 'unit-converter'),
    (r'^#?[0-9a-fA-F]{6}$', 'color-converter'),
    (r'^rgb\s*\(', 'color-converter'),
    (r'base64|encode.*decode|decode.*encode', 'base64'),
    (r'url\s*(?:en|de)code|percent.?encode|%[0-9A-F]{2}', 'url-encode'),
    (r'^\d{9,10}$', 'timestamp'),
    (r'unix\s*(?:time|stamp)', 'timestamp'),
    (r'password|passphrase|generate.*pass', 'password-generator'),
    (r'uuid|guid', 'uuid-generator'),
    (r'(?:md5|sha\d*|hash)\b', 'hash-generator'),
    (r'lorem\s*ipsum|placeholder\s*text', 'lorem-ipsum'),
    (r'\d*d\d+[+-]?\d*', 'dice-roller'),
    (r'flip.*coin|coin.*flip|heads.*tails', 'coin-flip'),
    (r'^[IVXLCDM]+$', 'roman-numeral'),
    (r'roman\s*numeral', 'roman-numeral'),
    (r'0x[0-9a-fA-F]+|0b[01]+|binary|hexadecimal', 'number-base'),
    (r'bmi|body\s*mass', 'bmi-calculator'),
    (r'tip\s*(calc|for|on)|%\s*tip|\d+%\s*\d+people', 'tip-calculator'),
    (r'word\s*count|char.*count|count.*words', 'word-counter'),
    (r'^\s*[{\[]', 'json-formatter'),
    (r'json\s*(format|valid|prett)', 'json-formatter'),
    (r'regex|regexp|pattern.*test', 'regex-tester'),
    (r'qr\s*code|generate.*qr', 'qr-code'),
]

def detect_tool(query: str) -> Optional[str]:
    q = query.strip()
    for pattern, tool_name in DETECT_PATTERNS:
        if re.search(pattern, q, re.I):
            return tool_name
    return None

# ─── HTML Templates ───────────────────────────────────────────────────────────

CSS = """
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;overflow-x:hidden}
a{color:#00d4ff;text-decoration:none}a:hover{text-decoration:underline}
.container{max-width:1100px;margin:0 auto;padding:20px}
.header{text-align:center;padding:40px 0 20px}
.header h1{font-size:2.2em;color:#fff;margin-bottom:8px}
.header h1 span{color:#00d4ff}
.header p{color:#888;font-size:1.1em}
.search-box{max-width:600px;margin:25px auto;position:relative}
.search-box input{width:100%;padding:14px 20px;border-radius:12px;border:1px solid #222;background:#111;color:#fff;font-size:16px;outline:none;transition:border-color .2s}
.search-box input:focus{border-color:#00d4ff}
.tools-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:30px}
.tool-card{background:#111;border:1px solid #1a1a2e;border-radius:12px;padding:20px;cursor:pointer;transition:all .2s}
.tool-card:hover{border-color:#00d4ff;transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,212,255,.1)}
.tool-card .icon{font-size:28px;margin-bottom:8px}
.tool-card h3{color:#fff;font-size:1em;margin-bottom:4px}
.tool-card p{color:#666;font-size:.85em}
.tool-page{max-width:700px;margin:0 auto}
.tool-page h2{color:#fff;font-size:1.8em;margin-bottom:8px}
.tool-page .desc{color:#888;margin-bottom:24px}
.input-group{margin-bottom:20px}
.input-group label{display:block;color:#aaa;margin-bottom:6px;font-size:.9em}
.input-group input,.input-group textarea{width:100%;padding:12px 16px;border-radius:10px;border:1px solid #222;background:#111;color:#fff;font-size:15px;outline:none;transition:border-color .2s}
.input-group input:focus,.input-group textarea:focus{border-color:#00d4ff}
.input-group textarea{min-height:100px;resize:vertical;font-family:monospace}
.btn{background:#00d4ff;color:#0a0a0f;border:none;padding:10px 24px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;transition:all .2s}
.btn:hover{background:#00bde0;transform:translateY(-1px)}
.btn-secondary{background:#1a1a2e;color:#00d4ff;border:1px solid #00d4ff30}
.btn-secondary:hover{background:#00d4ff15}
.result-box{background:#0d0d1a;border:1px solid #1a1a2e;border-radius:12px;padding:20px;margin-top:20px;display:none;position:relative}
.result-box.show{display:block}
.result-box pre{white-space:pre-wrap;word-break:break-all;color:#00d4ff;font-family:'Fira Code',monospace,monospace;font-size:14px;line-height:1.6}
.result-box .copy-btn{position:absolute;top:10px;right:10px;background:#1a1a2e;color:#00d4ff;border:1px solid #00d4ff40;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.result-box .copy-btn:hover{background:#00d4ff20}
.back-link{display:inline-block;margin-bottom:20px;color:#888;font-size:.9em}
.back-link:hover{color:#00d4ff}
.footer{text-align:center;padding:40px 0 20px;color:#444;font-size:.85em}
.qr-img{max-width:256px;margin:16px auto;display:block;border-radius:8px;background:#fff;padding:8px}
.badge{display:inline-block;background:#00d4ff15;color:#00d4ff;padding:2px 10px;border-radius:20px;font-size:.75em;margin-left:8px}
@media(max-width:768px){
  .header h1{font-size:1.8em}
  .header p{font-size:1em}
  .tools-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
  .btn{min-height:44px;padding:12px 20px}
  .copy-btn{min-width:44px;min-height:44px}
  .container{padding:16px}
}
@media(max-width:480px){
  .header h1{font-size:1.5em}
  .tools-grid{grid-template-columns:1fr 1fr;gap:10px}
  .container{padding:12px}
  .result-box pre{font-size:12px}
  input,select,textarea{font-size:16px}
}
@media(max-width:375px){
  .header h1{font-size:1.3em}
  .tools-grid{grid-template-columns:1fr;gap:8px}
  body{font-size:14px}
}
</style>
"""

def landing_page_html():
    tool_cards = ""
    for slug, t in TOOLS.items():
        tool_cards += f'''<a href="/instant/{slug}" class="tool-card">
            <div class="icon">{t["icon"]}</div>
            <h3>{t["name"]}</h3>
            <p>{t["desc"]}</p>
        </a>\n'''
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION Instant Answers - Free Online Tools</title>
<meta name="description" content="20 free instant answer tools: calculator, unit converter, color codes, base64, QR codes, password generator, and more. Fast, private, no tracking.">
<link rel="canonical" href="https://oblivionsearch.com/instant">
<meta property="og:title" content="OBLIVION Instant Answers — Free Online Tools">
<meta property="og:description" content="20 free instant answer tools: calculator, unit converter, color codes, base64, QR codes, password generator, and more. Fast, private, no tracking.">
<meta property="og:url" content="https://oblivionsearch.com/instant">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
{CSS}
</head><body>
<div class="container">
    <div class="header">
        <h1><span>OBLIVION</span> Instant Answers</h1>
        <p>20 free tools. Instant results. Zero tracking.</p>
    </div>
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search tools or type a query (e.g. 100 km to miles, #ff5500, 2d6)..."
               autocomplete="off" autofocus>
    </div>
    <div class="tools-grid" id="toolsGrid">{tool_cards}</div>
    <div class="footer">OBLIVION Search &mdash; Private by Design &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></div>
</div>
<script>
const searchInput = document.getElementById('searchInput');
const toolsGrid = document.getElementById('toolsGrid');
const cards = toolsGrid.querySelectorAll('.tool-card');
searchInput.addEventListener('input', function() {{
    const q = this.value.toLowerCase();
    cards.forEach(c => {{
        const text = c.textContent.toLowerCase();
        c.style.display = text.includes(q) ? '' : 'none';
    }});
}});
searchInput.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter' && this.value.trim()) {{
        window.location.href = '/api/instant/detect?q=' + encodeURIComponent(this.value.trim()) + '&redirect=1';
    }}
}});
</script>
</body></html>"""


def tool_page_html(slug: str):
    t = TOOLS.get(slug)
    if not t:
        return "<h1>Tool not found</h1>"
    is_textarea = slug in ('json-formatter', 'word-counter', 'regex-tester')
    input_el = f'<textarea id="toolInput" placeholder="{t["placeholder"]}" rows="5">{t["example"]}</textarea>' if is_textarea else f'<input type="text" id="toolInput" placeholder="{t["placeholder"]}" value="{t["example"]}">'

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t["name"]} - OBLIVION Instant Answers</title>
<meta name="description" content="{t["desc"]} - Free online tool by OBLIVION Search">
{CSS}
</head><body>
<div class="container">
    <div class="tool-page">
        <a href="/instant" class="back-link">&larr; All Tools</a>
        <h2>{t["icon"]} {t["name"]}</h2>
        <p class="desc">{t["desc"]}</p>
        <div class="input-group">
            <label>Input</label>
            {input_el}
        </div>
        <button class="btn" onclick="runTool()">Calculate</button>
        <button class="btn btn-secondary" onclick="clearAll()" style="margin-left:8px">Clear</button>
        <div class="result-box" id="resultBox">
            <button class="copy-btn" onclick="copyResult()">Copy</button>
            <pre id="resultText"></pre>
            <div id="qrContainer"></div>
        </div>
        <div class="footer">OBLIVION Search &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></div>
    </div>
</div>
<script>
const SLUG = "{slug}";
async function runTool() {{
    const input = document.getElementById('toolInput').value;
    const res = await fetch('/api/instant/' + SLUG + '?input=' + encodeURIComponent(input));
    const data = await res.json();
    const box = document.getElementById('resultBox');
    const text = document.getElementById('resultText');
    const qrC = document.getElementById('qrContainer');
    qrC.innerHTML = '';
    if (data.qr_base64_png) {{
        qrC.innerHTML = '<img class="qr-img" src="data:image/png;base64,' + data.qr_base64_png + '" alt="QR Code">';
        delete data.qr_base64_png;
    }}
    text.textContent = JSON.stringify(data, null, 2);
    box.classList.add('show');
}}
function clearAll() {{
    document.getElementById('toolInput').value = '';
    document.getElementById('resultBox').classList.remove('show');
}}
function copyResult() {{
    const text = document.getElementById('resultText').textContent;
    navigator.clipboard.writeText(text).then(() => {{
        const btn = document.querySelector('.copy-btn');
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy', 1500);
    }});
}}
document.getElementById('toolInput').addEventListener('keydown', function(e) {{
    if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); runTool(); }}
}});
</script>
</body></html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/instant", response_class=HTMLResponse)
async def instant_landing():
    return landing_page_html()

@app.get("/api/instant/detect")
async def instant_detect(q: str = Query(""), redirect: Optional[str] = None):
    tool = detect_tool(q)
    if not tool:
        if redirect:
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/instant")
        return {"detected_tool": None, "query": q, "suggestion": "Try a specific query like '100 km to miles' or '#ff5500'"}
    if redirect:
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=f"/instant/{tool}?q={urllib.parse.quote(q)}")
    # Also run the tool
    result = TOOL_FUNCS[tool](q)
    return {"detected_tool": tool, "tool_name": TOOLS[tool]["name"], "query": q, "result": result}

@app.get("/api/instant/{tool_name}")
async def instant_api(tool_name: str, input: str = Query(""), x_api_key: Optional[str] = Header(None)):
    if x_api_key:
        import sys
        sys.path.insert(0, "/opt/oblivionzone")
        from oblivion_stripe_saas import check_api_key
        plan = check_api_key(x_api_key, "oblivion_instant")
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    if tool_name not in TOOL_FUNCS:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=404)
    result = TOOL_FUNCS[tool_name](input)
    return result

@app.get("/api/instant")
async def instant_api_list():
    return {"tools": {k: {"name": v["name"], "desc": v["desc"]} for k, v in TOOLS.items()},
            "count": len(TOOLS)}

# ─── Main ─────────────────────────────────────────────────────────────────────



# ═══════════════════════════════════════════════════════════════════
# STRIPE SAAS ROUTES (auto-generated by add_stripe_to_all.py)
# ═══════════════════════════════════════════════════════════════════
import sys
sys.path.insert(0, "/opt/oblivionzone")
from oblivion_stripe_saas import (
    ensure_db, create_checkout_session, handle_success, handle_webhook,
    check_api_key, send_welcome_email, pricing_page_html, success_page_html,
    dashboard_page_html, get_db
)

_SAAS_DB = "oblivion_instant"
_SAAS_NAME = "OBLIVION Instant Answers"
_SAAS_PATH = "/instant"
_SAAS_PREFIX = "oblivion_ia"
_SAAS_TIERS = [('Free', '£0', ['Use all 20 tools on website', 'Basic functionality'], '', False), ('API', '£14/mo', ['REST API to all 20 tools', '10,000 requests/day', 'JSON responses', 'All future tools included'], '/instant/checkout/pro', True), ('API Unlimited', '£39/mo', ['Unlimited requests', 'Priority processing', 'Bulk operations', 'Dedicated support'], '/instant/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 1400
_SAAS_BIZ_PRICE = 3900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/instant/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/instant/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/instant/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/instant/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/instant/dashboard")
async def _saas_dashboard(key: str = ""):
    from fastapi.responses import HTMLResponse
    import psycopg2.extras
    account = None
    if key:
        try:
            conn = get_db(_SAAS_DB)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM saas_api_keys WHERE api_key=%s", (key,))
            account = cur.fetchone()
            cur.close(); conn.close()
        except: pass
    return HTMLResponse(dashboard_page_html(_SAAS_NAME, _SAAS_PATH, account, key))

@app.post("/instant/webhook")
async def _saas_webhook(request: Request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

# Wildcard tool route MUST be after all specific /instant/* routes
@app.get("/instant/{tool_name}", response_class=HTMLResponse)
async def instant_tool_page(tool_name: str):
    if tool_name not in TOOLS:
        return HTMLResponse(f"<h1>Tool not found: {tool_name}</h1>", status_code=404)
    return tool_page_html(tool_name)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3063, log_level="info")
