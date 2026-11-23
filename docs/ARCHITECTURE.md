# Architecture

## Overview

The workbench is a local, single-user FastAPI application. It converts uploaded CAD files into a working DXF, inspects editable text and geometry, builds a reviewable plan, executes deterministic edits, and verifies the result before delivery.

```text
Upload DWG/DXF
  -> read-only source + SHA-256
  -> DWG to DXF conversion when required
  -> drawing inspection
  -> intent parsing and plan generation
  -> human approval
  -> deterministic modification
  -> independent re-open and measurement
  -> DXF audit and side-effect checks
  -> DWG export / round-trip check
  -> previews, report, version snapshot
```

## Main Components

### `app.py`

FastAPI application and HTTP API:

- file upload and inspection
- task creation and planning
- approval and execution
- report, preview, and download delivery
- task versions and rollback
- feedback and learning candidates
- health and environment diagnostics

### `workbench/engine.py`

Execution orchestration. It routes a task to one of four chains:

- `geometry`: profile-driven movement and stretching of known CAD entities
- `annotation`: label-to-measurement conversion and layout repair
- `real_mm`: real-millimeter calibration and cross-view consistency
- `text_edit`: exact text and dimension-label edits guarded by CAD semantics

### `workbench/store.py`

File-based storage:

- `data/uploads/`: read-only source files and sidecar metadata
- `data/tasks/<task_id>/`: manifest, logs, reports, versions, and outputs

This storage is intentionally simple. It is not a multi-user database.

### `src/`

Core libraries:

- `intent_parser.py`, `edit_intent.py`, `planner.py`: natural-language intent and plan construction
- `dwg_bridge.py`, `dwg_export.py`: DWG conversion and export
- `geometry_modifier.py`: controlled geometric deformation
- `real_mm_engine.py`: real-millimeter calibration chain
- `annotation_engine.py`: annotation conversion and placement repair
- `text_edit_engine.py`, `text_locate.py`: exact text edit and location preview
- `drawing_profile.py`, `calibration.py`, `drawing_measure.py`: drawing identity, calibration, and verification
- `learning_loop.py`, `learning_ledger.py`: feedback and regression-candidate workflow
- `runtime.py`: dependency and environment bootstrap

### `web/`

A self-contained HTML/CSS/JavaScript workbench. It has no frontend build step.

### `knowledge/` and `spec/`

Generic CAD knowledge, domain vocabulary, change cards, invariants, and the JSON schema for modification plans.

## Modification Safety

1. **No silent label edits.** A dimension number cannot be changed unless the instruction explicitly permits label-only editing.
2. **No silent shared-block edits.** Shared block definitions require explicit permission to modify all instances.
3. **No uncalibrated geometry edits.** Unknown drawings are rejected before geometric modification.
4. **No visual estimation from screenshots.** Target values come from explicit instructions and source CAD measurements.
5. **No trust in the working file.** Verification reopens the delivered file and measures it independently.
6. **No unexpected entity loss.** Entity count, audit, geometry signature, and out-of-scope handles are checked.
7. **No annotated delivery files.** Colored comparison marks stay in preview copies, not in the final DWG.

## Data Policy

Runtime data is intentionally excluded from version control:

- customer drawings
- uploaded files
- task manifests and logs
- generated DXF/DWG files
- preview PNG/SVG files
- local calibrated profiles

The repository includes two DWG examples approved for public release, TD50-40G and 25QYL-2SL, and the matching TD50 calibration profile.

## Deployment Boundary

The application listens on `127.0.0.1` by default and has no authentication layer. It is designed for a trusted local machine. Add authentication, authorization, storage isolation, rate limits, and audit controls before exposing it to a network.

