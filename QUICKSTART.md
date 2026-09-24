# Quick Start

Locally-built snaps for both components — not yet published to the Snap
Store. Full details in [`GETTING_STARTED.md`](GETTING_STARTED.md); this is
the condensed "just get it running" version.

Artifacts built this session:
- `snap-dashboard/automated-ken_0.1.0_amd64.snap` (strict confinement)
- `automated-ken-runner/automated-ken-runner_0.1.0_amd64.snap` (classic confinement)

## 1. Install the dashboard

```bash
cd ~/src/github/kenvandine/automated-ken/snap-dashboard
sudo snap install ./automated-ken_0.1.0_amd64.snap --dangerous
```

## 2. GitHub OAuth App + PAT

- Create an OAuth App: **GitHub → Settings → Developer settings → OAuth Apps**
  - Homepage: `http://127.0.0.1:9080`
  - Callback: `http://127.0.0.1:9080/auth/callback`
- Create a PAT with `repo` + `actions` scopes.

```bash
sudo snap set automated-ken github-client-id=<id> github-client-secret=<secret>
sudo snap start automated-ken
```

Open **http://127.0.0.1:9080**, sign in with GitHub, and complete onboarding
(publisher name, PAT, testing repo — see `GETTING_STARTED.md` steps 5–7).

## 3. Install a runner (optional, for real-desktop YARF testing)

On a **dedicated** test machine (this disables screen-lock/suspend):

```bash
sudo snap install ./automated-ken-runner_0.1.0_amd64.snap --classic --dangerous
```

On the dashboard, **Runners → Add runner** for a one-time token, then:

```bash
automated-ken-runner enroll --server http://<dashboard-host>:9080 --token <token>
systemctl --user enable --now automated-ken-runner
```

Verify it shows `idle` on the **Runners** page.

## 4. Try it out

1. **Testing** page → trigger a YARF test on a snap with a version bump pending.
2. **Version bumps** page → review/merge agent-opened PRs.
3. **Settings → Agents & AI** → enable auto-test, auto-merge, delegated coding
   tasks as desired.

## Rebuilding the snaps

```bash
cd snap-dashboard && snapcraft pack
cd ../automated-ken-runner && snapcraft pack
```

Note: `automated-ken-runner` is classic-confinement, so its `snapcraft.yaml`
explicitly stages a Python interpreter (`stage-packages:
python3.12-minimal`, etc.) — classic snaps don't get the base snap's
runtime mounted, so the strict-confinement fallback to `/usr/bin/python3.12`
doesn't apply there.

| Page | URL |
|---|---|
| Dashboard | http://127.0.0.1:9080 |
| Testing | http://127.0.0.1:9080/testing |
| Agent activity | http://127.0.0.1:9080/agents |
| Version bumps | http://127.0.0.1:9080/version-bumps |
| Runners | http://127.0.0.1:9080/runners |
| Settings | http://127.0.0.1:9080/settings |
