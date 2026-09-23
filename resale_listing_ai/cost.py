"""Cost ledger — every model/search/image operation logs here, incl. retries.
Reporting (avg per attempt / per completed listing, by stage) is then a query."""

import json
from pathlib import Path


class CostLedger:
    def __init__(self):
        self.rows = []

    def add(self, submission_id, stage, service, cost_cad, retry=False,
            input_tokens=None, output_tokens=None, model=None):
        self.rows.append({
            "submission_id": submission_id, "stage": stage, "service": service,
            "cost_cad": round(float(cost_cad), 4), "retry": retry,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "model": model,
        })

    def total(self, submission_id=None):
        return round(sum(r["cost_cad"] for r in self.rows
                         if submission_id is None or r["submission_id"] == submission_id), 4)

    def by_stage(self):
        out = {}
        for r in self.rows:
            out[r["stage"]] = round(out.get(r["stage"], 0) + r["cost_cad"], 4)
        return out

    def tokens_by_stage(self):
        """Input/output token totals per stage (rows with no token detail
        contribute nothing). Companion to by_stage() for token reporting."""
        out = {}
        for r in self.rows:
            if r.get("input_tokens") is None and r.get("output_tokens") is None:
                continue
            agg = out.setdefault(r["stage"], {"input_tokens": 0, "output_tokens": 0})
            agg["input_tokens"] += r.get("input_tokens") or 0
            agg["output_tokens"] += r.get("output_tokens") or 0
        return out

    def dump(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            for r in self.rows:
                fh.write(json.dumps(r) + "\n")
