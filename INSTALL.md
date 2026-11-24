# Install Krillion on the Stackimals VPS

This guide serves the whole quiz at **https://krillion.duckdns.org**. Your existing `stackimals-caddy-1` container handles HTTPS for both sites. Krillion runs as a separate Compose project and listens on port 3000 inside Docker. The initial install and subsequent updates use the same deployment workflow.

## 1. Check DNS and the existing proxy

In your Duck DNS account, set the **krillion** subdomain to the VPS public IPv4 address. If you have an IPv6 record, it must also reach this VPS. Run `nslookup` on your own computer and compare the returned address with your VPS address.

```sh
nslookup krillion.duckdns.org
```

Log in to the VPS as `ubuntu` and inspect the existing Caddy container.

```sh
docker compose version
docker inspect stackimals-caddy-1 --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}'
docker inspect stackimals-caddy-1 --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
docker inspect stackimals-caddy-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
```

Record a **user-defined network** attached to Caddy and the host path supplying `/etc/caddy/Caddyfile` (possibly through a mounted directory). For example, the network might be `stackimals_default`. Use the value you actually see. Caddy already publishes ports 80 and 443. Keep those routed to Caddy. The guide uses Bash, Docker Compose v2.20 or newer, `curl`, Python 3 and `flock` on Ubuntu. [Docker Compose networking](https://docs.docker.com/compose/how-tos/networking/)

## 2. Prepare the quiz installation

Create a dedicated deployment account once. Docker group membership grants administrative control over the VPS. Limit the account’s SSH key to your production GitHub environment.

```sh
sudo adduser --disabled-password --gecos '' krilldeploy
sudo usermod -aG docker krilldeploy
sudo install -d -m 700 -o krilldeploy -g krilldeploy /opt/krillion /opt/krillion/releases /home/krilldeploy/.ssh
sudo -u krilldeploy touch /opt/krillion/.env /home/krilldeploy/.ssh/authorized_keys
sudo chmod 600 /opt/krillion/.env /home/krilldeploy/.ssh/authorized_keys
sudo -u krilldeploy nano /opt/krillion/.env
```

Use the network name you found in step 1 and set the password you want to use in the host console.

```dotenv
DOMAIN=krillion.duckdns.org
CADDY_NETWORK=stackimals_default
HOST_PASSWORD='your chosen quiz password'
```

Single quotes keep spaces, dollar signs and hash characters literal in Compose values. A new login picks up Docker group membership. Verify Docker access from a new login shell.

```sh
sudo -iu krilldeploy docker version
```

Keep `/opt/krillion/.env` private on the VPS. The deployment uploads release files into separate subdirectories. Use project name `krillion` for all Compose commands so upgrades keep the same quiz and backup volumes.

## 3. Add the quiz hostname to Stackimals’ Caddy

Back up the current Caddyfile. Add the following **separate site block** alongside the existing site in the Caddyfile maintained by the Stackimals project. If Stackimals deploys this file from its own repository, save the block there as well.

```caddyfile
krillion.duckdns.org {
    encode zstd gzip
    reverse_proxy krillion-quiz:3000
}
```

Confirm that the container can see the new block. Reload Caddy after the configuration passes validation.

```sh
docker exec stackimals-caddy-1 grep -F krillion.duckdns.org /etc/caddy/Caddyfile
docker exec stackimals-caddy-1 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile &&
  docker exec stackimals-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

For a single-file bind mount, an editor that replaces the host file can leave the container reading the old inode. If the first command cannot see the new block, apply the file through the Stackimals deployment that owns the mount, then validate again. Visit your existing Stackimals address after the reload. Caddy will obtain the quiz certificate automatically once DNS and ports 80/443 reach it. The quiz hostname can return 502 until its first container starts. [Caddy reload and validation](https://caddyserver.com/docs/command-line)

## 4. Connect GitHub Actions

Put the project in a GitHub repository with `main` as the deployment branch. Commit the application, Dockerfile, bundled models and index, Compose files, scripts and workflows. The project's ignore rules exclude the local `.env`, databases and backups.

Generate a dedicated deployment key on your workstation using OpenSSH.

```sh
ssh-keygen -t ed25519 -f ~/.ssh/krillion-actions -C "GitHub Actions Krillion"
```

Use an empty passphrase for this unattended key. On the VPS, append the contents of **the public `.pub` file** on one line in `/home/krilldeploy/.ssh/authorized_keys`, prefixed with `restrict `. The resulting line starts with `restrict ssh-ed25519`. Keep your normal VPS login key separate.

From an already trusted VPS session, generate the SSH host-key entry below. Set the host and port to the **same values that Actions will use**. The final command prints the `known_hosts` entry to copy into GitHub.

```sh
host=krillion.duckdns.org
port=22
name=$host
if [ "$port" != 22 ]; then name="[$host]:$port"; fi
awk -v host="$name" '{print host, $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

In **GitHub → Settings → Environments**, create `production`. Configure the deployment account and its SSH credentials in that environment.

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `VPS_HOST` | `krillion.duckdns.org`, or your SSH hostname/IPv4 address |
| Variable | `VPS_USER` | `krilldeploy` |
| Variable | `VPS_PORT` | SSH port, defaults to `22` |
| Secret | `VPS_SSH_KEY` | Entire private `krillion-actions` key, including its BEGIN and END lines |
| Secret | `VPS_KNOWN_HOSTS` | The complete public host-key entry printed above |

Allow the repository to use Actions and publish packages. The workflow gives the publishing job `packages: write` and the deployment job `packages: read`. For an existing private GHCR package, grant this repository Actions access in the package settings. The VPS uses the run’s temporary registry token for the pull. Your quiz password stays in the VPS `.env`. The SSH port must be reachable from the runner. [GitHub Container Registry access](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

## 5. Install and verify

In **Actions → Deploy quiz to VPS → Run workflow**, choose **main**. Subsequent pushes to `main` run the workflow automatically. It runs the build, game tests, mobile browser tests and a Docker upgrade and rollback rehearsal. Once those pass, it publishes images for AMD64 and ARM64, copies the release configuration over SSH and starts the exact published image digest.

```sh
curl -fsS https://krillion.duckdns.org/healthz
```

Expect JSON with `"ok":true` and the deployed Git commit in `revision`. Open **https://krillion.duckdns.org**, sign into **Host console**, create a room, and join it from a second phone using **Invite teams**. Also open your Stackimals site. Krillion’s deployment manages only its own Compose project.

### First installation or update directly from a checkout

You can run the same installer without Actions. Complete steps 1–3, then run the following commands as `krilldeploy` from a clean checkout of the commit you want to serve.

```sh
set -e
rev=$(git rev-parse HEAD)
dir=/opt/krillion/releases/"$rev"-manual-$(date -u +%Y%m%dT%H%M%S)
mkdir -m 700 "$dir"
docker build --build-arg APP_REVISION="$rev" -t "krillion-local:$rev" .
cp compose.vps.yaml scripts/deploy-vps.sh "$dir/"
printf 'KRILL_IMAGE=krillion-local:%s\nKRILL_REVISION=%s\n' "$rev" "$rev" > "$dir/release.env"
bash "$dir/deploy-vps.sh" deploy
```

## Updates, logs and backups

Each release gets a directory under `/opt/krillion/releases`. `current` points to the latest release that passed container health and public HTTPS revision checks. `previous` points to its predecessor. Keep both releases and their Docker images available for rollback.

A running phone stays on its loaded interface. Its next normal page load fetches fresh HTML and release-specific JavaScript, CSS, fonts, images and sounds. Recent asset versions are retained for open tabs. Sessions and drafts survive a refresh. Replacing the app container can briefly interrupt API requests, and round timers continue during a deployment.

For these commands, log in as `krilldeploy` (or run `sudo -iu krilldeploy`). The `kc` helper selects the running quiz project so you can inspect its status and logs.

```sh
root=/opt/krillion
dir=$root/current
kc() { docker compose -p krillion --env-file "$root/.env" --env-file "$dir/release.env" -f "$dir/compose.vps.yaml" "$@"; }
kc ps
kc logs --tail=80 app
```

Before each upgrade, the installer creates an integrity-checked backup in the `krillion_quiz_backups` volume. The attempted release’s `last-backup` file gives its filename. These commands copy a fresh backup to your home directory, ready to transfer off the VPS.

```sh
backup="event-$(date -u +%Y%m%dT%H%M%S).sqlite"
kc exec -T app node scripts/database.mjs backup "$backup"
kc cp "app:/app/backups/$backup" "$HOME/$backup"
chmod 600 "$HOME/$backup"
```

Backups include team and host sessions. Store them privately. The named volume `krillion_quiz_data` contains rooms, scores and sessions. Preserve it when maintaining Docker.

## Failed deployments and rollback

A failed health check fails the Actions run. On an upgrade, the installer attempts to restart the last healthy release if the database schema still matches its recorded version. It keeps the pre-upgrade backup. A successful recovery still reports the deployment failure.

- **Website returns 502.** Check `kc ps`, `kc logs --tail=80 app`, the Caddy logs and the value of `CADDY_NETWORK`. Verify that Caddy and the quiz are attached to that network and that the app has the `krillion-quiz` alias.
- **First deployment failed.** There may be no `current` yet. Set `dir` in the helper to the attempted directory under `/opt/krillion/releases`, inspect its logs, fix the reported error and rerun the workflow.
- **Image pull or SSH failed.** Check GHCR package access, the SSH account, port and pinned host key. A pull failure occurs before switching the app.
- **Public health reports the wrong revision.** Check DNS, Caddy’s site block, and whether another container on the shared network has the same `krillion-quiz` alias.

To undo a **successful** update, select the previous release and verify its revision. The installer checks database compatibility first.

```sh
bash /opt/krillion/previous/deploy-vps.sh rollback
curl -fsS https://krillion.duckdns.org/healthz
```

To recover a **failed** update manually, `current` still identifies the last healthy release. Use `bash /opt/krillion/current/deploy-vps.sh rollback`. Compare the public revision with the target’s `release.env`. A later push to `main` deploys that branch again, so revert or fix the bad change in GitHub too.

If the schema versions differ, first consider deploying a forward fix to retain newly submitted answers. Restoring the pre-upgrade backup rewinds scores and sessions to the time of that backup. If you choose restoration, use the helper above, identify the **target healthy release** and the **backup from immediately before the bad upgrade**, and stop the app before restoring the backup.

```sh
dir=/opt/krillion/releases/REPLACE_WITH_TARGET_HEALTHY_RELEASE
backup=REPLACE_WITH_PRE_UPGRADE_BACKUP.sqlite
kc stop app
kc run --rm --no-deps -T --pull never app node scripts/database.mjs restore "$backup"
bash "$dir/deploy-vps.sh" rollback
curl -fsS https://krillion.duckdns.org/healthz
```

The restore tool validates the backup and preserves the replaced database files beside the restored database. Open a known room and verify its standings before resuming play.
