"""One current forest: read a snapshot, review proposals, and merge accepted work."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

CANONICAL = Path(__file__).with_name("canonical.json")


def load(path=None) -> dict:
    """Read current state; only an explicit missing path creates an empty test state."""
    if path is None and not CANONICAL.exists():
        raise FileNotFoundError(f"Current canonical forest is missing: {CANONICAL}")
    path = Path(path) if path is not None else CANONICAL
    if not path.exists():
        return {"revision": 0, "nodes": {}, "links": [], "evidence": {}, "campaigns": []}
    return json.loads(path.read_text(encoding="utf-8"))


def context(scenario, limit=12, state=None) -> dict:
    """Return bounded hypothesis cards and explicit bridges, without candidates."""
    state = load() if state is None else state
    if limit < 1:
        raise ValueError("Context limit must be positive")
    hypotheses = {key: node for key, node in state["nodes"].items()
                  if node["kind"] == "hypothesis"
                  and node.get("status") not in {"retired", "superseded"}}
    direct = [node for node in hypotheses.values() if node.get("scenario") == scenario]
    direct.sort(key=lambda node: (node.get("updated_at", ""), node.get("revision", 1)), reverse=True)
    # Reserve a third of the reading budget for explicit source-to-target bridges.
    chosen = {node["id"]: node for node in direct[:max(1, limit * 2 // 3)]}
    for link in state["links"]:
        if link["relation"] != "transfer_candidate" or not link.get("transfer_contract"):
            continue
        source, target = link["source"], link["target"]
        if source not in hypotheses or target not in hypotheses:
            continue
        if source in chosen or target in chosen:
            for key in (source, target):
                if len(chosen) < limit:
                    chosen.setdefault(key, hypotheses[key])
    for node in direct:
        if len(chosen) < limit:
            chosen.setdefault(node["id"], node)
    links = [link for link in state["links"]
             if link["source"] in chosen and link["target"] in chosen]
    evidence_ids = {key for node in chosen.values() for key in node.get("evidence_ids", [])}
    evidence = [record for key, record in state["evidence"].items() if key in evidence_ids]
    fields = {"id", "version_id", "revision", "statement", "scope", "falsifiers",
              "predictions", "evidence_ids", "scenario", "status"}
    cards = []
    for node in chosen.values():
        card = {key: value for key, value in node.items() if key in fields}
        card["verification_plan"] = node.get("verification_plan") or node.get("verification_action")
        cards.append(card)
    return {"revision": state["revision"], "scenario": scenario,
            "hypotheses": copy.deepcopy(cards), "links": copy.deepcopy(links),
            "evidence": copy.deepcopy(evidence), "available_hypotheses": len(direct),
            "candidate_reuse": False}


def _accepted(records, identifiers, label):
    indexed = {record["id"]: record for record in records}
    if len(indexed) != len(records):
        raise ValueError(f"Duplicate {label} ID in proposal")
    unknown = set(identifiers) - indexed.keys()
    if unknown:
        raise ValueError(f"Review accepts unknown {label}: {sorted(unknown)}")
    return [copy.deepcopy(indexed[key]) for key in dict.fromkeys(identifiers)]


def _check_references(node, nodes, evidence):
    for key in node.get("evidence_ids", []):
        if key not in evidence:
            raise ValueError(f"{node['id']}: unknown evidence {key}")
    for field, kind in (("hypothesis_id", "hypothesis"), ("candidate_id", "candidate"),
                        ("plan_id", "experiment_plan")):
        references = list(node.get(field + "s", []))
        if node.get(field):
            references.append(node[field])
        for key in references:
            if key not in nodes or nodes[key]["kind"] != kind:
                raise ValueError(f"{node['id']}: {field} must reference an accepted {kind}: {key}")
    if node["kind"] == "candidate" and node.get("hypothesis_id"):
        hypothesis = nodes[node["hypothesis_id"]]
        if node.get("hypothesis_version") != hypothesis["revision"]:
            raise ValueError(f"{node['id']}: hypothesis_version differs from the current hypothesis")
    if node["kind"] == "candidate" and node.get("plan_id"):
        if nodes[node["plan_id"]].get("candidate_id") != node["id"]:
            raise ValueError(f"{node['id']}: plan belongs to a different candidate")
    if node["kind"] == "experiment_plan" and node.get("candidate_id"):
        if nodes[node["candidate_id"]].get("plan_id") != node["id"]:
            raise ValueError(f"{node['id']}: candidate names a different experiment plan")


def apply(state, delta, review) -> dict:
    """Apply accepted IDs to a detached private state; this does not write files.

    Delta contains `nodes`, `evidence`, and `links` lists. Review contains
    `accepted_node_ids`, `accepted_evidence_ids`, `reviewer`, and `reason`.
    Accepted node updates are complete records, not partial patches.
    """
    if not review.get("reviewer") or not review.get("reason"):
        raise ValueError("Review needs reviewer and reason")
    nodes = _accepted(delta.get("nodes", []), review.get("accepted_node_ids", []), "node")
    evidence = _accepted(delta.get("evidence", []), review.get("accepted_evidence_ids", []), "evidence")
    result = copy.deepcopy(state)
    now = datetime.now(timezone.utc).isoformat()
    for record in evidence:
        if record.get("kind") not in {"literature", "model"}:
            raise ValueError("Campaign evidence must be literature or model output")
        if not record.get("source") or not record.get("summary"):
            raise ValueError(f"{record['id']}: evidence needs source and summary")
        # Retrieval and prediction alone cannot create an experimental finding.
        record["physical_measurement"] = False
        record["status"] = "diagnostic" if record["kind"] == "model" else "unverified"
        record["scientific_result"] = False
        if record["id"] in result["evidence"] and record != result["evidence"][record["id"]]:
            raise ValueError(f"Evidence ID already exists: {record['id']}; use a new ID")
        result["evidence"][record["id"]] = record
    for node in nodes:
        for field in ("id", "kind", "statement", "scenario"):
            if not node.get(field):
                raise ValueError(f"Node needs {field}")
        previous = result["nodes"].get(node["id"])
        if previous and previous["kind"] != node["kind"]:
            raise ValueError(f"Cannot change node kind: {node['id']}")
        if node["kind"] == "hypothesis":
            if not node.get("scope") or not node.get("falsifiers"):
                raise ValueError(f"{node['id']}: hypothesis needs scope and falsifiers")
            if not (node.get("verification_plan") or node.get("verification_action")):
                raise ValueError(f"{node['id']}: hypothesis needs a verification plan")
        node["status"] = "planned" if node["kind"] == "experiment_plan" else "proposed"
        node["scientific_result"] = False
        node["revision"] = previous.get("revision", 0) + 1 if previous else 1
        node["version_id"] = f"{node['id']}@r{node['revision']}"
        node["updated_at"] = now
        result["nodes"][node["id"]] = node
    for node in nodes:
        _check_references(node, result["nodes"], result["evidence"])
    links = {(item["source"], item["target"], item["relation"]): item for item in result["links"]}
    known = result["nodes"].keys() | result["evidence"].keys()
    for link in delta.get("links", []):
        source, target, relation = link["source"], link["target"], link["relation"]
        # A rejected node also rejects the proposed links that need it.
        if source not in known or target not in known:
            continue
        if relation == "transfer_candidate" and not link.get("transfer_contract"):
            raise ValueError("Cross-scope transfer needs an explicit transfer_contract")
        links[(source, target, relation)] = copy.deepcopy(link)
    result["links"] = list(links.values())
    result["revision"] = state["revision"] + 1
    result["updated_at"] = now
    result["last_review"] = {key: copy.deepcopy(review[key]) for key in
                             ("reviewer", "reason", "accepted_node_ids", "accepted_evidence_ids") if key in review}
    if delta.get("campaign") and delta["campaign"] not in result["campaigns"]:
        result["campaigns"].append(copy.deepcopy(delta["campaign"]))
    return result


def merge(delta, review, expected_revision, path=None) -> dict:
    """Merge under a file lock; archive the replaced snapshot without reading it.

    A changed revision means another campaign committed first. Review against
    the new current forest before retrying. `path` allows isolated local tests.
    """
    path = Path(path) if path is not None else CANONICAL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load(path)
        if current["revision"] != expected_revision:
            raise ValueError(f"Forest changed: expected {expected_revision}, found {current['revision']}")
        updated = apply(current, delta, review)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if path.exists():
            archive = path.parent.parent / "recycle_bin" / "forest"
            archive.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(path, archive / f"revision_{current['revision']:06d}_{stamp}.json")
        os.replace(temporary, path)
        return updated
