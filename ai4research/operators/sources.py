"""Source operators O4-O6: load containers, select items, acquire documents.

Source intake (design §12): a container (channel/repo/folder) is not a document; it
yields candidate items (videos/files); each item is acquired into a document via its
adapter. Every selected item gets an acquisition attempt, even on failure.
"""
from __future__ import annotations

import json

from .. import ids
from ..adapters import SOURCE_PACK_MAP, get_adapter
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator


def _read_input_containers(ctx: RunContext) -> list[dict]:
    path = ctx.input_dir / "source_containers.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class SourceContainerLoadOperator(Operator):
    NAME = "SourceContainerLoadOperator"
    INPUT_SCHEMAS = ["physical_plan_nodes"]
    OUTPUT_SCHEMAS = ["source_containers"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        containers = _read_input_containers(ctx)
        contract = (work.read_rows("research_contracts") or [{}])[0]
        allowed = set(contract.get("source_policy", {}).get("allowed_source_pack_types") or [])
        seen: set[str] = set()
        rows = []
        for i, c in enumerate(containers):
            cid = c["container_id"]
            if cid in seen:
                raise ValueError(f"duplicate container_id: {cid}")
            seen.add(cid)
            spt = c["source_pack_type"]
            if spt not in SOURCE_PACK_MAP:
                raise ValueError(f"unsupported source_pack_type: {spt}")
            if allowed and spt not in allowed:
                raise ValueError(f"source_pack_type not allowed by contract: {spt}")
            rows.append({
                "container_id": f"{ctx.run_id}.{cid}",
                "run_id": ctx.run_id,
                "source_pack_type": spt,
                "container_locator": c.get("container_locator"),
                "label": c.get("label"),
                "container_rank": i,
                "user_supplied": 1,
            })
        work.write_rows("source_containers", rows)
        return {"containers": len(rows)}


class SourceItemSelectOperator(Operator):
    NAME = "SourceItemSelectOperator"
    INPUT_SCHEMAS = ["source_containers"]
    OUTPUT_SCHEMAS = ["selected_source_items"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        containers = _read_input_containers(ctx)
        contract = (work.read_rows("research_contracts") or [{}])[0]
        cap = contract.get("source_policy", {}).get("max_items_per_container")
        cap = None if cap is None else int(cap)
        rows = []
        seen: set[str] = set()
        supplied_items = 0
        for c in containers:
            source_family, adapter_id, _ = SOURCE_PACK_MAP[c["source_pack_type"]]
            container_id = f"{ctx.run_id}.{c['container_id']}"
            indexed_items = list(enumerate(c.get("items", [])))
            supplied_items += len(indexed_items)
            for _, item in indexed_items:
                sid = f"{ctx.run_id}.{item['item_id']}"
                if sid in seen:
                    raise ValueError(f"duplicate item_id: {item['item_id']}")
                seen.add(sid)
            if cap is not None and len(indexed_items) > cap:
                dated = [(idx, item) for idx, item in indexed_items if item.get("published_at")]
                undated = [(idx, item) for idx, item in indexed_items if not item.get("published_at")]
                dated_sorted = sorted(dated, key=lambda pair: pair[1]["published_at"], reverse=True)
                selected = (dated_sorted + undated)[:cap]
                selection_reason = f"freshness_top_{cap}"
            else:
                selected = indexed_items
                selection_reason = "include_all"
            for original_index, item in selected:
                sid = f"{ctx.run_id}.{item['item_id']}"
                rows.append({
                    "selected_item_id": sid,
                    "run_id": ctx.run_id,
                    "container_id": container_id,
                    "source_family": source_family,
                    "adapter_id": adapter_id,
                    "item_locator": item.get("item_locator") or {},
                    "title": item.get("title"),
                    "creator": item.get("creator"),
                    "published_at": item.get("published_at"),
                    "accessed_at": ids.utc_now_iso(),
                    "source_rank": original_index,
                    "selection_reason": selection_reason,
                    "acquisition_status": "pending",
                    "provider_metadata": item.get("provider_metadata"),
                })
        work.write_rows("selected_source_items", rows)
        return {"supplied_items": supplied_items, "selected_items": len(rows)}


class DocumentAcquisitionOperator(Operator):
    NAME = "DocumentAcquisitionOperator"
    INPUT_SCHEMAS = ["selected_source_items"]
    OUTPUT_SCHEMAS = ["acquisition_attempts", "documents"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        items = work.read_rows("selected_source_items")
        attempts = []
        succeeded = failed = docs = 0
        for i, item in enumerate(items):
            attempt_id = ids.mint(ctx.run_id, "DAA", i)
            try:
                adapter = get_adapter(item["adapter_id"])
                if adapter is None:
                    ok, fcode, fmsg, res = False, "adapter_not_available", \
                        f"no adapter '{item['adapter_id']}' in Phase 0", None
                else:
                    vok, vreason = adapter.validate_item(item)
                    if not vok:
                        ok, fcode, fmsg, res = False, "invalid_item", vreason, None
                    else:
                        res = adapter.acquire(item)
                        ok, fcode, fmsg = res.ok, res.failure_code, res.failure_message
            except Exception as exc:  # noqa: BLE001 - one bad item must not abort the batch
                ok, fcode, fmsg, res = False, "acquisition_error", f"{type(exc).__name__}: {exc}", None
            attempts.append({
                "attempt_id": attempt_id,
                "run_id": ctx.run_id,
                "selected_item_id": item["selected_item_id"],
                "adapter_id": item["adapter_id"],
                "attempt_number": 1,
                "started_at": ids.utc_now_iso(),
                "completed_at": ids.utc_now_iso(),
                "status": "succeeded" if ok else "failed",
                "input_locator": item.get("item_locator"),
                "failure_code": fcode,
                "failure_message": fmsg,
                "retryable": 0,
            })
            item["acquisition_status"] = "acquired" if ok else "failed"
            if ok and res is not None:
                document_id = ids.mint(ctx.run_id, "DOC", docs)
                work.write_document({
                    "document_id": document_id,
                    "run_id": ctx.run_id,
                    "selected_item_id": item["selected_item_id"],
                    "acquisition_attempt_id": attempt_id,
                    "document_kind": res.document_kind,
                    "title": res.title,
                    "raw_text": res.text,
                    "normalized_text": None,  # set by O7 (DocumentNormalize) in the next milestone
                    "language": None,
                    "published_at": item.get("published_at"),
                    "content_hash": ids.sha256_text(res.text),
                    "normalization": None,
                    "provider_metadata": item.get("provider_metadata"),
                })
                succeeded += 1
                docs += 1
            else:
                failed += 1
        work.write_rows("acquisition_attempts", attempts)
        work.write_rows("selected_source_items", items)  # persist resolved acquisition_status
        return {"attempts": len(attempts), "succeeded": succeeded, "failed": failed, "documents": docs}
