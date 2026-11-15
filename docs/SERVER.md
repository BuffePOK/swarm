# roj on the VPS (rootless Podman, image from GitHub Packages, auto-update)

> The complete step-by-step guide with a check after every step is [TUTORIAL.pl.md](TUTORIAL.pl.md) (Polish). This page is the short English reference.

Same pattern as apigate: GitHub Actions tests, then builds the image and pushes it to
`ghcr.io`. The server checks the registry every 2 minutes (`podman auto-update`) and replaces
the container if the image changed. No SSH keys and no deploy secrets in GitHub; nothing
connects to the server from outside.

```
push to main -> CI: tests on 3 OS x 3 Pythons -> image -> smoke test -> ghcr.io/<you>/roj:latest
                                                                             ^ pulled every 2 min
/home/roj/data      ROJ_HOME: roj.toml, projects, databases, logs, repo clone for selfdev
/home/roj/roj.env   secrets (mode 600)
container "roj"     `roj serve`: scheduler + Telegram + panel on 127.0.0.1:18732 + daily backup
```

> **Not verified by me:** I could not run Podman or build the image in my environment (the
> build stage and the container start command were simulated without a container). On the
> first deploy paste the output of the commands marked "-> paste" and we fix differences.

## 1. GitHub (once)

1. Create the repository `roj`, push the code. Actions -> `ci` must be green; the job `image`
   pushes `ghcr.io/<you>/roj:latest`.
2. Profile -> Packages -> `roj` -> Package settings -> Change visibility -> **Public**
   (the server pulls without credentials, as with apigate).
3. Recommended: protect `main` (changes only through pull requests, CI required).

## 2. [ADMIN] Account and limits (once)

```bash
sudo adduser --disabled-password --gecos "" roj
sudo loginctl enable-linger roj
grep ^roj: /etc/subuid /etc/subgid                                   # -> paste (entries must exist)
sudo systemctl set-property user-$(id -u roj).slice CPUQuota=100% MemoryMax=2G MemorySwapMax=0 TasksMax=1024
sudo machinectl shell roj@
```

Everything below runs as `roj`.

## 3. [roj] Files

```bash
mkdir -p ~/data ~/.config/containers/systemd
curl -fsSL https://raw.githubusercontent.com/<you>/roj/main/deploy/podman/roj.container -o ~/.config/containers/systemd/roj.container
sed -i 's#OWNER#<your github login in lowercase>#' ~/.config/containers/systemd/roj.container
curl -fsSL https://raw.githubusercontent.com/<you>/roj/main/deploy/podman/roj.env.example -o ~/roj.env && chmod 600 ~/roj.env
nano ~/roj.env
```

**Secrets (`~/roj.env`), all of them:**

| variable | required | what it is |
|---|---|---|
| `ROJ_API_KEY` | yes | a key of your gateway (apigate: `sk-gate-...`, create one for roj in the panel) |
| `ROJ_TELEGRAM_TOKEN` | no | bot token from @BotFather: approvals with buttons and reports in Telegram |
| `ROJ_GITHUB_TOKEN` | no | lets selfdev push a branch and open a pull request: fine-grained token, only the `roj` repository, *Contents: read and write*, *Pull requests: read and write* |

Nothing secret goes into `roj.toml`. Without Telegram you approve in the panel or CLI; without
the GitHub token selfdev leaves a branch and a patch file in the reports.

```bash
podman pull ghcr.io/<you>/roj:latest
systemctl --user daemon-reload && systemctl --user start roj.service
systemctl --user status roj.service | head -5                        # -> paste
ls -la ~/data                                                        # roj.toml must be there and owned by roj -> paste
```

The first start creates `~/data/roj.toml`. Edit it on the host (`nano ~/data/roj.toml`), using
`examples/roj.server.toml.example` from the repository: `[gateway] base_url =
"http://127.0.0.1:18731/v1"`, `[models.*]`, `[selfdev]`, `[notify] telegram_chat_id`. Then:

```bash
systemctl --user restart roj.service
alias roj='podman exec -it roj roj'           # put it into ~/.bashrc
roj models --refresh                          # exact model ids for [models.*]
roj doctor --live                             # -> paste
```

## 4. [roj] Auto-update every 2 minutes

```bash
mkdir -p ~/.config/systemd/user/podman-auto-update.timer.d
cat > ~/.config/systemd/user/podman-auto-update.timer.d/override.conf <<'EOT'
[Timer]
OnCalendar=
OnCalendar=*:0/2
RandomizedDelaySec=30
Persistent=true
EOT
systemctl --user daemon-reload && systemctl --user enable --now podman-auto-update.timer
podman auto-update --dry-run                  # -> paste
```

The container counts as started only when `roj healthcheck` passes (`Notify=healthy`); if a
new image does not come up, `podman auto-update` returns to the previous one. A restart
interrupts running tasks; the kernel resumes them from the last finished step.

Pin a version / manual rollback: in `roj.container` replace `:latest` with `:<commit sha>`,
remove `AutoUpdate=registry`, `systemctl --user daemon-reload && systemctl --user restart roj.service`.

## 5. Owner's channels

- **Telegram**: approvals with buttons; the service continues the task within one tick (30 s).
- **Panel**: `ssh -L 18732:127.0.0.1:18732 <admin>@<host>`, then
  `http://127.0.0.1:18732/?token=<contents of /home/roj/data/panel_token>`. Not exposed through nginx on purpose.
- **CLI**: `roj ...` (the alias above). Reports are plain files in `~/data/projects/<name>/reports/`.

## 6. Storage and backups

Databases are SQLite files in `~/data` on the local disk (never on the bucket mount).
For backups to the bucket: mount it on the host, uncomment `Volume=/mnt/roj-backup:/backup` in
`roj.container`, set `[storage] backup_dir = "/backup"`. `roj serve` writes one snapshot per day and
keeps `backup_keep`; `roj backup` makes one on demand. Restore: stop the service, unpack a
snapshot over an empty `~/data` (`tar xzf ... --strip-components=1`), start. PostgreSQL: see ADR-0007.

## 7. What runs agent-written code

Only selfdev's test run: inside the container (read-only image, no capabilities, 1 CPU / 2 GB),
in a git worktree under `~/data/selfdev/`, with an environment that holds no key or token and
with a timeout. The container uses the host network (to reach the gateway on 127.0.0.1), so
those tests are not network-isolated unless the kernel allows unprivileged network
namespaces inside the container; the selfdev report says which it was.
