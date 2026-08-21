---
name: art-vision
description: >-
  Dispatch art-vision agent for Sojourn/Circletown plate QA — character consistency,
  style drift, anachronisms, composition. Prep tiles + fail-soft metrics + STYLE/canon
  checklist. Not UI verify (ios-oracle) or art generation.
---

# Art Vision

Orchestrator-owned **dispatch** for painterly plate vision QA. The agent runs prep, local metrics, reads STYLE/canon, and returns a Flavor-OFF PASS/FAIL report. Do not add a decision-table row — route via this skill + agent `description`.

## When to dispatch

| Matt / context | Action |
|----------------|--------|
| Review story plate, character drift, style lock | `Task(art-vision, readonly: true)` |
| Sojourn station / cast plate QA | same + project `sojourn` |
| Circletown hub/spoke plate QA | same + project `circletown` |
| iOS build / sim screenshot | **ios-oracle** — not art-vision |
| Generate or inpaint art | orchestrator / repo art skill — not art-vision |

## Canon paths (verified)

| Asset | Path |
|-------|------|
| Circletown characters | `/Volumes/Cloud Storage/Code/circletown_art/canonical/characters/` |
| Sojourn characters | `/Volumes/Cloud Storage/Code/sojourn_art/sojourn/canonical/characters/` |
| Sojourn legacy masters | `/Volumes/Cloud Storage/Code/sojourn-art-pipeline/masters/` |
| Circletown STYLE | `/Volumes/Cloud Storage/Code/circletown/STYLE.md` |
| Sojourn STYLE | `/Volumes/Cloud Storage/Code/sojourn/STYLE.md` |

## Prep contract

```bash
cd "/Volumes/Cloud Storage/Claude"
bash .cursor/scripts/art_vision_prep.sh /absolute/path/to/plate.png \
  [--refs /path/canon.png ...] [--out /tmp/art-vision/<id>]
```

- Long edge ~1280 JPEG full frame + 5 crops (center + quadrants).
- Optional `--refs` shrunk to 640 long edge under `<out>/refs/`.
- Prints absolute paths (one per line). Prefers ImageMagick `magick`; falls back to `sips` with output-dim asserts — no network.
- One plate per invocation; orchestrator/agent loops for multi-plate QA.

## Metrics contract (fail-soft)

```bash
PYTHONPATH=".python_libs" python3 .cursor/scripts/art_vision_metrics.py \
  --plate /absolute/path/to/plate.png \
  [--ref /path/canon.png ...] [--human-face] [--caption "..."]
```

| Scorer | When | Notes |
|--------|------|-------|
| Aesthetic V2.5 | Always attempt | 1–10; skip if import/weights fail |
| ArcFace | `--human-face` + `--ref` | Human↔human only; **never** Heartwood wood faces |
| CLIP-I | `--ref` | Plate↔canon cosine |
| CLIP-T | `--caption` | Plate↔caption cosine |

Exit **0** always for v1; stdout is JSON with `metrics` and/or `skipped`. Prefer MPS when `torch.backends.mps.is_available()`, else CPU.

**Deps (optional, Matt install):** `torch`, `Pillow` (cp311 wheel — not broken 3.14 stubs), `aesthetic-predictor-v2-5`, `insightface`, `open_clip_torch`, `opencv-python-headless` into `.python_libs` via Python 3.11 pin. Until installed, metrics skip — Gemini + crops still ship.

## Dispatch

```
Task(subagent_type: "art-vision", readonly: true)
```

Prompt must include: absolute **plate path(s)** (agent loops prep/metrics once per plate), **project** (`sojourn` | `circletown`), optional **canon ref paths**, `--human-face` when comparing human cast (not wood faces).

Omit Task `model` — agent frontmatter owns `gemini-3-1-pro`.

## Orchestrator synthesis

Lane A: relay agent report. Metrics inform findings; agent owns PASS/FAIL. If agent reports `BLOCKED: not running on Gemini`, surface to Matt — do not silently fall back to Composer.

## Smoke

```bash
bash .cursor/scripts/smoke_harness_agents.sh
bash .cursor/scripts/smoke_harness_skills.sh
```
