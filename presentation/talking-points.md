# Automated Ken — Talk Talking Points

## What Problem This Solves

- Snap publishers maintain dozens of packages across multiple channels (stable, candidate, beta, edge) — manually tracking upstream releases, bumping versions, running tests, and reviewing results is tedious and error-prone.
- This project replaces that manual cycle with a pool of autonomous background agents that handle the full workflow: detect → PR → test → review → merge.
- Local inference is what makes the review step genuinely intelligent rather than just scripted — the model looks at your app the way a human maintainer would.

---

## The Agentic Pipeline

Five specialized agents chain together into a continuous maintenance loop:

1. **Release Scanner** — monitors every snap's packaging repo, parses `snapcraft.yaml`, and detects when an upstream source (GitHub, PyPI, GitLab) has a newer version.
2. **Version Bumper** — opens a version bump PR on the packaging repo via a bot GitHub account, entirely through the GitHub Contents API. Lemonade reads the upstream release notes and **writes the commit message and PR body** — turning raw changelog text into a human-readable summary.
3. **PR Monitor** — polls open PRs and triggers YARF (snap functional tests) once build CI passes.
4. **Screenshot Reviewer** — uses Lemonade's vision model to compare before/after screenshots and deliver a structured verdict.
5. **Stale Build Scanner** — finds snaps that haven't been published in N days and dispatches a fresh build to `candidate`.

The web dashboard exposes a live agent activity feed so you can watch every decision in real time via Server-Sent Events.

---

## Local Inference with Lemonade — The Core of the Review Loop

This is where local inference does the heavy lifting:

- After YARF tests run, the **Screenshot Reviewer** fetches before/after screenshots and sends them to Lemonade using its **OpenAI-compatible API**.
- The vision model is asked to look for regressions, crash dialogs, error messages, and missing UI elements — and returns structured JSON: a decision (`approve`, `reject`, `needs_review`), a confidence score, and a reasoning paragraph that shows up in the dashboard.
- Multiple screenshots are compared per test run; results are aggregated conservatively so any detected regression blocks the merge.
- The same Lemonade endpoint also generates PR descriptions from upstream release notes — the model reads the changelog and writes copy that a human maintainer would actually want to read.

**This is running entirely on your own hardware.** No API keys, no cloud costs, no data leaving the machine.

---

## Why Local Inference Is the Right Fit Here

- **Privacy** — screenshots of running applications can contain sensitive data: user content, credentials, internal tooling. Running the vision model locally means none of that is ever transmitted to a third party.
- **Cost model** — this agent runs continuously, potentially reviewing screenshots for dozens of snaps on every release cycle. Cloud inference at that volume would be expensive. Local inference makes the economics work for a personal or small-team tool.
- **Latency and availability** — the agent doesn't depend on an external API being up, rate-limited, or policy-compliant. The model is there when you need it.
- **Model flexibility** — because the client talks to Lemonade's OpenAI-compatible endpoint, you can swap vision models (LLaVA, Qwen-VL, etc.) by changing a config value. The application code doesn't care which model is running.

---

## Human in the Loop

The agentic approach isn't about removing humans — it's about removing toil:

- The `/version-bumps` dashboard groups PRs by the model's verdict: **Approved**, **Needs Your Review**, **Rejected**.
- The model's reasoning paragraph is always shown alongside the screenshot comparison — you see *why* the model made its call before you act on it.
- Confidence thresholds gate auto-promotion: the agent won't push to stable unless the model's confidence clears the configured threshold (default 85%).
- For approved, high-confidence updates, you can enable auto-merge and let the whole pipeline run without touching it.

---

## Self-Hosted, Snap-Packaged

- The entire platform ships as a **snap** — `snap install snap-dashboard`, configure with `snap set`, and it runs as a background daemon alongside your local Lemonade server.
- All data stays local: SQLite, no third-party analytics, no external inference.

---

## One-Liner Summary

> "A self-hosted snap that maintains your other snaps — using local vision models via Lemonade to review application screenshots and approve releases, without your data ever leaving the machine."
