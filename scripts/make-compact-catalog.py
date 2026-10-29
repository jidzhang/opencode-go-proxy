"""Build the compact model catalog from the full-shape Codex catalog.

The full catalog duplicates per-model instruction text; the compact form
keeps it once in "shared_instructions" so catalog updates stay small. Read
contrib/opencode-go-catalog.json, drop the per-model instruction fields, and
write contrib/opencode-go-models.json.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FULL_PATH = REPO_ROOT / "contrib" / "opencode-go-catalog.json"
COMPACT_PATH = REPO_ROOT / "contrib" / "opencode-go-models.json"
CLIENT_VERSION = "0.147.0"

# auto_compact_token_limit is derived from context_window at render time, so
# the compact form never stores it (the same rule as _default_model_record).
DROPPED_KEYS = frozenset({"base_instructions", "model_messages", "auto_compact_token_limit"})

# Client-contract values the Codex catalog shape needs but the upstream does not
# publish. They are only supplied when the source record omits the key: an
# explicit value in the full catalog always wins, so regenerating never flips a
# deliberate "hide" back to "list" or clears a real capability.
FILL_IF_ABSENT = {
    "effective_context_window_percent": 95,
    "apply_patch_tool_type": "freeform",
    "shell_type": "shell_command",
    "visibility": "list",
    "supported_in_api": True,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "availability_nux": None,
    "upgrade": None,
    "support_verbosity": False,
    "default_verbosity": "low",
    "experimental_supported_tools": [],
    "use_responses_lite": False,
}


def compact_model(full_model: dict) -> dict:
    """Project one full-shape model to the compact form.

    Only two things happen: per-model instruction fields are dropped (they live
    once in ``shared_instructions``), and client-contract keys the record does
    not carry get their documented default. Nothing already present is
    overwritten.
    """
    record = {key: value for key, value in full_model.items() if key not in DROPPED_KEYS}
    for key, default in FILL_IF_ABSENT.items():
        record.setdefault(key, default)
    return record


def main() -> None:
    with open(FULL_PATH, encoding="utf-8") as f:
        full = json.load(f)
    models = full.get("models")
    if not models:
        raise SystemExit(f"{FULL_PATH} contains no models")

    # One shared instruction blob for every model is only correct when the
    # source actually shares it; assert that instead of silently handing model
    # A's prompt to model B.
    shared = models[0].get("base_instructions") or ""
    divergent = [
        m.get("slug") for m in models[1:] if (m.get("base_instructions") or "") != shared
    ]
    if divergent:
        raise SystemExit(
            "models carry different base_instructions; the compact form keeps one "
            f"shared blob, so this catalog cannot be compacted safely: {divergent[:5]}"
        )

    compact = {
        "fetched_at": full["fetched_at"],
        "etag": full["etag"],
        "shared_instructions": shared,
        "client_version": CLIENT_VERSION,
        "models": [compact_model(model) for model in models],
    }
    with open(COMPACT_PATH, "w", encoding="utf-8") as f:
        json.dump(compact, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {COMPACT_PATH} ({len(compact['models'])} models, {COMPACT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
