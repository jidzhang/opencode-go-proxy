"""Build the compact model catalog from the full-shape Codex catalog.

The full catalog duplicates per-model instruction text; the compact form
keeps it once in "shared_instructions" so catalog updates stay small. Read
contrib/opencode-go-catalog.json, drop the per-model instruction fields, and
write contrib/opencode-go-models.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FULL_PATH = REPO_ROOT / "contrib" / "opencode-go-catalog.json"
COMPACT_PATH = REPO_ROOT / "contrib" / "opencode-go-models.json"
CLIENT_VERSION = "0.147.0"

sys.path.insert(0, str(REPO_ROOT / "src"))

from opencode_go_proxy.catalog import _canonical_model_defaults  # noqa: E402

# Keys the renderer computes at render time, so the compact form never stores
# them (the same rule as catalog._default_model_record).
_COMPUTED_KEYS = {"base_instructions", "model_messages", "auto_compact_token_limit"}
DROPPED_KEYS = frozenset(_COMPUTED_KEYS)

# Client-contract values the Codex catalog shape needs but the upstream does not
# publish. Derived from the renderer's canonical defaults so the generator and
# catalog.py can never drift apart; per-model identity keys and the window pair
# are excluded (identity comes from the record, an unknown window stays null).
# Supplied only when the source record omits the key: an explicit value in the
# full catalog always wins, so regenerating never flips a deliberate "hide"
# back to "list" or clears a real capability.
_IDENTITY_KEYS = {"slug", "display_name", "description", "comp_hash",
                  "context_window", "max_context_window"}
FILL_IF_ABSENT = {
    key: value
    for key, value in _canonical_model_defaults().items()
    if key not in _COMPUTED_KEYS and key not in _IDENTITY_KEYS
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
