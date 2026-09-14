#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 mock_server.py
--------------------------------------------------------------------------------
 A complete, self-contained MOCK CMMS / O&M backend (Metaagrow-like simulation).

 - Creates a SQLite database automatically (mock_cmms.db)
 - Populates it with LARGE, REALISTIC, INTERCONNECTED dummy data
   (deterministic -> restarting always regenerates the same data)
 - Exposes the database through a FastAPI REST API
 - Acts like a real CMMS API server

 RUN:
     pip install fastapi uvicorn
     python mock_server.py

 ENDPOINTS:
     GET  /api/tables
     GET  /api/data/{table}?limit=1000&offset=0&search=hvac
     GET  /api/data/{table}/{id}
     GET  /health

 NOTE:
     This is a FAKE backend for local testing only.
     It does NOT connect to the real Metaagrow system.
================================================================================
"""

import os
import sys
import sqlite3
import random
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
import uvicorn

# ==============================================================================
# CONFIGURATION
# ==============================================================================

DB_FILE = "mock_cmms.db"
HOST = "127.0.0.1"
PORT = 8000
SEED = 20260630                       # deterministic random generation

rng = random.Random(SEED)

# Fixed "today" so every restart produces identical historical data
BASE_DATE = date(2026, 6, 30)
BASE_DT = datetime(2026, 6, 30, 12, 0, 0)
HISTORY_START = BASE_DATE - timedelta(days=730)   # ~24 months of history
DATA_START_DT = datetime(HISTORY_START.year, HISTORY_START.month, HISTORY_START.day, 0, 0, 0)

# Record targets
N_CUSTOMERS = 20
N_LOCATIONS = 50
N_ASSETS = 300
N_TECHNICIANS = 50
N_TICKETS = 1000
N_PPM = 500
N_INSPECTIONS = 500
N_INVENTORY = 300
N_DOWNTIME = 700
N_COSTS = 1000
N_METERS = 1500

# Pattern sizing (NOT hard-coded conclusions, only underlying data patterns)
N_PROBLEM_ASSETS = 18                 # high downtime / failures / rising cost
N_PROBLEM_LOCATIONS = 10              # high tickets / low PPM compliance / SLA breaches
N_BUSY_TECHNICIANS = 8                # high workload
N_SLOW_TECHNICIANS = 6                # long resolution time

DT_FMT = "%Y-%m-%d %H:%M:%S"
D_FMT = "%Y-%m-%d"


def fmt_dt(dt: datetime) -> str:
    return dt.strftime(DT_FMT)


def fmt_d(d: date) -> str:
    return d.strftime(D_FMT)


def rnd_date(start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, max(0, (end - start).days)))


def rnd_dt(start_dt: datetime, end_dt: datetime) -> datetime:
    span = int((end_dt - start_dt).total_seconds())
    return start_dt + timedelta(seconds=rng.randint(0, max(0, span)))


def rnd_time_on(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))


# ==============================================================================
# TABLE DEFINITIONS
# ==============================================================================

TABLE_ORDER = [
    "customers", "locations", "assets", "asset_properties", "technicians",
    "tickets", "ppm", "inspections", "inventory", "downtime",
    "maintenance_costs", "meter_readings",
]

CREATE_SQL = {
    "customers": """
        CREATE TABLE customers (
            customer_id   INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            industry      TEXT,
            city          TEXT,
            contract_type TEXT
        )""",
    "locations": """
        CREATE TABLE locations (
            location_id  INTEGER PRIMARY KEY,
            customer_id  INTEGER,
            site_name    TEXT,
            city         TEXT,
            region       TEXT
        )""",
    "assets": """
        CREATE TABLE assets (
            asset_id          INTEGER PRIMARY KEY,
            asset_tag         TEXT,
            name              TEXT,
            category          TEXT,
            manufacturer      TEXT,
            model             TEXT,
            serial_number     TEXT,
            location_id       INTEGER,
            installation_date TEXT,
            purchase_value    REAL,
            status            TEXT,
            criticality       TEXT,
            warranty_expiry   TEXT
        )""",
    "asset_properties": """
        CREATE TABLE asset_properties (
            property_id    INTEGER PRIMARY KEY,
            asset_id       INTEGER,
            property_name  TEXT,
            property_value TEXT
        )""",
    "technicians": """
        CREATE TABLE technicians (
            technician_id  INTEGER PRIMARY KEY,
            employee_code  TEXT,
            name           TEXT,
            specialization TEXT,
            skill_level    TEXT,
            shift          TEXT,
            contact        TEXT,
            base_city      TEXT
        )""",
    "tickets": """
        CREATE TABLE tickets (
            ticket_id        INTEGER PRIMARY KEY,
            asset_id         INTEGER,
            location_id      INTEGER,
            technician_id    INTEGER,
            created_at       TEXT,
            closed_at        TEXT,
            status           TEXT,
            priority         TEXT,
            category         TEXT,
            problem          TEXT,
            resolution       TEXT,
            resolution_hours REAL,
            sla_hours        REAL,
            sla_breached     INTEGER
        )""",
    "ppm": """
        CREATE TABLE ppm (
            ppm_id         INTEGER PRIMARY KEY,
            asset_id       INTEGER,
            location_id    INTEGER,
            task           TEXT,
            frequency      TEXT,
            due_date       TEXT,
            completed_date TEXT,
            status         TEXT
        )""",
    "inspections": """
        CREATE TABLE inspections (
            inspection_id   INTEGER PRIMARY KEY,
            asset_id        INTEGER,
            location_id     INTEGER,
            inspection_date TEXT,
            inspector_id    INTEGER,
            inspection_type TEXT,
            result          TEXT,
            score           INTEGER,
            remarks         TEXT
        )""",
    "inventory": """
        CREATE TABLE inventory (
            part_id       INTEGER PRIMARY KEY,
            location_id   INTEGER,
            part_number   TEXT,
            name          TEXT,
            category      TEXT,
            stock         INTEGER,
            minimum_stock INTEGER,
            unit_cost     REAL,
            supplier      TEXT
        )""",
    "downtime": """
        CREATE TABLE downtime (
            downtime_id INTEGER PRIMARY KEY,
            asset_id    INTEGER,
            location_id INTEGER,
            start_time  TEXT,
            end_time    TEXT,
            hours       REAL,
            reason      TEXT
        )""",
    "maintenance_costs": """
        CREATE TABLE maintenance_costs (
            cost_id    INTEGER PRIMARY KEY,
            asset_id   INTEGER,
            location_id INTEGER,
            date       TEXT,
            cost_type  TEXT,
            amount     REAL
        )""",
    "meter_readings": """
        CREATE TABLE meter_readings (
            reading_id   INTEGER PRIMARY KEY,
            asset_id     INTEGER,
            meter_type   TEXT,
            reading_date TEXT,
            value        REAL,
            unit         TEXT
        )""",
}

TABLE_SCHEMAS: Dict[str, List[str]] = {
    "customers": ["customer_id", "name", "industry", "city", "contract_type"],
    "locations": ["location_id", "customer_id", "site_name", "city", "region"],
    "assets": ["asset_id", "asset_tag", "name", "category", "manufacturer", "model",
               "serial_number", "location_id", "installation_date", "purchase_value",
               "status", "criticality", "warranty_expiry"],
    "asset_properties": ["property_id", "asset_id", "property_name", "property_value"],
    "technicians": ["technician_id", "employee_code", "name", "specialization",
                    "skill_level", "shift", "contact", "base_city"],
    "tickets": ["ticket_id", "asset_id", "location_id", "technician_id", "created_at",
                "closed_at", "status", "priority", "category", "problem", "resolution",
                "resolution_hours", "sla_hours", "sla_breached"],
    "ppm": ["ppm_id", "asset_id", "location_id", "task", "frequency", "due_date",
            "completed_date", "status"],
    "inspections": ["inspection_id", "asset_id", "location_id", "inspection_date",
                    "inspector_id", "inspection_type", "result", "score", "remarks"],
    "inventory": ["part_id", "location_id", "part_number", "name", "category",
                  "stock", "minimum_stock", "unit_cost", "supplier"],
    "downtime": ["downtime_id", "asset_id", "location_id", "start_time", "end_time",
                 "hours", "reason"],
    "maintenance_costs": ["cost_id", "asset_id", "location_id", "date", "cost_type", "amount"],
    "meter_readings": ["reading_id", "asset_id", "meter_type", "reading_date", "value", "unit"],
}

# ==============================================================================
# MASTER DATA POOLS (realistic CMMS vocabulary)
# ==============================================================================

CITIES = [
    "Mumbai", "Pune", "Delhi NCR", "Bengaluru", "Chennai", "Hyderabad",
    "Kolkata", "Ahmedabad", "Jaipur", "Nagpur", "Surat", "Indore",
    "Kochi", "Coimbatore", "Lucknow", "Chandigarh", "Bhopal", "Vadodara",
    "Visakhapatnam", "Mysuru",
]

CITY_REGION = {
    "Mumbai": "West", "Pune": "West", "Ahmedabad": "West", "Surat": "West",
    "Vadodara": "West", "Nagpur": "West", "Indore": "Central", "Bhopal": "Central",
    "Delhi NCR": "North", "Jaipur": "North", "Lucknow": "North", "Chandigarh": "North",
    "Bengaluru": "South", "Chennai": "South", "Hyderabad": "South", "Kochi": "South",
    "Coimbatore": "South", "Mysuru": "South", "Visakhapatnam": "South",
    "Kolkata": "East",
}

INDUSTRIES = [
    "Manufacturing", "Pharmaceuticals", "IT / ITES", "Healthcare", "Retail",
    "Hospitality", "Banking & Finance", "Logistics", "Automotive", "Textiles",
    "Food Processing", "Chemicals", "FMCG", "Aerospace", "Data Centers",
    "Commercial Real Estate",
]

CONTRACT_TYPES = [
    "AMC - Comprehensive", "AMC - Preventive Only", "Full O&M",
    "Time & Material", "Performance Based Contract",
]

COMPANY_PREFIX = [
    "Sunrise", "Vertex", "Apex", "Orion", "NovaTech", "Precision", "Zenith",
    "Summit", "Bluewave", "PrimeCorp", "Infinity", "TitanForge", "Everest",
    "PinnacleWorks", "QuantumBase", "HorizonLine", "Silverline", "CrystalWorks",
    "MeridianPlus", "PulseCore", "CascadeGroup", "Brightline", "Ironwood",
    "SkylineEdge", "GreenLeaf", "SterlingMax", "TridentStar", "Ironclad",
    "NimbusWorks", "CrystalPeak",
]

COMPANY_SUFFIX = [
    "Industries Pvt Ltd", "Technologies Pvt Ltd", "Healthcare Ltd",
    "Logistics Pvt Ltd", "Manufacturing Ltd", "Services Pvt Ltd",
    "Enterprises Pvt Ltd", "Solutions Ltd", "Group of Companies", "Corp Ltd",
]

SITE_KINDS = [
    "Plant", "Unit", "Campus", "Warehouse", "Data Center", "Office Tower",
    "Distribution Center", "Hospital Block", "Mall", "Manufacturing Unit",
    "Depot", "Lab Facility",
]

# (display name, tag prefix, category, purchase value range, criticality bias 0..1)
ASSET_TYPES = [
    ("HVAC Chiller",           "CH",   "HVAC",                (2_500_000, 9_000_000), 0.70),
    ("Air Handling Unit",      "AHU",  "HVAC",                (250_000, 900_000),     0.40),
    ("Cooling Tower",          "CT",   "HVAC",                (400_000, 1_500_000),   0.50),
    ("Diesel Generator",       "GEN",  "Electrical",          (1_500_000, 6_000_000), 0.80),
    ("UPS System",             "UPS",  "Electrical",          (300_000, 2_500_000),   0.80),
    ("Transformer",            "TX",   "Electrical",          (1_000_000, 5_000_000), 0.80),
    ("Electrical Panel",       "EP",   "Electrical",          (150_000, 1_200_000),   0.60),
    ("Elevator",               "ELV",  "Vertical Transport",  (1_800_000, 5_500_000), 0.80),
    ("Escalator",              "ESC",  "Vertical Transport",  (2_000_000, 6_000_000), 0.60),
    ("Water Pump",             "WP",   "Plumbing",            (60_000, 450_000),      0.40),
    ("Fire Pump",              "FP",   "Fire Safety",         (300_000, 1_200_000),   0.90),
    ("Air Compressor",         "CMP",  "Mechanical",          (200_000, 1_800_000),   0.50),
    ("RO Plant",               "RO",   "Plumbing",            (350_000, 2_000_000),   0.50),
    ("Boiler",                 "BLR",  "Mechanical",          (900_000, 4_000_000),   0.70),
    ("Conveyor System",        "CVY",  "Mechanical",          (400_000, 2_500_000),   0.50),
    ("Server Rack",            "SRV",  "IT",                  (500_000, 3_000_000),   0.80),
    ("CCTV System",            "CCTV", "Security",            (120_000, 800_000),     0.30),
    ("Access Control System",  "ACS",  "Security",            (80_000, 500_000),      0.30),
    ("Lighting System",        "LTS",  "Electrical",          (50_000, 400_000),      0.20),
]

MANUFACTURERS = {
    "HVAC Chiller":          ["Carrier", "Voltas", "Daikin", "Trane", "Blue Star", "Hitachi"],
    "Air Handling Unit":     ["Voltas", "Blue Star", "Daikin", "Lloyd", "Munters"],
    "Cooling Tower":         ["Evapco", "Delta Towers", "Baltimore Aircoil", "Kelvion", "Pahal"],
    "Diesel Generator":      ["Kirloskar", "Cummins", "Caterpillar", "Mahindra Powerol", "Ashok Leyland"],
    "UPS System":            ["APC", "Vertiv", "Eaton", "Socomec", "Luminous"],
    "Transformer":           ["ABB", "Siemens", "Crompton", "Voltamp", "Bharat Bijlee"],
    "Electrical Panel":      ["Schneider", "L&T", "Siemens", "ABB", "Legrand"],
    "Elevator":              ["Kone", "Otis", "Schindler", "ThyssenKrupp", "Johnson Lifts"],
    "Escalator":             ["Otis", "Schindler", "Kone", "Hyundai Elevators"],
    "Water Pump":            ["Kirloskar", "Grundfos", "Crompton", "KSB", "Shakti Pumps"],
    "Fire Pump":             ["Kirloskar", "Patterson", "SPP Pumps", "Naffco", "Zodiac"],
    "Air Compressor":        ["Atlas Copco", "ELGi", "Ingersoll Rand", "Kaeser", "Chicago Pneumatic"],
    "RO Plant":              ["Ion Exchange", "Kent", "A.O. Smith", "HiTech Ro", "Dow"],
    "Boiler":                ["Thermax", "Forbes Marshall", "Cochran", "Bosch", "Volcano Boilers"],
    "Conveyor System":       ["Sandvik", "FlexLink", "Nielsen", "Continental", "Beumer"],
    "Server Rack":           ["Dell EMC", "HPE", "NetApp", "IBM", "Cisco"],
    "CCTV System":           ["Hikvision", "Dahua", "Bosch Security", "Honeywell", "CP Plus"],
    "Access Control System": ["Honeywell", "Assa Abloy", "HID Global", "ZKTeco", "Matrix Comsec"],
    "Lighting System":       ["Philips", "Havells", "Wipro Lighting", "Crompton", "Bajaj Electricals"],
}

TICKET_CATEGORIES = ["HVAC", "Electrical", "Mechanical", "Plumbing", "Fire Safety",
                     "Security", "IT", "Vertical Transport"]

PROBLEMS = {
    "HVAC": [
        "Unit not cooling adequately", "Water leakage from drain pan",
        "High noise from blower motor", "Frequent tripping on overload",
        "Bad odor from supply vents", "Thermostat not responding",
        "Ice formation on evaporator coils", "Low airflow from diffusers",
        "Refrigerant pressure alarm", "AHU vibration above limit",
    ],
    "Electrical": [
        "Panel breaker tripping repeatedly", "Burning smell near panel",
        "Phase failure alarm", "Voltage fluctuation observed",
        "Loose termination found overheating", "Indicator lamp not working",
        "Insulation resistance below limit", "Earth leakage alarm",
        "Battery bank not holding charge", "Contactor chattering",
    ],
    "Mechanical": [
        "Excessive vibration during operation", "Unusual noise from gearbox",
        "Output pressure below setpoint", "Overheating of bearing housing",
        "Oil leakage observed", "Coupling misalignment suspected",
        "Machine stops intermittently", "Seal failure - process fluid leak",
        "Abnormal power draw", "Safety guard interlock faulty",
    ],
    "Plumbing": [
        "Pump not building pressure", "Pipeline leakage at joint",
        "Low water flow at outlet", "Motor overheating",
        "Valve stuck in closed position", "Tank level sensor malfunction",
        "Frequent pump cycling", "Cavitation noise in pump",
        "Water quality parameter out of range", "Sump pump auto-start failure",
    ],
    "Fire Safety": [
        "Fire pump fails auto-start test", "Sprinkler head leakage",
        "Smoke detector false alarm", "Fire alarm panel trouble signal",
        "Hose reel pressure low", "Jockey pump frequent start",
        "Emergency exit light not working", "Fire extinguisher pressure low",
        "Hydrant valve leaking", "Addressable loop fault",
    ],
    "Security": [
        "Camera feed lost", "DVR/NVR recording failure",
        "Access control reader not responding", "Door forced alarm not triggering",
        "Night vision degraded", "Monitor display blank",
        "Card reader intermittent failure", "Barrier gate stuck",
        "Motion detection not capturing", "Cabling fault to camera 12",
    ],
    "IT": [
        "Server rack overheating alarm", "UPS on bypass mode",
        "Network switch port failure", "Server fan failure alert",
        "Disk degradation warning", "Redundant PSU failure",
        "Cooling airflow obstruction", "PDU outlet not powering",
        "Frequent server reboot observed", "Cabling congestion in rack",
    ],
    "Vertical Transport": [
        "Elevator door closing slowly", "Leveling issue at floor 3",
        "Car jerking during travel", "Emergency alarm button faulty",
        "Cab light not working", "Elevator stuck between floors",
        "Escalator handrail speed mismatch", "Overload sensor miscalibrated",
        "Intercom not working in cab", "Machine room temperature high",
    ],
}

GENERIC_PROBLEMS = [
    "Equipment not operating as expected", "Abnormal noise reported by operator",
    "Performance degradation observed", "Intermittent shutdowns",
    "Control system fault", "Requires on-site diagnosis",
]

RESOLUTIONS = [
    "Replaced faulty component and verified operation",
    "Cleaned and serviced unit, tested OK",
    "Tightened electrical connections and reset breaker",
    "Replaced worn belt and realigned pulleys",
    "Refilled lubricant and checked alignment",
    "Repaired leak and pressure tested",
    "Reprogrammed controller and verified parameters",
    "Replaced sensor and calibrated",
    "Cleared blockage and flushed line",
    "Replaced bearing assembly, vibration now normal",
    "Reset alarm and monitored for 24 hours, stable",
    "Replaced filter set and verified airflow",
    "Updated firmware and tested all functions",
    "Replaced PCB and verified output",
    "Adjusted settings and performed trial run",
    "Temporary fix applied; permanent part ordered",
]

PPM_TASKS = {
    "HVAC": ["Filter replacement", "Coil cleaning", "Refrigerant level check",
             "Belt inspection and replacement", "Condenser cleaning",
             "Duct inspection", "Compressor oil analysis"],
    "Electrical": ["Thermographic scan", "Breaker servicing",
                   "Torque check of terminations", "Earth pit testing",
                   "Insulation resistance test", "Battery health check"],
    "Mechanical": ["Lubrication of bearings", "Gearbox oil change",
                   "Belt tension check", "Coupling alignment check",
                   "Vibration analysis", "Safety valve testing"],
    "Plumbing": ["Pump seal inspection", "Valve exercising",
                 "Tank cleaning", "Pressure test", "Strainer cleaning"],
    "Fire Safety": ["Fire pump churn test", "Sprinkler flow test",
                    "Alarm panel testing", "Extinguisher inspection",
                    "Hydrant pressure test", "Emergency light test"],
    "Security": ["Camera lens cleaning", "DVR storage check",
                 "Reader function test", "Battery backup test"],
    "IT": ["Server dust cleaning", "UPS battery load test",
           "Cooling system check", "Backup verification"],
    "Vertical Transport": ["Rail lubrication", "Door operator adjustment",
                           "Rope inspection", "Safety gear test",
                           "Escalator step chain check"],
}
GENERIC_PPM_TASKS = ["General visual inspection", "Performance test",
                     "Safety device check", "Calibration check",
                     "Preventive lubrication"]

INSPECTION_TYPES = [
    "Fire Safety Audit", "Electrical Safety Audit", "Statutory Inspection",
    "Energy Audit", "Hygiene & Housekeeping Audit", "Lift Annual Inspection",
    "Pressure Vessel Inspection", "General Safety Walkdown",
]

INSPECTION_PASS_REMARKS = [
    "All parameters within limits", "No defects observed",
    "Compliant with checklist requirements", "Condition satisfactory",
]
INSPECTION_FAIL_REMARKS = [
    "Component found defective, needs replacement",
    "Safety interlock not functional",
    "Earthing resistance above permissible limit",
    "Pressure drop beyond tolerance observed",
    "Guarding missing at drive end",
    "Leakage observed at multiple points",
]
INSPECTION_OBS_REMARKS = [
    "Minor wear observed, monitor next cycle",
    "Housekeeping needs improvement",
    "Calibration due next month",
    "Labeling partially faded",
]

PARTS_POOL = [
    ("Air Filter 24x24x2", "HVAC", (350, 1800)),
    ("HEPA Filter H13", "HVAC", (4000, 12000)),
    ("V-Belt B-52", "Mechanical", (150, 600)),
    ("Ball Bearing 6205", "Mechanical", (200, 900)),
    ("Compressor Oil 5L", "HVAC", (1200, 4500)),
    ("Contactor 32A 3P", "Electrical", (800, 3200)),
    ("MCB 63A Triple Pole", "Electrical", (600, 2500)),
    ("Thermal Overload Relay", "Electrical", (900, 3800)),
    ("PLC Input Module", "IT", (5500, 22000)),
    ("HMI Display 7 inch", "IT", (7000, 25000)),
    ("Emergency Stop Button", "Electrical", (250, 900)),
    ("LED Flood Light 150W", "Electrical", (900, 3200)),
    ("Gate Valve 2 inch", "Plumbing", (1200, 5000)),
    ("Pump Mechanical Seal", "Plumbing", (1800, 7500)),
    ("Pump Impeller 3 inch", "Plumbing", (2500, 9000)),
    ("Pressure Gauge 0-10 bar", "Mechanical", (400, 1500)),
    ("Digital Thermostat", "HVAC", (1500, 6000)),
    ("Run Capacitor 440VAC", "Electrical", (300, 1200)),
    ("Battery 12V 7.2Ah", "Security", (700, 2400)),
    ("Fire Sprinkler Head", "Fire Safety", (250, 950)),
    ("Fire Hose 30m", "Fire Safety", (2500, 8500)),
    ("Smoke Detector", "Fire Safety", (600, 2200)),
    ("CCTV Dome Camera 4MP", "Security", (2200, 8000)),
    ("RFID Card Reader", "Security", (3000, 11000)),
    ("Elevator Door Roller", "Vertical Transport", (900, 3400)),
    ("Elevator Guide Shoe", "Vertical Transport", (1500, 6000)),
    ("Conveyor Belt PVC 600mm", "Mechanical", (4500, 18000)),
    ("Sprocket 14 Tooth", "Mechanical", (1200, 4800)),
    ("RO Membrane 8040", "Plumbing", (9000, 28000)),
    ("Boiler Gauge Glass", "Mechanical", (800, 3000)),
    ("Safety Valve 1 inch", "Mechanical", (1600, 6500)),
    ("UPS Battery 12V 26Ah", "Electrical", (2400, 8000)),
    ("Cooling Tower Fan Belt", "HVAC", (700, 2800)),
    ("Float Valve 1.5 inch", "Plumbing", (450, 1700)),
    ("Terminal Block Set", "Electrical", (200, 800)),
    ("Cable Tie Pack 4 inch", "Electrical", (100, 400)),
    ("Gasket Sheet 3mm", "Mechanical", (300, 1100)),
    ("O-Ring Assortment Kit", "Mechanical", (500, 1900)),
    ("Servo Motor 1kW", "Mechanical", (14000, 45000)),
    ("VFD 5.5kW", "Electrical", (12000, 40000)),
    ("Proximity Sensor PNP", "Electrical", (600, 2400)),
    ("Hydraulic Oil 20L", "Mechanical", (2200, 7500)),
]

SUPPLIERS = [
    "IndSupplies Corp", "TechnoParts India", "OmniIndustrial", "Shree Electricals",
    "PrimeMech Components", "BlueStar Spares", "Voltas Service Center",
    "Fusion Maintenance Supply", "GenuineParts Co", "EastWest Industrial",
    "Metro Spares & Traders", "Ace Industrial Stores",
]

FIRST_NAMES = [
    "Rajesh", "Suresh", "Amit", "Vikram", "Sandeep", "Prakash", "Manoj",
    "Deepak", "Ravi", "Anil", "Sunil", "Ajay", "Vijay", "Ramesh", "Mahesh",
    "Kiran", "Naveen", "Arjun", "Rohit", "Karthik", "Ganesh", "Dinesh",
    "Mukesh", "Naresh", "Umesh", "Sanjay", "Ashok", "Nitin", "Sameer", "Gaurav",
]
LAST_NAMES = [
    "Sharma", "Patil", "Kumar", "Singh", "Gupta", "Reddy", "Nair", "Mehta",
    "Joshi", "Desai", "Kulkarni", "Iyer", "Pillai", "Verma", "Mishra", "Yadav",
    "Pawar", "Shinde", "Jadhav", "Chavan", "Rao", "Shetty", "Menon", "Khan",
    "Shaikh", "Kaur", "Bose", "Das", "Saxena", "Trivedi",
]

COST_TYPES = ["Preventive", "Corrective", "Emergency Repair", "Spare Parts",
              "Labour", "Overhaul", "AMC Payment", "Calibration"]

DOWN_REASONS_PLANNED = ["Preventive Maintenance", "Planned Shutdown",
                        "Statutory Inspection", "Modification Work"]
DOWN_REASONS_UNPLANNED = ["Breakdown", "Waiting for Parts",
                          "Corrective Maintenance", "Power Failure",
                          "Operator Error"]

METER_PROFILES = {
    "HVAC": [("Energy Consumption", "kWh", (8000, 60000)),
             ("Run Hours", "hrs", (350, 700)),
             ("Vibration Level", "mm/s", (1.5, 4.0))],
    "Electrical": [("Energy Consumption", "kWh", (10000, 80000)),
                   ("Load Percentage", "%", (55, 85))],
    "Mechanical": [("Run Hours", "hrs", (300, 720)),
                   ("Vibration Level", "mm/s", (1.2, 4.5)),
                   ("Inlet Pressure", "bar", (2.5, 6.0))],
    "Plumbing": [("Water Consumption", "m3", (300, 6000)),
                 ("Inlet Pressure", "bar", (1.5, 4.5)),
                 ("Run Hours", "hrs", (200, 650))],
    "Fire Safety": [("Line Pressure", "bar", (4.0, 8.0)),
                    ("Run Hours", "hrs", (5, 40))],
    "Security": [("Uptime Percentage", "%", (95, 99.9)),
                 ("Energy Consumption", "kWh", (400, 2500))],
    "IT": [("Energy Consumption", "kWh", (5000, 40000)),
           ("Inlet Temperature", "C", (18, 27))],
    "Vertical Transport": [("Run Hours", "hrs", (150, 500)),
                           ("Trips Per Month", "count", (800, 4000))],
}
GENERIC_METERS = [("Run Hours", "hrs", (200, 700)),
                  ("Energy Consumption", "kWh", (2000, 25000))]


# ==============================================================================
# DATA GENERATION  (deterministic; only patterns are embedded — never conclusions)
# ==============================================================================

def gen_customers() -> List[Dict[str, Any]]:
    rows = []
    used_names = set()
    for cid in range(1, N_CUSTOMERS + 1):
        while True:
            name = f"{rng.choice(COMPANY_PREFIX)} {rng.choice(COMPANY_SUFFIX)}"
            if name not in used_names:
                used_names.add(name)
                break
        rows.append({
            "customer_id": cid,
            "name": name,
            "industry": rng.choice(INDUSTRIES),
            "city": rng.choice(CITIES),
            "contract_type": rng.choice(CONTRACT_TYPES),
        })
    return rows


def gen_locations(customers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    # Bias city mix so Mumbai and Pune have enough sites for meaningful comparison
    city_plan = (["Mumbai"] * 8 + ["Pune"] * 6 +
                 [rng.choice(CITIES) for _ in range(N_LOCATIONS - 14)])
    rng.shuffle(city_plan)
    site_counts: Dict[str, int] = {}
    for lid in range(1, N_LOCATIONS + 1):
        cust = rng.choice(customers)
        city = city_plan[lid - 1]
        site_counts[city] = site_counts.get(city, 0) + 1
        kind = rng.choice(SITE_KINDS)
        rows.append({
            "location_id": lid,
            "customer_id": cust["customer_id"],
            "site_name": f"{city} {kind} {site_counts[city]:02d}",
            "city": city,
            "region": CITY_REGION.get(city, "West"),
        })
    return rows


def gen_assets(locations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    seq_by_prefix: Dict[str, int] = {}
    for aid in range(1, N_ASSETS + 1):
        type_name, prefix, category, value_range, crit_bias = rng.choice(ASSET_TYPES)
        seq_by_prefix[prefix] = seq_by_prefix.get(prefix, 0) + 1
        seq = seq_by_prefix[prefix]
        loc = rng.choice(locations)

        r = rng.random()
        if r < crit_bias * 0.5:
            criticality = "Critical"
        elif r < crit_bias:
            criticality = "High"
        else:
            criticality = rng.choices(["Critical", "High", "Medium", "Low"],
                                      weights=[8, 18, 42, 32])[0]

        sr = rng.random()
        if sr < 0.88:
            status = "Operational"
        elif sr < 0.94:
            status = "Under Maintenance"
        elif sr < 0.98:
            status = "Breakdown"
        else:
            status = "Decommissioned"

        install = date(rng.randint(2009, 2025), rng.randint(1, 12), rng.randint(1, 28))
        warranty = install + timedelta(days=rng.randint(365, 5 * 365))

        rows.append({
            "asset_id": aid,
            "asset_tag": f"{prefix}-{seq:04d}",
            "name": f"{type_name} {seq:03d}",
            "category": category,
            "manufacturer": rng.choice(MANUFACTURERS[type_name]),
            "model": f"{prefix}-{rng.randint(100, 999)}{rng.choice('ABCDE')}",
            "serial_number": f"SN-{install.year}-{rng.randint(100000, 999999)}",
            "location_id": loc["location_id"],
            "installation_date": fmt_d(install),
            "purchase_value": round(rng.uniform(*value_range), 2),
            "status": status,
            "criticality": criticality,
            "warranty_expiry": fmt_d(warranty),
        })
    return rows


def gen_asset_properties(assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    pid = 1
    common_props = [
        ("Operating Zone", lambda a: rng.choice(["Zone-A", "Zone-B", "Zone-C", "Utility Area", "Production Floor"])),
        ("AMC Vendor", lambda a: rng.choice(MANUFACTURERS.get(a["name"].rsplit(" ", 1)[0], MANUFACTURERS["Electrical Panel"]) + ["Local Vendor"])),
        ("Run Hours Total", lambda a: str(rng.randint(2000, 60000))),
        ("Commissioned By", lambda a: rng.choice(["Inhouse Team", "OEM Team", "Third Party Contractor"])),
        ("Maintenance Access", lambda a: rng.choice(["Front", "Rear", "Top", "Side"])),
    ]
    category_props = {
        "HVAC": [("Capacity (TR)", lambda: str(rng.randint(20, 800))),
                 ("Refrigerant", lambda: rng.choice(["R-134a", "R-410A", "R-22", "R-407C"])),
                 ("Compressor Type", lambda: rng.choice(["Scroll", "Screw", "Centrifugal", "Reciprocating"]))],
        "Electrical": [("Rated Voltage", lambda: rng.choice(["415V", "11kV", "33kV", "230V"])),
                       ("IP Rating", lambda: rng.choice(["IP54", "IP55", "IP65"])),
                       ("Rated Current (A)", lambda: str(rng.randint(63, 3200)))],
        "Mechanical": [("Rated Power (kW)", lambda: str(rng.randint(5, 250))),
                       ("Operating Pressure (bar)", lambda: str(round(rng.uniform(1, 12), 1)))],
        "Plumbing": [("Flow Rate (m3/h)", lambda: str(rng.randint(5, 500))),
                     ("Head (m)", lambda: str(rng.randint(10, 120)))],
        "Fire Safety": [("Certification", lambda: rng.choice(["UL Listed", "FM Approved", "ISI Marked"])),
                        ("Last Statutory Due", lambda: fmt_d(rnd_date(BASE_DATE, BASE_DATE + timedelta(days=400))))],
        "Security": [("Channel Count", lambda: str(rng.choice([4, 8, 16, 32]))),
                     ("Storage (TB)", lambda: str(rng.choice([2, 4, 8, 16])))],
        "IT": [("Rack Units", lambda: str(rng.choice([12, 24, 42]))),
               ("Redundancy", lambda: rng.choice(["N+1", "2N", "N"]))],
        "Vertical Transport": [("Floors Served", lambda: str(rng.randint(2, 25))),
                               ("Rated Load (kg)", lambda: str(rng.choice([408, 544, 680, 1000, 2000])))],
    }
    for a in assets:
        # 2-3 category-specific properties per asset
        chosen = rng.sample(category_props.get(a["category"], []), k=min(2, len(category_props.get(a["category"], []))))
        if not chosen:
            chosen = [("Notes", lambda: "Standard installation")]
        for prop_name, fn in chosen:
            rows.append({"property_id": pid, "asset_id": a["asset_id"],
                         "property_name": prop_name, "property_value": fn()})
            pid += 1
        # 1 common property
        name_, fn_ = rng.choice(common_props)
        rows.append({"property_id": pid, "asset_id": a["asset_id"],
                     "property_name": name_, "property_value": fn_(a)})
        pid += 1
    return rows


def gen_technicians() -> List[Dict[str, Any]]:
    rows = []
    for tid in range(1, N_TECHNICIANS + 1):
        rows.append({
            "technician_id": tid,
            "employee_code": f"TECH-{tid:03d}",
            "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            "specialization": rng.choice(TICKET_CATEGORIES),
            "skill_level": rng.choices(["Junior", "Senior", "Expert"],
                                       weights=[35, 45, 20])[0],
            "shift": rng.choices(["Morning", "Evening", "Night", "Rotational"],
                                 weights=[40, 30, 15, 15])[0],
            "contact": f"+91-{rng.randint(70000, 99999)}{rng.randint(10000, 99999)}",
            "base_city": rng.choice(CITIES),
        })
    return rows


def pick_problem_entities(assets, locations):
    problem_assets = set(rng.sample([a["asset_id"] for a in assets], N_PROBLEM_ASSETS))
    problem_locations = set(rng.sample([l["location_id"] for l in locations], N_PROBLEM_LOCATIONS))
    return problem_assets, problem_locations


def gen_tickets(assets, technicians, problem_assets, problem_locations):
    rows = []
    assets_by_id = {a["asset_id"]: a for a in assets}

    busy_techs = set(rng.sample([t["technician_id"] for t in technicians], N_BUSY_TECHNICIANS))
    slow_techs = set(rng.sample([t["technician_id"] for t in technicians], N_SLOW_TECHNICIANS))

    # Weighted asset pool -> problems concentrate on problem assets / problem locations
    pool = (list(problem_assets) * 8 +
            [a["asset_id"] for a in assets if a["location_id"] in problem_locations] * 2 +
            [a["asset_id"] for a in assets])

    techs_by_spec: Dict[str, List[Dict[str, Any]]] = {}
    for t in technicians:
        techs_by_spec.setdefault(t["specialization"], []).append(t)

    skill_factor = {"Junior": 1.45, "Senior": 1.0, "Expert": 0.75}
    res_range = {"Critical": (0.5, 6.0), "High": (1.0, 14.0),
                 "Medium": (2.0, 30.0), "Low": (4.0, 60.0)}
    sla_base = {"Critical": 4.0, "High": 8.0, "Medium": 24.0, "Low": 48.0}

    for tid in range(1, N_TICKETS + 1):
        asset = assets_by_id[rng.choice(pool)]
        category = asset["category"]

        candidates = techs_by_spec.get(category) or technicians
        weights = [3.0 if t["technician_id"] in busy_techs else 1.0 for t in candidates]
        tech = rng.choices(candidates, weights=weights)[0]

        created = rnd_dt(DATA_START_DT, BASE_DT)
        problem_loc = asset["location_id"] in problem_locations
        is_problem_asset = asset["asset_id"] in problem_assets

        if is_problem_asset:
            priority = rng.choices(["Critical", "High", "Medium", "Low"],
                                   weights=[22, 38, 28, 12])[0]
        else:
            priority = rng.choices(["Critical", "High", "Medium", "Low"],
                                   weights=[10, 26, 42, 22])[0]

        sla_hours = sla_base[priority] + rng.choice([0, 0, 0, 2, 4])

        base = rng.uniform(*res_range[priority])
        tf = skill_factor[tech["skill_level"]]
        if tech["technician_id"] in slow_techs:
            tf *= rng.uniform(1.5, 2.3)
        af = rng.uniform(1.4, 2.1) if is_problem_asset else rng.uniform(0.85, 1.2)
        lf = rng.uniform(1.15, 1.5) if problem_loc else 1.0
        resolution_hours = round(base * tf * af * lf, 1)

        age_hours = (BASE_DT - created).total_seconds() / 3600.0
        roll = rng.random()
        problem = rng.choice(PROBLEMS.get(category, GENERIC_PROBLEMS))

        if roll < 0.80:  # Closed
            status = "Closed"
            closed_at = created + timedelta(hours=resolution_hours)
            sla_breached = 1 if resolution_hours > sla_hours else 0
            resolution = rng.choice(RESOLUTIONS)
        else:
            status = rng.choice(["Open", "In Progress"])
            closed_at = None
            resolution = None
            resolution_hours = None
            if age_hours > sla_hours:
                status = "Overdue"
                sla_breached = 1
            else:
                sla_breached = 0

        rows.append({
            "ticket_id": tid,
            "asset_id": asset["asset_id"],
            "location_id": asset["location_id"],
            "technician_id": tech["technician_id"],
            "created_at": fmt_dt(created),
            "closed_at": fmt_dt(closed_at) if closed_at else None,
            "status": status,
            "priority": priority,
            "category": category,
            "problem": problem,
            "resolution": resolution,
            "resolution_hours": resolution_hours,
            "sla_hours": sla_hours,
            "sla_breached": sla_breached,
        })
    return rows


def gen_ppm(assets, problem_locations):
    rows = []
    for pid in range(1, N_PPM + 1):
        a = rng.choice(assets)
        category = a["category"]
        task = rng.choice(PPM_TASKS.get(category, GENERIC_PPM_TASKS))
        frequency = rng.choices(["Monthly", "Quarterly", "Half-Yearly", "Annual"],
                                weights=[30, 35, 20, 15])[0]
        due = rnd_date(BASE_DATE - timedelta(days=365), BASE_DATE + timedelta(days=90))

        is_problem_loc = a["location_id"] in problem_locations
        if due < BASE_DATE:  # past due
            comply_p = 0.45 if is_problem_loc else 0.78
            if rng.random() < comply_p:
                status = "Completed"
                completed = due + timedelta(days=rng.randint(-3, 10))
                completed = min(completed, BASE_DATE)
                completed_s = fmt_d(completed)
            else:
                status = "Overdue"
                completed_s = None
        else:
            status = "Scheduled" if rng.random() < 0.85 else "In Progress"
            completed_s = None

        rows.append({
            "ppm_id": pid,
            "asset_id": a["asset_id"],
            "location_id": a["location_id"],
            "task": task,
            "frequency": frequency,
            "due_date": fmt_d(due),
            "completed_date": completed_s,
            "status": status,
        })
    return rows


def gen_inspections(assets, technicians, problem_assets):
    rows = []
    for iid in range(1, N_INSPECTIONS + 1):
        # concentrate FAILs: 60% of inspections go to problem assets
        if rng.random() < 0.6:
            asset = next(a for a in assets if a["asset_id"] in problem_assets)
        else:
            asset = rng.choice(assets)
        inspector = rng.choice(technicians)
        insp_date = rnd_date(HISTORY_START, BASE_DATE)
        is_problem = asset["asset_id"] in problem_assets

        if is_problem:
            result = rng.choices(["FAIL", "Pass With Observations", "PASS"],
                                 weights=[55, 20, 25])[0]
        else:
            result = rng.choices(["FAIL", "Pass With Observations", "PASS"],
                                 weights=[12, 16, 72])[0]

        if result == "PASS":
            score = rng.randint(80, 100)
            remarks = rng.choice(INSPECTION_PASS_REMARKS)
        elif result == "Pass With Observations":
            score = rng.randint(60, 79)
            remarks = rng.choice(INSPECTION_OBS_REMARKS)
        else:
            score = rng.randint(30, 59)
            remarks = rng.choice(INSPECTION_FAIL_REMARKS)

        rows.append({
            "inspection_id": iid,
            "asset_id": asset["asset_id"],
            "location_id": asset["location_id"],
            "inspection_date": fmt_d(insp_date),
            "inspector_id": inspector["technician_id"],
            "inspection_type": rng.choice(INSPECTION_TYPES),
            "result": result,
            "score": score,
            "remarks": remarks,
        })
    return rows


def gen_inventory(locations, problem_locations):
    rows = []
    for pid in range(1, N_INVENTORY + 1):
        name, category, cost_range = rng.choice(PARTS_POOL)
        loc = rng.choice(locations)
        minimum = rng.randint(5, 25)

        low_prob = 0.40 if loc["location_id"] in problem_locations else 0.12
        if rng.random() < low_prob:
            stock = rng.randint(0, max(1, minimum - 1))   # BELOW minimum
        else:
            stock = minimum + rng.randint(0, minimum * 6)

        rows.append({
            "part_id": pid,
            "location_id": loc["location_id"],
            "part_number": f"PN-{rng.randint(10000, 99999)}",
            "name": name,
            "category": category,
            "stock": stock,
            "minimum_stock": minimum,
            "unit_cost": round(rng.uniform(*cost_range), 2),
            "supplier": rng.choice(SUPPLIERS),
        })
    return rows


def gen_downtime(assets, problem_assets):
    rows = []
    assets_by_id = {a["asset_id"]: a for a in assets}
    # 55% of downtime concentrated on problem assets with a rising recent trend
    pool = (list(problem_assets) * 9 +
            [a["asset_id"] for a in assets])

    for did in range(1, N_DOWNTIME + 1):
        aid = rng.choice(pool)
        a = assets_by_id[aid]
        start_dt = rnd_dt(DATA_START_DT, BASE_DT)

        # normalized recency t: 0 = oldest, 1 = most recent
        t = (start_dt - DATA_START_DT).total_seconds() / (BASE_DT - DATA_START_DT).total_seconds()

        is_problem = aid in problem_assets
        if is_problem:
            hours = rng.uniform(3.0, 40.0) * (0.6 + 1.3 * t)   # rising downtime pattern
            reason = rng.choice(DOWN_REASONS_UNPLANNED)
        else:
            hours = rng.uniform(0.5, 8.0)
            reason = rng.choice(DOWN_REASONS_PLANNED + DOWN_REASONS_UNPLANNED)

        hours = round(min(hours, 96.0), 1)
        end_dt = start_dt + timedelta(hours=hours)

        rows.append({
            "downtime_id": did,
            "asset_id": aid,
            "location_id": a["location_id"],
            "start_time": fmt_dt(start_dt),
            "end_time": fmt_dt(end_dt),
            "hours": hours,
            "reason": reason,
        })
    return rows


def gen_costs(assets, problem_assets):
    rows = []
    assets_by_id = {a["asset_id"]: a for a in assets}
    cid = 1

    def size_factor(a):
        return max(0.5, min(4.0, a["purchase_value"] / 1_000_000))

    # Rising-cost pattern for the first ~12 problem assets
    rising = sorted(problem_assets)[:12]
    for aid in rising:
        a = assets_by_id[aid]
        n_months = rng.randint(8, 12)
        base_amt = rng.uniform(12000, 45000) * size_factor(a)
        for i in range(n_months):
            d = BASE_DATE - timedelta(days=30 * (n_months - 1 - i)) + timedelta(days=rng.randint(-4, 4))
            d = min(d, BASE_DATE)
            amt = base_amt * (1 + 0.13 * i) * rng.uniform(0.85, 1.15)   # upward trend
            rows.append({
                "cost_id": cid, "asset_id": aid, "location_id": a["location_id"],
                "date": fmt_d(d),
                "cost_type": rng.choice(["Corrective", "Emergency Repair", "Spare Parts", "Labour"]),
                "amount": round(amt, 2),
            })
            cid += 1

    # Remaining costs spread across all assets
    while cid <= N_COSTS:
        a = rng.choice(assets)
        d = rnd_date(HISTORY_START, BASE_DATE)
        if rng.random() < 0.07:   # occasional big overhaul
            amount = rng.uniform(60000, 250000) * size_factor(a) * 0.5
            ctype = "Overhaul"
        else:
            amount = rng.uniform(800, 25000) * size_factor(a)
            ctype = rng.choice(COST_TYPES)
        rows.append({
            "cost_id": cid, "asset_id": a["asset_id"], "location_id": a["location_id"],
            "date": fmt_d(d), "cost_type": ctype, "amount": round(amount, 2),
        })
        cid += 1
    return rows


def gen_meter_readings(assets, problem_assets):
    rows = []
    assets_by_id = {a["asset_id"]: a for a in assets}
    # abnormal meters: subset of problem assets
    abnormal = set(sorted(problem_assets)[:8])

    # choose assets round-robin-ish until we hit N_METERS
    meter_assets = [a for a in assets if a["asset_id"] in abnormal]
    meter_assets += rng.sample([a for a in assets if a["asset_id"] not in abnormal],
                               k=max(0, min(len(assets) - len(meter_assets), 120)))
    if not meter_assets:
        meter_assets = assets[:50]

    rid = 1
    i = 0
    while rid <= N_METERS:
        a = meter_assets[i % len(meter_assets)]
        i += 1
        profiles = METER_PROFILES.get(a["category"], GENERIC_METERS)
        meter_type, unit, (lo, hi) = rng.choice(profiles)
        base = rng.uniform(lo, hi)

        # 12 monthly readings per chosen asset/profile
        for m in range(12):
            if rid > N_METERS:
                break
            d = BASE_DATE - timedelta(days=30 * (11 - m)) + timedelta(days=rng.randint(-3, 3))
            d = min(d, BASE_DATE)
            value = base * rng.uniform(0.92, 1.08)
            if a["asset_id"] in abnormal and m >= 9:
                spike = [1.5, 1.9, 2.4][m - 9] * rng.uniform(0.9, 1.1)
                value *= spike        # abnormal (spiking) readings
            rows.append({
                "reading_id": rid,
                "asset_id": a["asset_id"],
                "meter_type": meter_type,
                "reading_date": fmt_d(d),
                "value": round(value, 2),
                "unit": unit,
            })
            rid += 1
    return rows


# ==============================================================================
# DATABASE BOOTSTRAP
# ==============================================================================

def insert_rows(conn: sqlite3.Connection, table: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def build_database() -> Dict[str, int]:
    """Create + populate the SQLite database. Deterministic on every run."""
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)

    print("Generating deterministic CMMS data ...")
    customers = gen_customers()
    locations = gen_locations(customers)
    assets = gen_assets(locations)
    asset_properties = gen_asset_properties(assets)
    technicians = gen_technicians()
    problem_assets, problem_locations = pick_problem_entities(assets, locations)
    tickets = gen_tickets(assets, technicians, problem_assets, problem_locations)
    ppm = gen_ppm(assets, problem_locations)
    inspections = gen_inspections(assets, technicians, problem_assets)
    inventory = gen_inventory(locations, problem_locations)
    downtime = gen_downtime(assets, problem_assets)
    costs = gen_costs(assets, problem_assets)
    meters = gen_meter_readings(assets, problem_assets)

    data = {
        "customers": customers,
        "locations": locations,
        "assets": assets,
        "asset_properties": asset_properties,
        "technicians": technicians,
        "tickets": tickets,
        "ppm": ppm,
        "inspections": inspections,
        "inventory": inventory,
        "downtime": downtime,
        "maintenance_costs": costs,
        "meter_readings": meters,
    }

    conn = sqlite3.connect(DB_FILE)
    try:
        for table in TABLE_ORDER:
            conn.execute(CREATE_SQL[table])
            insert_rows(conn, table, data[table])
        conn.commit()
    finally:
        conn.close()

    return {t: len(data[t]) for t in TABLE_ORDER}


# ==============================================================================
# FASTAPI APPLICATION
# ==============================================================================

app = FastAPI(
    title="Mock CMMS API",
    description="Fake CMMS/O&M backend for local AI Copilot testing. "
                "NOT connected to any real Metaagrow system.",
    version="1.0.0",
)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/")
def root():
    return {
        "service": "Mock CMMS API",
        "database": DB_FILE,
        "endpoints": [
            "GET /api/tables",
            "GET /api/data/{table}?limit=&offset=&search=",
            "GET /api/data/{table}/{id}",
            "GET /health",
        ],
        "tables": TABLE_ORDER,
    }


@app.get("/health")
def health():
    return {"status": "ok", "database": DB_FILE}


@app.get("/api/tables")
def list_tables():
    conn = get_conn()
    try:
        tables = []
        for table in TABLE_ORDER:
            cols = TABLE_SCHEMAS[table]
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            tables.append({"name": table, "columns": cols, "row_count": count})
        return {"database": DB_FILE, "table_count": len(tables), "tables": tables}
    finally:
        conn.close()


@app.get("/api/data/{table}")
def read_table(
    table: str,
    limit: int = Query(1000, ge=1, le=20000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip"),
    search: Optional[str] = Query(None, description="Search term across all columns"),
):
    if table not in TABLE_SCHEMAS:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table}'. Available: {TABLE_ORDER}")

    cols = TABLE_SCHEMAS[table]
    where_sql = ""
    params: List[Any] = []

    if search:
        like_clauses = " OR ".join(f'"{c}" LIKE ?' for c in cols)
        where_sql = f"WHERE {like_clauses}"
        params = [f"%{search}%"] * len(cols)

    conn = get_conn()
    try:
        total = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" {where_sql}', params
        ).fetchone()[0]
        rows = conn.execute(
            f'SELECT * FROM "{table}" {where_sql} ORDER BY rowid LIMIT ? OFFSET ?',
            params + [limit, offset],
        ).fetchall()
        return {
            "table": table,
            "total_rows": total,
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "data": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@app.get("/api/data/{table}/{row_id}")
def read_row(table: str, row_id: str):
    if table not in TABLE_SCHEMAS:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table}'. Available: {TABLE_ORDER}")

    id_col = TABLE_SCHEMAS[table][0]
    try:
        pk = int(row_id)
    except ValueError:
        pk = row_id

    conn = get_conn()
    try:
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{id_col}" = ?', (pk,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"{table} '{row_id}' not found")
        return {"table": table, "data": dict(row)}
    finally:
        conn.close()


# ==============================================================================
# MAIN
# ==============================================================================

def print_banner(counts: Dict[str, int]) -> None:
    print()
    print("=" * 46)
    print("           Mock CMMS API")
    print("=" * 46)
    print(f"Database: {DB_FILE}")
    print()
    print(f"Customers:         {counts['customers']}")
    print(f"Locations:         {counts['locations']}")
    print(f"Assets:            {counts['assets']}")
    print(f"Asset Properties:  {counts['asset_properties']}")
    print(f"Technicians:       {counts['technicians']}")
    print(f"Tickets:           {counts['tickets']}")
    print(f"PPM:               {counts['ppm']}")
    print(f"Inspections:       {counts['inspections']}")
    print(f"Inventory:         {counts['inventory']}")
    print(f"Downtime:          {counts['downtime']}")
    print(f"Maintenance Costs: {counts['maintenance_costs']}")
    print(f"Meter Readings:    {counts['meter_readings']}")
    print()
    print("API running:")
    print(f"  http://{HOST}:{PORT}")
    print(f"  Docs: http://{HOST}:{PORT}/docs")
    print(f"  Tables:   GET /api/tables")
    print(f"  Data:     GET /api/data/{{table}}?limit=&offset=&search=")
    print("=" * 46)
    print()


if __name__ == "__main__":
    print("Mock CMMS Server — deterministic seed:", SEED)
    counts = build_database()
    print_banner(counts)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
