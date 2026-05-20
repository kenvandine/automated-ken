# Automated Ken — Slide Bullets

---

## Slide: The Problem

- Dozens of snaps across stable / candidate / beta / edge
- Upstream releases happen constantly — detecting them is manual toil
- Version bumps, CI, functional tests, screenshot review — all done by hand
- One person can't keep up; things go stale

---

## Slide: The Solution — An Agentic Maintenance Platform

- A pool of background agents runs continuously inside a single snap
- Each agent owns one step in the pipeline and hands off to the next
- The web dashboard is a live command center, not a static report
- Local inference via Lemonade is what makes review intelligent

---

## Slide: The Agent Pipeline

| Agent | What it does |
|---|---|
| Release Scanner | Detects new upstream versions in GitHub / PyPI / GitLab |
| Version Bumper | Opens a version bump PR via bot account — Lemonade writes the description |
| PR Monitor | Watches CI; triggers YARF functional tests on pass |
| Screenshot Reviewer | Vision model compares before/after screenshots; delivers a verdict |
| Stale Build Scanner | Rebuilds snaps with no recent publication |

---

## Slide: Local Inference — The Screenshot Reviewer

- YARF runs the snap and captures screenshots
- Screenshot Reviewer sends baseline + new images to Lemonade vision model
- Model returns: `decision`, `confidence`, `reasoning` as structured JSON
- Multiple screenshots aggregated — any regression blocks the merge
- Confidence threshold gates auto-promotion to stable

---

## Slide: Lemonade — Local, OpenAI-Compatible

- Runs entirely on-device — no API keys, no cloud costs
- Exposes the OpenAI chat completions API
- Client code is model-agnostic: swap LLaVA, Qwen-VL, or any vision model via config
- Also used for PR description generation from upstream release notes

---

## Slide: Why Local Inference Matters Here

- **Privacy** — app screenshots can contain sensitive data; nothing leaves the machine
- **Cost** — continuous agents reviewing dozens of snaps; cloud API costs don't scale
- **Reliability** — no external dependency, rate limits, or policy changes
- **Flexibility** — any Lemonade-compatible model works; no code changes required

---

## Slide: Human in the Loop

- `/version-bumps` groups PRs by verdict: Approved / Needs Review / Rejected
- Model reasoning shown alongside every screenshot comparison
- Auto-merge available for high-confidence approvals (configurable threshold)
- One-click merge, reject, or re-run YARF from the dashboard

---

## Slide: Self-Hosted, Zero Telemetry

- Ships as a snap: `snap install snap-dashboard`
- Configured with `snap set` — no config files to manage
- All storage: local SQLite
- External calls: Snap Store API, GitHub API, your local Lemonade server — nothing else

---

## Slide: One-Liner

> "A self-hosted snap that maintains your other snaps — using local vision models via Lemonade to review application screenshots and approve releases, without your data ever leaving the machine."
