"""Generate one report: payload -> (LLM | fallback) -> guardrail -> database.

The control flow has one rule: **every failure path lands on the deterministic
report**. Disabled, unreachable, timed out, malformed, guardrail-rejected - the
clinician still gets the same four sections, with `status` recording which path
produced them.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .client import EgressBlocked, LlmError, chat
from .fallback import render_template_report
from .guardrail import GuardrailResult, check
from .payload import DeidentificationError, build_payload
from .settings import resolved_ai

PROMPT_FILE = "knee_zh_v1.md"
DISCLAIMER_ZH = (
    "本报告由算法自动生成，非诊断结论，须由具备资质的医师结合原始图像复核后方可使用。")


def get_report(conn: sqlite3.Connection, seg_id: int) -> dict[str, Any] | None:
    """The segmentation's report, or None. There is at most one."""
    row = conn.execute(
        "SELECT * FROM ai_report WHERE segmentation_id=?", (seg_id,)).fetchone()
    return _row_to_json(row) if row else None


def report_state(conn: sqlite3.Connection, seg_id: int) -> dict[str, Any]:
    """Whether a report exists, without transferring it.

    Absence is ``exists: false``, not a 404. Asking for a report that has not
    been generated yet is the normal first visit, and answering it with an
    error puts a red line in the console that teaches people to ignore the
    console.
    """
    row = conn.execute(
        "SELECT status, review_state, created_at, version_int"
        " FROM ai_report WHERE segmentation_id=?", (seg_id,)).fetchone()
    if row is None:
        return {"exists": False}
    return {
        "exists": True, "status": row["status"],
        "reviewState": row["review_state"], "createdAt": row["created_at"],
        "generation": row["version_int"],
    }


def _row_to_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "segmentationId": row["segmentation_id"],
        "morphometryId": row["morphometry_id"],
        "version": row["version_int"],
        "lang": row["lang"],
        "provider": row["provider"],
        "model": row["model"],
        "findings": row["findings"],
        "quant": row["quant"],
        "impression": row["impression"],
        "advice": row["advice"],
        "status": row["status"],
        "error": row["error"],
        "guardrail": json.loads(row["guardrail_json"]) if row["guardrail_json"] else None,
        "input": json.loads(row["input_json"]) if row["input_json"] else None,
        "latencyMs": row["latency_ms"],
        "promptTokens": row["prompt_tokens"],
        "completionTokens": row["completion_tokens"],
        "reviewState": row["review_state"],
        "reviewedBy": row["reviewed_by"],
        "reviewedAt": row["reviewed_at"],
        "reviewedText": row["reviewed_text"],
        "createdAt": row["created_at"],
        "disclaimer": DISCLAIMER_ZH,
    }


def generate_report(conn: sqlite3.Connection, cfg: Any, seg_id: int, *,
                    banned_terms: list[str] | None = None) -> dict[str, Any]:
    """Produce the cartilage report for this segmentation (replacing any prior one).

    ``banned_terms`` lets the chapter template decide what this chapter's AI
    may not mention; None keeps the module default.
    """
    from ..morph.service import compute_and_store

    ai = resolved_ai(conn, cfg)
    morph = compute_and_store(conn, cfg, seg_id)
    if morph["state"] != "ready":
        raise ValueError(morph.get("error") or "morphometry failed")

    refs = _load_refs(cfg)
    meta = conn.execute(
        "SELECT p.sex, p.age, p.laterality FROM segmentation sg"
        " JOIN series s ON s.id=sg.series_id JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id WHERE sg.id=?", (seg_id,)).fetchone()

    payload = build_payload(
        morph, refs,
        sex=meta["sex"] if meta else None,
        age=meta["age"] if meta else None,
        laterality=(morph.get("frame") or {}).get("laterality"),
        patient_context=list(ai.patient_context),
    )

    report, status, error, guard, llm = _produce(cfg, ai, payload, banned_terms)

    # Replace, do not append: the clinician sees exactly one current report.
    # version_int survives as "how many times this has been generated", which is
    # useful context without keeping the superseded text around.
    generation = (conn.execute(
        "SELECT COALESCE(MAX(version_int), 0) FROM ai_report WHERE segmentation_id=?",
        (seg_id,)).fetchone()[0]) + 1

    conn.execute(
        "INSERT INTO ai_report (segmentation_id, morphometry_id, version_int, lang,"
        " provider, model, base_url, prompt_hash, input_json, output_json,"
        " findings, quant, impression, advice, status, error, guardrail_json,"
        " latency_ms, prompt_tokens, completion_tokens, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(segmentation_id) DO UPDATE SET"
        "  morphometry_id=excluded.morphometry_id,"
        "  version_int=excluded.version_int, provider=excluded.provider,"
        "  model=excluded.model, base_url=excluded.base_url,"
        "  prompt_hash=excluded.prompt_hash, input_json=excluded.input_json,"
        "  output_json=excluded.output_json, findings=excluded.findings,"
        "  quant=excluded.quant, impression=excluded.impression,"
        "  advice=excluded.advice, status=excluded.status, error=excluded.error,"
        "  guardrail_json=excluded.guardrail_json, latency_ms=excluded.latency_ms,"
        "  prompt_tokens=excluded.prompt_tokens,"
        "  completion_tokens=excluded.completion_tokens,"
        "  created_at=excluded.created_at,"
        # A regenerated report has not been reviewed. Carrying the old sign-off
        # forward would attach a clinician's name to text they never read.
        "  review_state='unreviewed', reviewed_by=NULL, reviewed_at=NULL,"
        "  reviewed_text=NULL",
        (seg_id, morph["id"], generation, "zh",
         "openai-compatible" if llm else "builtin",
         llm.model if llm else None,
         ai.base_url if llm else None,
         _prompt_hash(cfg),
         json.dumps(payload, ensure_ascii=False),
         json.dumps(report, ensure_ascii=False),
         report.get("findings"), report.get("quant"),
         report.get("impression"), report.get("advice"),
         status, error,
         json.dumps(guard.to_json(), ensure_ascii=False) if guard else None,
         llm.latency_ms if llm else None,
         llm.prompt_tokens if llm else None,
         llm.completion_tokens if llm else None))
    conn.commit()

    out = get_report(conn, seg_id)
    assert out is not None
    return out


def _produce(cfg: Any, ai: Any, payload: dict[str, Any],
             banned_terms: list[str] | None = None):
    """Try the model; fall back deterministically on any problem.

    Returns (report, status, error, guardrail_result, llm_response).
    """
    if not ai.enabled:
        return render_template_report(payload), "fallback", "ai.enabled is false", None, None

    prompt = _render_prompt(cfg, payload)
    messages = [
        {"role": "system",
         "content": "你是一位严谨的中文骨关节影像科医师，只根据给定的 json 数据撰写报告。"},
        {"role": "user", "content": prompt},
    ]

    last_error = ""
    last_guard: GuardrailResult | None = None
    for attempt in (1, 2):
        try:
            resp = chat(ai.base_url, ai.model, ai.read_api_key(),
                        messages, timeout_s=ai.timeout_s,
                        allow_egress=ai.allow_egress)
        except (EgressBlocked, LlmError, DeidentificationError) as exc:
            return (render_template_report(payload), "fallback",
                    "%s: %s" % (type(exc).__name__, exc), None, None)

        try:
            parsed = json.loads(_strip_fence(resp.content))
        except json.JSONDecodeError as exc:
            last_error = "model did not return JSON: %s" % exc
            messages.append({"role": "assistant", "content": resp.content[:2000]})
            messages.append({"role": "user", "content": "上一次回复不是合法 json，请只输出 json 对象。"})
            continue

        guard = check(parsed, payload, banned_terms=banned_terms)
        last_guard = guard
        if guard.ok:
            return parsed, "ok", None, guard, resp

        last_error = "; ".join(guard.violations)
        if attempt == 1:
            # One retry, told exactly what was wrong. A second failure means the
            # model cannot stay inside the data, and we stop paying for tries.
            messages.append({"role": "assistant", "content": resp.content[:2000]})
            messages.append({
                "role": "user",
                "content": "上一次回复被校验拒绝，原因：" + last_error
                + "。请严格只使用数据中出现过的数字与亚区代码重写，只输出 json 对象。"})

    return (render_template_report(payload), "rejected_by_guardrail", last_error,
            last_guard, None)


def _strip_fence(text: str) -> str:
    """Models wrap JSON in ```json fences even when told not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _prompt_path(cfg: Any):
    from pathlib import Path
    return Path(cfg.ai.prompts_dir) / PROMPT_FILE


def _render_prompt(cfg: Any, payload: dict[str, Any]) -> str:
    template = _prompt_path(cfg).read_text(encoding="utf-8")
    return template.replace("{PAYLOAD}", json.dumps(payload, ensure_ascii=False, indent=1))


def _prompt_hash(cfg: Any) -> str:
    try:
        raw = _prompt_path(cfg).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_refs(cfg: Any) -> dict[str, Any]:
    import yaml
    path = cfg.refs_dir / "knee_cartilage_reference_v1.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
