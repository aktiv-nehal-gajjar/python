"""
Odoo Auto-Translation Backend
Run: uvicorn backend:app --host 0.0.0.0 --port 8000 --reload

Install deps:
  pip install fastapi uvicorn polib deep-translator
"""

import os
import sys
import threading
import time
from typing import Optional
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────

_config: dict = {}          # { odoo_path, custom_path, db_name }
_env = None                 # Odoo environment (kept open)
_cr  = None                 # DB cursor

translation_state = {
    "status":             "idle",   # idle | running | completed | failed
    "log":                [],
    "current_module":     None,
    "current_lang":       None,
    "total_modules":      0,
    "processed_modules":  0,
    "total_files":        0,
    "processed_files":    0,
    "stats":              {"created": 0, "updated": 0, "no_change": 0, "errors": 0},
    "error":              None,
}

# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

app = FastAPI(title="Odoo Translation Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class ValidateRequest(BaseModel):
    odoo_path:   str
    custom_path: str
    db_name:     str


# ─────────────────────────────────────────────
# HELPERS  (same logic as original script)
# ─────────────────────────────────────────────

def bootstrap_odoo(odoo_path: str, custom_path: str, db_name: str):
    global _env, _cr
    if odoo_path not in sys.path:
        sys.path.insert(0, odoo_path)
    if custom_path not in sys.path:
        sys.path.insert(0, custom_path)

    from odoo.modules.registry import Registry
    from odoo import api, SUPERUSER_ID
    from odoo.tools import config as odoo_config

    odoo_config.parse_config([])
    registry = Registry(db_name)
    _cr  = registry.cursor()
    _env = api.Environment(_cr, SUPERUSER_ID, {})
    return _env, _cr


def get_module_last_update(module_path: str) -> float:
    latest = 0.0
    for root, _, files in os.walk(module_path):
        for f in files:
            if f.endswith((".py", ".xml", ".js")):
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
    return latest


def is_module_changed(module_path: str, po_file: str) -> bool:
    if not os.path.exists(po_file):
        return True
    return get_module_last_update(module_path) > os.path.getmtime(po_file)


def translate_text(text: str, lang: str) -> str:
    try:
        from deep_translator import GoogleTranslator
        if not text:
            return ""
        return GoogleTranslator(source="auto", target=lang.split("_")[0]).translate(text).strip()
    except Exception:
        return ""


def export_and_translate(env, module: str, lang: str, custom_path: str):
    from odoo.tools.translate import trans_export

    module_path = os.path.join(custom_path, module)
    i18n_path   = os.path.join(module_path, "i18n")
    os.makedirs(i18n_path, exist_ok=True)
    po_file     = os.path.join(i18n_path, f"{lang}.po")

    if not is_module_changed(module_path, po_file):
        return "no_change", po_file

    existed_before = os.path.exists(po_file)

    with open(po_file, "wb") as f:
        trans_export(lang, [module], f, format="po", env=env)

    import polib
    po      = polib.pofile(po_file)
    updated = False

    for entry in po:
        if entry.msgid and not entry.msgstr:
            entry.msgstr = translate_text(entry.msgid, lang)
            updated = True

    if updated:
        po.save(po_file)

    if not existed_before:
        return "created", po_file
    return ("updated" if updated else "no_change"), po_file


# ─────────────────────────────────────────────
# BACKGROUND TRANSLATION JOB
# ─────────────────────────────────────────────

def run_translation_job():
    global translation_state

    try:
        env         = _env
        custom_path = _config["custom_path"]

        modules   = [m["name"] for m in _get_modules_data()]
        languages = _get_languages_data()

        translation_state.update({
            "status":            "running",
            "total_modules":     len(modules),
            "total_files":       len(modules) * len(languages),
            "processed_modules": 0,
            "processed_files":   0,
            "stats":             {"created": 0, "updated": 0, "no_change": 0, "errors": 0},
            "log":               [],
            "error":             None,
        })

        for module in modules:
            translation_state["current_module"] = module
            _log(f"Processing module: {module}")

            for lang in languages:
                translation_state["current_lang"] = lang
                try:
                    status, po_file = export_and_translate(env, module, lang, custom_path)
                    translation_state["stats"][status] += 1
                    translation_state["processed_files"] += 1
                    _log(f"  {lang}: {status} → {os.path.basename(po_file)}")
                except Exception as e:
                    translation_state["stats"]["errors"] += 1
                    translation_state["processed_files"] += 1
                    _log(f"  {lang}: ERROR — {e}")

            translation_state["processed_modules"] += 1

        _cr.commit()
        translation_state["status"]         = "completed"
        translation_state["current_module"] = None
        translation_state["current_lang"]   = None
        _log("✓ Translation completed successfully")

    except Exception as e:
        translation_state["status"] = "failed"
        translation_state["error"]  = str(e)
        _log(f"✗ Fatal error: {e}")


def _log(msg: str):
    translation_state["log"].append({"ts": time.time(), "msg": msg})


# ─────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────

def _get_modules_data():
    if _env is None:
        return []
    modules = _env["ir.module.module"].search([("state", "=", "installed")]).mapped("name")
    custom_path = _config.get("custom_path", "")
    return [
        {"name": m, "path": os.path.join(custom_path, m)}
        for m in modules
        if os.path.exists(os.path.join(custom_path, m))
    ]


def _get_languages_data():
    if _env is None:
        return []
    return _env["res.lang"].search([("active", "=", True)]).mapped("code")


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "config_loaded": bool(_config)}


@app.post("/api/validate")
def validate_config(req: ValidateRequest):
    global _config
    if not os.path.exists(req.odoo_path):
        return {"success": False, "error": f"Odoo path not found: {req.odoo_path}"}
    if not os.path.exists(req.custom_path):
        return {"success": False, "error": f"Custom addons path not found: {req.custom_path}"}

    try:
        env, cr = bootstrap_odoo(req.odoo_path, req.custom_path, req.db_name)
        _config = {
            "odoo_path":   req.odoo_path,
            "custom_path": req.custom_path,
            "db_name":     req.db_name,
        }
        return {"success": True, "message": "Paths and database connection validated successfully."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/modules")
def get_modules():
    if not _config:
        return {"success": False, "error": "Not configured. Run /api/validate first."}
    modules = _get_modules_data()
    return {"success": True, "modules": modules, "count": len(modules)}


@app.get("/api/languages")
def get_languages():
    if not _config:
        return {"success": False, "error": "Not configured. Run /api/validate first."}
    langs = _get_languages_data()
    return {"success": True, "languages": langs, "count": len(langs)}


@app.post("/api/translate/start")
def start_translation():
    if translation_state["status"] == "running":
        return {"success": False, "error": "Translation is already running."}
    if not _config:
        return {"success": False, "error": "Not configured. Run /api/validate first."}

    thread = threading.Thread(target=run_translation_job, daemon=True)
    thread.start()
    return {"success": True, "message": "Translation job started in background."}


@app.get("/api/translate/status")
def get_status():
    return {
        "success": True,
        **{k: v for k, v in translation_state.items() if k != "log"},
        "recent_log": translation_state["log"][-20:],
    }


@app.post("/api/translate/reset")
def reset():
    translation_state.update({
        "status": "idle", "log": [], "current_module": None,
        "current_lang": None, "total_modules": 0, "processed_modules": 0,
        "total_files": 0, "processed_files": 0,
        "stats": {"created": 0, "updated": 0, "no_change": 0, "errors": 0},
        "error": None,
    })
    return {"success": True}
