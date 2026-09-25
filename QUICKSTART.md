# Quick Start

The condensed "just get it running" version. Details and explanations are
in [`GETTING_STARTED.md`](GETTING_STARTED.md).

## 1. Dashboard server

Create a GitHub OAuth App (**GitHub → Settings → Developer settings → OAuth
Apps**) with callback `http://<host>:9080/auth/callback`, then:

```bash
sudo snap install automated-ken
sudo snap set automated-ken github-client-id=<id> github-client-secret=<secret> bind=0.0.0.0
```

Open **http://&lt;host&gt;:9080**, sign in with GitHub (first login = admin),
and enter your Store publisher name in onboarding. Then in **Settings**:

- **GitHub Token** — a PAT with `repo` and `workflow` scopes
- **Snapcraft Store Credential** — output of `snapcraft export-login -`
- **Agents & AI** — bot account login + token (for version-bump PRs)

## 2. Runners (one amd64, one arm64)

On each **dedicated** test machine (logged into GNOME; this enables
autologin and disables screen lock):

```bash
sudo snap install automated-ken-runner --classic
echo "$USER ALL=(root) NOPASSWD: /usr/bin/snap" | sudo tee /etc/sudoers.d/automated-ken-runner
automated-ken-runner prepare-machine
```

On the dashboard, **Runners → + Enroll a runner**, then run the command it
shows on the machine and start the service:

```bash
automated-ken-runner enroll --server http://<dashboard-host>:9080 --token <token>
systemctl --user enable --now snap.automated-ken-runner.run.service
```

It should appear on **Runners** as `idle` with its architecture.

## 3. Try it out

1. **Testing** → **Run Tests (amd64, arm64)** on a snap with a newer candidate version.
2. When both architectures pass, **Pending Promotion → 🚀 Promote set to Stable**.
3. **Version Bumps** → review agent-opened PRs; each shows every architecture's result.
4. **Settings** → turn on automatic testing, auto-merge and auto-promote once you trust it.

## Building the snaps yourself

```bash
cd snap-dashboard && snapcraft pack
cd ../automated-ken-runner && snapcraft pack
sudo snap install ./automated-ken_*.snap --dangerous
sudo snap install ./automated-ken-runner_*.snap --classic --dangerous
```

`automated-ken-runner` is classic, so its `snapcraft.yaml` stages its own
Python interpreter (classic snaps don't get the base snap's runtime).

| Page | Path |
|---|---|
| Dashboard | `/` |
| Testing | `/testing` |
| Runners | `/runners` |
| Version bumps | `/version-bumps` |
| Agent activity | `/agents` |
| Settings | `/settings` |
| Help | `/docs` |
