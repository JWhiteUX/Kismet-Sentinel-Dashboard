#!/usr/bin/env python3
"""
Kismet Dashboard - Inspect data, schedule batch saves, monitor alerts & drone sightings.
Usage: python kismet_dashboard.py [--host HOST] [--port PORT]
"""

import json
import os
import time
import threading
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests as req_lib

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_KISMET_URL = os.environ.get("KISMET_URL", "http://localhost:2501")
DEFAULT_API_KEY    = os.environ.get("KISMET_API_KEY", "")
SAVE_DIR           = Path(os.environ.get("KISMET_SAVE_DIR", "./kismet_saves"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kismet_dash")

app = Flask(__name__)
scheduler = BackgroundScheduler(daemon=True)

# ─── In-memory state ──────────────────────────────────────────────────────────

state = {
    "kismet_url": DEFAULT_KISMET_URL,
    "api_key":    DEFAULT_API_KEY,
    "username":   os.environ.get("KISMET_USER", ""),
    "password":   os.environ.get("KISMET_PASS", ""),
    "alerts":     [],           # {ts, type, severity, title, body}
    "schedules":  [],           # {id, name, interval_min, last_run, next_run, enabled}
    "last_save":  None,
    "save_log":   [],           # {ts, file, count, ok}
    "last_error": None,
    "automations": {
        "alert_save_enabled": True,
        "save_device_details": True,
        "save_device_traffic": True,
        "save_watched_only": False,
        "auto_watch_rules": {
            "drone_alerts": True,
            "btle_alerts": True,
            "strong_signal": False,
        },
    },
    "watched_devices": {},  # MAC -> {mac, name, phyname, added_at}
    "alert_saves": [],
}

ALERT_LOCK = threading.Lock()
MAX_ALERTS = 500
DEMO_MODE  = os.environ.get("KISMET_DEMO", "1") == "1"

# ─── Dummy data for UI testing ──────────────────────────────────────────────

import random
import hashlib

def _rand_mac():
    return ":".join(f"{random.randint(0,255):02X}" for _ in range(6))

def _rand_ts(base=None):
    if base is None:
        base = int(time.time())
    return base - random.randint(0, 3600)

DUMMY_DEVICES = [
    # ── Wi-Fi devices ──
    {"kismet_device_base_macaddr": "AA:BB:CC:11:22:33", "kismet_device_base_name": "HomeNetwork_5G",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Netgear",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "36",
     "kismet_device_base_frequency": 5180, "kismet_device_base_packets_total": 48210,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -42, "kismet_common_signal_max_signal": -38, "kismet_common_signal_min_signal": -65}},
    {"kismet_device_base_macaddr": "AA:BB:CC:44:55:66", "kismet_device_base_name": "ASUS_RT-AX86U",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "ASUSTek",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 102847,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -55, "kismet_common_signal_max_signal": -40, "kismet_common_signal_min_signal": -72}},
    {"kismet_device_base_macaddr": "11:22:33:AA:BB:CC", "kismet_device_base_name": "iPhone-Sarah",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Apple",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 8921,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -58, "kismet_common_signal_max_signal": -45, "kismet_common_signal_min_signal": -78}},
    {"kismet_device_base_macaddr": "22:33:44:BB:CC:DD", "kismet_device_base_name": "Galaxy-S24",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Samsung",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "11",
     "kismet_device_base_frequency": 2462, "kismet_device_base_packets_total": 5432,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -67, "kismet_common_signal_max_signal": -52, "kismet_common_signal_min_signal": -85}},
    {"kismet_device_base_macaddr": "33:44:55:CC:DD:EE", "kismet_device_base_name": "Ring-Doorbell",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Amazon",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 12890,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -71, "kismet_common_signal_max_signal": -60, "kismet_common_signal_min_signal": -88}},
    {"kismet_device_base_macaddr": "44:55:66:DD:EE:FF", "kismet_device_base_name": "Nest-Thermostat",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Google",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 3201,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -63, "kismet_common_signal_max_signal": -50, "kismet_common_signal_min_signal": -79}},
    {"kismet_device_base_macaddr": "55:66:77:EE:FF:00", "kismet_device_base_name": "TP-Link_Deco_M5",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "TP-Link",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "44",
     "kismet_device_base_frequency": 5220, "kismet_device_base_packets_total": 67320,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -48, "kismet_common_signal_max_signal": -35, "kismet_common_signal_min_signal": -62}},
    {"kismet_device_base_macaddr": "66:77:88:FF:00:11", "kismet_device_base_name": "Sonos-Living-Room",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Sonos",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "36",
     "kismet_device_base_frequency": 5180, "kismet_device_base_packets_total": 21099,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -52, "kismet_common_signal_max_signal": -41, "kismet_common_signal_min_signal": -70}},
    {"kismet_device_base_macaddr": "77:88:99:00:11:22", "kismet_device_base_name": "",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Intel",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 1572,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -79, "kismet_common_signal_max_signal": -65, "kismet_common_signal_min_signal": -90}},
    {"kismet_device_base_macaddr": "88:99:AA:11:22:33", "kismet_device_base_name": "Xfinity-WiFi",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Comcast",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "11",
     "kismet_device_base_frequency": 2462, "kismet_device_base_packets_total": 89410,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -75, "kismet_common_signal_max_signal": -62, "kismet_common_signal_min_signal": -91}},
    {"kismet_device_base_macaddr": "99:AA:BB:22:33:44", "kismet_device_base_name": "Pixel-8-Pro",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Google",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "36",
     "kismet_device_base_frequency": 5180, "kismet_device_base_packets_total": 4102,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -54, "kismet_common_signal_max_signal": -42, "kismet_common_signal_min_signal": -73}},
    {"kismet_device_base_macaddr": "AA:CC:EE:33:55:77", "kismet_device_base_name": "HP-LaserJet",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "HP",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 921,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -68, "kismet_common_signal_max_signal": -55, "kismet_common_signal_min_signal": -82}},
    # ── Bluetooth devices ──
    {"kismet_device_base_macaddr": "BB:CC:DD:44:55:66", "kismet_device_base_name": "AirPods-Pro",
     "kismet_device_base_phyname": "Bluetooth", "kismet_device_base_manuf": "Apple",
     "kismet_device_base_type": "BR/EDR", "kismet_device_base_channel": "",
     "kismet_device_base_frequency": 2402, "kismet_device_base_packets_total": 3401,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -45, "kismet_common_signal_max_signal": -32, "kismet_common_signal_min_signal": -60}},
    {"kismet_device_base_macaddr": "CC:DD:EE:55:66:77", "kismet_device_base_name": "Tile-Tracker",
     "kismet_device_base_phyname": "BTLE", "kismet_device_base_manuf": "Tile",
     "kismet_device_base_type": "BLE", "kismet_device_base_channel": "",
     "kismet_device_base_frequency": 2426, "kismet_device_base_packets_total": 890,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -72, "kismet_common_signal_max_signal": -58, "kismet_common_signal_min_signal": -85}},
    {"kismet_device_base_macaddr": "DD:EE:FF:66:77:88", "kismet_device_base_name": "Fitbit-Sense2",
     "kismet_device_base_phyname": "BTLE", "kismet_device_base_manuf": "Fitbit",
     "kismet_device_base_type": "BLE", "kismet_device_base_channel": "",
     "kismet_device_base_frequency": 2426, "kismet_device_base_packets_total": 2105,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -61, "kismet_common_signal_max_signal": -48, "kismet_common_signal_min_signal": -77}},
    {"kismet_device_base_macaddr": "EE:FF:00:77:88:99", "kismet_device_base_name": "JBL-Flip6",
     "kismet_device_base_phyname": "Bluetooth", "kismet_device_base_manuf": "Harman",
     "kismet_device_base_type": "BR/EDR", "kismet_device_base_channel": "",
     "kismet_device_base_frequency": 2402, "kismet_device_base_packets_total": 5670,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -50, "kismet_common_signal_max_signal": -38, "kismet_common_signal_min_signal": -66}},
    {"kismet_device_base_macaddr": "FF:00:11:88:99:AA", "kismet_device_base_name": "Apple-Watch-7",
     "kismet_device_base_phyname": "BTLE", "kismet_device_base_manuf": "Apple",
     "kismet_device_base_type": "BLE", "kismet_device_base_channel": "",
     "kismet_device_base_frequency": 2426, "kismet_device_base_packets_total": 7812,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -47, "kismet_common_signal_max_signal": -35, "kismet_common_signal_min_signal": -62}},
    # ── Drone devices (should trigger detection) ──
    {"kismet_device_base_macaddr": "60:60:1F:AA:BB:CC", "kismet_device_base_name": "DJI-Mavic-3-Pro",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "DJI Technology",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "149",
     "kismet_device_base_frequency": 5745, "kismet_device_base_packets_total": 34100,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -38, "kismet_common_signal_max_signal": -28, "kismet_common_signal_min_signal": -55}},
    {"kismet_device_base_macaddr": "90:3A:E6:DD:EE:FF", "kismet_device_base_name": "Parrot-ANAFI",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Parrot SA",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "44",
     "kismet_device_base_frequency": 5220, "kismet_device_base_packets_total": 12450,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -52, "kismet_common_signal_max_signal": -40, "kismet_common_signal_min_signal": -68}},
    {"kismet_device_base_macaddr": "A0:B1:C2:D3:E4:F5", "kismet_device_base_name": "UAV-RemoteID-0x4F2A",
     "kismet_device_base_phyname": "UAV", "kismet_device_base_manuf": "DJI Technology",
     "kismet_device_base_type": "UAV", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 8901,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -41, "kismet_common_signal_max_signal": -30, "kismet_common_signal_min_signal": -58}},
    # ── More Wi-Fi to fill out the list ──
    {"kismet_device_base_macaddr": "11:AA:22:BB:33:CC", "kismet_device_base_name": "Roku-Ultra",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Roku",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "36",
     "kismet_device_base_frequency": 5180, "kismet_device_base_packets_total": 15672,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -56, "kismet_common_signal_max_signal": -44, "kismet_common_signal_min_signal": -71}},
    {"kismet_device_base_macaddr": "22:BB:33:CC:44:DD", "kismet_device_base_name": "Tesla-Model3-WiFi",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Tesla",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 2891,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -82, "kismet_common_signal_max_signal": -70, "kismet_common_signal_min_signal": -93}},
    {"kismet_device_base_macaddr": "33:CC:44:DD:55:EE", "kismet_device_base_name": "Wyze-Cam-v3",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Wyze Labs",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "11",
     "kismet_device_base_frequency": 2462, "kismet_device_base_packets_total": 6789,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -74, "kismet_common_signal_max_signal": -61, "kismet_common_signal_min_signal": -87}},
    {"kismet_device_base_macaddr": "44:DD:55:EE:66:FF", "kismet_device_base_name": "ESP32-Sensor-01",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Espressif",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 430,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -69, "kismet_common_signal_max_signal": -55, "kismet_common_signal_min_signal": -83}},
    {"kismet_device_base_macaddr": "55:EE:66:FF:77:00", "kismet_device_base_name": "UniFi-AP-Pro",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Ubiquiti",
     "kismet_device_base_type": "Wi-Fi AP", "kismet_device_base_channel": "48",
     "kismet_device_base_frequency": 5240, "kismet_device_base_packets_total": 156000,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -35, "kismet_common_signal_max_signal": -25, "kismet_common_signal_min_signal": -52}},
    {"kismet_device_base_macaddr": "66:FF:77:00:88:11", "kismet_device_base_name": "",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Xiaomi",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 1230,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -77, "kismet_common_signal_max_signal": -64, "kismet_common_signal_min_signal": -89}},
    {"kismet_device_base_macaddr": "77:00:88:11:99:22", "kismet_device_base_name": "iPad-Office",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Apple",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "44",
     "kismet_device_base_frequency": 5220, "kismet_device_base_packets_total": 18432,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -46, "kismet_common_signal_max_signal": -36, "kismet_common_signal_min_signal": -63}},
    {"kismet_device_base_macaddr": "88:11:99:22:AA:33", "kismet_device_base_name": "Ecobee-SmartThermostat",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Ecobee",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "6",
     "kismet_device_base_frequency": 2437, "kismet_device_base_packets_total": 2910,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -65, "kismet_common_signal_max_signal": -52, "kismet_common_signal_min_signal": -80}},
    {"kismet_device_base_macaddr": "99:22:AA:33:BB:44", "kismet_device_base_name": "Philips-Hue-Bridge",
     "kismet_device_base_phyname": "IEEE802.11", "kismet_device_base_manuf": "Signify",
     "kismet_device_base_type": "Wi-Fi Client", "kismet_device_base_channel": "1",
     "kismet_device_base_frequency": 2412, "kismet_device_base_packets_total": 4560,
     "kismet_device_base_signal": {"kismet_common_signal_last_signal": -59, "kismet_common_signal_max_signal": -47, "kismet_common_signal_min_signal": -74}},
]

def _stamp_dummy_devices():
    """Add timestamps to dummy devices."""
    now = int(time.time())
    for d in DUMMY_DEVICES:
        d["kismet_device_base_first_time"] = now - random.randint(3600, 86400)
        d["kismet_device_base_last_time"]  = now - random.randint(0, 600)

def _seed_dummy_alerts():
    """Populate initial alerts from dummy device analysis."""
    now = datetime.now()
    alerts = [
        {"ts": (now - __import__('datetime').timedelta(seconds=30)).isoformat(),
         "type": "drone", "severity": "critical",
         "title": "🚁 Drone detected: DJI-Mavic-3-Pro",
         "body": "MAC: 60:60:1F:AA:BB:CC | PHY: IEEE802.11 | Manuf: DJI Technology | Signal: -38 dBm",
         "id": int(time.time() * 1000) - 30000},
        {"ts": (now - __import__('datetime').timedelta(seconds=45)).isoformat(),
         "type": "drone", "severity": "critical",
         "title": "🚁 UAV PHY device: UAV-RemoteID-0x4F2A",
         "body": "MAC: A0:B1:C2:D3:E4:F5 | Manuf: DJI Technology | Signal: -41 dBm",
         "id": int(time.time() * 1000) - 45000},
        {"ts": (now - __import__('datetime').timedelta(seconds=60)).isoformat(),
         "type": "drone", "severity": "critical",
         "title": "🚁 Drone detected: Parrot-ANAFI",
         "body": "MAC: 90:3A:E6:DD:EE:FF | PHY: IEEE802.11 | Manuf: Parrot SA | Signal: -52 dBm",
         "id": int(time.time() * 1000) - 60000},
        {"ts": (now - __import__('datetime').timedelta(seconds=120)).isoformat(),
         "type": "signal", "severity": "warning",
         "title": "📶 Strong signal: DJI-Mavic-3-Pro",
         "body": "MAC: 60:60:1F:AA:BB:CC | Signal: -38 dBm | PHY: IEEE802.11",
         "id": int(time.time() * 1000) - 120000},
        {"ts": (now - __import__('datetime').timedelta(seconds=180)).isoformat(),
         "type": "signal", "severity": "warning",
         "title": "📶 Strong signal: UniFi-AP-Pro",
         "body": "MAC: 55:EE:66:FF:77:00 | Signal: -35 dBm | PHY: IEEE802.11",
         "id": int(time.time() * 1000) - 180000},
        {"ts": (now - __import__('datetime').timedelta(seconds=240)).isoformat(),
         "type": "signal", "severity": "warning",
         "title": "📶 Strong signal: HomeNetwork_5G",
         "body": "MAC: AA:BB:CC:11:22:33 | Signal: -42 dBm | PHY: IEEE802.11",
         "id": int(time.time() * 1000) - 240000},
        {"ts": (now - __import__('datetime').timedelta(seconds=300)).isoformat(),
         "type": "signal", "severity": "warning",
         "title": "📶 Strong signal: AirPods-Pro",
         "body": "MAC: BB:CC:DD:44:55:66 | Signal: -45 dBm | PHY: Bluetooth",
         "id": int(time.time() * 1000) - 300000},
        {"ts": (now - __import__('datetime').timedelta(seconds=500)).isoformat(),
         "type": "kismet", "severity": "info",
         "title": "New SSID detected",
         "body": "SSID: Xfinity-WiFi on channel 11",
         "id": int(time.time() * 1000) - 500000},
        {"ts": (now - __import__('datetime').timedelta(seconds=600)).isoformat(),
         "type": "save", "severity": "info",
         "title": "💾 Batch save complete: 28 devices",
         "body": "kismet_saves/kismet_auto_save_20260224_140000.json",
         "id": int(time.time() * 1000) - 600000},
        {"ts": (now - __import__('datetime').timedelta(seconds=900)).isoformat(),
         "type": "kismet", "severity": "warning",
         "title": "Deauthentication flood detected",
         "body": "Multiple deauth frames targeting HomeNetwork_5G from 77:88:99:00:11:22",
         "id": int(time.time() * 1000) - 900000},
        {"ts": (now - __import__('datetime').timedelta(seconds=1200)).isoformat(),
         "type": "kismet", "severity": "info",
         "title": "Channel hop complete",
         "body": "Scanned 14 channels across 2.4GHz and 5GHz bands",
         "id": int(time.time() * 1000) - 1200000},
    ]
    state["alerts"] = alerts

# ─── Kismet helpers ───────────────────────────────────────────────────────────

def kismet_auth():
    """Return auth tuple if username set, else API key header, else nothing."""
    if state["username"]:
        return None, (state["username"], state["password"])
    if state["api_key"]:
        return {"KISMET": state["api_key"]}, None
    return {}, None

def kismet_get(path, timeout=10):
    url = f"{state['kismet_url']}{path}"
    headers, auth = kismet_auth()
    r = req_lib.get(url, headers=headers or {}, auth=auth, timeout=timeout)
    r.raise_for_status()
    # ekjson returns one JSON object per line, not a JSON array
    if path.endswith(".ekjson"):
        lines = [l.strip() for l in r.text.splitlines() if l.strip()]
        return [json.loads(l) for l in lines]
    return r.json()

def kismet_post(path, payload=None, timeout=10):
    url = f"{state['kismet_url']}{path}"
    headers, auth = kismet_auth()
    r = req_lib.post(url, headers=headers or {}, auth=auth, json=payload or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ─── Drone / alert detection ──────────────────────────────────────────────────

DRONE_KEYWORDS = [
    "dji", "parrot", "yuneec", "autel", "skydio", "bebop", "phantom",
    "mavic", "inspire", "matrice", "tello", "fpv", "drone", "uav",
    "ardupilot", "pixhawk", "droneid"
]

INTERESTING_SIGNAL = -60  # dBm threshold for "strong" signal alerts

def _sanitize_filename(s):
    """Strip non-alphanumeric chars for safe filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:80]

def _save_alert_device(alert_entry, device=None):
    """Save device details/traffic to a file when an alert fires."""
    auto = state["automations"]
    if not auto["alert_save_enabled"]:
        return
    if not auto["save_device_details"] and not auto["save_device_traffic"]:
        return

    # Check watchlist filter
    if auto["save_watched_only"] and device:
        mac = device.get("kismet_device_base_macaddr", "")
        if mac not in state["watched_devices"]:
            return

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    dev_name = ""
    if device:
        dev_name = device.get("kismet_device_base_name", "") or device.get("kismet_device_base_macaddr", "unknown")
    alert_type = alert_entry.get("type", "alert")
    filename = f"alert_{_sanitize_filename(alert_type)}_{_sanitize_filename(dev_name)}_{ts_str}.json"
    out_file = SAVE_DIR / filename

    payload = {
        "saved_at": ts_str,
        "alert": alert_entry,
    }

    if auto["save_device_details"] and device:
        payload["device"] = {
            "mac": device.get("kismet_device_base_macaddr"),
            "name": device.get("kismet_device_base_name"),
            "phyname": device.get("kismet_device_base_phyname"),
            "manuf": device.get("kismet_device_base_manuf"),
            "type": device.get("kismet_device_base_type"),
            "channel": device.get("kismet_device_base_channel"),
            "frequency": device.get("kismet_device_base_frequency"),
            "signal": device.get("kismet_device_base_signal"),
            "first_time": device.get("kismet_device_base_first_time"),
            "last_time": device.get("kismet_device_base_last_time"),
            "packets_total": device.get("kismet_device_base_packets_total"),
        }

    if auto["save_device_traffic"] and device:
        payload["traffic"] = {
            "packets_total": device.get("kismet_device_base_packets_total"),
            "packets_data": device.get("kismet_device_base_packets_data"),
            "packets_crypt": device.get("kismet_device_base_crypt"),
            "datasize": device.get("kismet_device_base_datasize"),
            "raw": {k: v for k, v in device.items()
                    if "packet" in k.lower() or "data" in k.lower() or "crypt" in k.lower()},
        }

    try:
        with open(out_file, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        state["alert_saves"].insert(0, {
            "ts": ts_str, "file": str(out_file),
            "alert_type": alert_type, "device": dev_name, "ok": True,
        })
        state["alert_saves"] = state["alert_saves"][:100]
        log.info(f"Alert save: {filename}")
    except Exception as e:
        log.error(f"Alert save failed: {e}")
        state["alert_saves"].insert(0, {
            "ts": ts_str, "file": str(out_file),
            "alert_type": alert_type, "device": dev_name,
            "ok": False, "error": str(e),
        })

def _auto_watch_device(atype, device):
    """Add device to watchlist automatically based on alert type and rules."""
    if device is None:
        return
    rules = state["automations"].get("auto_watch_rules", {})
    mac = device.get("kismet_device_base_macaddr", "")
    if not mac or mac in state["watched_devices"]:
        return

    phy = device.get("kismet_device_base_phyname", "")
    should_watch = False

    if rules.get("drone_alerts") and atype == "drone":
        should_watch = True
    if rules.get("btle_alerts") and atype in ("signal", "kismet") and phy in ("BTLE", "Bluetooth"):
        should_watch = True
    if rules.get("strong_signal") and atype == "signal":
        should_watch = True

    if should_watch:
        name = device.get("kismet_device_base_name", "") or mac
        state["watched_devices"][mac] = {
            "mac": mac,
            "name": name,
            "phyname": phy,
            "added_at": datetime.now().isoformat(),
            "auto": True,
        }
        log.info(f"Auto-watched device: {name} ({mac}) — triggered by {atype} alert")

def push_alert(atype, severity, title, body, device=None):
    with ALERT_LOCK:
        alert_entry = {
            "ts":       datetime.now().isoformat(),
            "type":     atype,
            "severity": severity,
            "title":    title,
            "body":     body,
            "id":       int(time.time() * 1000),
        }
        state["alerts"].insert(0, alert_entry)
        if len(state["alerts"]) > MAX_ALERTS:
            state["alerts"].pop()

    # Auto-watch check (outside lock)
    _auto_watch_device(atype, device)

    # Trigger automation save (outside lock)
    if device is not None:
        threading.Thread(target=_save_alert_device, args=(alert_entry, device), daemon=True).start()

def analyze_devices(devices):
    if not isinstance(devices, list):
        return
    for dev in devices:
        try:
            ssid_map   = dev.get("kismet_device_base_name", "")
            mac        = dev.get("kismet_device_base_macaddr", "")
            phy        = dev.get("kismet_device_base_phyname", "")
            signal     = dev.get("kismet_device_base_signal", {})
            last_signal = signal.get("kismet_common_signal_last_signal", -100)
            manuf      = dev.get("kismet_device_base_manuf", "")

            # Drone keyword match in SSID or manuf
            combined = (ssid_map + " " + manuf).lower()
            for kw in DRONE_KEYWORDS:
                if kw in combined:
                    push_alert(
                        "drone", "critical",
                        f"🚁 Drone detected: {ssid_map or mac}",
                        f"MAC: {mac} | PHY: {phy} | Manuf: {manuf} | Signal: {last_signal} dBm | Keyword matched: '{kw}'",
                        device=dev
                    )
                    break

            # UAV PHY sighting
            if phy == "UAV":
                push_alert(
                    "drone", "critical",
                    f"🚁 UAV PHY device: {ssid_map or mac}",
                    f"MAC: {mac} | Manuf: {manuf} | Signal: {last_signal} dBm",
                    device=dev
                )

            # Unusually strong signal
            if last_signal > INTERESTING_SIGNAL:
                push_alert(
                    "signal", "warning",
                    f"📶 Strong signal: {ssid_map or mac}",
                    f"MAC: {mac} | Signal: {last_signal} dBm | PHY: {phy}",
                    device=dev
                )

        except Exception:
            pass

def poll_kismet_alerts():
    """Pull Kismet's own alert feed and mirror into our alert list."""
    try:
        data = kismet_get("/alerts/all_alerts.json")
        if isinstance(data, list):
            for a in data[-20:]:  # last 20
                push_alert(
                    "kismet",
                    "warning" if a.get("kismet.alert.severity", 5) < 10 else "info",
                    a.get("kismet.alert.header", "Kismet Alert"),
                    a.get("kismet.alert.text", "")
                )
    except Exception as e:
        push_alert("error", "error", "Kismet alert poll failed", str(e))

# ─── Batch save ───────────────────────────────────────────────────────────────

def do_save(label="scheduled"):
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = SAVE_DIR / f"kismet_{label}_{ts}.json"
    try:
        devices = kismet_get("/devices/all_devices.ekjson")
        analyze_devices(devices)
        count = len(devices) if isinstance(devices, list) else 0
        with open(out_file, "w") as f:
            json.dump({"ts": ts, "devices": devices}, f, indent=2)
        state["last_save"] = ts
        state["save_log"].insert(0, {"ts": ts, "file": str(out_file), "count": count, "ok": True})
        state["save_log"] = state["save_log"][:50]
        log.info(f"Saved {count} devices to {out_file}")
        push_alert("save", "info", f"💾 Batch save complete: {count} devices", str(out_file))
    except Exception as e:
        err = str(e)
        state["last_error"] = err
        state["save_log"].insert(0, {"ts": ts, "file": str(out_file), "count": 0, "ok": False, "error": err})
        push_alert("error", "error", "Batch save failed", err)
        log.error(f"Save failed: {e}")

# ─── Scheduler management ─────────────────────────────────────────────────────

def add_schedule(name, interval_min):
    job_id = f"save_{int(time.time())}"
    scheduler.add_job(
        func=lambda: do_save(name.replace(" ", "_").lower()),
        trigger="interval",
        minutes=interval_min,
        id=job_id,
        replace_existing=True
    )
    entry = {
        "id":           job_id,
        "name":         name,
        "interval_min": interval_min,
        "enabled":      True,
        "created":      datetime.now().isoformat(),
    }
    state["schedules"].append(entry)
    return entry

def remove_schedule(job_id):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    state["schedules"] = [s for s in state["schedules"] if s["id"] != job_id]

# ─── Routes: API ──────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.json or {}
        if "kismet_url" in data:
            state["kismet_url"] = data["kismet_url"].rstrip("/")
        if "api_key" in data:
            state["api_key"] = data["api_key"]
        if "username" in data:
            state["username"] = data["username"]
        if "password" in data:
            state["password"] = data["password"]
        return jsonify({"ok": True})
    return jsonify({
        "kismet_url": state["kismet_url"],
        "api_key":    "***" if state["api_key"] else "",
        "username":   state["username"],
    })

@app.route("/api/status")
def api_status():
    try:
        status = kismet_get("/system/status.json")
        return jsonify({"ok": True, "data": status})
    except Exception:
        if DEMO_MODE:
            return jsonify({"ok": True, "data": {
                "kismet.system.version": "demo",
                "kismet.system.devices.count": len(DUMMY_DEVICES),
            }})
        return jsonify({"ok": False, "error": "Kismet not available"}), 200

@app.route("/api/devices")
def api_devices():
    try:
        last_ts = request.args.get("since", "0")
        if last_ts != "0":
            data = kismet_get(f"/devices/last-time/{last_ts}/devices.ekjson")
        else:
            data = kismet_get("/devices/all_devices.ekjson")
        analyze_devices(data)
        return jsonify({"ok": True, "devices": data, "ts": int(time.time())})
    except Exception:
        if DEMO_MODE:
            return jsonify({"ok": True, "devices": DUMMY_DEVICES, "ts": int(time.time())})
        return jsonify({"ok": False, "error": "Kismet not available"}), 200

@app.route("/api/ssids")
def api_ssids():
    try:
        data = kismet_get("/phy/phy80211/ssids/views/ssids.json")
        return jsonify({"ok": True, "ssids": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

@app.route("/api/alerts")
def api_alerts():
    severity = request.args.get("severity")
    atype    = request.args.get("type")
    limit    = int(request.args.get("limit", 100))
    alerts   = state["alerts"]
    if severity:
        alerts = [a for a in alerts if a["severity"] == severity]
    if atype:
        alerts = [a for a in alerts if a["type"] == atype]
    return jsonify({"alerts": alerts[:limit]})

@app.route("/api/alerts/clear", methods=["POST"])
def api_alerts_clear():
    with ALERT_LOCK:
        state["alerts"].clear()
    return jsonify({"ok": True})

@app.route("/api/alerts/poll", methods=["POST"])
def api_alerts_poll():
    threading.Thread(target=poll_kismet_alerts, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/save", methods=["POST"])
def api_save():
    label = (request.json or {}).get("label", "manual")
    threading.Thread(target=do_save, args=(label,), daemon=True).start()
    return jsonify({"ok": True, "message": "Save started"})

@app.route("/api/save/log")
def api_save_log():
    return jsonify({"log": state["save_log"]})

@app.route("/api/schedules", methods=["GET"])
def api_schedules_get():
    return jsonify({"schedules": state["schedules"]})

@app.route("/api/schedules", methods=["POST"])
def api_schedules_add():
    data = request.json or {}
    name     = data.get("name", "Auto Save")
    interval = int(data.get("interval_min", 30))
    entry = add_schedule(name, interval)
    return jsonify({"ok": True, "schedule": entry})

@app.route("/api/schedules/<job_id>", methods=["DELETE"])
def api_schedules_delete(job_id):
    remove_schedule(job_id)
    return jsonify({"ok": True})

@app.route("/api/automations", methods=["GET", "POST"])
def api_automations():
    if request.method == "POST":
        data = request.json or {}
        for k in ("alert_save_enabled", "save_device_details", "save_device_traffic", "save_watched_only"):
            if k in data:
                state["automations"][k] = bool(data[k])
        if "auto_watch_rules" in data and isinstance(data["auto_watch_rules"], dict):
            for k in ("drone_alerts", "btle_alerts", "strong_signal"):
                if k in data["auto_watch_rules"]:
                    state["automations"]["auto_watch_rules"][k] = bool(data["auto_watch_rules"][k])
        return jsonify({"ok": True, "automations": state["automations"]})
    return jsonify({"automations": state["automations"],
                     "watched_devices": list(state["watched_devices"].values())})

@app.route("/api/automations/saves")
def api_automation_saves():
    return jsonify({"saves": state["alert_saves"]})

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify({"devices": list(state["watched_devices"].values())})

@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    data = request.json or {}
    mac = data.get("mac", "").upper()
    if not mac:
        return jsonify({"ok": False, "error": "MAC required"}), 400
    state["watched_devices"][mac] = {
        "mac": mac,
        "name": data.get("name", ""),
        "phyname": data.get("phyname", ""),
        "added_at": datetime.now().isoformat(),
    }
    return jsonify({"ok": True, "watched": len(state["watched_devices"])})

@app.route("/api/watchlist/<mac>", methods=["DELETE"])
def api_watchlist_remove(mac):
    mac = mac.upper()
    state["watched_devices"].pop(mac, None)
    return jsonify({"ok": True, "watched": len(state["watched_devices"])})

# ─── Main UI ──────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KISMET SENTINEL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #090c0f;
    --surface:   #0d1117;
    --border:    #1a2535;
    --border-hi: #243447;
    --amber:     #f0a500;
    --amber-dim: #a06800;
    --green:     #00e676;
    --green-dim: #007a3f;
    --red:       #ff3b3b;
    --red-dim:   #7a1a1a;
    --cyan:      #00bcd4;
    --text:      #c8d8e8;
    --text-dim:  #5a7a9a;
    --drone:     #ff6b35;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; background: var(--bg); color: var(--text); font-family: 'Barlow Condensed', sans-serif; overflow: hidden; }

  /* scanline effect */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: 9999; pointer-events: none;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.08) 2px, rgba(0,0,0,0.08) 4px);
  }

  #app { display: grid; grid-template-rows: 48px 1fr; height: 100vh; }

  /* Header */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--amber-dim);
    display: flex; align-items: center; gap: 20px; padding: 0 20px;
    position: relative; overflow: hidden;
  }
  header::after {
    content: '';
    position: absolute; bottom: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--amber), transparent);
    animation: scan 3s linear infinite;
  }
  @keyframes scan { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

  .logo {
    font-family: 'Share Tech Mono', monospace;
    font-size: 18px; letter-spacing: 4px;
    color: var(--amber);
    text-shadow: 0 0 20px rgba(240,165,0,0.5);
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--red); }
  .status-dot.online { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  .header-stats { display: flex; gap: 24px; margin-left: auto; font-size: 13px; }
  .stat-item { display: flex; flex-direction: column; align-items: flex-end; }
  .stat-label { color: var(--text-dim); font-size: 10px; letter-spacing: 2px; }
  .stat-value { color: var(--amber); font-family: 'Share Tech Mono', monospace; font-size: 14px; }

  #conn-btn {
    background: transparent; border: 1px solid var(--border-hi); color: var(--text-dim);
    padding: 4px 12px; font-family: 'Barlow Condensed', sans-serif; font-size: 13px;
    cursor: pointer; letter-spacing: 1px;
    transition: all 0.2s;
  }
  #conn-btn:hover { border-color: var(--amber); color: var(--amber); }

  /* Nav tabs */
  nav {
    display: flex; gap: 2px; margin-left: 20px;
  }
  .tab {
    padding: 6px 16px; font-size: 13px; letter-spacing: 2px; cursor: pointer;
    color: var(--text-dim); background: transparent; border: none;
    border-bottom: 2px solid transparent;
    font-family: 'Barlow Condensed', sans-serif;
    transition: all 0.15s;
    position: relative;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--amber); border-bottom-color: var(--amber); }
  .tab .badge {
    position: absolute; top: 2px; right: 4px;
    background: var(--red); color: #fff;
    font-size: 9px; padding: 1px 4px; border-radius: 8px;
    min-width: 16px; text-align: center;
  }
  .tab .badge.drone-badge { background: var(--drone); }

  /* Main layout */
  main { display: grid; overflow: hidden; }
  .panel { display: none; height: 100%; overflow: hidden; flex-direction: column; }
  .panel.active { display: flex; }

  /* ── Devices Panel ── */
  #panel-devices { display: none; grid-template-columns: 1fr 320px; gap: 0; }
  #panel-devices.active { display: grid; }

  .device-list-wrap { display: flex; flex-direction: column; overflow: hidden; border-right: 1px solid var(--border); }
  .toolbar {
    padding: 10px 16px; display: flex; gap: 10px; align-items: center;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .search-input {
    flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 6px 12px; font-family: 'Share Tech Mono', monospace; font-size: 12px;
  }
  .search-input:focus { outline: none; border-color: var(--amber); }
  .filter-btn {
    padding: 6px 12px; border: 1px solid var(--border); background: transparent;
    color: var(--text-dim); font-family: 'Barlow Condensed', sans-serif; font-size: 12px;
    cursor: pointer; letter-spacing: 1px; transition: all 0.15s;
  }
  .filter-btn:hover, .filter-btn.active { border-color: var(--amber); color: var(--amber); }
  .filter-btn.drone-filter.active { border-color: var(--drone); color: var(--drone); }
  .btn-sm {
    padding: 6px 14px; border: 1px solid var(--border-hi); background: transparent;
    color: var(--text); font-family: 'Barlow Condensed', sans-serif; font-size: 12px;
    cursor: pointer; letter-spacing: 1px; transition: all 0.15s;
  }
  .btn-sm:hover { border-color: var(--cyan); color: var(--cyan); }
  .btn-sm.primary { border-color: var(--amber); color: var(--amber); }
  .btn-sm.danger { border-color: var(--red); color: var(--red); }

  .device-table-wrap { flex: 1; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead { position: sticky; top: 0; background: var(--surface); z-index: 2; }
  th { padding: 8px 12px; text-align: left; font-size: 10px; letter-spacing: 2px; color: var(--text-dim); font-weight: 400; border-bottom: 1px solid var(--border); }
  td { padding: 7px 12px; border-bottom: 1px solid rgba(26,37,53,0.5); font-family: 'Share Tech Mono', monospace; font-size: 11px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr:hover td { background: rgba(240,165,0,0.04); cursor: pointer; }
  tr.selected td { background: rgba(240,165,0,0.08); }
  tr.drone-row td { color: var(--drone); }
  .signal-bar { display: inline-block; width: 6px; border-radius: 2px; vertical-align: middle; margin-right: 2px; }
  .phy-tag { font-size: 9px; padding: 2px 6px; border-radius: 2px; }
  .phy-80211 { background: rgba(0,188,212,0.15); color: var(--cyan); }
  .phy-bt    { background: rgba(0,230,118,0.15); color: var(--green); }
  .phy-uav   { background: rgba(255,107,53,0.15); color: var(--drone); }
  .phy-other { background: rgba(90,122,154,0.15); color: var(--text-dim); }
  .watched-indicator { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--cyan); margin-right: 4px; vertical-align: middle; flex-shrink: 0; }

  /* Toggle switch */
  .toggle { width: 36px; height: 20px; border-radius: 10px; border: 1px solid var(--border-hi); background: var(--bg); cursor: pointer; position: relative; transition: all 0.2s; padding: 0; flex-shrink: 0; }
  .toggle::after { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: var(--text-dim); transition: all 0.2s; }
  .toggle.active { background: rgba(240,165,0,0.2); border-color: var(--amber); }
  .toggle.active::after { left: 18px; background: var(--amber); }

  /* Device detail sidebar */
  .device-detail {
    padding: 0; overflow-y: auto; background: var(--surface);
    font-size: 12px;
  }
  .detail-header { padding: 16px; border-bottom: 1px solid var(--border); }
  .detail-mac { font-family: 'Share Tech Mono', monospace; font-size: 14px; color: var(--amber); }
  .detail-name { font-size: 16px; font-weight: 600; margin-top: 4px; }
  .detail-section { padding: 12px 16px; border-bottom: 1px solid rgba(26,37,53,0.5); }
  .detail-section h4 { font-size: 10px; letter-spacing: 2px; color: var(--text-dim); margin-bottom: 8px; }
  .kv { display: flex; justify-content: space-between; padding: 2px 0; }
  .kv .k { color: var(--text-dim); font-size: 11px; }
  .kv .v { font-family: 'Share Tech Mono', monospace; font-size: 11px; color: var(--text); }
  .empty-detail { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--text-dim); gap: 8px; }
  .empty-detail .icon { font-size: 32px; opacity: 0.3; }

  /* ── Alerts Panel ── */
  #panel-alerts { overflow: hidden; }
  .alerts-toolbar { padding: 10px 16px; display: flex; gap: 10px; align-items: center; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
  .alert-filters { display: flex; gap: 6px; flex: 1; }
  .alerts-scroll { flex: 1; overflow-y: auto; padding: 0; }
  .alert-item {
    display: grid; grid-template-columns: 140px 80px 1fr;
    gap: 16px; padding: 12px 16px; border-bottom: 1px solid rgba(26,37,53,0.4);
    transition: background 0.1s; cursor: default;
    animation: slideIn 0.2s ease;
  }
  @keyframes slideIn { from { opacity: 0; transform: translateX(-8px); } to { opacity: 1; transform: none; } }
  .alert-item:hover { background: rgba(255,255,255,0.02); }
  .alert-ts { font-family: 'Share Tech Mono', monospace; font-size: 10px; color: var(--text-dim); }
  .sev-badge { font-size: 10px; padding: 2px 8px; border-radius: 2px; text-align: center; align-self: start; letter-spacing: 1px; }
  .sev-critical { background: rgba(255,107,53,0.2); color: var(--drone); border: 1px solid var(--drone); }
  .sev-error    { background: rgba(255,59,59,0.2);  color: var(--red);   border: 1px solid var(--red); }
  .sev-warning  { background: rgba(240,165,0,0.15); color: var(--amber); border: 1px solid var(--amber-dim); }
  .sev-info     { background: rgba(0,188,212,0.1);  color: var(--cyan);  border: 1px solid rgba(0,188,212,0.3); }
  .alert-content .title { font-size: 13px; font-weight: 600; margin-bottom: 3px; }
  .alert-content .body  { font-size: 11px; color: var(--text-dim); font-family: 'Share Tech Mono', monospace; }
  .no-alerts { display: flex; flex-direction: column; align-items: center; justify-content: center; flex: 1; color: var(--text-dim); gap: 8px; }

  /* ── Saves Panel ── */
  #panel-saves { overflow: hidden; }
  .saves-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 0; flex: 1; overflow: hidden; }
  .saves-col { display: flex; flex-direction: column; overflow: hidden; }
  .saves-col + .saves-col { border-left: 1px solid var(--border); }
  .col-header { padding: 12px 16px; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
  .col-header h3 { font-size: 11px; letter-spacing: 2px; color: var(--text-dim); }
  .col-body { flex: 1; overflow-y: auto; padding: 16px; }
  .schedule-item {
    background: var(--bg); border: 1px solid var(--border);
    padding: 12px 16px; margin-bottom: 8px;
    display: flex; align-items: center; gap: 12px;
  }
  .sched-info { flex: 1; }
  .sched-name { font-size: 14px; font-weight: 600; }
  .sched-meta { font-size: 11px; color: var(--text-dim); font-family: 'Share Tech Mono', monospace; margin-top: 2px; }
  .del-btn { background: transparent; border: 1px solid var(--red-dim); color: var(--red); font-size: 11px; padding: 4px 8px; cursor: pointer; transition: all 0.15s; }
  .del-btn:hover { border-color: var(--red); background: rgba(255,59,59,0.1); }

  .add-schedule-form { background: var(--bg); border: 1px solid var(--border); padding: 16px; margin-bottom: 16px; }
  .form-row { display: flex; gap: 10px; margin-bottom: 10px; align-items: flex-end; }
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-group label { font-size: 10px; letter-spacing: 2px; color: var(--text-dim); }
  .form-input {
    background: var(--surface); border: 1px solid var(--border); color: var(--text);
    padding: 6px 10px; font-family: 'Share Tech Mono', monospace; font-size: 12px; width: 100%;
  }
  .form-input:focus { outline: none; border-color: var(--amber); }
  .save-log-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; border-bottom: 1px solid rgba(26,37,53,0.4);
    font-size: 11px;
  }
  .save-log-item .log-ts { font-family: 'Share Tech Mono', monospace; color: var(--text-dim); font-size: 10px; }
  .save-log-item .log-file { font-family: 'Share Tech Mono', monospace; font-size: 10px; color: var(--cyan); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .log-ok { color: var(--green); } .log-fail { color: var(--red); }

  /* ── Settings Panel ── */
  #panel-settings { overflow-y: auto; }
  .settings-wrap { max-width: 560px; padding: 24px; }
  .settings-section { margin-bottom: 32px; }
  .settings-section h3 { font-size: 11px; letter-spacing: 3px; color: var(--text-dim); border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 16px; }
  .setting-row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
  .setting-row label { font-size: 12px; letter-spacing: 1px; color: var(--text); }
  .setting-row .hint { font-size: 11px; color: var(--text-dim); }
  .save-btn { padding: 8px 20px; border: 1px solid var(--amber); background: rgba(240,165,0,0.1); color: var(--amber); font-family: 'Barlow Condensed', sans-serif; font-size: 13px; letter-spacing: 2px; cursor: pointer; transition: all 0.2s; }
  .save-btn:hover { background: rgba(240,165,0,0.2); }

  /* scrollbars */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 2px; }

  /* connection modal */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8); z-index: 100; display: flex; align-items: center; justify-content: center; display: none; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border-hi); padding: 24px; width: 480px; }
  .modal h2 { font-size: 14px; letter-spacing: 3px; color: var(--amber); margin-bottom: 20px; }
  .toast { position: fixed; bottom: 20px; right: 20px; background: var(--surface); border: 1px solid var(--border-hi); padding: 10px 18px; font-size: 13px; z-index: 200; animation: fadeToast 3s forwards; pointer-events: none; }
  @keyframes fadeToast { 0%,80% { opacity: 1; } 100% { opacity: 0; } }
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="logo">KISMET SENTINEL</div>
    <div class="status-dot" id="status-dot"></div>
    <span id="status-text" style="font-size:11px;color:var(--text-dim);letter-spacing:1px;">OFFLINE</span>
    <nav>
      <button class="tab active" data-panel="devices">DEVICES</button>
      <button class="tab" data-panel="alerts" id="tab-alerts">ALERTS <span class="badge" id="alert-badge" style="display:none">0</span></button>
      <button class="tab" data-panel="saves">SAVES</button>
      <button class="tab" data-panel="settings">CONFIG</button>
    </nav>
    <div class="header-stats">
      <div class="stat-item"><span class="stat-label">DEVICES</span><span class="stat-value" id="hdr-devices">—</span></div>
      <div class="stat-item"><span class="stat-label">DRONES</span><span class="stat-value" id="hdr-drones" style="color:var(--drone)">—</span></div>
      <div class="stat-item"><span class="stat-label">LAST SAVE</span><span class="stat-value" id="hdr-save" style="font-size:11px">—</span></div>
    </div>
    <button class="btn-sm" id="conn-btn">⚙ CONNECT</button>
  </header>

  <main>
    <!-- Devices -->
    <div class="panel" id="panel-devices">
      <div class="device-list-wrap">
        <div class="toolbar">
          <input class="search-input" id="dev-search" placeholder="filter mac / ssid / manuf..." />
          <button class="filter-btn active" data-phy="ALL">ALL</button>
          <button class="filter-btn" data-phy="IEEE802.11">802.11</button>
          <button class="filter-btn" data-phy="Bluetooth">BT</button>
          <button class="filter-btn drone-filter" data-phy="UAV">🚁 DRONE</button>
          <button class="btn-sm" id="refresh-btn">↻ REFRESH</button>
          <span id="dev-count" style="font-size:11px;color:var(--text-dim);white-space:nowrap">0 devices</span>
        </div>
        <div class="device-table-wrap">
          <table id="dev-table">
            <thead><tr>
              <th>MAC</th><th>SSID / NAME</th><th>PHY</th><th>MANUF</th><th>SIGNAL</th><th>LAST SEEN</th>
            </tr></thead>
            <tbody id="dev-tbody"></tbody>
          </table>
        </div>
      </div>
      <div class="device-detail" id="dev-detail">
        <div class="empty-detail">
          <div class="icon">⬛</div>
          <div style="font-size:12px;letter-spacing:2px;">SELECT A DEVICE</div>
        </div>
      </div>
    </div>

    <!-- Alerts -->
    <div class="panel" id="panel-alerts">
      <div class="alerts-toolbar">
        <div class="alert-filters">
          <button class="filter-btn active" data-sev="ALL">ALL</button>
          <button class="filter-btn drone-filter" data-sev="critical">🚁 DRONE</button>
          <button class="filter-btn" data-sev="error">ERROR</button>
          <button class="filter-btn" data-sev="warning">WARN</button>
          <button class="filter-btn" data-sev="info">INFO</button>
        </div>
        <button class="btn-sm" id="poll-alerts-btn">↻ POLL KISMET</button>
        <button class="btn-sm danger" id="clear-alerts-btn">✕ CLEAR</button>
      </div>
      <div class="alerts-scroll" id="alerts-list">
        <div class="no-alerts"><div style="font-size:28px;opacity:0.3">◎</div><div style="font-size:12px;letter-spacing:2px;">NO ALERTS</div></div>
      </div>
    </div>

    <!-- Saves -->
    <div class="panel" id="panel-saves">
      <div class="saves-layout">
        <div class="saves-col">
          <div class="col-header"><h3>SCHEDULES</h3></div>
          <div class="col-body">
            <div class="add-schedule-form">
              <div class="form-row">
                <div class="form-group" style="flex:1">
                  <label>SCHEDULE NAME</label>
                  <input class="form-input" id="sched-name" value="Auto Save" />
                </div>
                <div class="form-group" style="width:100px">
                  <label>INTERVAL (MIN)</label>
                  <input class="form-input" id="sched-interval" type="number" value="30" min="1" />
                </div>
                <button class="btn-sm primary" id="add-sched-btn">+ ADD</button>
              </div>
            </div>
            <div id="schedules-list"></div>
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
              <button class="btn-sm primary" id="manual-save-btn" style="width:100%">💾 SAVE NOW</button>
            </div>
          </div>
        </div>
        <div class="saves-col">
          <div class="col-header"><h3>SAVE LOG</h3></div>
          <div class="col-body" style="padding:0">
            <div id="save-log-list"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Settings -->
    <div class="panel" id="panel-settings">
      <div class="settings-wrap">
        <div class="settings-section">
          <h3>KISMET CONNECTION</h3>
          <div class="setting-row">
            <label>KISMET URL</label>
            <input class="form-input" id="cfg-url" value="http://localhost:2501" />
            <div class="hint">Base URL of your Kismet server</div>
          </div>
          <div class="setting-row">
            <label>USERNAME</label>
            <input class="form-input" id="cfg-user" placeholder="kismet username..." />
            <div class="hint">Use username + password OR API key below, not both</div>
          </div>
          <div class="setting-row">
            <label>PASSWORD</label>
            <input class="form-input" id="cfg-pass" type="password" placeholder="kismet password..." />
          </div>
          <div class="setting-row">
            <label>API KEY <span style="color:var(--text-dim);font-size:10px">(optional, overrides user/pass)</span></label>
            <input class="form-input" id="cfg-key" type="password" placeholder="paste API key..." />
            <div class="hint">Generate in Kismet → username → API Tokens</div>
          </div>
          <div style="display:flex;gap:10px">
            <button class="save-btn" id="save-cfg-btn">SAVE &amp; CONNECT</button>
            <button class="btn-sm" id="test-conn-btn">TEST</button>
          </div>
          <div id="conn-result" style="margin-top:12px;font-size:12px;font-family:'Share Tech Mono',monospace;color:var(--text-dim)"></div>
        </div>
        <div class="settings-section">
          <h3>AUTOMATIONS</h3>

          <!-- Master toggle -->
          <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)">
            <div>
              <div style="font-size:13px;font-weight:600;letter-spacing:1px">SAVE ON ALERT</div>
              <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Automatically save device data when an alert is triggered</div>
            </div>
            <button class="toggle active" id="toggle-alert-save" onclick="toggleAutomation(this,'alert_save_enabled')"></button>
          </div>

          <div id="alert-save-options" style="margin-top:8px">
            <!-- Data toggles -->
            <div style="padding-left:16px">
              <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
                <div>
                  <div style="font-size:12px;font-weight:600;letter-spacing:1px">DEVICE DETAILS</div>
                  <div style="font-size:11px;color:var(--text-dim);margin-top:2px">MAC, PHY, manufacturer, signal, channel info</div>
                </div>
                <button class="toggle active" id="toggle-device-details" onclick="toggleAutomation(this,'save_device_details')"></button>
              </div>
              <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
                <div>
                  <div style="font-size:12px;font-weight:600;letter-spacing:1px">DEVICE TRAFFIC</div>
                  <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Packet counts, data size, encryption info</div>
                </div>
                <button class="toggle active" id="toggle-device-traffic" onclick="toggleAutomation(this,'save_device_traffic')"></button>
              </div>
            </div>

            <!-- Save watched only toggle -->
            <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
              <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;margin-bottom:8px">
                <div>
                  <div style="font-size:12px;font-weight:600;letter-spacing:1px">SAVE WATCHED ONLY</div>
                  <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Only save device data for devices in the watchlist</div>
                </div>
                <button class="toggle" id="toggle-watched-only" onclick="toggleAutomation(this,'save_watched_only')"></button>
              </div>

              <!-- Auto-watch rules -->
              <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
                <div style="font-size:12px;font-weight:600;letter-spacing:1px;margin-bottom:4px">AUTO-WATCH RULES</div>
                <div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">Automatically add devices to the watchlist when they trigger these alert types.</div>
                <div style="display:flex;flex-direction:column;gap:2px;padding-left:8px">
                  <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
                    <div>
                      <div style="font-size:12px;font-weight:600;letter-spacing:1px">DRONE ALERTS</div>
                      <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Watch devices detected as drones or UAVs</div>
                    </div>
                    <button class="toggle active" id="toggle-aw-drone" onclick="toggleAutoWatch(this,'drone_alerts')"></button>
                  </div>
                  <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
                    <div>
                      <div style="font-size:12px;font-weight:600;letter-spacing:1px">BLUETOOTH / BTLE ALERTS</div>
                      <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Watch Bluetooth and BTLE devices that trigger alerts</div>
                    </div>
                    <button class="toggle active" id="toggle-aw-btle" onclick="toggleAutoWatch(this,'btle_alerts')"></button>
                  </div>
                  <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
                    <div>
                      <div style="font-size:12px;font-weight:600;letter-spacing:1px">STRONG SIGNAL ALERTS</div>
                      <div style="font-size:11px;color:var(--text-dim);margin-top:2px">Watch any device with signal above -60 dBm</div>
                    </div>
                    <button class="toggle" id="toggle-aw-signal" onclick="toggleAutoWatch(this,'strong_signal')"></button>
                  </div>
                </div>
              </div>

              <!-- Watched devices list -->
              <div id="watched-devices-section" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                  <div style="font-size:12px;font-weight:600;letter-spacing:1px">WATCHED DEVICES</div>
                  <span id="watched-count" style="font-size:10px;color:var(--text-dim);letter-spacing:2px">0 DEVICES</span>
                </div>
                <div id="watched-list"></div>
                <div style="font-size:11px;color:var(--text-dim);margin-top:8px">Devices are added automatically by rules above, or manually from the device detail panel.</div>
              </div>
            </div>

            <div style="margin-top:12px;font-size:10px;color:var(--text-dim);font-family:'Share Tech Mono',monospace">
              Files saved to: kismet_saves/alert_&lt;type&gt;_&lt;device&gt;_&lt;timestamp&gt;.json
            </div>
          </div>
        </div>
        <div class="settings-section">
          <h3>DRONE DETECTION KEYWORDS</h3>
          <div style="font-size:12px;color:var(--text-dim);font-family:'Share Tech Mono',monospace;line-height:1.8">
            dji · parrot · yuneec · autel · skydio · bebop · phantom · mavic · inspire · matrice · tello · fpv · drone · uav · ardupilot · pixhawk · droneid
          </div>
          <div style="margin-top:8px;font-size:11px;color:var(--text-dim)">
            Also flags any device on the UAV PHY regardless of SSID/manufacturer.
          </div>
        </div>
      </div>
    </div>
  </main>
</div>

<!-- Connection modal -->
<div class="modal-overlay" id="conn-modal">
  <div class="modal">
    <h2>⚙ KISMET CONNECTION</h2>
    <div class="setting-row">
      <label>URL</label>
      <input class="form-input" id="modal-url" value="http://localhost:2501" />
    </div>
    <div class="setting-row" style="margin-top:12px">
      <label>USERNAME</label>
      <input class="form-input" id="modal-user" placeholder="kismet username..." />
    </div>
    <div class="setting-row" style="margin-top:12px">
      <label>PASSWORD</label>
      <input class="form-input" id="modal-pass" type="password" placeholder="kismet password..." />
    </div>
    <div class="setting-row" style="margin-top:12px">
      <label>API KEY <span style="color:var(--text-dim);font-size:10px">(optional)</span></label>
      <input class="form-input" id="modal-key" type="password" placeholder="API key..." />
    </div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="save-btn" id="modal-save-btn">CONNECT</button>
      <button class="btn-sm" id="modal-cancel-btn">CANCEL</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let devices = [];
let selectedDev = null;
let phyFilter = 'ALL';
let sevFilter = 'ALL';
let alertCount = 0;
let droneCount = 0;
let pollTimer = null;
let lastTs = 0;
let watchedMacs = new Set();

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.panel).classList.add('active');
    if (t.dataset.panel === 'alerts') { alertCount = 0; updateBadge(); }
    if (t.dataset.panel === 'saves') { loadSaveData(); }
    if (t.dataset.panel === 'settings') { loadAutomations(); }
  });
});
// activate first panel
document.getElementById('panel-devices').classList.add('active');

// ── API helpers ───────────────────────────────────────────────────────────────
async function api(path, opts={}) {
  const r = await fetch(path, { headers: {'Content-Type':'application/json'}, ...opts });
  return r.json();
}
async function get(path) { return api(path); }
async function post(path, body) { return api(path, { method:'POST', body: JSON.stringify(body) }); }

// ── Status / connection ───────────────────────────────────────────────────────
async function checkStatus() {
  const dot  = document.getElementById('status-dot');
  const txt  = document.getElementById('status-text');
  const data = await get('/api/status');
  if (data.ok) {
    dot.classList.add('online');
    txt.textContent = 'ONLINE';
    txt.style.color = 'var(--green)';
  } else {
    dot.classList.remove('online');
    txt.textContent = 'OFFLINE';
    txt.style.color = 'var(--red)';
  }
}

// ── Devices ───────────────────────────────────────────────────────────────────
async function loadDevices(full=false) {
  const ts = full ? 0 : lastTs;
  const data = await get('/api/devices' + (ts ? `?since=${ts}` : ''));
  if (!data.ok) return;
  if (ts && Array.isArray(data.devices)) {
    // merge by mac
    const map = {};
    devices.forEach(d => map[d['kismet_device_base_macaddr']] = d);
    data.devices.forEach(d => map[d['kismet_device_base_macaddr']] = d);
    devices = Object.values(map);
  } else {
    devices = Array.isArray(data.devices) ? data.devices : [];
  }
  lastTs = data.ts || Math.floor(Date.now()/1000);
  renderDevices();
  updateHeaderStats();
}

function renderDevices() {
  const query = document.getElementById('dev-search').value.toLowerCase();
  let rows = devices.filter(d => {
    if (phyFilter !== 'ALL' && d['kismet_device_base_phyname'] !== phyFilter) return false;
    if (!query) return true;
    const mac   = (d['kismet_device_base_macaddr'] || '').toLowerCase();
    const name  = (d['kismet_device_base_name']    || '').toLowerCase();
    const manuf = (d['kismet_device_base_manuf']   || '').toLowerCase();
    return mac.includes(query) || name.includes(query) || manuf.includes(query);
  });

  document.getElementById('dev-count').textContent = `${rows.length} devices`;
  const tbody = document.getElementById('dev-tbody');
  tbody.innerHTML = '';

  rows.forEach(d => {
    const mac     = d['kismet_device_base_macaddr'] || '—';
    const name    = d['kismet_device_base_name']    || '';
    const phy     = d['kismet_device_base_phyname'] || '';
    const manuf   = d['kismet_device_base_manuf']   || '—';
    const sig     = d['kismet_device_base_signal']  || {};
    const dbm     = sig['kismet_common_signal_last_signal'] ?? '—';
    const lastSeen = d['kismet_device_base_last_time'] ?
      new Date(d['kismet_device_base_last_time']*1000).toLocaleTimeString() : '—';
    const isDrone = phy === 'UAV' || isDroneByName(name+' '+manuf);

    const phyClass = phy.includes('802') ? 'phy-80211' : phy === 'Bluetooth' || phy === 'BTLE' ? 'phy-bt' : phy === 'UAV' ? 'phy-uav' : 'phy-other';
    const sigColor = typeof dbm === 'number' ? (dbm > -60 ? 'var(--green)' : dbm > -80 ? 'var(--amber)' : 'var(--red)') : 'var(--text-dim)';

    const tr = document.createElement('tr');
    if (isDrone) tr.classList.add('drone-row');
    if (selectedDev && selectedDev['kismet_device_base_macaddr'] === mac) tr.classList.add('selected');
    const isDevWatched = watchedMacs.has(mac);
    tr.innerHTML = `
      <td style="color:var(--amber)">${isDevWatched ? '<span class="watched-indicator"></span>' : ''}${mac}</td>
      <td>${name || '<span style="color:var(--text-dim)">—</span>'}</td>
      <td><span class="phy-tag ${phyClass}">${phy}</span></td>
      <td style="color:var(--text-dim)">${manuf}</td>
      <td style="color:${sigColor}">${typeof dbm === 'number' ? dbm+' dBm' : dbm}</td>
      <td style="color:var(--text-dim)">${lastSeen}</td>
    `;
    tr.addEventListener('click', () => { selectedDev = d; renderDevices(); renderDetail(d); });
    tbody.appendChild(tr);
  });
}

function isDroneByName(s) {
  const kw = ['dji','parrot','yuneec','autel','skydio','bebop','phantom','mavic','inspire','matrice','tello','fpv','drone','uav','ardupilot','pixhawk','droneid'];
  const l = s.toLowerCase();
  return kw.some(k => l.includes(k));
}

function renderDetail(d) {
  const el = document.getElementById('dev-detail');
  const mac    = d['kismet_device_base_macaddr'] || '—';
  const name   = d['kismet_device_base_name']    || '—';
  const phy    = d['kismet_device_base_phyname'] || '—';
  const manuf  = d['kismet_device_base_manuf']   || '—';
  const sig    = d['kismet_device_base_signal']  || {};
  const dbm    = sig['kismet_common_signal_last_signal'] ?? '—';
  const maxDbm = sig['kismet_common_signal_max_signal']  ?? '—';
  const minDbm = sig['kismet_common_signal_min_signal']  ?? '—';
  const type   = d['kismet_device_base_type']    || '—';
  const pkts   = d['kismet_device_base_packets_total'] ?? '—';
  const first  = d['kismet_device_base_first_time'] ? new Date(d['kismet_device_base_first_time']*1000).toLocaleString() : '—';
  const last   = d['kismet_device_base_last_time']  ? new Date(d['kismet_device_base_last_time']*1000).toLocaleString()  : '—';
  const freq   = d['kismet_device_base_frequency']  ?? '—';
  const chan   = d['kismet_device_base_channel']    || '—';
  const isDrone = phy === 'UAV' || isDroneByName(name+' '+manuf);
  const isWatched = watchedMacs.has(mac);

  el.innerHTML = `
    <div class="detail-header" style="${isDrone ? 'border-left:3px solid var(--drone)' : ''}">
      <div style="display:flex;align-items:start;justify-content:space-between">
        <div>
          ${isDrone ? '<div style="color:var(--drone);font-size:11px;letter-spacing:2px;margin-bottom:4px">🚁 DRONE DETECTED</div>' : ''}
          <div class="detail-mac">${mac}</div>
          <div class="detail-name">${name !== '—' ? name : manuf}</div>
        </div>
        <button class="btn-sm ${isWatched ? 'primary' : ''}" id="watch-btn"
          onclick="toggleWatch('${mac}','${(name !== '—' ? name : manuf).replace(/'/g,"\\'")}','${phy}')"
          style="flex-shrink:0;margin-left:8px;border-color:${isWatched ? 'var(--cyan)' : 'var(--border-hi)'};color:${isWatched ? 'var(--cyan)' : 'var(--text-dim)'}">
          ${isWatched ? '✓ WATCHING' : '👁 WATCH'}
        </button>
      </div>
    </div>
    <div class="detail-section">
      <h4>IDENTITY</h4>
      <div class="kv"><span class="k">PHY</span><span class="v">${phy}</span></div>
      <div class="kv"><span class="k">TYPE</span><span class="v">${type}</span></div>
      <div class="kv"><span class="k">MANUFACTURER</span><span class="v">${manuf}</span></div>
      <div class="kv"><span class="k">CHANNEL</span><span class="v">${chan}</span></div>
      <div class="kv"><span class="k">FREQUENCY</span><span class="v">${freq !== '—' ? freq+' MHz' : freq}</span></div>
    </div>
    <div class="detail-section">
      <h4>SIGNAL</h4>
      <div class="kv"><span class="k">CURRENT</span><span class="v">${typeof dbm === 'number' ? dbm+' dBm' : dbm}</span></div>
      <div class="kv"><span class="k">MAX</span><span class="v">${typeof maxDbm === 'number' ? maxDbm+' dBm' : maxDbm}</span></div>
      <div class="kv"><span class="k">MIN</span><span class="v">${typeof minDbm === 'number' ? minDbm+' dBm' : minDbm}</span></div>
    </div>
    <div class="detail-section">
      <h4>ACTIVITY</h4>
      <div class="kv"><span class="k">PACKETS</span><span class="v">${pkts}</span></div>
      <div class="kv"><span class="k">FIRST SEEN</span><span class="v" style="font-size:10px">${first}</span></div>
      <div class="kv"><span class="k">LAST SEEN</span><span class="v" style="font-size:10px">${last}</span></div>
    </div>
  `;
}

function updateHeaderStats() {
  document.getElementById('hdr-devices').textContent = devices.length;
  droneCount = devices.filter(d => d['kismet_device_base_phyname'] === 'UAV' || isDroneByName((d['kismet_device_base_name']||'')+(d['kismet_device_base_manuf']||''))).length;
  document.getElementById('hdr-drones').textContent = droneCount;
}

// ── Alerts ────────────────────────────────────────────────────────────────────
async function loadAlerts() {
  const data = await get('/api/alerts?limit=200');
  renderAlerts(data.alerts || []);
}

function renderAlerts(alerts) {
  const list = document.getElementById('alerts-list');
  let filtered = alerts;
  if (sevFilter !== 'ALL') filtered = alerts.filter(a => a.severity === sevFilter);
  if (!filtered.length) { list.innerHTML = '<div class="no-alerts"><div style="font-size:28px;opacity:0.3">◎</div><div style="font-size:12px;letter-spacing:2px;">NO ALERTS</div></div>'; return; }
  list.innerHTML = filtered.map(a => `
    <div class="alert-item">
      <div class="alert-ts">${a.ts ? a.ts.replace('T',' ').substring(0,19) : ''}</div>
      <div class="sev-badge sev-${a.severity}">${a.severity.toUpperCase()}</div>
      <div class="alert-content">
        <div class="title">${a.title}</div>
        <div class="body">${a.body}</div>
      </div>
    </div>
  `).join('');
}

function updateBadge() {
  const badge = document.getElementById('alert-badge');
  badge.textContent = alertCount;
  badge.style.display = alertCount > 0 ? 'block' : 'none';
  const droneBadge = alertCount > 0 && droneCount > 0;
  badge.className = 'badge' + (droneBadge ? ' drone-badge' : '');
}

// ── Saves ─────────────────────────────────────────────────────────────────────
async function loadSaveData() {
  const [schedData, logData] = await Promise.all([get('/api/schedules'), get('/api/save/log')]);
  renderSchedules(schedData.schedules || []);
  renderSaveLog(logData.log || []);
}

function renderSchedules(schedules) {
  const el = document.getElementById('schedules-list');
  if (!schedules.length) { el.innerHTML = '<div style="color:var(--text-dim);font-size:12px;text-align:center;padding:20px">No schedules configured</div>'; return; }
  el.innerHTML = schedules.map(s => `
    <div class="schedule-item">
      <div class="sched-info">
        <div class="sched-name">${s.name}</div>
        <div class="sched-meta">every ${s.interval_min} min</div>
      </div>
      <button class="del-btn" onclick="deleteSchedule('${s.id}')">✕ REMOVE</button>
    </div>
  `).join('');
}

function renderSaveLog(log) {
  const el = document.getElementById('save-log-list');
  if (!log.length) { el.innerHTML = '<div style="color:var(--text-dim);font-size:12px;text-align:center;padding:20px">No saves yet</div>'; return; }
  el.innerHTML = log.map(l => `
    <div class="save-log-item">
      <div>
        <div class="log-ts">${l.ts}</div>
        <div class="log-file">${l.file}</div>
      </div>
      <div>
        <span class="${l.ok ? 'log-ok' : 'log-fail'}">${l.ok ? '✓' : '✗'}</span>
        ${l.ok ? `<span style="font-size:11px;color:var(--text-dim);margin-left:6px">${l.count} dev</span>` : ''}
      </div>
    </div>
  `).join('');
}

async function deleteSchedule(id) {
  await fetch(`/api/schedules/${id}`, {method:'DELETE'});
  loadSaveData();
}

// ── Config ────────────────────────────────────────────────────────────────────
async function saveConfig() {
  const url  = document.getElementById('cfg-url').value.trim();
  const key  = document.getElementById('cfg-key').value.trim();
  const user = document.getElementById('cfg-user').value.trim();
  const pass = document.getElementById('cfg-pass').value.trim();
  await post('/api/config', {kismet_url: url, api_key: key, username: user, password: pass});
  const r = await get('/api/status');
  const el = document.getElementById('conn-result');
  el.textContent = r.ok ? '✓ Connected successfully' : '✗ Connection failed: ' + (r.error||'');
  el.style.color = r.ok ? 'var(--green)' : 'var(--red)';
  checkStatus();
  showToast(r.ok ? '✓ Connected to Kismet' : '✗ Connection failed');
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal() { document.getElementById('conn-modal').classList.add('open'); }
function closeModal() { document.getElementById('conn-modal').classList.remove('open'); }
async function saveModal() {
  const url  = document.getElementById('modal-url').value.trim();
  const key  = document.getElementById('modal-key').value.trim();
  const user = document.getElementById('modal-user').value.trim();
  const pass = document.getElementById('modal-pass').value.trim();
  if (url)  document.getElementById('cfg-url').value  = url;
  if (key)  document.getElementById('cfg-key').value  = key;
  if (user) document.getElementById('cfg-user').value = user;
  if (pass) document.getElementById('cfg-pass').value = pass;
  await post('/api/config', {kismet_url: url, api_key: key, username: user, password: pass});
  closeModal();
  checkStatus();
  loadDevices(true);
  showToast('Connecting to Kismet...');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3100);
}

// ── Event wiring ──────────────────────────────────────────────────────────────
document.getElementById('refresh-btn').addEventListener('click', () => loadDevices(true));
document.getElementById('conn-btn').addEventListener('click', openModal);
document.getElementById('modal-save-btn').addEventListener('click', saveModal);
document.getElementById('modal-cancel-btn').addEventListener('click', closeModal);
document.getElementById('save-cfg-btn').addEventListener('click', saveConfig);
document.getElementById('test-conn-btn').addEventListener('click', async () => {
  const r = await get('/api/status');
  const el = document.getElementById('conn-result');
  el.textContent = r.ok ? '✓ ' + JSON.stringify(r.data?.['kismet.system.version'] || 'OK') : '✗ ' + (r.error||'Failed');
  el.style.color = r.ok ? 'var(--green)' : 'var(--red)';
});
document.getElementById('dev-search').addEventListener('input', renderDevices);

document.querySelectorAll('[data-phy]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-phy]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    phyFilter = btn.dataset.phy;
    renderDevices();
  });
});

document.querySelectorAll('[data-sev]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-sev]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    sevFilter = btn.dataset.sev;
    loadAlerts();
  });
});

document.getElementById('clear-alerts-btn').addEventListener('click', async () => {
  await post('/api/alerts/clear', {});
  alertCount = 0; updateBadge();
  loadAlerts();
});

document.getElementById('poll-alerts-btn').addEventListener('click', async () => {
  await post('/api/alerts/poll', {});
  setTimeout(loadAlerts, 1500);
  showToast('Polling Kismet alerts...');
});

document.getElementById('manual-save-btn').addEventListener('click', async () => {
  await post('/api/save', {label: 'manual'});
  showToast('💾 Save started...');
  setTimeout(loadSaveData, 2000);
});

document.getElementById('add-sched-btn').addEventListener('click', async () => {
  const name     = document.getElementById('sched-name').value.trim();
  const interval = parseInt(document.getElementById('sched-interval').value);
  if (!name || interval < 1) return showToast('Invalid schedule');
  await post('/api/schedules', {name, interval_min: interval});
  loadSaveData();
  showToast(`✓ Schedule "${name}" added`);
});

// ── Automations ───────────────────────────────────────────────────────────────
async function toggleAutomation(btn, key) {
  const isActive = btn.classList.toggle('active');
  await post('/api/automations', { [key]: isActive });
  if (key === 'alert_save_enabled') {
    document.getElementById('alert-save-options').style.opacity = isActive ? '1' : '0.4';
    document.getElementById('alert-save-options').style.pointerEvents = isActive ? 'auto' : 'none';
  }
}

async function loadAutomations() {
  const data = await get('/api/automations');
  const auto = data.automations || {};
  const rules = auto.auto_watch_rules || {};
  const watched = data.watched_devices || [];
  const setToggle = (id, val) => {
    const el = document.getElementById(id);
    if (el) { if (val) el.classList.add('active'); else el.classList.remove('active'); }
  };
  setToggle('toggle-alert-save', auto.alert_save_enabled);
  setToggle('toggle-device-details', auto.save_device_details);
  setToggle('toggle-device-traffic', auto.save_device_traffic);
  setToggle('toggle-aw-drone', rules.drone_alerts);
  setToggle('toggle-aw-btle', rules.btle_alerts);
  setToggle('toggle-aw-signal', rules.strong_signal);
  const opts = document.getElementById('alert-save-options');
  if (opts) {
    opts.style.opacity = auto.alert_save_enabled ? '1' : '0.4';
    opts.style.pointerEvents = auto.alert_save_enabled ? 'auto' : 'none';
  }
  setToggle('toggle-watched-only', auto.save_watched_only);
  watchedMacs = new Set(watched.map(w => w.mac));
  renderWatchedList(watched);
}

async function toggleAutoWatch(btn, rule) {
  const isActive = btn.classList.toggle('active');
  await post('/api/automations', { auto_watch_rules: { [rule]: isActive } });
}

async function toggleWatch(mac, name, phy) {
  if (watchedMacs.has(mac)) {
    await fetch('/api/watchlist/' + encodeURIComponent(mac), { method: 'DELETE' });
    watchedMacs.delete(mac);
  } else {
    await post('/api/watchlist', { mac, name, phyname: phy });
    watchedMacs.add(mac);
  }
  if (selectedDev) renderDetail(selectedDev);
  renderDevices();
  refreshWatchlist();
}

async function refreshWatchlist() {
  const data = await get('/api/watchlist');
  const watched = data.devices || [];
  watchedMacs = new Set(watched.map(w => w.mac));
  renderWatchedList(watched);
}

function renderWatchedList(watched) {
  const el = document.getElementById('watched-list');
  const countEl = document.getElementById('watched-count');
  if (!el) return;
  countEl.textContent = watched.length + ' DEVICE' + (watched.length !== 1 ? 'S' : '');
  if (!watched.length) {
    el.innerHTML = '<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:12px">No devices watched</div>';
    return;
  }
  el.innerHTML = watched.map(w => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border:1px solid var(--border);margin-bottom:4px;background:var(--bg)">
      <div style="min-width:0;display:flex;align-items:center;gap:8px">
        <div>
          <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${w.name || w.mac}</div>
          <div style="font-size:10px;color:var(--text-dim);font-family:'Share Tech Mono',monospace">${w.mac}${w.phyname ? ' · ' + w.phyname : ''}</div>
        </div>
        ${w.auto ? '<span style="font-size:9px;padding:1px 4px;border-radius:2px;background:rgba(0,188,212,0.15);color:var(--cyan)">AUTO</span>' : ''}
      </div>
      <button class="del-btn" onclick="unwatchFromConfig('${w.mac}')" style="flex-shrink:0;margin-left:8px">✕</button>
    </div>
  `).join('');
}

async function unwatchFromConfig(mac) {
  await fetch('/api/watchlist/' + encodeURIComponent(mac), { method: 'DELETE' });
  watchedMacs.delete(mac);
  refreshWatchlist();
  renderDevices();
  if (selectedDev && selectedDev['kismet_device_base_macaddr'] === mac) renderDetail(selectedDev);
}

// ── Poll loop ─────────────────────────────────────────────────────────────────
async function pollLoop() {
  await checkStatus();
  await loadDevices();
  const alertData = await get('/api/alerts?limit=200');
  const newAlerts = alertData.alerts || [];
  const newCount  = newAlerts.length;
  const activePanel = document.querySelector('.tab.active')?.dataset?.panel;
  if (activePanel !== 'alerts' && newCount > alertCount) {
    alertCount = newCount; updateBadge();
  }
  if (activePanel === 'alerts') renderAlerts(newAlerts);
  const lastSave = newAlerts.find(a => a.type === 'save');
  if (lastSave) document.getElementById('hdr-save').textContent = lastSave.ts?.substring(11,19) || '—';
}

// Load initial config
get('/api/config').then(c => {
  if (c.kismet_url) {
    document.getElementById('cfg-url').value = c.kismet_url;
    document.getElementById('modal-url').value = c.kismet_url;
  }
  if (c.username) {
    document.getElementById('cfg-user').value = c.username;
    document.getElementById('modal-user').value = c.username;
  }
});

// Start
pollLoop();
setInterval(pollLoop, 10000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kismet Dashboard")
    parser.add_argument("--host",       default="0.0.0.0")
    parser.add_argument("--port", "-p", default=5000, type=int)
    parser.add_argument("--save-dir",   default=str(SAVE_DIR))
    args = parser.parse_args()

    SAVE_DIR = Path(args.save_dir)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    if DEMO_MODE:
        _stamp_dummy_devices()
        _seed_dummy_alerts()
        analyze_devices(DUMMY_DEVICES)
        log.info("Demo mode active — using dummy data (set KISMET_DEMO=0 to disable)")

    scheduler.start()
    log.info(f"Kismet Sentinel running → http://localhost:{args.port}")
    log.info(f"Save dir: {SAVE_DIR.resolve()}")
    app.run(host=args.host, port=args.port, debug=False)