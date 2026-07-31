import json
import os

BASE_DIR = os.path.dirname(__file__)
DEFAULTS_PATH = os.path.join(BASE_DIR, "avito_defaults.json")


def load_defaults() -> dict:
    if not os.path.exists(DEFAULTS_PATH):
        return {}
    with open(DEFAULTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_defaults(values: dict) -> None:
    with open(DEFAULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False, indent=2)
