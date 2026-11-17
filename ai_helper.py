"""AI content writer (Google Gemini, free tier) with a resume fact-check and manual approval.

Usage:
    python ai_helper.py generate            # write new summary variants -> generated_content.yaml
    python ai_helper.py list                # show variants with ids
    python ai_helper.py approve 1 3         # approve variants (or: approve all)
    python ai_helper.py unapprove 2
Needs the key in the environment variable named by ai.api_key_env (default GEMINI_API_KEY).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import yaml

from logger import ActivityLogger, load_config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_CHARS = 950  # Naukri summary limit is about 1000


# ---------------------------------------------------------------- resume
def resume_text(cfg: dict) -> str:
    """Extract resume text and strip contact details before anything leaves the PC."""
    from pypdf import PdfReader

    reader = PdfReader(str(Path(cfg["paths"]["resume"]).resolve()))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", text)                      # emails
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", " ", text)                           # phone numbers
    text = re.sub(r"https?://\S+|\S*(?:linkedin|github)\.com\S*|\S+\.vercel\.app", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------------------------------------------------------- Gemini
def _call_gemini(cfg: dict, prompt: str) -> str:
    ai = cfg.get("ai", {})
    key = os.environ.get(ai.get("api_key_env", "GEMINI_API_KEY"), "")
    if not key:
        raise RuntimeError(f"Set the {ai.get('api_key_env', 'GEMINI_API_KEY')} environment variable first.")
    url = GEMINI_URL.format(model=ai.get("model", "gemini-2.5-flash"))
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=ai.get("timeout_seconds", 120)) as r:
                data = json.load(r)
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(6 * (attempt + 1))  # free tier rate limits: back off and retry
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, json.JSONDecodeError) as e:
            if attempt < 3:
                time.sleep(4)
                continue
            raise RuntimeError(f"Gemini call failed: {e}")
    raise RuntimeError("Gemini call failed")


SUMMARY_PROMPT = """You are writing the "Profile summary" for a fresher's Naukri profile.

RESUME (contact details removed):
{resume}

Write {n} different variants. Rules:
- Use ONLY facts, numbers, tools and technologies that appear in the resume. Do not invent skills, employers, metrics or experience.
- Each variant 550-900 characters, plain text, no bullet points, no emojis, no first-person "I".
- Each variant takes a different angle: (1) AI/ML engineering overall, (2) NLP and RAG, (3) research and the IEEE publication, (4) data science and end-to-end projects.
- Include relevant search keywords naturally. No keyword stuffing.
- End by naming the roles sought: Machine Learning Engineer, AI Engineer or Data Scientist.
Return JSON: {{"variants": [{{"text": "...", "skills_used": ["every tool, technology or skill named in the text"]}}]}}"""


# ---------------------------------------------------------------- fact check
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def validate(item: dict, resume: str) -> str | None:
    """Return a rejection reason, or None if the variant is safe."""
    text = (item.get("text") or "").strip()
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return f"too long ({len(text)} chars)"
    r = _norm(resume)
    resume_nums = set(re.findall(r"\d+(?:\.\d+)?", resume))
    bad_nums = sorted(n for n in set(re.findall(r"\d+(?:\.\d+)?", text)) if n not in resume_nums)
    if bad_nums:
        return f"numbers not in resume: {bad_nums}"
    bad = [s for s in item.get("skills_used", []) if _norm(str(s)) not in r]
    if bad:
        return f"skills not in resume: {bad}"
    return None


# ---------------------------------------------------------------- store
def _store_path(cfg: dict) -> Path:
    return Path(cfg.get("ai", {}).get("content_file", "./generated_content.yaml"))


def load_store(cfg: dict) -> dict:
    p = _store_path(cfg)
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def save_store(cfg: dict, store: dict):
    _store_path(cfg).write_text(yaml.safe_dump(store, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")


def approved_texts(cfg: dict, field: str) -> list:
    return [v["text"] for v in load_store(cfg).get(field, []) if v.get("approved")]


def pick_for_today(texts: list) -> str:
    """Rotate through approved texts by date. With only one text, toggle the trailing period."""
    if len(texts) >= 2:
        return texts[date.today().toordinal() % len(texts)]
    base = texts[0].rstrip(".")
    return base + "." if date.today().toordinal() % 2 else base


# ---------------------------------------------------------------- commands
def generate_summary(cfg: dict, log: ActivityLogger, n: int = 4) -> int:
    resume = resume_text(cfg)
    raw = _call_gemini(cfg, SUMMARY_PROMPT.format(resume=resume, n=n))
    try:
        items = json.loads(raw)["variants"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.log("ai_generate", "error", details="model did not return valid JSON")
        return 0

    store = load_store(cfg)
    existing = store.setdefault("summary", [])
    seen = {v["text"] for v in existing}
    next_id = max([v["id"] for v in existing], default=0) + 1
    added = 0
    for it in items:
        reason = validate(it, resume)
        text = (it.get("text") or "").strip()
        if reason:
            log.log("ai_generate", "rejected", details=reason)
            continue
        if text in seen:
            continue
        existing.append({"id": next_id, "approved": False, "text": text})
        next_id += 1
        added += 1
    save_store(cfg, store)
    log.log("ai_generate", "ok", details=f"{added} summary variants saved (not approved yet)")
    return added


def cmd_list(cfg: dict):
    for field, rows in load_store(cfg).items():
        print(f"\n== {field} ==")
        for v in rows:
            print(f"[{v['id']}] approved={v['approved']} ({len(v['text'])} chars)\n{v['text']}\n")


def cmd_approve(cfg: dict, ids: list, value: bool):
    store = load_store(cfg)
    n = 0
    for rows in store.values():
        for v in rows:
            if "all" in ids or str(v["id"]) in ids:
                v["approved"] = value
                n += 1
    save_store(cfg, store)
    print(f"{'Approved' if value else 'Unapproved'} {n} variant(s).")


def main() -> int:
    ap = argparse.ArgumentParser(description="AI profile content (Gemini)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    sub.add_parser("list")
    for name in ("approve", "unapprove"):
        sub.add_parser(name).add_argument("ids", nargs="+", help="variant ids, or 'all'")
    args = ap.parse_args()

    cfg = load_config()
    log = ActivityLogger(cfg["paths"]["activity_log"])
    try:
        if args.cmd == "generate":
            generate_summary(cfg, log)
            cmd_list(cfg)
            print("Review the variants, then run: python ai_helper.py approve <ids>")
        elif args.cmd == "list":
            cmd_list(cfg)
        else:
            cmd_approve(cfg, args.ids, args.cmd == "approve")
    except Exception as e:
        log.log("ai_generate", "error", details=str(e)[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())