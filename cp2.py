#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 copilot.py — DYNAMIC CMMS AI COPILOT (Web UI + CLI) — MEGA EXPORT EDITION
================================================================================
 Web UI   : http://127.0.0.1:8002   (default port is now 8002)
 Mock API : http://127.0.0.1:8001   (set MOCK_API_BASE in .env if different)

 DOWNLOADS: TXT | MD | HTML | CSV | XLSX (multi-sheet) | PDF | JSON |
            Chart PNG | ZIP bundle (everything) | raw table -> CSV/XLSX

 CHARTS   : bar, barh, line, pie, doughnut, radar, polarArea (Chart.js)
            + fullscreen modal + client-side PNG export + matplotlib PNG/PDF

 CLI      : python copilot.py --cli
            python copilot.py --cli "top assets by downtime"

 .env
   GEMINI_API_KEY=...
   GEMINI_MODEL=gemini-3.5-flash-lite
   (optional) COPILOT_HOST=127.0.0.1  COPILOT_PORT=8002  MOCK_API_BASE=http://127.0.0.1:8001

 DEPENDENCIES:
   pip install requests pandas fastapi uvicorn matplotlib openpyxl reportlab
================================================================================
"""

import os
import sys
import io
import json
import re
import time
import uuid
import base64
import zipfile
import threading
import datetime as _dt
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------ imports --
def _die(msg: str):
    print(msg)
    sys.exit(1)

try:
    import requests
except ImportError:
    _die("ERROR: 'requests' required  ->  pip install requests")

try:
    import pandas as pd
except ImportError:
    _die("ERROR: 'pandas' required  ->  pip install pandas")

try:
    import numpy as np
except ImportError:
    np = None

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, Response, PlainTextResponse
    import uvicorn
except ImportError:
    _die("ERROR: fastapi/uvicorn required  ->  pip install fastapi uvicorn")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False

try:
    import openpyxl  # noqa: F401
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    HAS_XLSX = True
except Exception:
    HAS_XLSX = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors as rl_colors
    from reportlab.pdfgen import canvas as pdfcanvas
    HAS_PDF = True
except Exception:
    HAS_PDF = False

# ==============================================================================
# CONFIG
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

HTTP_TIMEOUT = 90
MAX_ROWS_PER_TABLE = 20000
FETCH_PAGE = 2000
MAX_RESULT_CHARS = 14000
MAX_CODE_ATTEMPTS = 3
MAX_JOBS_KEPT = 100


def load_env(path: str = ENV_FILE) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    return vals


load_env()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL)
API_BASE = os.environ.get("MOCK_API_BASE", "http://127.0.0.1:8001").strip().rstrip("/")
COPILOT_HOST = os.environ.get("COPILOT_HOST", "127.0.0.1").strip()
COPILOT_PORT = int(os.environ.get("COPILOT_PORT", "8002"))

DATA_CONTEXT = (
    "DATA CONTEXT: This is a local mock CMMS. Historical data spans roughly "
    "2024-07 to 2026-06. Treat 'today' for analysis purposes as 2026-06-30. "
    "All dates are strings ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'); parse with "
    "pd.to_datetime when needed. 'sla_breached' is 0/1."
)

STEP_NAMES = [
    ("understand", "Understanding request"),
    ("plan",       "Selecting required data"),
    ("fetch",      "Fetching data from API"),
    ("analyze",    "Running Python analysis"),
    ("chart",      "Creating chart"),
    ("report",     "Generating report"),
]
STEP_KEYS = [k for k, _ in STEP_NAMES]

CHART_KINDS = ("line", "bar", "barh", "pie", "doughnut", "radar", "polarArea")

# ==============================================================================
# GEMINI CLIENT
# ==============================================================================

class GeminiError(RuntimeError):
    pass


class Gemini:
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise GeminiError(
                "\n" + "=" * 62 +
                "\n ERROR: GEMINI_API_KEY is missing."
                "\n Create a '.env' file next to copilot.py:"
                "\n     GEMINI_API_KEY=YOUR_KEY"
                "\n     GEMINI_MODEL=gemini-3.5-flash-lite"
                "\n" + "=" * 62)
        self.key, self.model = api_key, model
        self.url = GEMINI_URL.format(model=model, key=api_key)

    def generate(self, prompt: str, json_mode: bool = False,
                 temperature: float = 0.2, max_output_tokens: int = 2048) -> str:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_output_tokens},
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        last = ""
        for attempt in range(1, 5):
            try:
                r = requests.post(self.url, json=payload, timeout=HTTP_TIMEOUT)
            except requests.RequestException as e:
                last = f"network error: {e}"; time.sleep(2 * attempt); continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts)
                    if text.strip():
                        return text
                    last = "empty response"
                except (KeyError, IndexError, ValueError) as e:
                    last = f"bad response shape: {e}"
            elif r.status_code in (429, 500, 502, 503):
                last = f"HTTP {r.status_code} (transient)"; time.sleep(2.5 * attempt)
            elif r.status_code == 404:
                raise GeminiError(f"Model '{self.model}' not found (404). "
                                  f"Set GEMINI_MODEL in .env (e.g. gemini-2.0-flash).")
            else:
                detail = ""
                try:
                    detail = r.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                raise GeminiError(f"Gemini HTTP {r.status_code}. {detail}")
        raise GeminiError(f"Gemini failed after retries: {last}")


# ==============================================================================
# MOCK API CLIENT
# ==============================================================================

class MockApi:
    def __init__(self, base: str):
        self.base = base

    def check(self) -> bool:
        try:
            return requests.get(f"{self.base}/health", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def tables(self) -> List[Dict[str, Any]]:
        r = requests.get(f"{self.base}/api/tables", timeout=15)
        r.raise_for_status()
        return r.json()["tables"]

    def fetch_table(self, table: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        offset = 0
        while len(rows) < MAX_ROWS_PER_TABLE:
            params: Dict[str, Any] = {"limit": FETCH_PAGE, "offset": offset}
            if search:
                params["search"] = search
            r = requests.get(f"{self.base}/api/data/{table}", params=params,
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            batch = body.get("data", [])
            rows.extend(batch)
            total = body.get("total_rows", len(rows))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return rows[:MAX_ROWS_PER_TABLE]

    def browse(self, table: str, limit: int, offset: int, search: Optional[str] = None):
        params = {"limit": limit, "offset": offset}
        if search:
            params["search"] = search
        r = requests.get(f"{self.base}/api/data/{table}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()


# ==============================================================================
# SMALL HELPERS
# ==============================================================================

def parse_json_loose(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise RuntimeError(f"Could not parse JSON from model:\n{raw[:400]}")


def tables_digest(meta: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"- {t['name']} ({t['row_count']} rows): {', '.join(t['columns'])}"
        for t in meta)


def table_samples(df: pd.DataFrame, n: int = 3) -> str:
    try:
        head = df.head(n).copy()
        for c in head.columns:
            head[c] = head[c].astype(str).str.slice(0, 60)
        return head.to_json(orient="records", force_ascii=False)
    except Exception:
        return "[]"


def slugify(text: str, max_len: int = 48) -> str:
    t = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return t[:max_len] or "report"


def _json_default(o: Any) -> Any:
    if np is not None:
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()[:60]
    if isinstance(o, (pd.Timestamp, _dt.datetime, _dt.date)):
        return str(o)
    if isinstance(o, (set, tuple, frozenset)):
        return list(o)[:60]
    return str(o)


# ==============================================================================
# STEP 1 — PLANNER
# ==============================================================================

def plan_tables(gem: Gemini, question: str, meta: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [t["name"] for t in meta]
    prompt = f"""You are the DATA PLANNER of a CMMS analytics copilot.

{DATA_CONTEXT}

AVAILABLE API TABLES (each becomes one pandas DataFrame named exactly as the table):
{tables_digest(meta)}

USER QUESTION:
\"\"\"{question}\"\"\"

Decide which tables are required to answer accurately.
Rules:
1. Only choose from the available list.
2. Prefer the MINIMAL set that still allows a correct answer (DataFrames can be
   merged in pandas; e.g. assets has location_id, locations has city).
3. City/region comparisons need "locations". Technician questions need "technicians".
4. Trend analysis needs a table with a date column.

Respond with ONLY JSON, no markdown:
{{
  "understanding": "one or two sentences rephrasing the request",
  "question_type": "ranking|comparison|trend|diagnostic|summary|lookup|executive|other",
  "tables": ["table1", "table2"],
  "search": {{"optional_table": "optional search term to pre-filter that table"}},
  "reason": "short selection reason"
}}

Valid tables: {valid}
"""
    plan = parse_json_loose(gem.generate(prompt, json_mode=True, temperature=0.1,
                                         max_output_tokens=700))
    picked = [t for t in (plan.get("tables") or []) if t in valid]
    if not picked:
        raise RuntimeError(f"Planner selected no valid tables: {plan.get('tables')}")
    plan["tables"] = picked
    plan["search"] = {k: str(v) for k, v in (plan.get("search") or {}).items() if k in picked}
    plan.setdefault("understanding", question)
    plan.setdefault("question_type", "other")
    return plan


# ==============================================================================
# STEP 2 — ANALYST
# ==============================================================================

ANALYST_RULES = """You are the ANALYSIS CODE GENERATOR of a CMMS analytics copilot.
You write ONE Python program that computes the answer using pandas.

STRICT RULES:
1. The DataFrames listed below already exist as variables named exactly like the
   tables (e.g. variable `assets` IS the assets DataFrame). Also available:
   `pd` (pandas) and `np` (numpy).
2. You MUST NOT: import anything, use dunder names (__xxx__), open files, use
   network, eval/exec/compile/getattr/setattr, pd.read_csv/read_sql/...,
   df.to_csv/to_sql/..., os/sys/subprocess/requests, input(). They are blocked
   and will crash the program. Do not even write the words in comments.
3. Your code MUST define a variable named `result` at the end.
   - Prefer a pandas DataFrame with clear, human-readable column names.
   - Rankings/Top-N: sort appropriately, keep at most ~15-20 rows.
   - Comparisons: one row per group with a few metric columns.
   - Trends: one row per time bucket, chronological.
   - Rates/compliance: include totals AND percentage.
   - You may also define `summary` (dict of headline numbers, optional).
4. OPTIONAL CHART: if a chart genuinely helps, ALSO define:
       chart_data  = DataFrame or Series ready to plot
                     (index or first column = labels; numeric columns = series)
       chart_kind  = "line" | "bar" | "barh" | "pie" | "doughnut" | "radar" | "polarArea"
       chart_title = short title string
   The runner renders it. Never touch files or plt yourself.
5. Dates are strings: use pd.to_datetime(df["col"]) for time grouping/filtering.
6. Handle missing values (dropna/fillna) so the code never crashes.
7. No printing, no comments containing blocked words. Compute and assign only.
"""


def build_analyst_prompt(question, plan, frames, meta, error_feedback=None) -> str:
    mmap = {t["name"]: t for t in meta}
    sections = []
    for name in plan["tables"]:
        df = frames[name]
        info = mmap.get(name, {})
        sections.append(
            f"### DataFrame `{name}`  shape={df.shape}\n"
            f"columns: {', '.join(info.get('columns', list(df.columns)))}\n"
            f"sample rows (JSON): {table_samples(df)}")
    fb = ""
    if error_feedback:
        fb = (f"\nPREVIOUS ATTEMPT FAILED. Fix it and return corrected code.\n"
              f"--- previous code ---\n{error_feedback.get('code','')}\n"
              f"--- execution error ---\n{error_feedback.get('error','')}\n"
              f"----------------------\n")
    return f"""{ANALYST_RULES}

{DATA_CONTEXT}

PLANNER CONTEXT:
- understanding : {plan.get('understanding')}
- question_type : {plan.get('question_type')}
- tables chosen : {plan.get('tables')}

DATAFRAMES AVAILABLE (already loaded; do NOT fetch anything):
{chr(10).join(sections)}

USER QUESTION:
\"\"\"{question}\"\"\"
{fb}
Return ONLY a JSON object (no markdown fences):
{{
  "analysis_code": "the python program as one string",
  "explanation": "one or two sentences describing what it computes"
}}
"""


# ==============================================================================
# STEP 3 — SAFE EXECUTION
# ==============================================================================

class CodeSafetyError(RuntimeError):
    pass


SAFE_BUILTIN_NAMES = [
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "int", "isinstance", "issubclass", "len",
    "list", "map", "max", "min", "pow", "range", "repr", "round", "set",
    "sorted", "str", "sum", "tuple", "type", "zip", "print",
]

BLOCKED_REGEXES = [
    (r"\bimport\b", "import"),
    (r"__\w*__", "dunder name"),
    (r"\beval\s*\(", "eval("),
    (r"\bexec\s*\(", "exec("),
    (r"\bcompile\s*\(", "compile("),
    (r"\bgetattr\s*\(", "getattr("),
    (r"\bsetattr\s*\(", "setattr("),
    (r"\bdelattr\s*\(", "delattr("),
    (r"\bglobals\s*\(", "globals("),
    (r"\blocals\s*\(", "locals("),
    (r"\bopen\s*\(", "open("),
    (r"\binput\s*\(", "input("),
    (r"\bos\s*\.", "os."),
    (r"\bsys\s*\.", "sys."),
    (r"\bsubprocess\b", "subprocess"),
    (r"\bsocket\b", "socket"),
    (r"\burllib\b", "urllib"),
    (r"\brequests\b", "requests"),
    (r"\bshutil\b", "shutil"),
    (r"\bpathlib\b", "pathlib"),
    (r"\bPath\s*\(", "Path("),
    (r"\benviron\b", "environ"),
    (r"\bread_(csv|excel|json|sql|html|pickle|feather|parquet|clipboard)\b", "pd.read_*"),
    (r"\bto_(csv|excel|sql|pickle|json|html|clipboard|records)\b", "df.to_*"),
    (r"\bplt\b", "plt"),
]


def sanitize_code(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\s*", "", code)
        code = re.sub(r"\s*```$", "", code).strip()
    for pattern, label in BLOCKED_REGEXES:
        if re.search(pattern, code):
            raise CodeSafetyError(f"blocked construct detected: {label}")
    return code


def build_safe_namespace(frames: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    import builtins as _b
    safe = {n: getattr(_b, n) for n in SAFE_BUILTIN_NAMES if hasattr(_b, n)}
    ns: Dict[str, Any] = {"__builtins__": safe, "pd": pd, "np": np}
    ns.update(frames)
    return ns


def run_analysis_code(code: str, frames: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    ns = build_safe_namespace(frames)
    try:
        compiled = compile(code, "<analysis>", "exec")
    except SyntaxError as e:
        raise RuntimeError(f"SyntaxError in generated code: {e}")
    exec(compiled, ns)  # noqa: restricted namespace, sanitized input
    return ns


def extract_outputs(ns: Dict[str, Any]) -> Dict[str, Any]:
    result = ns.get("result", None)
    if result is None:
        raise RuntimeError("generated code did not define a `result` variable")

    if isinstance(result, pd.Series):
        result = result.to_frame(name=str(result.name or "value"))
    if isinstance(result, (int, float, str, bool)):
        result = pd.DataFrame([{"value": result}])
    if not isinstance(result, pd.DataFrame):
        result = pd.DataFrame({"value": list(result)[:50]}) if isinstance(result, (list, tuple)) else None
    if result is None:
        raise RuntimeError("`result` must be a DataFrame/Series/scalar")

    chart = ns.get("chart_data", None)
    if isinstance(chart, pd.Series):
        chart = chart.to_frame(name=str(chart.name or "value"))
    if chart is not None and not isinstance(chart, pd.DataFrame):
        chart = None

    kind = ns.get("chart_kind", None)
    kind = str(kind).lower().strip("'\" ") if isinstance(kind, str) else None
    if kind not in CHART_KINDS:
        kind = None
    title = ns.get("chart_title") if isinstance(ns.get("chart_title"), str) else None
    summary = ns.get("summary") if isinstance(ns.get("summary"), dict) else None

    return {"result": result, "chart_data": chart, "chart_kind": kind,
            "chart_title": title, "summary": summary}


# ==============================================================================
# SERIALIZATION / CHART CONFIG
# ==============================================================================

def serialize_result(obj: Any, max_rows: int = 40, max_chars: int = MAX_RESULT_CHARS) -> str:
    payload: Dict[str, Any] = {}
    if isinstance(obj, pd.Series):
        obj = obj.to_frame(name=str(obj.name or "value"))
    if isinstance(obj, pd.DataFrame):
        payload["type"] = "dataframe"
        payload["shape"] = list(obj.shape)
        payload["columns"] = [str(c) for c in obj.columns]
        payload["dtypes"] = {str(c): str(t) for c, t in obj.dtypes.items()}
        payload["rows"] = json.loads(
            obj.head(max_rows).to_json(orient="records", force_ascii=False,
                                       default_handler=str))
        if len(obj) > max_rows:
            payload["note"] = f"showing first {max_rows} of {len(obj)} rows"
    else:
        payload["type"] = type(obj).__name__
        try:
            json.dumps(obj, default=_json_default)
            payload["value"] = obj
        except Exception:
            payload["value"] = str(obj)
    if isinstance(obj, pd.DataFrame) and not obj.empty:
        try:
            payload["describe_numeric"] = json.loads(
                obj.describe(include="number").round(2).to_json(force_ascii=False))
        except Exception:
            pass
    text = json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    return text


def df_to_chart_config(df: pd.DataFrame, kind: str, title: str) -> Optional[Dict[str, Any]]:
    """DataFrame -> Chart.js-ready config (rendered interactively in the UI)."""
    try:
        d = df.copy()
        if any(pd.api.types.is_numeric_dtype(d[c]) for c in d.columns):
            labels = [str(i) for i in d.index]
            numeric = d
        else:
            label_col = d.columns[0]
            labels = [str(v) for v in d[label_col]]
            numeric = d.drop(columns=[label_col])
            numeric.index = labels
        numeric = numeric.select_dtypes(include="number")
        if numeric.empty:
            return None
        if kind in ("pie", "doughnut", "polarArea"):
            numeric = numeric.iloc[:, :1]
        numeric = numeric.iloc[:60, :5]

        labels = [str(i) for i in numeric.index]
        datasets = []
        for col in numeric.columns:
            vals = []
            for v in numeric[col].tolist():
                try:
                    vals.append(None if pd.isna(v) else round(float(v), 4))
                except Exception:
                    vals.append(None)
            datasets.append({"label": str(col), "data": vals})
        return {"kind": kind or "bar", "title": title or "Chart",
                "labels": labels, "datasets": datasets}
    except Exception:
        return None


def chart_config_to_png(cfg: Dict[str, Any], out_path: str) -> Optional[str]:
    if not HAS_MPL:
        return None
    try:
        import matplotlib.pyplot as _plt
        kind = cfg.get("kind", "bar")
        if kind in ("doughnut", "radar", "polarArea"):
            kind = "pie" if kind == "doughnut" else "line"
        labels = cfg["labels"]
        datasets = cfg["datasets"]
        fig, ax = _plt.subplots(figsize=(10, 5.5))
        x = range(len(labels))
        if kind == "pie":
            vals = datasets[0]["data"]
            ax.pie([v or 0 for v in vals], labels=labels, autopct="%1.1f%%")
        elif kind == "line":
            for ds in datasets:
                ax.plot(x, ds["data"], marker="o", label=ds["label"])
            ax.legend()
        elif kind == "barh":
            for ds in datasets:
                ax.barh(x, ds["data"], label=ds["label"])
            ax.set_yticks(x); ax.set_yticklabels(labels)
            ax.legend()
        else:
            w = 0.8 / max(1, len(datasets))
            for i, ds in enumerate(datasets):
                ax.bar([v + i * w for v in x], ds["data"], width=w, label=ds["label"])
            ax.set_xticks([v + w * (len(datasets) - 1) / 2 for v in x])
            ax.set_xticklabels(labels, rotation=30, ha="right")
            ax.legend()
        ax.set_title(cfg.get("title", "Chart"))
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_path, dpi=130)
        _plt.close(fig)
        return out_path
    except Exception:
        return None


def chart_config_to_svg_bytes(cfg: Dict[str, Any]) -> Optional[bytes]:
    if not HAS_MPL:
        return None
    try:
        import matplotlib.pyplot as _plt
        kind = cfg.get("kind", "bar")
        if kind in ("doughnut", "radar", "polarArea"):
            kind = "pie" if kind == "doughnut" else "line"
        labels, datasets = cfg["labels"], cfg["datasets"]
        fig, ax = _plt.subplots(figsize=(10, 5.5))
        x = range(len(labels))
        if kind == "pie":
            ax.pie([v or 0 for v in datasets[0]["data"]], labels=labels, autopct="%1.1f%%")
        elif kind == "line":
            for ds in datasets:
                ax.plot(x, ds["data"], marker="o", label=ds["label"])
            ax.legend()
        elif kind == "barh":
            for ds in datasets:
                ax.barh(x, ds["data"], label=ds["label"])
            ax.set_yticks(x); ax.set_yticklabels(labels)
        else:
            w = 0.8 / max(1, len(datasets))
            for i, ds in enumerate(datasets):
                ax.bar([v + i * w for v in x], ds["data"], width=w, label=ds["label"])
            ax.legend()
        ax.set_title(cfg.get("title", "Chart"))
        buf = io.BytesIO()
        fig.savefig(buf, format="svg", bbox_inches="tight")
        _plt.close(fig)
        return buf.getvalue()
    except Exception:
        return None


# ==============================================================================
# RESULT STORES (full DataFrames for Excel/CSV/ZIP exports)
# ==============================================================================

RESULT_FRAMES: Dict[str, pd.DataFrame] = {}
RESULT_CHARTS: Dict[str, pd.DataFrame] = {}
ENGINE_ERROR: Optional[str] = None
BOOT_STARTED_AT: Optional[str] = None


# ==============================================================================
# ENGINE — full dynamic pipeline
# ==============================================================================

class Engine:
    def __init__(self, gem: Gemini, api: MockApi):
        self.gem = gem
        self.api = api
        self.meta: List[Dict[str, Any]] = []
        self.ready = False

    def bootstrap(self):
        self.meta = self.api.tables()
        self.ready = True

    def run(self, question: str, progress=None, job_id: Optional[str] = None) -> Dict[str, Any]:
        def report_step(key, detail=None):
            if progress:
                progress(key, detail)

        # ---- 1. understand & plan -------------------------------------- #
        report_step("understand", "running")
        plan = plan_tables(self.gem, question, self.meta)
        report_step("understand", plan.get("understanding", ""))
        report_step("plan", ", ".join(plan["tables"]))

        # ---- 2. fetch --------------------------------------------------- #
        frames: Dict[str, pd.DataFrame] = {}
        row_counts: Dict[str, int] = {}
        for tname in plan["tables"]:
            search = plan.get("search", {}).get(tname)
            rows = self.api.fetch_table(tname, search=search)
            frames[tname] = pd.DataFrame(rows)
            row_counts[tname] = len(rows)
            report_step("fetch", f"{tname}: {len(rows)} rows"
                        + (f" (search='{search}')" if search else ""))

        # ---- 3. dynamic pandas analysis --------------------------------- #
        out: Dict[str, Any] = {}
        code, explanation = "", ""
        last_failure: Dict[str, str] = {}
        for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
            report_step("analyze", f"attempt {attempt}/{MAX_CODE_ATTEMPTS}")
            prompt = build_analyst_prompt(question, plan, frames, self.meta,
                                          error_feedback=last_failure or None)
            spec = parse_json_loose(self.gem.generate(
                prompt, json_mode=True, temperature=0.15, max_output_tokens=2600))
            code = sanitize_code(spec.get("analysis_code", ""))
            explanation = spec.get("explanation", "")
            try:
                ns = run_analysis_code(code, frames)
                out = extract_outputs(ns)
                break
            except CodeSafetyError as e:
                last_failure = {"code": code, "error": f"SAFETY: {e}"}
                if attempt == MAX_CODE_ATTEMPTS:
                    raise RuntimeError(f"Analysis code blocked by safety rules: {e}")
            except Exception as e:
                last_failure = {"code": code, "error": f"{type(e).__name__}: {e}"}
                if attempt == MAX_CODE_ATTEMPTS:
                    raise RuntimeError(
                        f"Analysis failed after {MAX_CODE_ATTEMPTS} attempts. "
                        f"Last error: {last_failure['error']}")
        report_step("analyze", explanation or "done")

        result_df: pd.DataFrame = out["result"]
        chart_data: Optional[pd.DataFrame] = out["chart_data"]
        chart_kind: Optional[str] = out["chart_kind"]
        chart_title: Optional[str] = out["chart_title"]
        summary: Optional[dict] = out["summary"]

        # ---- 4. chart ---------------------------------------------------- #
        chart_cfg = None
        chart_png = None
        if chart_data is not None and not chart_data.empty:
            report_step("chart", f"{chart_kind or 'bar'}")
            chart_cfg = df_to_chart_config(chart_data, chart_kind or "bar",
                                           chart_title or "")
            if chart_cfg:
                os.makedirs(REPORTS_DIR, exist_ok=True)
                stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                png_path = os.path.join(
                    REPORTS_DIR, f"chart_{stamp}_{slugify(chart_cfg['title'])}.png")
                saved = chart_config_to_png(chart_cfg, png_path)
                if saved:
                    chart_png = saved
            report_step("chart", "ok" if chart_cfg else "not needed")
        else:
            report_step("chart", "not needed for this question")

        # ---- 5. results -> text ------------------------------------------ #
        results_text = serialize_result(result_df)
        summary_text = ""
        if summary:
            try:
                summary_text = json.dumps(summary, indent=2, default=_json_default)
            except Exception:
                summary_text = str(summary)
        fetched_info = ", ".join(f"{n} ({c} rows)" for n, c in row_counts.items())

        # ---- 6. report --------------------------------------------------- #
        report_step("report", "writing")
        chart_note = (f"\nAn interactive chart was generated for the user"
                      f"{' (also saved as ' + os.path.basename(chart_png) + ')' if chart_png else ''}.\n"
                      if chart_cfg else "")
        report_prompt = f"""You are the REPORT WRITER of a CMMS analytics copilot.
Write the final answer/report for the user's question.

{DATA_CONTEXT}

USER QUESTION:
\"\"\"{question}\"\"\"

DATA FETCHED (via API): {fetched_info}
QUESTION TYPE: {plan.get('question_type')}

CALCULATED RESULTS (produced by pandas - these are the ONLY real numbers):
--------------------------------------------------
{results_text}
--------------------------------------------------
{('HEADLINE NUMBERS:' + chr(10) + summary_text + chr(10)) if summary_text else ''}{chart_note}
STRICT ANTI-HALLUCINATION RULES:
1. Every number, asset name, location, technician, percentage and trend MUST
   come from the CALCULATED RESULTS above. Never invent or estimate numbers.
2. If the results are empty or insufficient, say so clearly and explain what
   data would be needed. Do NOT fabricate an answer.
3. Refer to assets by asset_tag/name, locations by site_name/city, technicians
   by name, exactly as in the results.

FORMAT RULES:
- Plain text (terminal/report friendly). Use "==========" style separators.
- Fit the structure to the question.
- Always include a short "KEY FINDINGS" section (3-5 bullets with real values).
- End with "RECOMMENDATIONS" grounded in the data + one line on data analyzed.
"""
        report_raw = self.gem.generate(report_prompt, json_mode=False,
                                       temperature=0.35, max_output_tokens=2400)
        report = report_raw.strip()
        if report.startswith("```"):
            report = re.sub(r"^```[a-zA-Z]*\s*", "", report)
            report = re.sub(r"\s*```$", "", report).strip()
        report_step("report", "done")

        # ---- preview for the UI ------------------------------------------ #
        prev = result_df.head(30).copy()
        for c in prev.columns:
            prev[c] = prev[c].astype(str).str.slice(0, 80)
        table_preview = {
            "columns": [str(c) for c in result_df.columns],
            "rows": prev.values.tolist(),
            "total_rows": int(len(result_df)),
        }

        # ---- durable copies ---------------------------------------------- #
        os.makedirs(REPORTS_DIR, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = slugify(question)
        md_path = os.path.join(REPORTS_DIR, f"report_{stamp}_{slug}.md")
        try:
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(f"# CMMS Copilot Report\n\n**Question:** {question}\n\n"
                         f"**Generated:** {_dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
                         f"---\n\n{report}\n")
        except Exception:
            md_path = None

        # keep FULL data for exports
        if job_id:
            RESULT_FRAMES[job_id] = result_df.copy()
            if chart_data is not None and not chart_data.empty:
                RESULT_CHARTS[job_id] = chart_data.copy()

        return {
            "question": question,
            "understanding": plan.get("understanding", ""),
            "question_type": plan.get("question_type", ""),
            "tables": plan["tables"],
            "row_counts": row_counts,
            "analysis_explanation": explanation,
            "analysis_code": code,
            "report": report,
            "chart": chart_cfg,
            "chart_png": os.path.basename(chart_png) if chart_png else None,
            "table": table_preview,
            "summary": summary,
            "report_md": os.path.basename(md_path) if md_path else None,
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }


# ==============================================================================
# JOB STORE
# ==============================================================================

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ENGINE: Optional[Engine] = None


def _new_job(question: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "question": question,
            "status": "running",
            "steps": [{"key": k, "name": n, "state": "pending", "detail": ""}
                      for k, n in STEP_NAMES],
            "current": None,
            "error": None,
            "result": None,
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "duration_seconds": None,
        }
        if len(JOBS) > MAX_JOBS_KEPT:
            done_ids = [j for j, v in JOBS.items() if v["status"] in ("done", "error")]
            if done_ids:
                old = sorted(done_ids)[0]
                del JOBS[old]
                RESULT_FRAMES.pop(old, None)
                RESULT_CHARTS.pop(old, None)
    return job_id


def _step_index(key: str) -> int:
    return STEP_KEYS.index(key) if key in STEP_KEYS else 0


def _run_job_async(job_id: str, question: str):
    job = JOBS.get(job_id)
    if job is None:
        return

    def progress(key, detail=None):
        idx = _step_index(key)
        with JOBS_LOCK:
            for i, s in enumerate(job["steps"]):
                if i < idx:
                    s["state"] = "done"
                elif i == idx:
                    s["state"] = "running"
                    if detail:
                        s["detail"] = str(detail)[:300]
            job["current"] = key
            for i, s in enumerate(job["steps"]):
                if i < idx and s["state"] == "pending":
                    s["state"] = "done"

    try:
        result = ENGINE.run(question, progress, job_id=job_id)
        with JOBS_LOCK:
            for s in job["steps"]:
                s["state"] = "done"
            job["status"] = "done"
            job["result"] = result
            result["job_id"] = job_id
            job["current"] = None
            job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            try:
                job["duration_seconds"] = round(
                    (_dt.datetime.fromisoformat(job["finished_at"]) -
                     _dt.datetime.fromisoformat(job["started_at"])).total_seconds(), 2
                )
            except Exception:
                job["duration_seconds"] = None
    except Exception as e:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            job["current"] = None
            job["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            try:
                job["duration_seconds"] = round(
                    (_dt.datetime.fromisoformat(job["finished_at"]) -
                     _dt.datetime.fromisoformat(job["started_at"])).total_seconds(), 2
                )
            except Exception:
                job["duration_seconds"] = None


# ==============================================================================
# EXPORT BUILDERS  (txt / md / html / csv / xlsx / pdf / svg / json / zip)
# ==============================================================================

def _report_txt(res: Dict[str, Any]) -> str:
    lines = [
        "=" * 64,
        " CMMS AI COPILOT — REPORT",
        "=" * 64,
        f"Question    : {res['question']}",
        f"Generated   : {res['generated_at']}",
        f"Tables used : {', '.join(res['tables'])}  {res['row_counts']}",
        f"Analysis    : {res['analysis_explanation']}",
        "=" * 64,
        "",
        res["report"],
        "",
    ]
    if res.get("chart_png"):
        lines.append(f"[chart saved: reports/{res['chart_png']}]")
    return "\n".join(lines)


def _report_md(res: Dict[str, Any]) -> str:
    return (f"# CMMS Copilot Report\n\n**Question:** {res['question']}\n\n"
            f"**Generated:** {res['generated_at']}\n\n---\n\n{res['report']}\n")


def _report_html(res: Dict[str, Any]) -> str:
    import html as _h
    img = ""
    if res.get("chart_png"):
        p = os.path.join(REPORTS_DIR, res["chart_png"])
        if os.path.exists(p):
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            img = f'<img style="max-width:100%;border-radius:8px;margin-top:14px" src="data:image/png;base64,{b64}">'
    body = _h.escape(_report_txt(res))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CMMS Report</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;
      margin:0;padding:32px}}
 .card{{max-width:960px;margin:auto;background:#1e293b;border-radius:12px;
      padding:28px;box-shadow:0 8px 30px rgba(0,0,0,.4)}}
 pre{{white-space:pre-wrap;font-family:Consolas,monospace;font-size:13px;
      line-height:1.5;color:#d1e3f8}}
 h1{{font-size:18px;color:#5eead4}}
</style></head>
<body><div class="card"><h1>CMMS AI Copilot Report</h1>
<pre>{body}</pre>{img}</div></body></html>"""


def _report_csv(res: Dict[str, Any], job_id: str) -> Optional[str]:
    df = RESULT_FRAMES.get(job_id)
    if df is None:
        return None
    return df.to_csv(index=(df.index.name is not None))


def _report_json(res: Dict[str, Any]) -> str:
    return json.dumps(res, indent=2, default=_json_default, ensure_ascii=False)


# ------------------------------------------------------------------ EXCEL ---
def _report_xlsx_bytes(res: Dict[str, Any], job_id: str) -> Optional[bytes]:
    if not HAS_XLSX:
        return None
    try:
        buf = io.BytesIO()
        result_df = RESULT_FRAMES.get(job_id)
        chart_df = RESULT_CHARTS.get(job_id)

        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            info = pd.DataFrame([
                ("Question", res["question"]),
                ("Generated", res["generated_at"]),
                ("Model", GEMINI_MODEL),
                ("Question type", res.get("question_type", "")),
                ("Tables used", ", ".join(res["tables"])),
                ("Rows fetched", str(res["row_counts"])),
                ("Analysis", res["analysis_explanation"]),
            ], columns=["Field", "Value"])
            info.to_excel(xw, sheet_name="Info", index=False)

            pd.DataFrame({"Report": (res["report"] or "").splitlines()}
                         ).to_excel(xw, sheet_name="Report", index=False)

            if result_df is not None and not result_df.empty:
                out_df = result_df.copy()
                for c in out_df.columns:
                    out_df[c] = out_df[c].astype(str).where(out_df[c].notna(), "")
                out_df.to_excel(xw, sheet_name="Results", index=False)

            if chart_df is not None and not chart_df.empty:
                cdf = chart_df.copy()
                for c in cdf.columns:
                    cdf[c] = cdf[c].astype(str).where(cdf[c].notna(), "")
                cdf.to_excel(xw, sheet_name="Chart Data")

            if res.get("summary"):
                pd.DataFrame([(str(k), str(v)) for k, v in res["summary"].items()],
                             columns=["Metric", "Value"]
                             ).to_excel(xw, sheet_name="Summary", index=False)

        # ---- styling ----
        buf.seek(0)
        wb = load_workbook(buf)
        header_fill = PatternFill("solid", fgColor="134E4A")
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = header_fill
                cell.alignment = Alignment(vertical="center")
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None),
                            default=10)
                ws.column_dimensions[col[0].column_letter].width = \
                    min(70, max(10, width + 2))
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()
    except Exception:
        return None


# -------------------------------------------------------------------- PDF ---
def _report_pdf_bytes(res: Dict[str, Any]) -> Optional[bytes]:
    if not HAS_PDF:
        return None
    try:
        import textwrap
        buf = io.BytesIO()
        c = pdfcanvas.Canvas(buf, pagesize=A4)
        W, H = A4
        M = 18 * mm
        y = H - M

        def para(txt, size=9.5, leading=13, col=None, font="Helvetica"):
            nonlocal y
            col = col or rl_colors.HexColor("#1f2937")
            c.setFont(font, size)
            c.setFillColor(col)
            for line in (txt or "").splitlines() or [""]:
                for sub in textwrap.wrap(line, 105) or [""]:
                    if y < M + 40:
                        c.showPage(); y = H - M
                        c.setFont(font, size); c.setFillColor(col)
                    c.drawString(M, y, sub)
                    y -= leading

        def heading(txt, size=13):
            nonlocal y
            if y < M + 60:
                c.showPage(); y = H - M
            c.setFont("Helvetica-Bold", size)
            c.setFillColor(rl_colors.HexColor("#0f766e"))
            c.drawString(M, y, txt)
            y -= size + 5

        # ---- cover band ----
        c.setFillColor(rl_colors.HexColor("#0b1220"))
        c.rect(0, H - 32 * mm, W, 32 * mm, stroke=0, fill=1)
        c.setFillColor(rl_colors.HexColor("#2dd4bf"))
        c.setFont("Helvetica-Bold", 19)
        c.drawString(M, H - 17 * mm, "CMMS AI Copilot — Analysis Report")
        c.setFillColor(rl_colors.HexColor("#94a3b8"))
        c.setFont("Helvetica", 9)
        c.drawString(M, H - 24 * mm,
                     f"Generated {res['generated_at']}   •   Model: {GEMINI_MODEL}")
        y = H - 40 * mm

        para(f"Question: {res['question']}", 11, 15,
             rl_colors.HexColor("#111827"), "Helvetica-Bold")
        y -= 3
        para(f"Tables: {', '.join(res['tables'])}  |  Rows: {res['row_counts']}",
             8.5, 11, rl_colors.HexColor("#6b7280"))
        y -= 6

        heading("Report")
        para(res["report"])

        # ---- chart image ----
        if res.get("chart_png"):
            p = os.path.join(REPORTS_DIR, res["chart_png"])
            if os.path.exists(p):
                if y < M + 95 * mm:
                    c.showPage(); y = H - M
                heading("Chart")
                c.drawImage(p, M, y - 75 * mm, width=W - 2 * M, height=72 * mm,
                            preserveAspectRatio=True, anchor='c')
                y -= 78 * mm

        # ---- results table ----
        t = res.get("table") or {}
        cols, rows = t.get("columns") or [], t.get("rows") or []
        if cols:
            if y < M + 50 * mm:
                c.showPage(); y = H - M
            heading(f"Results table (showing {len(rows)} of "
                    f"{t.get('total_rows', len(rows))} rows)")
            ncols = min(len(cols), 6)
            col_w = (W - 2 * M) / ncols
            char_w = int(col_w / 4.6)
            row_h = 13

            def draw_row(vals, bold=False, bg=None):
                nonlocal y
                if y < M + 20:
                    c.showPage(); y = H - M
                if bg:
                    c.setFillColor(bg)
                    c.rect(M, y - 3, W - 2 * M, row_h, stroke=0, fill=1)
                c.setFont("Helvetica-Bold" if bold else "Helvetica", 7.5)
                c.setFillColor(rl_colors.HexColor("#0f766e") if bold
                               else rl_colors.HexColor("#1f2937"))
                for i in range(ncols):
                    txt = str(vals[i]) if i < len(vals) else ""
                    c.drawString(M + 2 + i * col_w, y + 1, txt[:char_w])
                y -= row_h

            draw_row(cols, bold=True, bg=rl_colors.HexColor("#ccfbf1"))
            for r in rows:
                draw_row(r)
            y -= 8

        # ---- appendix: generated code ----
        code = res.get("analysis_code")
        if code:
            c.showPage(); y = H - M
            heading("Appendix — Generated Analysis Code")
            para(code, 7.5, 10, rl_colors.HexColor("#374151"), "Courier")

        c.setTitle("CMMS AI Copilot Report")
        c.save()
        return buf.getvalue()
    except Exception:
        return None


# -------------------------------------------------------------------- ZIP ---
def _report_zip_bytes(job_id: str, res: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(res["question"])
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt",
                   "CMMS AI Copilot export bundle\n"
                   f"Question: {res['question']}\nGenerated: {res['generated_at']}\n"
                   f"Contents: report.txt/md/html, results.csv, report.xlsx, "
                   f"report.pdf, chart.png, result.json\n")
        z.writestr("report.txt", _report_txt(res))
        z.writestr("report.md", _report_md(res))
        z.writestr("report.html", _report_html(res))
        csv = _report_csv(res, job_id)
        z.writestr("results.csv", csv if csv else "no tabular result")
        x = _report_xlsx_bytes(res, job_id)
        if x:
            z.writestr(f"report_{slug}_{stamp}.xlsx", x)
        p = _report_pdf_bytes(res)
        if p:
            z.writestr(f"report_{slug}_{stamp}.pdf", p)
        if res.get("chart_png"):
            path = os.path.join(REPORTS_DIR, res["chart_png"])
            if os.path.exists(path):
                z.write(path, res["chart_png"])
        z.writestr("result.json", _report_json(res))
    return buf.getvalue()


# ==============================================================================
# WEB SERVER (FastAPI)
# ==============================================================================

app = FastAPI(title="CMMS AI Copilot UI")


@app.on_event("startup")
def _startup():
    def _boot():
        global ENGINE, ENGINE_ERROR, BOOT_STARTED_AT
        BOOT_STARTED_AT = _dt.datetime.now().isoformat(timespec="seconds")
        ENGINE_ERROR = None
        try:
            gem = Gemini(GEMINI_API_KEY, GEMINI_MODEL)
            api = MockApi(API_BASE)
            if not api.check():
                raise RuntimeError(
                    f"Mock CMMS API not reachable at {API_BASE}. "
                    f"Start it with: uvicorn mc:app --host 127.0.0.1 --port 8001"
                )
            eng = Engine(gem, api)
            eng.bootstrap()
            ENGINE = eng
            print(f"[copilot] ready — {len(eng.meta)} tables, "
                  f"CMMS API {API_BASE}, UI http://{COPILOT_HOST}:{COPILOT_PORT}")
        except Exception as e:
            ENGINE = None
            ENGINE_ERROR = f"{type(e).__name__}: {e}"
            print(f"[copilot] startup error: {ENGINE_ERROR}")
    threading.Thread(target=_boot, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/api/status")
def api_status():
    api_ok = False
    api_error = None
    try:
        r = requests.get(f"{API_BASE}/health", timeout=3)
        api_ok = r.status_code == 200
        if not api_ok:
            api_error = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        api_error = str(e)

    return {
        "ok": bool(ENGINE is not None and ENGINE.ready and api_ok and GEMINI_API_KEY),
        "engine_ready": bool(ENGINE is not None and ENGINE.ready),
        "api_ok": api_ok,
        "api_base": API_BASE,
        "copilot_url": f"http://{COPILOT_HOST}:{COPILOT_PORT}",
        "model": GEMINI_MODEL,
        "gemini_configured": bool(GEMINI_API_KEY),
        "tables_count": len(ENGINE.meta) if ENGINE else 0,
        "total_records": sum(t["row_count"] for t in ENGINE.meta) if ENGINE else 0,
        "has_matplotlib": HAS_MPL,
        "has_xlsx": HAS_XLSX,
        "has_pdf": HAS_PDF,
        "engine_error": ENGINE_ERROR,
        "api_error": api_error,
        "jobs": len(JOBS),
        "time": _dt.datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/meta")
def api_meta():
    status = api_status()
    status["tables"] = ENGINE.meta if ENGINE else []
    return status


@app.post("/api/ask")
def api_ask(payload: Dict[str, Any] = None):
    if payload is None:
        payload = {}
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    if ENGINE is None or not ENGINE.ready:
        raise HTTPException(503, "engine not ready — is the mock server running?")
    job_id = _new_job(question)
    threading.Thread(target=_run_job_async, args=(job_id, question),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/history")
def api_history():
    with JOBS_LOCK:
        items = [{"id": j["id"], "question": j["question"], "status": j["status"],
                  "created": j["created"]}
                 for j in sorted(JOBS.values(), key=lambda x: x["created"],
                                 reverse=True)[:25]]
    return {"items": items}


@app.get("/api/browse/{table}")
def api_browse(table: str, limit: int = 50, offset: int = 0,
               search: Optional[str] = None):
    if ENGINE is None or not ENGINE.ready:
        raise HTTPException(503, "engine not ready")
    if table not in [t["name"] for t in ENGINE.meta]:
        raise HTTPException(404, f"unknown table {table}")
    try:
        return ENGINE.api.browse(table, min(limit, 500), max(offset, 0), search)
    except requests.RequestException as e:
        raise HTTPException(502, f"mock API error: {e}")


@app.get("/api/browse_csv/{table}")
def api_browse_csv(table: str, search: Optional[str] = None):
    if ENGINE is None or not ENGINE.ready:
        raise HTTPException(503, "engine not ready")
    rows = ENGINE.api.fetch_table(table, search=search)
    df = pd.DataFrame(rows)
    csv = df.to_csv(index=False)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(content=csv, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{table}_{stamp}.csv"'})


@app.get("/api/browse_xlsx/{table}")
def api_browse_xlsx(table: str, search: Optional[str] = None):
    """Download an entire raw table as a styled Excel workbook."""
    if not HAS_XLSX:
        raise HTTPException(501, "openpyxl missing -> pip install openpyxl")
    if ENGINE is None or not ENGINE.ready:
        raise HTTPException(503, "engine not ready")
    rows = ENGINE.api.fetch_table(table, search=search)
    df = pd.DataFrame(rows)
    res = {"question": f"Raw table export: {table}",
           "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
           "tables": [table], "row_counts": {table: len(rows)},
           "analysis_explanation": f"Full raw export of '{table}'.",
           "report": f"Raw export of table '{table}' ({len(rows)} rows).",
           "summary": None, "question_type": "export",
           "chart_png": None, "table": None}
    tmp_id = f"browse_{table}"
    RESULT_FRAMES[tmp_id] = df
    data = _report_xlsx_bytes(res, tmp_id) or b""
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{table}_{stamp}.xlsx"'})


@app.get("/api/download/{job_id}")
def api_download(job_id: str, fmt: str = "txt"):
    job = JOBS.get(job_id)
    if job is None or job.get("result") is None:
        raise HTTPException(404, "no finished result for this job")
    res = job["result"]
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(res["question"])
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    if fmt == "txt":
        return PlainTextResponse(_report_txt(res), media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="report_{slug}_{stamp}.txt"'})
    if fmt == "md":
        return PlainTextResponse(_report_md(res), media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="report_{slug}_{stamp}.md"'})
    if fmt == "html":
        return Response(_report_html(res), media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="report_{slug}_{stamp}.html"'})
    if fmt == "json":
        return Response(_report_json(res), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="result_{slug}_{stamp}.json"'})
    if fmt == "csv":
        csv = _report_csv(res, job_id)
        if csv is None:
            raise HTTPException(404, "result CSV not available")
        return PlainTextResponse(csv, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="result_{slug}_{stamp}.csv"'})
    if fmt == "xlsx":
        if not HAS_XLSX:
            raise HTTPException(501, "Excel export needs openpyxl -> pip install openpyxl")
        data = _report_xlsx_bytes(res, job_id)
        if not data:
            raise HTTPException(500, "Excel generation failed")
        return Response(data, media_type=XLSX_MIME,
            headers={"Content-Disposition": f'attachment; filename="report_{slug}_{stamp}.xlsx"'})
    if fmt == "pdf":
        if not HAS_PDF:
            raise HTTPException(501, "PDF export needs reportlab -> pip install reportlab")
        data = _report_pdf_bytes(res)
        if not data:
            raise HTTPException(500, "PDF generation failed")
        return Response(data, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report_{slug}_{stamp}.pdf"'})
    if fmt == "svg":
        if not res.get("chart"):
            raise HTTPException(404, "no chart for this job")
        data = chart_config_to_svg_bytes(res["chart"])
        if not data:
            raise HTTPException(500, "SVG generation failed (matplotlib needed)")
        return Response(data, media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="chart_{slug}_{stamp}.svg"'})
    if fmt == "png":
        if not res.get("chart_png"):
            raise HTTPException(404, "no chart PNG for this job")
        path = os.path.join(REPORTS_DIR, res["chart_png"])
        if not os.path.exists(path):
            raise HTTPException(404, "chart file missing")
        with open(path, "rb") as fh:
            return Response(fh.read(), media_type="image/png",
                headers={"Content-Disposition": f'attachment; filename="{res["chart_png"]}"'})
    if fmt == "zip":
        data = _report_zip_bytes(job_id, res)
        return Response(data, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="cmms_export_{slug}_{stamp}.zip"'})
    raise HTTPException(400, "fmt must be one of txt|md|html|csv|xlsx|pdf|json|png|svg|zip")


# ==============================================================================
# EMBEDDED WEB UI
# ==============================================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMMS AI Copilot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#07111f;--panel:#0d1a2c;--panel2:#12233b;--panel3:#172b47;--line:#243b5c;--text:#e7f0fb;--muted:#8ea6c2;--accent:#37e0c1;--accent2:#47a9ff;--ok:#4ade80;--err:#fb7185;--shadow:0 18px 50px rgba(0,0,0,.28)}
body.light{--bg:#eef4f8;--panel:#fff;--panel2:#edf3f7;--panel3:#e4edf4;--line:#d5e0e8;--text:#102033;--muted:#61758a;--shadow:0 14px 34px rgba(25,58,84,.12)}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:radial-gradient(circle at 15% 0%,rgba(55,224,193,.07),transparent 30%),var(--bg);color:var(--text);font-family:Inter,Segoe UI,system-ui,Arial,sans-serif}
button,input,select{font:inherit}.layout{display:grid;grid-template-columns:300px 1fr;height:100vh;overflow:hidden}.side{background:linear-gradient(180deg,var(--panel),var(--panel2));border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:auto}
.brand{padding:22px 20px 16px;border-bottom:1px solid var(--line)}.brand b{font-size:19px}.brand b span{color:var(--accent)}.brand small{display:block;color:var(--muted);margin-top:6px}
.side h3{margin:16px 16px 8px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.16em}.side .item{padding:9px 16px;cursor:pointer;border-left:3px solid transparent;transition:.16s}.side .item:hover{background:var(--panel3);border-left-color:var(--accent)}
.chips{padding:0 12px 7px}.chip{display:inline-block;margin:3px 2px;padding:7px 9px;border-radius:14px;background:var(--panel2);border:1px solid var(--line);font-size:11px;cursor:pointer;transition:.16s}.chip:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
.history{min-height:40px}.history .item{font-size:12px}.history small{display:block;color:var(--muted);margin-top:3px}.status{margin-top:auto;padding:14px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.statusrow{display:flex;justify-content:space-between;gap:8px;margin:6px 0}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}.dot.ok{background:var(--ok);box-shadow:0 0 0 4px rgba(74,222,128,.08)}.dot.bad{background:var(--err)}
.main{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}.topbar{height:68px;display:flex;align-items:center;gap:10px;padding:0 20px;background:var(--panel);border-bottom:1px solid var(--line)}.title{font-weight:750;font-size:16px}.title span{color:var(--accent)}.badge{background:var(--panel2);border:1px solid var(--line);padding:5px 9px;border-radius:999px;font-size:10px;color:var(--muted)}.spacer{flex:1}
.content{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:22px;scroll-behavior:smooth;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.hero{max-width:1100px;margin:0 auto 18px;text-align:center;padding:20px 8px 6px}.hero h1{margin:0;font-size:30px;letter-spacing:-.03em}.hero p{color:var(--muted);margin:8px 0 0}
.ask{max-width:1100px;margin:0 auto 16px;background:var(--panel);border:1px solid var(--line);padding:10px;border-radius:16px;box-shadow:var(--shadow)}.askrow{display:flex;gap:9px}.ask input{flex:1;min-width:0;background:var(--panel2);color:var(--text);border:1px solid var(--line);outline:none;border-radius:11px;padding:13px 14px}.ask input:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(55,224,193,.10)}
.primary{background:var(--accent);color:#05211b;font-weight:750;padding:12px 18px;border-radius:11px;cursor:pointer}.primary:disabled{opacity:.45;cursor:not-allowed}.ghost{background:var(--panel2);color:var(--text);border:1px solid var(--line);padding:9px 11px;border-radius:9px;cursor:pointer}.ghost:hover{border-color:var(--accent);color:var(--accent)}.quick{display:flex;gap:7px;flex-wrap:wrap;padding:8px 3px 0}
.grid{max-width:1100px;margin:0 auto}.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:14px}.kpi{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:13px;box-shadow:var(--shadow)}.kpi .value{font-size:21px;font-weight:800;color:var(--accent)}.kpi .label{margin-top:5px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.09em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:16px 18px;margin-bottom:14px;box-shadow:var(--shadow)}.cardhead{display:flex;align-items:center;gap:10px;margin-bottom:12px}.cardhead h2{font-size:12px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted);margin:0}.meta{color:var(--muted);font-size:11px}
.steps{display:grid;grid-template-columns:repeat(6,1fr);gap:7px}.step{background:var(--panel2);border:1px solid var(--line);padding:10px;border-radius:10px;min-height:70px}.step .top{display:flex;align-items:center;gap:7px;font-size:11px}.step .detail{font-size:10px;color:var(--muted);margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.step.running{border-color:var(--accent2)}.step.done{border-color:rgba(74,222,128,.45)}.ic{font-size:16px}.spin{display:inline-block;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.progressline{height:5px;background:var(--panel2);border-radius:99px;overflow:hidden;margin-top:12px}.progressline i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .35s ease}
.report{white-space:pre-wrap;line-height:1.72;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;max-height:none;overflow:visible;word-break:break-word}.tools{display:flex;gap:6px;flex-wrap:wrap}.tool.active{border-color:var(--accent);color:var(--accent);background:rgba(55,224,193,.08)}.viewtools{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;overflow:hidden}.seg button{border:0;border-right:1px solid var(--line);border-radius:0;background:transparent}.seg button:last-child{border-right:0}.seg button.active{background:var(--accent);color:#05211b}.report.compact{font-size:12px;line-height:1.5}.report.large{font-size:15px;line-height:1.8}.card.collapsed>.cardbody{display:none}.card.collapsed .collapseIcon{transform:rotate(-90deg)}.collapseIcon{transition:.2s}.actionbar{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.actionbar .ghost{padding:8px 10px}.scrollhint{color:var(--muted);font-size:10px;margin-left:auto} .chartbox{height:390px;position:relative}
.viewtools{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin:0 0 12px;padding:10px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;overflow:hidden}.seg .ghost{border:0;border-right:1px solid var(--line);border-radius:0}.seg .ghost:last-child{border-right:0}.seg .active{background:var(--accent);color:#05211b;border-color:var(--accent)}.scrollhint{color:var(--muted);font-size:10px;margin-left:auto}.cardbody{min-height:0}.report.compact{font-size:12px;line-height:1.5}.report.large{font-size:15px;line-height:1.85}.card.collapsed .cardbody,.card.collapsed .viewtools{display:none}.tabletools{display:flex;gap:8px;margin-bottom:9px}.tabletools input{flex:1;background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:8px 10px;border-radius:8px;outline:none}.tablewrap{max-height:370px;overflow:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;font-size:11px}th{position:sticky;top:0;background:var(--panel3);color:var(--muted);text-align:left;padding:8px;border-bottom:1px solid var(--line);cursor:pointer}td{padding:7px 8px;border-bottom:1px solid var(--line)}tr:hover td{background:var(--panel2)}
.err{background:rgba(190,18,60,.13);border:1px solid var(--err);color:#fecdd3;padding:13px;border-radius:12px;white-space:pre-wrap;margin-bottom:14px}.hint{color:var(--muted);font-size:11px;margin-top:7px}.codeblk{background:#050b14;border:1px solid var(--line);border-radius:10px;padding:12px;color:#b7d7c8;overflow:auto}
.modal{display:none;position:fixed;inset:0;background:rgba(2,8,23,.72);backdrop-filter:blur(5px);z-index:50;align-items:center;justify-content:center}.modal.open{display:flex}.modalbox{width:min(1100px,94vw);max-height:92vh;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;overflow:auto;box-shadow:var(--shadow)}
#toasts{position:fixed;right:16px;bottom:16px;z-index:99;display:flex;flex-direction:column;gap:8px}.toast{background:var(--panel);border:1px solid var(--accent);padding:10px 13px;border-radius:10px;box-shadow:var(--shadow);font-size:12px}.toast.err{border-color:var(--err)}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(3,1fr)}.steps{grid-template-columns:repeat(3,1fr)}}@media(max-width:760px){.layout{grid-template-columns:1fr}.side{display:none}.content{padding:13px}.askrow{flex-direction:column}.kpis{grid-template-columns:repeat(2,1fr)}.steps{grid-template-columns:1fr 1fr}.topbar{padding:0 12px}}
</style>
</head>
<body>
<div class="layout">
<aside class="side">
<div class="brand"><b>CMMS <span>AI Copilot</span></b><small>Dynamic analytics · reports · exports</small></div>
<h3>Suggested questions</h3><div class="chips" id="suggestions"></div>
<h3>Recent analysis</h3><div class="history" id="history"><div class="item"><small>No analyses yet</small></div></div>
<h3>Workspace</h3><div class="item" onclick="openBrowser()">▦ Browse CMMS tables</div><div class="item" onclick="refreshAll()">↻ Refresh connection</div><div class="item" onclick="toggleTheme()">◐ Toggle theme</div>
<div class="status"><div class="statusrow"><span><span id="apiDot" class="dot bad"></span>CMMS API</span><b id="apiTxt">checking</b></div><div class="statusrow"><span>Gemini</span><b id="gemTxt">checking</b></div><div class="statusrow"><span>Model</span><b id="modelTxt">—</b></div><div class="statusrow"><span>Records</span><b id="recTxt">—</b></div><div class="statusrow"><span>Exports</span><b id="expTxt">—</b></div></div>
</aside>
<main class="main">
<header class="topbar"><div class="title">CMMS <span>AI Copilot</span></div><div class="badge">UI : 8002</div><div class="badge" id="apiBadge">API : 8001</div><div class="badge" id="lastBadge" style="display:none"></div><div class="spacer"></div><button class="ghost" onclick="toggleTheme()">Theme</button></header>
<section class="content" id="content">
<div class="hero" id="hero"><h1>Ask your CMMS anything</h1><p>AI plans the data → fetches tables → runs pandas analysis → builds your report and chart.</p></div>
<div class="ask"><div class="askrow"><input id="q" autocomplete="off" placeholder="Try: Which 10 assets have the highest downtime?"><button class="primary" id="askBtn" onclick="ask()">Analyze ⚡</button></div><div class="quick" id="quick"></div><div class="actionbar"><button class="ghost" onclick="openBrowser()">▦ Data Browser</button><button class="ghost" onclick="loadHistory()">↻ Refresh History</button><button class="ghost" onclick="fillExample()">✨ Example Question</button><button class="ghost" onclick="document.getElementById('q').focus()">⌨ Focus</button><button class="ghost" onclick="toggleTheme()">◐ Theme</button></div></div>
<div class="grid"><div id="errBox" class="err" style="display:none"></div>
<div id="progCard" class="card" style="display:none"><div class="cardhead"><h2>Live analysis pipeline</h2><div class="spacer"></div><span id="jobTimer" class="meta"></span></div><div class="steps" id="progSteps"></div><div class="progressline"><i id="progressFill"></i></div></div>
<div class="kpis" id="kpis"></div>
<div class="card" id="reportCard" style="display:none"><div class="cardhead"><h2>Executive report</h2><div class="spacer"></div><span class="meta" id="reportMeta"></span><button class="ghost" onclick="toggleCollapse('reportCard')" title="Collapse report">⌄</button></div><div class="viewtools"><div class="seg"><button id="viewNormal" class="ghost active" onclick="setReportSize('normal')">Normal</button><button id="viewCompact" class="ghost" onclick="setReportSize('compact')">Compact</button><button id="viewLarge" class="ghost" onclick="setReportSize('large')">Large</button></div><button class="ghost" onclick="scrollToSection('chartCard')">Chart ↓</button><button class="ghost" onclick="scrollToSection('tableCard')">Data ↓</button><button class="ghost" onclick="scrollToTop()">↑ Top</button><span class="scrollhint">Use the workspace scrollbar to continue through the report</span></div><div class="cardbody"><div class="report" id="reportText"></div><div class="tools" style="margin-top:13px"><button class="ghost" onclick="copyReport()">Copy</button><button class="ghost" onclick="dl('txt')">TXT</button><button class="ghost" onclick="dl('md')">MD</button><button class="ghost" onclick="dl('html')">HTML</button><button class="ghost" onclick="dl('csv')">CSV</button><button class="ghost" id="xlsxBtn" onclick="dl('xlsx')">Excel</button><button class="ghost" id="pdfBtn" onclick="dl('pdf')">PDF</button><button class="ghost" onclick="dl('json')">JSON</button><button class="ghost" onclick="dl('zip')">ZIP</button><button class="ghost" onclick="toggleCode()">Analysis code</button></div><div class="hint" id="exportHint"></div><pre class="codeblk" id="codeBlk" style="display:none"></pre></div></div>
<div class="card" id="chartCard" style="display:none"><div class="cardhead"><h2 id="chartHeading">Interactive analytics</h2><div class="spacer"></div><span class="meta">Change chart type live</span></div><div class="tools" id="chartTools"><button class="ghost tool" data-k="bar" onclick="setKind('bar')">Bar</button><button class="ghost tool" data-k="barh" onclick="setKind('barh')">H-Bar</button><button class="ghost tool" data-k="line" onclick="setKind('line')">Line</button><button class="ghost tool" data-k="pie" onclick="setKind('pie')">Pie</button><button class="ghost tool" data-k="doughnut" onclick="setKind('doughnut')">Doughnut</button><button class="ghost tool" data-k="radar" onclick="setKind('radar')">Radar</button><button class="ghost tool" data-k="polarArea" onclick="setKind('polarArea')">Polar</button><span class="spacer"></span><button class="ghost" onclick="chartPNG()">PNG</button><button class="ghost" onclick="openChartModal()">Fullscreen</button></div><div class="chartbox"><canvas id="chartCv"></canvas></div></div>
<div class="card" id="tableCard" style="display:none"><div class="cardhead"><h2>Calculated result data</h2><div class="spacer"></div><span class="meta" id="tblCount"></span></div><div class="tabletools"><input id="tblFilter" placeholder="Filter current result rows…" oninput="renderTable()"><button class="ghost" onclick="copyTSV()">Copy TSV</button></div><div class="tablewrap"><table id="dataTable"></table></div><div class="hint">Click a column heading to sort.</div></div>
</div></section>
</main></div>
<div class="modal" id="browserModal"><div class="modalbox"><div class="cardhead"><h2>CMMS data browser</h2><div class="spacer"></div><button class="ghost" onclick="closeBrowser()">Close</button></div><div class="tabletools"><select id="brTable" onchange="brLoad()" style="background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px"></select><input id="brSearch" placeholder="Search table…" onkeydown="if(event.key==='Enter')brLoad()"><button class="ghost" onclick="brDl('csv')">CSV</button><button class="ghost" onclick="brDl('xlsx')">Excel</button></div><div class="tablewrap"><table id="brTableEl"></table></div><div style="display:flex;gap:8px;align-items:center;margin-top:10px"><button class="ghost" onclick="brNav(-1)">← Previous</button><button class="ghost" onclick="brNav(1)">Next →</button><span class="meta" id="brInfo"></span></div></div></div>
<div class="modal" id="chartModal"><div class="modalbox" style="height:88vh;display:flex;flex-direction:column"><div class="cardhead"><h2 id="chartModalTitle">Chart</h2><div class="spacer"></div><button class="ghost" onclick="closeChartModal()">Close</button></div><div style="position:relative;flex:1"><canvas id="chartModalCv"></canvas></div></div></div>
<div id="toasts"></div>
<script>
const S={jobId:null,result:null,chartCfg:null,chartKind:null,meta:null,sortCol:null,sortAsc:true,brOffset:0,brTotal:0,pollStarted:0};
let chart=null,modalChart=null;const $=id=>document.getElementById(id);const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));const PALETTE=['#37e0c1','#47a9ff','#a78bfa','#fbbf24','#fb7185','#f472b6','#4ade80','#60a5fa'];const cssVar=n=>getComputedStyle(document.body).getPropertyValue(n).trim();const hexA=(h,a)=>{const n=parseInt(h.slice(1),16);return`rgba(${n>>16&255},${n>>8&255},${n&255},${a})`};
const SUGG=["Top 10 assets by downtime","Which locations breached SLA most in 2025?","Monthly work order trend since 2024","Compare technician performance year over year","Failure causes ranked by frequency","Executive summary of asset health","Average repair time by technician","SLA compliance rate by city"];
window.addEventListener('load',()=>{if(localStorage.getItem('cmms-theme')==='light')document.body.classList.add('light');if(localStorage.getItem('cmms-report-collapsed')==='1')$('reportCard').classList.add('collapsed');renderSuggestions();loadMeta();loadHistory();$('q').addEventListener('keydown',e=>{if(e.key==='Enter')ask()});document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==$('q')){e.preventDefault();$('q').focus()}if(e.key==='Escape'){closeBrowser();closeChartModal()}})});
function toast(msg,err=false){const d=document.createElement('div');d.className='toast'+(err?' err':'');d.textContent=msg;$('toasts').appendChild(d);setTimeout(()=>{d.style.opacity='0';d.style.transition='opacity .3s';setTimeout(()=>d.remove(),300)},2800)}
function renderSuggestions(){$('suggestions').innerHTML=SUGG.map(s=>`<span class="chip" onclick="useSuggestion(this)">${esc(s)}</span>`).join('');$('quick').innerHTML=SUGG.slice(0,4).map(s=>`<button class="ghost" onclick="useSuggestion(this)">${esc(s)}</button>`).join('')}
function useSuggestion(el){$('q').value=el.textContent;ask()}
function toggleTheme(){document.body.classList.toggle('light');localStorage.setItem('cmms-theme',document.body.classList.contains('light')?'light':'dark');if(chart&&S.chartCfg){chart.destroy();chart=new Chart($('chartCv'),chartConfig(S.chartKind,S.chartCfg))}}
async function loadMeta(){try{const m=await(await fetch('/api/meta')).json();S.meta=m;$('apiDot').className='dot '+(m.api_ok?'ok':'bad');$('apiTxt').textContent=m.api_ok?'connected':'offline';$('gemTxt').textContent=m.gemini_configured?'ready':'missing';$('modelTxt').textContent=m.model||'—';$('recTxt').textContent=m.total_records?.toLocaleString()||'—';$('expTxt').textContent=(m.has_xlsx?'Excel ':'')+(m.has_pdf?'PDF':'');$('apiBadge').textContent='API : '+((m.api_base||'8001').split(':').pop());$('xlsxBtn').disabled=!m.has_xlsx;$('pdfBtn').disabled=!m.has_pdf;if(!m.ok&&m.engine_error)showError(m.engine_error)}catch(e){$('apiTxt').textContent='error'}}
async function refreshAll(){await loadMeta();await loadHistory();toast('Connection refreshed')}
async function loadHistory(){try{const h=await(await fetch('/api/history')).json();$('history').innerHTML=h.items.length?h.items.map(i=>`<div class="item" onclick="openJob('${i.id}')">${esc(i.question)}<small>${i.status} · ${i.created}</small></div>`).join(''):'<div class="item"><small>No analyses yet</small></div>'}catch(e){}}
async function openJob(id){const j=await(await fetch('/api/job/'+id)).json();if(j.status==='done'){S.jobId=id;renderResult(j)}else toast('Job is '+j.status,true)}
async function ask(){const q=$('q').value.trim();if(!q)return toast('Enter a CMMS question first',true);$('askBtn').disabled=true;$('hero').style.display='none';$('errBox').style.display='none';$('reportCard').style.display='none';$('chartCard').style.display='none';$('tableCard').style.display='none';$('kpis').innerHTML='';if(chart){chart.destroy();chart=null}$('progCard').style.display='block';S.pollStarted=Date.now();try{const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});if(!r.ok){const b=await r.json().catch(()=>({}));throw new Error(b.detail||r.statusText)}S.jobId=(await r.json()).job_id;poll(S.jobId)}catch(e){$('askBtn').disabled=false;showError(e.message);$('progCard').style.display='none'}}
async function poll(id){try{const j=await(await fetch('/api/job/'+id)).json();renderSteps(j);if(j.status==='running'){updateTimer();setTimeout(()=>poll(id),700)}else if(j.status==='done'){$('askBtn').disabled=false;renderResult(j);loadHistory();toast('Analysis complete — exports are ready')}else{$('askBtn').disabled=false;showError(j.error||'Analysis failed');$('progCard').style.display='none'}}catch(e){$('askBtn').disabled=false;showError('Polling error: '+e.message)}}
function updateTimer(){if($('progCard').style.display==='none')return;$('jobTimer').textContent=((Date.now()-S.pollStarted)/1000).toFixed(1)+'s'}
function renderSteps(j){const done=j.steps.filter(s=>s.state==='done').length;$('progressFill').style.width=Math.round((done/j.steps.length)*100)+'%';$('progSteps').innerHTML=j.steps.map(s=>{const ic=s.state==='done'?'✓':s.state==='running'?'<span class="spin">◌</span>':'○';return`<div class="step ${s.state}"><div class="top"><span class="ic">${ic}</span><b>${esc(s.name)}</b></div><div class="detail">${esc(s.detail||'')}</div></div>`}).join('')}
function showError(msg){$('errBox').style.display='block';$('errBox').textContent='⚠ '+msg}
function renderResult(j){$('progCard').style.display='none';$('errBox').style.display='none';S.result=j.result;const r=j.result;$('reportCard').style.display='block';$('reportMeta').textContent=`${r.question_type} · ${r.tables.join(', ')} · ${r.generated_at}${j.duration_seconds!=null?' · '+j.duration_seconds+'s':''}`;$('reportText').textContent=r.report;$('codeBlk').textContent=r.analysis_code||'';$('lastBadge').style.display='inline-block';$('lastBadge').textContent=r.tables.join(' + ');renderKPIs(r.summary||{});buildChartUI(r.chart);S.sortCol=null;$('tblFilter').value='';renderTable();$('content').scrollTo({top:0,behavior:'smooth'});setReportSize(localStorage.getItem('cmms-report-size')||'normal')}
function renderKPIs(sum){const entries=Object.entries(sum).slice(0,6);if(!entries.length){$('kpis').innerHTML='';return}$('kpis').innerHTML=entries.map(([k,v])=>`<div class="kpi"><div class="value">${esc(v)}</div><div class="label">${esc(k.replace(/_/g,' '))}</div></div>`).join('')}
function filteredRows(){if(!S.result?.table)return[];const f=($('tblFilter').value||'').toLowerCase();const rows=S.result.table.rows||[];return f?rows.filter(r=>r.join(' ').toLowerCase().includes(f)):rows}
function renderTable(){const t=S.result?.table;if(!t){$('tableCard').style.display='none';return}$('tableCard').style.display='block';$('tblCount').textContent=`${t.total_rows||0} rows`;let rows=filteredRows();if(S.sortCol!==null){const i=S.sortCol;rows=[...rows].sort((a,b)=>{const x=parseFloat(a[i]),y=parseFloat(b[i]);const c=(!isNaN(x)&&!isNaN(y))?x-y:String(a[i]).localeCompare(String(b[i]));return S.sortAsc?c:-c})}const arrow=i=>S.sortCol===i?(S.sortAsc?' ▲':' ▼'):'';$('dataTable').innerHTML='<thead><tr>'+t.columns.map((c,i)=>`<th onclick="sortBy(${i})">${esc(c)}${arrow(i)}</th>`).join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(v=>`<td>${esc(v)}</td>`).join('')+'</tr>').join('')+'</tbody>'}
function sortBy(i){if(S.sortCol===i)S.sortAsc=!S.sortAsc;else{S.sortCol=i;S.sortAsc=true}renderTable()}
function copyTSV(){const t=S.result.table,n=[t.columns.join('\t')].concat(filteredRows().map(r=>r.join('\t'))).join('\n');navigator.clipboard.writeText(n);toast('Result table copied as TSV')}
function copyReport(){navigator.clipboard.writeText(S.result.report);toast('Report copied')}
function chartConfig(kind,cfg){const type=kind==='barh'?'bar':kind;const datasets=cfg.datasets.map((d,i)=>{const col=PALETTE[i%PALETTE.length];if(['pie','doughnut','polarArea'].includes(kind))return{label:d.label,data:d.data,backgroundColor:cfg.labels.map((_,j)=>PALETTE[j%PALETTE.length]),borderColor:cssVar('--panel'),borderWidth:1};return{label:d.label,data:d.data,borderColor:col,backgroundColor:hexA(col,kind==='line'?.16:.52),pointRadius:3,tension:.3,fill:['line','radar'].includes(kind)}});const conf={type,data:{labels:cfg.labels,datasets},options:{responsive:true,maintainAspectRatio:false,animation:{duration:650},plugins:{legend:{labels:{color:cssVar('--text')}},tooltip:{backgroundColor:'#08111e',borderColor:'#37e0c1',borderWidth:1,titleColor:'#37e0c1'}},interaction:{intersect:false,mode:'index'}}};if(kind==='barh')conf.options.indexAxis='y';if(['bar','line','radar'].includes(type)){const ax={ticks:{color:cssVar('--muted')},grid:{color:cssVar('--line')}};conf.options.scales=type==='radar'?{r:{ticks:{color:cssVar('--muted')},grid:{color:cssVar('--line')},pointLabels:{color:cssVar('--muted')}}}:{x:ax,y:{...ax,beginAtZero:true}}}return conf}
function buildChartUI(cfg){if(!cfg){$('chartCard').style.display='none';S.chartCfg=null;return}S.chartCfg=cfg;S.chartKind=cfg.kind||'bar';$('chartCard').style.display='block';$('chartHeading').textContent=cfg.title||'Interactive analytics';document.querySelectorAll('#chartTools [data-k]').forEach(b=>b.classList.toggle('active',b.dataset.k===S.chartKind));if(chart)chart.destroy();chart=new Chart($('chartCv'),chartConfig(S.chartKind,cfg))}
function setKind(k){if(!S.chartCfg)return;S.chartKind=k;document.querySelectorAll('#chartTools [data-k]').forEach(b=>b.classList.toggle('active',b.dataset.k===k));if(chart)chart.destroy();chart=new Chart($('chartCv'),chartConfig(k,S.chartCfg))}
function chartPNG(){if(!chart)return;const a=document.createElement('a');a.href=chart.toBase64Image();a.download='cmms_chart.png';a.click();toast('Chart PNG downloaded')}
function openChartModal(){if(!chart)return;$('chartModalTitle').textContent=S.chartCfg.title||'Chart';$('chartModal').classList.add('open');if(modalChart)modalChart.destroy();modalChart=new Chart($('chartModalCv'),chartConfig(S.chartKind,S.chartCfg))}
function closeChartModal(){$('chartModal').classList.remove('open');if(modalChart){modalChart.destroy();modalChart=null}}
function toggleCollapse(id){const c=$(id);c.classList.toggle('collapsed');localStorage.setItem('cmms-report-collapsed',c.classList.contains('collapsed')?'1':'0')}
function setReportSize(size){const r=$('reportText');r.classList.remove('compact','large');if(size==='compact')r.classList.add('compact');if(size==='large')r.classList.add('large');document.querySelectorAll('.seg button').forEach(b=>b.classList.remove('active'));$('view'+size.charAt(0).toUpperCase()+size.slice(1)).classList.add('active');localStorage.setItem('cmms-report-size',size)}
function scrollToSection(id){const el=$(id);if(el){$('content').scrollTo({top:Math.max(0,el.offsetTop-18),behavior:'smooth'})}}
function scrollToTop(){$('content').scrollTo({top:0,behavior:'smooth'})}
function fillExample(){const examples=['Which 10 assets have the highest downtime?','Compare Mumbai and Pune maintenance costs.','Which technicians have the best SLA performance?','Show monthly maintenance cost trend for 2025.'];$('q').value=examples[Math.floor(Math.random()*examples.length)];$('q').focus()}
function toggleCollapse(id){const c=$(id);c.classList.toggle('collapsed');localStorage.setItem('cmms-report-collapsed',c.classList.contains('collapsed')?'1':'0')}
function setReportSize(size){const r=$('reportText');r.classList.remove('compact','large');if(size==='compact')r.classList.add('compact');if(size==='large')r.classList.add('large');document.querySelectorAll('.seg .ghost').forEach(b=>b.classList.remove('active'));const el=$('view'+size.charAt(0).toUpperCase()+size.slice(1));if(el)el.classList.add('active');localStorage.setItem('cmms-report-size',size)}
function scrollToSection(id){const el=$(id);if(el)el.scrollIntoView({behavior:'smooth',block:'start'})}
function scrollToTop(){$('content').scrollTo({top:0,behavior:'smooth'})}
function toggleCode(){const b=$('codeBlk');b.style.display=b.style.display==='none'?'block':'none'}
function dl(fmt){if(!S.jobId)return;window.open(`/api/download/${S.jobId}?fmt=${fmt}`,'_blank');toast('Downloading '+fmt.toUpperCase())}
async function openBrowser(){if(!S.meta||!S.meta.ok)await loadMeta();if(!S.meta?.tables?.length)return toast('CMMS API is not ready',true);$('brTable').innerHTML=S.meta.tables.map(t=>`<option value="${esc(t.name)}">${esc(t.name)} (${t.row_count.toLocaleString()})</option>`).join('');$('browserModal').classList.add('open');S.brOffset=0;brFetch()}
function closeBrowser(){$('browserModal').classList.remove('open')}
async function brLoad(){S.brOffset=0;brFetch()}
async function brNav(d){S.brOffset=Math.max(0,S.brOffset+d*25);brFetch()}
async function brFetch(){const t=$('brTable').value;if(!t)return;const s=$('brSearch').value.trim();const p=new URLSearchParams({limit:25,offset:S.brOffset});if(s)p.set('search',s);const rr=await fetch(`/api/browse/${encodeURIComponent(t)}?${p}`);const d=await rr.json();const cols=d.columns||(d.data?.[0]?Object.keys(d.data[0]):[]);S.brTotal=d.total_rows||0;$('brTableEl').innerHTML='<thead><tr>'+cols.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr></thead><tbody>'+(d.data||[]).map(r=>'<tr>'+cols.map(c=>`<td>${esc(r[c])}</td>`).join('')+'</tr>').join('')+'</tbody>';$('brInfo').textContent=`${S.brTotal.toLocaleString()} rows · ${(d.data||[]).length?S.brOffset+1:0}–${S.brOffset+(d.data||[]).length}`}
function brDl(fmt){const t=$('brTable').value,s=$('brSearch').value.trim();const p=s?`?search=${encodeURIComponent(s)}`:'';window.open(`/api/browse_${fmt}/${encodeURIComponent(t)}${p}`,'_blank')}
setInterval(()=>{if(document.visibilityState==='visible')loadMeta()},10000)
</script>
</body></html>"""
# ==============================================================================
# CLI MODE
# ==============================================================================

def run_cli(argv: List[str]):
    global ENGINE
    gem = Gemini(GEMINI_API_KEY, GEMINI_MODEL)
    api = MockApi(API_BASE)
    if not api.check():
        _die(f"ERROR: mock API not reachable at {API_BASE}")
    ENGINE = Engine(gem, api)
    ENGINE.bootstrap()

    if len(argv) > argv.index("--cli") + 1:
        question = " ".join(argv[argv.index("--cli") + 1:])
        result = ENGINE.run(question)
        print(_report_txt(result))
        return

    print("=" * 60)
    print(" CMMS AI Copilot CLI  (type 'exit' to quit)")
    print("=" * 60)
    while True:
        try:
            q = input("\n❓ question> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("exit", "quit", ""):
            break
        try:
            print(ENGINE.run(q)["report"])
        except Exception as e:
            print(f"ERROR: {e}")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    argv = sys.argv[1:]
    if "--cli" in argv:
        run_cli(argv)
        return
    print("=" * 62)
    print(" CMMS AI COPILOT")
    print(f"   Web UI   : http://{COPILOT_HOST}:{COPILOT_PORT}")
    print(f"   Mock API : {API_BASE}")
    print(f"   Excel    : {'ready' if HAS_XLSX else 'pip install openpyxl'}")
    print(f"   PDF      : {'ready' if HAS_PDF else 'pip install reportlab'}")
    print("=" * 62)
    uvicorn.run(app, host=COPILOT_HOST, port=COPILOT_PORT)


if __name__ == "__main__":
    main()
