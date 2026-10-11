# HostProp

HostProp uses PVE Scripts Local (PSL) as its guest source and propagates guest changes to configured destinations. NPMPlus is the first and currently supported destination: it manages proxy hosts so homelab services stay connected and accessible. The source and destination adapters are separated so additional destinations, including Checkmk, can be added without coupling them to PSL record formats. The dashboard provides previews and NPMPlus host management, with continuous propagation available through the watcher.

## Mapping

- Public proxy hostname: `<guest-hostname>.<public domain>`; enter the domain on the NPMPlus tab.
- VLAN routing has one card per NPMPlus instance: every Proxmox VLAN tag that instance serves is a row in its card, next to the DNS zone that instance can resolve for guests on that VLAN. One instance can have any number of rows.
- A created proxy host sends `https://<guest-hostname>.<public domain>` to `http://<guest-hostname>.<zone>:<web_ui_port>` on the instance its VLAN row names.
- Guest hostname and `web_ui_port` come from the PVE Scripts Local installed-script response.
- The VLAN tag comes from the guest's cached LXC config in that response (`net_vlan`) whenever PSL sends one. A PSL deployment that sends no LXC config at all still reports each guest's web UI address, and a network that segments by VLAN repeats the tag in the second and third octets (`<network>.<tag>.<tag>.<host>`), so there the address supplies the tag. An address that does not repeat the same number twice is not read as a tag: those guests fall back to the `untagged` tag, which is also what a guest whose cached config carries no tag always uses.
- Manual `Preview now` and `Apply now` first check container status through PSL, skip containers Proxmox reports as stopped, then force-refresh LXC config and auto-detect the live Web UI IP/port for every remaining non-VM PSL record with a container ID, including historical install records. A valid port returned by auto-detection takes precedence over the saved port and is written back to PSL, so the preview reflects the detected endpoint; if detection does not return a valid port, a valid saved port is retained. The button and status message show that refresh is in progress, including the current record; a manual run waits for an active watcher run instead of returning its old plan. Refresh may take longer when many records need updating. If the selected current record cannot be refreshed but already has a stored IP and valid port, those values are used without adding a plan error; refresh failures on superseded install records are not reported. Without both stored values, the current record is skipped and its error appears in `Latest PSL plan`. A container-status lookup failure still stops the run because PSL has not identified which containers are stopped. PSL's auto-detect API requires SSH-mode records. When enabled, the background watcher also refreshes running containers' LXC config and Web UI endpoints through PSL before every scheduled plan.
- A PSL name that ends in `.sh` keeps the word before the period as the guest hostname (`photo-service.sh` → `photo-service`); that name is used whether it comes from the guest's cached LXC hostname or from the script name. Any other name has to be a valid host label already, and one that is not is skipped.
- PSL keeps one record per install attempt, so one container can be described by several records: an install that failed and was retried, or a reinstall under another script name, leaves the earlier record beside the later one. Records are matched by `container_id`, never by name, and one container yields at most one proxy host: the newest record is used regardless of PSL's install status, even if an older record has more complete or stale networking data. Historical records are not reported as skipped guests. A record that states no `container_id` is left as a guest of its own, because nothing ties it to another record.
- The PSL tab's excluded-hostname list is empty initially and authoritative: only listed names are excluded. Excluding an NPMPlus admin guest is recommended to avoid proxy loops or accidental exposure of its admin UI.
- A guest may be given extra names for its proxy host (aliases), so one entry serves `<guest>.<public domain>` plus, say, `photos.<public domain>`. See [Aliases](#aliases-one-host-several-names).

Any number of NPMPlus instances and any number of VLAN rows can be configured, and several VLAN tags can point at the same instance. Renaming an instance row carries its VLAN card along, while removing one leaves that card flagged as missing until the instance is added back or its rows are deleted, so a saved mapping never points at an instance that does not exist. Instance names are slugs such as `lab`, `public`, or `dmz`, and each becomes the `<instance>:<domain>` ownership key in the state file, so renaming an instance orphans its recorded hosts: those existing hosts are then reported as conflicts and are never updated or deleted until you rename the instance back or record them under the new name.

Whether PSL includes the LXC config decides where the VLAN tag is read from (see above), and a guest that states no VLAN anywhere routes through a VLAN row tagged `untagged` if one exists, or is skipped if not. VM records are always skipped because nothing in that response states a VM's VLAN tag. Records with no container ID, missing/invalid ports, unmapped VLAN tags, two containers claiming one hostname, and missing certificates are shown as skipped in the latest plan; PSL's success/failure status does not determine whether a record is planned. A skip that came from a guest address names that address in its reason.

## Configure

Use Python 3.11 or newer. The app uses only Python's standard library, so there are no third-party dependencies to install: `requirements.txt` is intentionally comments-only, and `python -m venv .venv` is the whole setup step.

The dashboard groups setup and host operations on four tabs inside one card: `PSL` holds the PVE Scripts Local endpoint and the guests to leave alone, `NPMPlus` holds every instance this app may write to plus the public names it creates there, `VLAN routing` holds the VLAN tag cards that send a tag to an instance, and `Host management` lists and manages individual proxy entries. Connection details, public domain, exclusions, VLAN routes, check interval, and deletion limit start blank; fill in what you use. Watch PSL and automatic deletion are off until enabled, and TLS certificate verification remains on unless you explicitly turn it off. Each settings tab has its own save button, and any of them writes the whole settings file, so it does not matter which settings tab you save from.

The page is a shell plus one panel per destination. The shell owns the PVE Scripts Local settings, the watcher controls and the sidebar's `Destinations` card; each destination brings its own tabs, panel markup, look and actions, and the server hands those files over from that destination's own package without reading them. The `Destinations` card names every destination the core reported, the panel file each one brought, and how many tabs and actions it mounted, with `Open <label>` beside it to jump to that destination's first tab; it says outright that a run has nothing to write to when nothing is registered. The header badge counts those destinations. A destination added later therefore appears in the card and brings its own tabs with no change to the shell, and the `PSL`, `NPMPlus`, `VLAN routing` and `Host management` tabs today are what the shell plus its one destination contribute to that order.

1. Start the dashboard (see Run below) and open <http://127.0.0.1:8088/>.
2. On the `NPMPlus` tab, press `＋ Add instance` once per NPMPlus host and give each one a name, URL, identity, and secret. The URL is the one that serves the admin API, which for NPMPlus is `https://<host>:81` rather than the bare hostname (see [The URL and its port](#the-url-and-its-port)). Two instances are enough to split separate environments; add more for extra VLANs. If the instance's `https://` certificate is not trusted by this machine, turn on the `self-signed` switch beside its URL (see below).
3. On the `VLAN routing` tab each instance has its own card: press that card's `Add VLAN` once per Proxmox VLAN tag the instance serves, and give each tag the guest DNS zone that instance can resolve. In networks that encode VLANs in addresses, the repeated second and third octets identify the tag; a card also accepts the word `untagged` to catch guests that report no VLAN at all. A row is only saved once it has both a tag and a zone, and a tag cannot be mapped twice.
4. On the `NPMPlus` tab, enter the public hostname domain that guests should use.
5. Fill in the PVE Scripts Local endpoint on the `PSL` tab, then press `Save PSL settings`. `Test PSL` and each instance's `Test` button confirm the endpoints before you apply anything.
6. Enter PVE Scripts Local credentials only if its authentication is enabled. Each NPMPlus identity is that account's **email address** (the API signs in by email, not by username) and its secret is that account's password; give it an account that can manage proxy hosts and view certificates. A sign-in the API refuses names the instance and repeats that.
7. Give every instance a certificate covering the public domain (`*.<public domain>`) or each exact host. A planned host with no matching unexpired certificate is skipped; a host that has aliases needs one certificate covering all of them.
8. Each NPMPlus must be able to resolve the guest DNS zone it is mapped to (`internal.example.net`, `dmz.example.net`, …) so it can forward to `<guest-hostname>.<zone>`.

Entries that already exist on an NPMPlus are shown in `Host management`, where you can adopt one to let a guest manage it, release it, edit its guest-specific aliases, or delete it outright (see [Managing the entries](#managing-the-entries)). The same tab's `Latest PSL plan` section shows the most recent preview/apply results and skip reasons; Preview does not create the NPMPlus entries.

Passwords and API secrets are write-only in the page: leaving a secret field blank keeps the stored value, and deleting an instance row deletes its credentials with it. They are saved in `data/app-config.json`, created with mode `0600` inside a directory created with mode `0700`, and rewritten atomically; Windows ignores those modes, so there the file relies on the NTFS permissions it inherits from your profile directory. Treat the file as a password store: keep it out of version control and out of any backup or folder share you would not already trust with a password, or use an environment reference so it holds no secret at all. The page lists every secret still saved as plain text, and the launcher repeats that warning at startup, so a literal credential is never silent. A settings file that still uses the older `npm_lab_*` / `npm_public_*` keys is migrated to named instances the first time it is read. `.env.example` lists the optional environment variables the launcher reads.

When the dashboard is accessed through a DNS name (for example, behind a reverse proxy), add that name to the comma-separated `WEB_ALLOWED_HOSTS` environment variable. `localhost`, loopback addresses, and IP-literal Host values are accepted by default; other DNS names are rejected to protect the dashboard from DNS-rebinding requests. Keep `APP_PASSWORD` enabled when exposing the dashboard beyond the local machine, and use HTTPS or a trusted TLS reverse proxy.

### Keeping passwords out of the settings file

Every secret field — an instance's secret and the PVE Scripts Local password — also accepts an environment reference instead of the password itself. The dashboard stores the reference verbatim and reads the value from its own environment each time it builds a client, so `data/app-config.json` can hold no plaintext credential at all:

```json
{
  "pve_local_password": "env:PVE_LOCAL_PASSWORD",
  "instances": {
    "lab": {"url": "https://npmplus-one.internal.example.net:81", "identity": "user@example.org", "secret": "env:NPM_LAB_SECRET"}
  }
}
```

- A reference is `env:` followed by a supported variable name: `env:NPM_LAB_SECRET` for an NPMPlus instance or `env:PVE_LOCAL_PASSWORD` for PSL. Anything else stays a literal password.
- The variable name must match the field: NPMPlus secrets use `NPM_<INSTANCE>_SECRET`, and the PSL password uses `PVE_LOCAL_PASSWORD`. The `env:` prefix is reserved, so a password that itself begins with `env:` cannot be stored literally.
- Existing settings files that use custom environment-variable names still load so the dashboard can be opened and repaired. Replace those references with the supported field-specific names before saving; runs and settings saves reject unsupported references.
- There is no implicit lookup, so a field left blank still means "no password".
- The variables belong to the process that runs the dashboard, so set them where it is started: `$env:NPM_LAB_SECRET = "..."` in the shell or `.venv` activation you launch it from, `Environment=` in a systemd unit, or the task's environment when Task Scheduler runs it. A `.env` file is not parsed.
- An unresolved reference fails the run and the `Test` buttons with a message naming the variable, and the launcher prints the same warning at startup. It never falls back to an empty password, because a request without a secret would just be rejected.
- The page shows `From NPM_LAB_SECRET` in place of a stored reference, or `NPM_LAB_SECRET is not set` when the variable is missing; secret values remain write-only and are never sent to the browser.
- The page lists every secret still saved as plain text, and the launcher prints the same warning at startup, so a literal credential is never silent. Saving the field with an `env:` reference removes it from the file, and the warning clears once no literal secret is left.
- An environment variable is readable by anything running as the same user, so this keeps credentials out of the settings file and out of version control rather than out of reach of a compromised session.

### Self-signed certificates

A LAN NPMPlus or PVE Scripts Local normally serves a certificate signed by its own CA, which this machine does not trust, so the request is refused with `CERTIFICATE_VERIFY_FAILED` before anything is sent. Each endpoint has its own switch for that:

- `self-signed` beside an instance's URL (stored as `"verify_tls": false` on that instance) relaxes the certificate check for that instance only; every other instance keeps verifying.
- `PSL certificate is self-signed` does the same for the PVE Scripts Local URL (`"pve_local_verify_tls": false`).

Both default to verifying, so nothing changes until you turn one on. A failed connection names the cause and points at the switch:

```
Could not reach https://npmplus-one.internal.example.net:81/api/tokens: [SSL: CERTIFICATE_VERIFY_FAILED] certificate
verify failed: self-signed certificate. This machine does not trust that certificate: turn on the
'self-signed' switch for this connection, or install the issuing CA on this machine. Check the port as
well: NPMPlus serves its admin API on 81, for example https://host:81
```

Instead of skipping the check you can make the machine trust the certificate: import the issuing CA into the OS trust store (`certutil -addstore -f ROOT ca.crt` on Windows, `update-ca-certificates` on Debian) or point the dashboard's own environment at a PEM bundle with `SSL_CERT_FILE`. Skipping the check means a host that is already impersonating your NPMPlus would be accepted, so it fits a LAN endpoint rather than a public one.

### The URL and its port

An instance needs the address that serves the admin API directly: for NPMPlus that is `https://<host>:81`, and the scheme matters as much as the port, because plain `http://` on 81 answers a `GET` with JSON but answers a login `POST` with a `308` to the `https://` URL, which is refused (a `POST` is never replayed across a redirect, and the message names the URL it points at). Both failures name what came back, and `POST`/`PUT`/`DELETE` are never carried across a redirect:

```
GET https://npmplus-one.internal.example.net/api/nginx/proxy-hosts did not return JSON (content-type text/html).
That is a web page rather than the API: check the URL's port. NPMPlus serves its admin API on port 81
(for example https://host:81), while 80 and 443 usually answer with the proxy host or the default page.
The reply was: <!DOCTYPE html> <html> <head> <title>Default Page</title> ...

POST http://npmplus-one.internal.example.net/api/tokens was redirected to https://npmplus-one.internal.example.net/api/tokens:
following that redirect turns the POST into a GET and drops the request body, so the call can never
succeed. Use the URL that answers without redirecting instead. NPMPlus serves its admin API on port 81 ...

POST http://npmplus-one.internal.example.net:81/api/tokens failed (308): <html> <head><title>308 Permanent
Redirect</title></head> ... That is a redirect (308) to https://npmplus-one.internal.example.net:81/api/tokens, not an
API reply: a POST is never resent across a 307/308, and across a 301/302/303 it arrives without its body,
so the login could only come back unauthenticated and fail later as a permission error. Use the URL it
points at

```

A `301`/`302` is refused rather than followed for anything but a `GET` or `HEAD`, because `urllib` resends those as a `GET` with no body: a login sent that way could only come back unauthenticated, and the mistake would surface much later as a permission error. `GET`s still follow redirects, and a reply that is not JSON reports the redirect, its content type, and the start of the body, so an HTML page is obvious rather than puzzling.

Neither side is a screen to be scraped. An NPMPlus instance publishes its own OpenAPI document at `/api/schema` (title `NPMplus API`, 44 documented paths including `/nginx/proxy-hosts` and `/nginx/certificates`), and the cookie it sets on `POST /api/tokens` (`__Host-Http-token`) is the same session the web UI runs on, so no API key has to be created anywhere: the identity and secret are the email address and password of an account that can manage proxy hosts, which is why the schema's own example for `identity` is `me@example.com`. PVE Scripts Local likewise answers the tRPC call this app makes, `/api/trpc/installedScripts.getAllInstalledScripts`, once its session cookie has been set by `POST /api/auth/login`.


## Run

```powershell
.\.venv\Scripts\python.exe -m reverse_proxy_automation
```

`python -m reverse_proxy_automation.webapp` is equivalent.

These all start the same program, so use whichever fits your workflow:

```powershell
python -m reverse_proxy_automation                 # package form, runs __main__.py
python -m reverse_proxy_automation.webapp          # module form
python reverse_proxy_automation/webapp.py          # file form (also what an IDE's Run Python File uses)
```

Open the dashboard at its `http://127.0.0.1:8088/` address after starting it. Do not open `reverse_proxy_automation/web/index.html` directly as a `file://` page; dashboard settings and actions need the running server's `/api` endpoints.

Only `webapp.py` and `__main__.py` are meant to be started as files. `clients.py`, `planner.py`, `service.py`, and `sync.py` are libraries imported from inside the package, so launching one of those directly exits with `attempted relative import with no known parent package`. The two entry points avoid that by giving themselves their package name (PEP 366) before importing it, so the package's modules are imported once, by name, no matter how the app is started. Options:

- `--host` — bind address, default `127.0.0.1` (`WEB_HOST`)
- `--port` — listen port, default `8088` (`WEB_PORT`)
- `--config` — settings file, default `./data/app-config.json` (`APP_CONFIG_PATH`)
- `--state` — ownership state file, default `./data/sync-state.json` (`SYNC_STATE_PATH`)

Binding to anything other than localhost requires `APP_PASSWORD` (HTTP Basic), and the app refuses to start without it. Use HTTPS or a trusted TLS reverse proxy on untrusted networks.

## Install in a Debian or Ubuntu LXC

The repository includes a systemd unit that keeps the dashboard and PSL watcher running across logouts and LXC reboots. Use a Debian 12+ or Ubuntu 24.04+ LXC so the system Python meets the app's Python 3.11+ requirement. The LXC needs network access to PSL, every configured NPMPlus API, and the DNS names NPMPlus uses to reach guest Web UIs. Give the LXC a stable address or DHCP reservation. The service listens on port `8088`; restrict access to that port to your trusted network, or put the dashboard behind a trusted TLS reverse proxy.

Install Git and Python's virtual-environment support, then clone the repository. Replace `<repository-url>` with the URL where you keep this project:

```sh
apt update
apt install -y git python3 python3-venv
git clone <repository-url> /opt/reverse-proxy-automation
python3 -m venv /opt/reverse-proxy-automation/.venv
/opt/reverse-proxy-automation/.venv/bin/python -m pip install -r /opt/reverse-proxy-automation/requirements.txt
useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin reverse-proxy-automation
install -o root -g root -m 0644 /opt/reverse-proxy-automation/deploy/reverse-proxy-automation.service /etc/systemd/system/reverse-proxy-automation.service
install -o root -g root -m 0600 /opt/reverse-proxy-automation/deploy/reverse-proxy-automation.env.example /etc/default/reverse-proxy-automation
```

Edit `/etc/default/reverse-proxy-automation` and replace the `APP_PASSWORD` placeholder with a long random password. Then enable the service:

```sh
systemctl daemon-reload
systemctl enable --now reverse-proxy-automation
systemctl status reverse-proxy-automation
```

Open `http://<lxc-address>:8088/` on your trusted network and enter `reverse-proxy-automation` as the HTTP Basic username and the configured password. Add the PSL and NPMPlus connections, set the public domain and VLAN routing, then run **Preview now** and confirm the plan. To propagate PSL changes to your proxies automatically, set a polling interval (for example, 60 seconds), enable **Watch PSL** and **Automatically apply**, and save. The watcher then reads PSL and propagates the resulting changes at the configured interval; there is no need to leave the browser open. Keep **Delete removed guests** disabled unless you specifically want the app to remove managed proxy hosts whose guests disappear from PSL, and set its deletion limit before enabling it.

The service's config and ownership files are stored under `/var/lib/reverse-proxy-automation/` and survive upgrades and reboots. To update the app, stop the service, update the checkout, then start it again:

```sh
systemctl stop reverse-proxy-automation
git -C /opt/reverse-proxy-automation pull --ff-only
/opt/reverse-proxy-automation/.venv/bin/python -m pip install -r /opt/reverse-proxy-automation/requirements.txt
systemctl start reverse-proxy-automation
```

Use `journalctl -u reverse-proxy-automation -f` to follow logs. If you change the service unit itself, run `systemctl daemon-reload` before restarting.

When `Watch PSL` is enabled, the watcher runs once immediately when the app starts, then every configured interval refreshes the running containers' LXC config and Web UI endpoints through PSL before planning. If `Automatically apply` is also enabled, it propagates that fresh plan to NPMPlus; without it, the watcher refreshes and previews the plan without writing proxy changes. Manual `Preview now` and `Apply now` also perform a refresh. Refreshing makes PSL queries (including SSH-based guest config and endpoint detection) for each eligible non-VM record, so the load grows with the number of records; in a typical home setup, start with a 300-second (5-minute) interval and shorten it only if you need changes applied sooner. The minimum allowed interval is 30 seconds. The dashboard's `PSL guests` figure counts containers, so a container PSL describes with several records is counted once.

The app deletes a proxy host only when its ID is recorded in the state file and the guest has left PVE Scripts Local, or when the guest moved to another NPMPlus instance. A container PSL now describes under a new name — a reinstall under another script name, say — counts as a guest that has left under its old name, so a host created for that old name is deleted the same way. An instance that has no configured URL is never contacted, so its recorded hosts are left in place. `Delete removed guests` is off initially; enable it and set `Max deletions per run` to permit cleanup. A run that would delete more than the configured limit deletes nothing. A matching host created manually is reported as a conflict and never overwritten unless you adopt it.

## Managing the entries

The `Host management` tab lists every proxy host on each configured instance, and each one is in one of three states:

- **managed** — this app created it. It is kept in step with its guest's plan, and deleted when the guest leaves PVE Scripts Local or moves to another instance.
- **adopted** — you handed an existing entry to the app with `Adopt`. A matching guest updates it exactly like a managed host, but it is never deleted on its own: only `Delete` removes it.
- **unmanaged** — the app leaves it alone. A planned guest whose name is already served by an unmanaged entry is reported as a conflict instead of being written over.

`Latest PSL plan` below the entries table shows the latest Preview or Apply results, including each guest hostname, target instance, route details, and skip reason. Preview is read-only; use Apply to create or update NPMPlus entries.

`Release` takes an entry back out of the app's hands: its record is dropped from the state file, so from then on the app neither updates nor deletes it. `Adopt` and `Release` change only the state file, never NPMPlus. The per-entry alias editor saves aliases for that instance and guest route; bare labels use the public hostname domain, and fully qualified names are kept as written. Alias changes are stored in the app configuration and are applied on the next preview or apply. A saved per-entry alias list takes precedence over aliases in older guest-wide settings for that route. The port override editor saves a port for that instance and guest route, taking precedence over the PSL-detected port in preview and apply; leave it blank to clear the override and use PSL detection again. An unmanaged host is still left alone until it is adopted. `Delete` removes one entry from NPMPlus straight away, whichever state it is in, after a confirmation naming the entry and its instance. The `Delete removed guests` switch and `Max deletions per run` limit guard the app's own cleanup on a run, not that explicit button — and a managed entry does come back on the next apply while its guest is still in PSL, so `Release` it first if it should stay away.

### Aliases: one host, several names

A guest can answer to more than one public hostname. In the `Host management` tab, edit the aliases on that guest's entry as a comma-separated list:

```text
photos, video-service, requests.example.net
```

A name without a dot is a label under the public hostname domain; a name with a dot is used as it stands. Saving an empty list removes the guest-specific aliases. Existing settings that used the older guest-wide `aliases` list are still honored until you save aliases for that guest route in `Host management`; the per-entry list then takes precedence for that instance and guest.

The extra names are written on the guest's own proxy host, so `media-service.example.net` and `photos.example.net` reach the same container through one entry; the guest's own name always comes first, because that is the name the app records the host under. A name belongs to one host only: a claim on another guest's hostname, or on a name another guest's alias already claimed, is dropped and reported as a skipped item rather than written. A host with aliases needs a certificate covering every one of its names — normally the wildcard for the public domain — and is skipped with the uncovered names listed when it has none. An alias counts as the guest still being present, and an entry serving a name this app does not know for that guest is never deleted by itself: it is reported, and `Host management` is where the entry can be deleted.

The dashboard needs `reverse_proxy_automation/web/index.html`; the launcher stops with an error at startup if that file is missing.

Run tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
