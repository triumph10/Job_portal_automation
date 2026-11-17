"""Logging to console and activity_log.csv, plus config loading."""
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CSV_HEADER = ["timestamp", "action", "status", "title", "company", "details"]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_console_logger(name: str = "naukri_agent") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


class ActivityLogger:
    """Writes every action to the console and to a CSV file."""

    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)
        self.console = get_console_logger()
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADER)

    def log(self, action: str, status: str, title: str = "", company: str = "", details: str = ""):
        row = [datetime.now().isoformat(timespec="seconds"), action, status, title, company, details]
        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except OSError as e:
            self.console.error("Could not write CSV log: %s", e)
        msg = f"{action} [{status}] {title} {company} {details}".strip()
        if status.lower() in ("error", "failed"):
            self.console.error(msg)
        else:
            self.console.info(msg)