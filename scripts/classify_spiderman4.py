#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pick(row: Dict[str, Any], names: Iterable[str]) -> Tuple[Optional[str], Any]:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return name, row[name]
    return None, None


def to_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {"-", ".", "-."}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def classify(row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    cinema_key, cinema = pick(row, cfg["cinema_fields"])
    film_key, tech = pick(row, cfg["technical_film_fields"])
    revenue_key, revenue = pick(row, cfg["revenue_fields"])
    sessions_key, sessions = pick(row, cfg["sessions_fields"])
    viewers_key, viewers = pick(row, cfg["viewers_fields"])

    base = {
        "class": "unallocated",
        "reason": "No verified classification rule matched this row.",
        "cinema": cinema,
        "technical_film": tech,
        "revenue": to_number(revenue),
        "sessions": to_number(sessions),
        "viewers": to_number(viewers),
        "source_keys": {
            "cinema": cinema_key,
            "technical_film": film_key,
            "revenue": revenue_key,
            "sessions": sessions_key,
            "viewers": viewers_key,
        },
        "source": row,
    }

    if not cinema:
        base["reason"] = "EAIS row has no cinema field; clean separation requires cinema-level or showtime-level data."
        return base

    cinema_s = str(cinema)
    tech_s = "" if tech is None else str(tech)

    for rule in cfg.get("rules", []):
        if re.search(rule["cinema_regex"], cinema_s) and re.search(rule["technical_film_regex"], tech_s):
            base["class"] = rule["class"]
            base["reason"] = rule.get("reason", "Matched classification rule.")
            base["matched_rule"] = rule
            return base

    return base


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "rows": len(rows),
        "classes": {},
        "clean_target": {"revenue": 0.0, "sessions": 0.0, "viewers": 0.0},
        "mixed_unallocated": {"revenue": 0.0, "sessions": 0.0, "viewers": 0.0},
        "excluded_other_titles": {"revenue": 0.0, "sessions": 0.0, "viewers": 0.0},
        "unallocated": {"revenue": 0.0, "sessions": 0.0, "viewers": 0.0},
    }
    bucket_map = {
        "target": "clean_target",
        "mixed": "mixed_unallocated",
        "exclude": "excluded_other_titles",
        "unallocated": "unallocated",
    }
    for r in rows:
        cls = r["class"]
        out["classes"][cls] = out["classes"].get(cls, 0) + 1
        b = bucket_map.get(cls, "unallocated")
        for k in ("revenue", "sessions", "viewers"):
            out[b][k] += r[k]
    return out


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: classify_spiderman4.py RESULT_JSON RULES_JSON OUTPUT_DIR", file=sys.stderr)
        return 2

    result_path, rules_path, out_dir = sys.argv[1:4]
    result = load_json(result_path)
    cfg = load_json(rules_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    data = result.get("data", {})
    candidates: List[Dict[str, Any]] = []
    source_arrays = []

    # Prefer the most granular arrays first. Future API normalization may add cinema/showtime rows.
    for name in (
        "hourly_schedule",
        "cinema_schedule",
        "showtimes",
        "daily_schedule",
        "daily_stats",
    ):
        arr = data.get(name)
        if isinstance(arr, list) and arr:
            source_arrays.append({"name": name, "rows": len(arr)})
            candidates.extend(arr)

    classified = [classify(r, cfg) for r in candidates if isinstance(r, dict)]
    summary = summarize(classified)
    summary["target"] = cfg.get("target")
    summary["source_arrays"] = source_arrays
    summary["cinema_level_rows"] = sum(1 for r in classified if r.get("cinema"))
    summary["separation_possible"] = summary["cinema_level_rows"] > 0
    summary["method_note"] = (
        "Only rows matched by verified cinema + technical-film rules with class=target are counted as clean target. "
        "Mixed rows are deliberately not assigned to Spider-Man 4. Cinema name alone is insufficient when the same wrapper is used for multiple western releases."
    )

    with open(out / "spiderman4-classified.json", "w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)
    with open(out / "spiderman4-clean-summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
