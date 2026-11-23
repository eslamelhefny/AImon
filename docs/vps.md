# AI Meter on the Hostinger VPS

- VPS: Hostinger KVM 1, Ubuntu 26.04 with Docker.
- Docker Manager project name: **`aimon`** (kept from before the rename, so data paths did not move).
- Host data folder: `/opt/aimon` (mounted at `/data`; holds `config.yaml` and the SQLite database).
- Hermes Agent's data volume `hermes-workspace_hermes-agent-data` is mounted read-only at `/hermes` for token usage.
- Dashboard: `127.0.0.1:8787` on the VPS only. Reach it with `ssh -N -L 8787:127.0.0.1:8787 you@your-server`.
- Dashboard token: `AIMON_DASHBOARD_TOKEN` in the project's environment in Docker Manager (never commit it).

## How the container gets its code

The compose file does not build an image. On every start it:
1. downloads `https://raw.githubusercontent.com/eslamelhefny/aimon/main/aimon-repo-files_1.zip`
   (the browser renamed the upload to `_1`; the older `aimon-repo-files.zip` in the repo is stale),
2. copies `aimon-repo/` into `/app`,
3. applies a one-line patch to `/app/aimeter.py` for the Claude sign-in values (harmless once the zip already contains them),
4. installs requirements and runs `entrypoint.sh`.

## Shipping an update

1. `scripts/build_server_zip.sh` builds `dist/aimon-repo-files.zip`.
2. Upload it to the GitHub repo `eslamelhefny/AImon` under the name the compose file downloads (`aimon-repo-files_1.zip`),
   or change the URL in the compose file to the new name.
3. Restart the `aimon` project in Docker Manager (or via the Hostinger API: `vps_docker_restart` / `vps_docker_create` with the same compose and environment).
4. Check the logs for `AI Meter is running`.

Claude (in chat) could not push to GitHub: the Claude GitHub App is not installed on the AImon repo.
Installing it at https://github.com/apps/claude/installations/select_target would allow pushing and restarting in one step.
