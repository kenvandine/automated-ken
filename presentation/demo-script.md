# Automated Ken — Demo Script

## Before You Start

- Lemonade server running locally with a vision-capable model loaded
- `snap-dashboard` running at `http://127.0.0.1:8080`
- At least one version bump PR in the system with YARF screenshots attached
- Browser open, dashboard visible on screen

---

## 1. Open the Summary Dashboard (`/`)

**Say:** "This is the main dashboard. Every snap I publish is listed here with its channel map — stable, candidate, beta, edge — side by side. The amber cards at the top are snaps that need attention: a newer revision is waiting in candidate but hasn't made it to stable yet."

**Point out:**
- The "Attention Needed" section at the top
- Channel badges colour-coded green/amber/red
- "Last updated" timestamp and the Refresh button

---

## 2. Show the Agent Feed (`/agents`)

**Say:** "Behind the scenes, a pool of agents is running continuously. This feed shows every action they've taken — which snap they scanned, what they found, when they fired. These aren't cron jobs that run a script; each agent has its own state machine and logs decisions back to the database in real time."

**Point out:**
- Agent types listed: Release Scanner, Version Bumper, PR Monitor, Screenshot Reviewer, Stale Build Scanner
- Timestamps and status badges (running / done / error)
- The live SSE feed updating without a page refresh

---

## 3. Walk Through a Version Bump PR (`/version-bumps`)

**Say:** "When the Release Scanner finds a new upstream version, it hands off to the Version Bumper. The bot account opens a PR on the packaging repo — no local git clone, just the GitHub Contents API. But look at the PR description: that wasn't written by hand. Lemonade read the upstream release notes and generated it."

**Point out:**
- The PR grouped under "Agent Approved", "Needs Review", or "Rejected"
- Click into a PR to show the detail view

---

## 4. The Screenshot Review Detail (`/version-bumps/<id>`)

**Say:** "This is the heart of it. After YARF ran the snap's functional tests, the Screenshot Reviewer agent pulled the before and after screenshots and sent them to the vision model running locally in Lemonade. The model compared them, looked for regressions, and returned a structured decision."

**Point out:**
- Side-by-side before/after screenshots
- The agent's reasoning paragraph: "The application appears to launch correctly in both versions. The main window layout is consistent and no error dialogs are visible."
- The confidence score
- The decision badge: Approved / Rejected / Needs Review
- The Merge / Reject / Re-run YARF action buttons

**Say:** "That reasoning wasn't templated — the model wrote it by looking at the images. And the whole inference happened on this machine. No API call left the room."

---

## 5. Show the Lemonade Integration in Settings (`/settings`)

**Say:** "All it takes to wire this up is a URL and a model name. The client speaks the OpenAI chat completions API, so any model Lemonade can serve works here. I'm running a Qwen vision model right now, but you could swap in LLaVA or anything else without touching the application code."

**Point out:**
- The "Agents & AI" settings section
- Lemonade server URL field
- Lemonade model field
- Auto-merge and auto-promote confidence threshold sliders

---

## 6. Show a Rejection (if available)

**Say:** "When the model finds a problem, it blocks the merge. Here's a rejection — the model spotted a crash dialog in the new screenshot that wasn't in the baseline. Without this, that broken build would have sat in candidate waiting for me to notice."

**Point out:**
- Red "Agent Rejected" badge
- The reasoning explaining what the model saw
- The Re-run YARF button for triggering a retest after a fix

---

## Closing the Demo

**Say:** "The whole pipeline — release detection, PR creation, CI, testing, visual regression analysis — runs on a machine in this room. Lemonade is what makes the review step actually intelligent. It's the difference between a glorified cron job and a system that can tell you whether your app looks broken."
