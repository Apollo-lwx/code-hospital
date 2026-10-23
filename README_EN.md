# Code Hospital

[中文](README.md) | English

**Code Hospital is an open-source, local-first MCP (Model Context Protocol) server for AI code review and verified auto-fixing.** It orchestrates deterministic static-analysis tools — Ruff, Semgrep, Gitleaks — inside a stage-gated "hospital" workflow (registration → lab tests → diagnosis → prescription → treatment → discharge), so every AI-driven change gets an auditable record, a human approval gate, and a verifiable diff. Zero API keys required; works with Cursor, Qoder, Codex, or any MCP-compatible host.

Take your code to the hospital instead of letting AI practice "barefoot-doctor medicine" — a quick glance, immediate edits, no verification, drifting further off course. Code Hospital practices like a proper hospital built on **medical records, prescriptions, and nursing logs**: **every fix has a traceable prescription, controllable boundaries, and verifiable diffs** — local-first, with all data landing in the current repo's `.hospital/`.

> Current version v0.1: registration / lab tests / diagnosis / prescribing / patent-medicine & decoction patch submission / discharge gate / case lookup are usable; re-verification and conflict resumption remain open per the [roadmap](#roadmap).
> User-facing glossary: [`docs/code-hospital-glossary.md`](docs/code-hospital-glossary.md); design contract: [`docs/code-hospital-design.md`](docs/code-hospital-design.md); IO contracts: [`docs/code-hospital-io-contracts.md`](docs/code-hospital-io-contracts.md); algorithm & modeling design: [`docs/code-hospital-algorithms.md`](docs/code-hospital-algorithms.md). The runtime implementation lives in `code_hospital.core.algorithms` (`docs/algorithms_reference.py` is only a self-check entry point).

## Table of contents

- [Key features](#key-features)
- [Installation](#installation)
- [Connecting to Cursor](#connecting-to-cursor)
- [Connecting to other hosts](#connecting-to-other-hosts)
- [Visiting the doctor in chat](#visiting-the-doctor-in-chat)
- [Terminal smoke test](#terminal-smoke-test)
- [MCP tools](#mcp-tools)
- [Roadmap](#roadmap)
- [Configuration](#configuration)
- [Development](#development)
- [FAQ](#faq)
- [Related projects](#related-projects)

## Key features

- **Stage-gated AI workflow (human-in-the-loop)** — every stage suspends for your confirmation after producing its artifact; high-risk prescription items require explicit human approval; nothing is applied silently.
- **Deterministic instruments, not vibes** — lab tests run real static analysis (ruff for Python, plus semgrep and gitleaks); missing tools degrade gracefully into the report's "Coverage & Limitations" instead of blocking the flow.
- **Auditable prescriptions** — treatment happens dose by dose from an approved prescription; host-model patches pass four validations before landing; every change is reverse-appliable.
- **PR-ready reports** — Markdown lab reports with severity tiers and coverage notes, ready to paste straight into pull request comments.
- **Recurrence tracking** — fingerprinting links findings to historical cases: each finding is labeled "new" or "recurrence ×N", with its first-seen case and outcome.
- **Local-first & private** — no API keys, no cloud calls on the main path; all records stay in the repo's own `.hospital/` (auto git-excluded).
- **Repo-owned policy** — `code-hospital.yaml` in the treated repo decides what counts as a disease: structure thresholds, ignored rules/prefixes, severity floors.

## Installation

Requires Python 3.10+. From this repo's root:

```bash
python -m venv .venv
.venv\Scripts\pip install -e .     # Windows
# .venv/bin/pip install -e .       # macOS / Linux
```

Install scanning instruments as needed (missing instruments degrade automatically and are noted in the report's "Coverage & Limitations" section without blocking the flow):

```bash
.venv\Scripts\pip install ruff     # Python lab tests (recommended, the P0 workhorse)
# semgrep / gitleaks: see their official installation docs
```

## Connecting to Cursor

In the `.cursor/mcp.json` of **the project you want to treat**, add (pointing to `hosp` in this repo's venv):

```json
{
  "mcpServers": {
    "code-hospital": {
      "command": "D:/project/study/code-hospital/.venv/Scripts/hosp.exe",
      "args": ["serve"],
      "env": {
        "HOSP_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

Then go to Settings → MCP and confirm `code-hospital` is enabled. Reload the MCP after changing this repo's code.

Workspace binding order: `HOSP_PROJECT_ROOT` (project-level mcp.json, recommended, explicit contract) → process cwd. Do **not** configure a user-level `mcp.json`: `${workspaceFolder}` often fails to expand there, which leaks medical records into other repos.

## Connecting to other hosts

For manual integration (Qoder / Codex, etc.) use the same config as the root [`mcp.json`](mcp.json):

```bash
# Qoder
qoder mcp add code-hospital -- hosp serve

# Codex (~/.codex/config.toml or project-level .codex/config.toml)
# [mcp_servers.code-hospital]
# command = "hosp"
# args = ["serve"]
```

Just launch from the project root — the MCP subprocess inherits cwd, and Code Hospital claims the workspace by cwd. To pin it explicitly, set the environment variable `HOSP_PROJECT_ROOT=/absolute/path`.

## Visiting the doctor in chat

With the MCP connected, just talk to the agent in the current project window:

| You say | What the agent does |
|---------|---------------------|
| "Check whether this change has any problems" | `hosp_register` (scope=staged) → **shows you the registration slip for confirmation** → after you confirm, `hosp_scan` → shows you the lab report |
| "Give this repo a check-up" | `hosp_register` (scope=repo) → **confirm the registration slip** → `hosp_scan` |
| "Can this PR be merged?" | scope=range (e.g. `main..HEAD`) → step-by-step confirmation → the lab report is ready to paste into PR comments |

**Call-number confirmation (stage gates)**: like a real hospital, the flow **suspends for your confirmation** after each stage produces its artifact — when the registration slip comes out it waits for you before entering the consult room; when the lab report comes out it waits before diagnosing; after each dose is administered it waits before the next one. Without your confirmation, the hospital never rushes everything through in one go. To skip the call numbers and run end-to-end (CI smoke tests, one-click check-ups), declare full delegation at registration (`auto_advance=true`).

The hospital also **stops to ask you** when uncertain (ten checkpoints such as unclear scope, conflicting cases, high-risk items, course-of-treatment limits) instead of plowing ahead; when you don't answer, everything falls back to the safe side (deny / hold / escalate to a human).

## Terminal smoke test

For CLI-only usage without chat:

```bash
hosp check                          # register + lab tests on the staged area
hosp check --scope-type repo        # whole-repo check-up
hosp check --scope-type range --range main..HEAD
hosp check --force                  # refer out an existing active case before registering
hosp check --json                   # full JSON output
hosp serve                          # normally launched by the host; no need to run by hand
```

Report artifacts land in `.hospital/reports/<case_id>/` (the lab report can be pasted straight into PR comments); patch archives land in `.hospital/patches/`; `.hospital/` is automatically added to `.git/info/exclude` so it never pollutes git status. **Readmissions don't start from scratch**: scanning auto-links findings by fingerprint to this repo's historical cases, and the "History" column of the report's findings detail marks each finding as "new" or "recurrence ×N" (including the first case and its outcome); `.rsi` project memory is annotated on the same page (unverified tier).

## MCP tools

| Tool | What it does | Status |
|------|--------------|--------|
| `hosp_register` | Registration & case creation: pins the scope (repo/paths/staged/commit/range), lightweight pre-check triage, case dedup; one active case per repo | ✅ P0 |
| `hosp_scan` | Lab tests: orchestrates deterministic instruments (ruff/semgrep/gitleaks), normalizes fingerprinted findings, produces a PR-ready report | ✅ P0 |
| `hosp_case` | Case lookup: list / detail / findings / raw reports | ✅ P0 |
| `hosp_diagnose` | Consultation: fingerprint clustering, template-based attribution, history retrieval; runs only after the lab report is confirmed | ✅ template attribution |
| `hosp_prescribe` | Prescribing: ordered draft + approval gate (high-risk items require your own confirmation). Does not touch code | ✅ draft & approval |
| `hosp_dispense` | Treatment: patent medicine (`ruff --fix`) edits code dose by dose; decoction takes a token, then `submit_patch` passes four validations before landing; over-the-counter items are skipped | ✅ patent medicine & decoction submission |
| `hosp_discharge` | Follow-up diff + discharge gate co-signing + follow-up plan. Readmits if iatrogenic new symptoms appear and the course isn't finished | ✅ follow-up & gate |
| `hosp_treat` | One-click full course (auto mode): low-risk self-approves, high-risk downgrades to over-the-counter; decoction stops at `need_patch` | ✅ orchestration |

## Roadmap

- **P0 PR outpatient clinic (current)**: read-only pipeline, lab report pasted into PRs.
- **P1 outpatient closed loop**: diagnosis + prescription approval + patent-medicine treatment (autofix) + discharge gate + reverse-apply rollback.
- **P2 inpatient ward**: decoction protocol (host-model patches, four validations), drift control, multi-round courses, `hosp_treat`, consult referral slips, follow-up plans.
- **P3 integrated care**: rsi-boot integration (medication-contraindication recall, treatment-outcome experience distillation), symbol-level scope, worktree high-risk isolation.

## Configuration

Works zero-config on the main path. Optional environment variables:

| Variable | Purpose |
|----------|---------|
| `HOSP_PROJECT_ROOT` | Pin the workspace explicitly (recommended in project-level mcp.json) |

**Scan ignores (three layers)**: ① repo scope enumerates via `git ls-files`, natively respecting `.gitignore`; ② built-in excluded directories (`node_modules`/`.venv`/`__pycache__`/`dist`, etc., effective in all scopes); ③ project-level customization — create `code-hospital.yaml` at the **treated repo's** root:

```yaml
# These paths are never scanned in any scope (fnmatch, repo-relative paths)
scan_ignore:
  - "gen/**"           # generated code
  - "**/*_pb2.py"      # protobuf artifacts
  - "migrations/**"
```

The number of ignored files is written into the lab report's "Coverage & Limitations" — skipped doesn't mean invisible.

**What counts as a disease** lives in the same `code-hospital.yaml`. The structure ruler defaults to 80 lines per function, 500 lines per file, cyclomatic complexity 15; write `0` to turn a ruler off. Rule IDs or prefixes listed in `catalog.rules.ignore` are dropped at the initial scan. See the full example in `code-hospital.yaml.example`. The effective criteria are written into the lab report's "Coverage & Limitations".

Records, reports, and patches all live in each repo's own `.hospital/`; open another repo and you get another set — no cross-contamination.

## Development

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Engineering conventions: adapters only produce raw findings; normalization (fingerprinting / sanitization / anchoring) is centralized in the lab department; instrument failures go into "Coverage & Limitations" without interrupting other instruments; tests inject doubles via `adapters.register()`.

## FAQ

**What is Code Hospital?**
An open-source, local-first MCP server that turns AI code review and auto-fixing into a stage-gated hospital workflow — registration, lab tests, diagnosis, prescription, treatment, discharge — with auditable records and human confirmation at every gate.

**How is Code Hospital different from running ruff or semgrep directly?**
Ruff and semgrep are the instruments; Code Hospital is the hospital. It orchestrates the instruments, normalizes findings into fingerprinted records, tracks recurrences across scans, and produces PR-ready reports. More importantly, every code change requires an approved prescription and is verified by a follow-up diff afterwards — raw linters do none of that.

**Does Code Hospital require an API key or send code to the cloud?**
No, and no. The main path is fully local with zero API keys. All data is written to the treated repo's `.hospital/` directory, which is automatically excluded from git.

**Which languages and tools does Code Hospital support?**
Python via ruff is the P0 workhorse; semgrep and gitleaks plug in as optional instruments. Any missing instrument degrades gracefully and is disclosed in the report's "Coverage & Limitations" — the flow never blocks on a missing tool.

**Will Code Hospital modify my code without asking?**
No. By default the flow suspends at every stage gate for your confirmation, and high-risk prescription items always require explicit human approval. For CI or one-click check-ups you can declare `auto_advance=true`: then only low-risk items self-approve, and high-risk ones are downgraded to "over-the-counter" (skipped).

**How do I use Code Hospital with Cursor or other MCP hosts?**
Add one block to the treated project's `.cursor/mcp.json` pointing at `hosp serve` (see [Connecting to Cursor](#connecting-to-cursor)); for Qoder, Codex, or any MCP-compatible host, register the same `hosp serve` command — that's all.

**Where are reports, patches, and medical records stored?**
In each repo's own `.hospital/`: reports under `.hospital/reports/<case_id>/`, patch archives under `.hospital/patches/`. Opening another repo gives you a completely separate case book.

## Related projects

[rsi-boot](../rsi-boot): a local memory-layer MCP by the same author. Project memory in `.rsi/memory/` (conventions / prohibitions) appears as **medical history** in lab reports and in the `memory_context` field of `hosp_scan` output — two trust tiers, official (approved) / pending (unapproved), uniformly labeled "may be outdated or wrong; for consultation reference only, not fact". From P3 there is also MCP-level integration (recalling medication contraindications before prescribing, distilling treatment-outcome experience after discharge); works standalone without rsi-boot.
