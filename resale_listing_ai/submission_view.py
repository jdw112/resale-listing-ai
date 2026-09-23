"""Shapes a submission's status/result into the JSON body both the intake API
(GET /v1/submissions/{id}) and the worker's completion callback (Task 4) send —
one definition so the two can't drift apart.
"""

_TERMINAL_STATE_BY_STATUS = {
    "Rejected": "REJECTED",
    "Clarification required": "CLARIFICATION_REQUIRED",
}


def submission_status_body(submission_id, state):
    if state["state"] == "DONE":
        result = state["result"]
        api_state = _TERMINAL_STATE_BY_STATUS.get(result["status"], "PUBLISHED")
        return {
            "submission_id": submission_id, "state": api_state, "outcome": result["status"],
            "product_key": result.get("product_key"), "listing_sku": result.get("listing_sku"),
            "missing": result.get("missing", []), "cost_cad": result.get("cost_cad"),
            "product": result.get("product"), "listing": result.get("listing"),
        }
    return {
        "submission_id": submission_id, "state": state["state"], "outcome": None,
        "product_key": state["product_key"], "listing_sku": state["sku"], "missing": [], "cost_cad": None,
        "product": None, "listing": None,
    }
