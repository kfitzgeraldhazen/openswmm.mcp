# RUNBOOK — starting the OpenSWMM MCP server on this machine

The definitive start-to-finish sequence for **this** machine, with this machine's actual
values. Everything below was verified live on 2026-10-09.

**How this relates to the other docs.** This is the operational runbook — what to type,
in what order. The others are the reference material behind it, and they remain the
source of truth for *why*:

| | |
|---|---|
| `HOST_SETUP.md` | Colleague-facing. Standing the server up on a **different** machine from scratch. |
| `APIKEY_SETUP.md` | The auth design, what it does and does not protect, verified test results. |
| `TUNNEL_SETUP.md` | Tunnel mechanics and the full Copilot Studio walkthrough, including publishing. |
| `ENTRA_SETUP.md` | The intended end state. Blocked on an IT ticket; never shipped. |

If a command here disagrees with one of those, those win and this file is stale — fix it.

---

## Current configuration

| | |
|---|---|
| Project root | `C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host` |
| Interpreter | Resolved automatically from `.python_path` — **do not hardcode it** |
| Port | `8080` |
| Working dir / write jail | `<project root>\scratch` |
| Tunnel ID | `openswmm-936d58ec.use` |
| Public URL | `https://8tk2vvls-8080.use.devtunnels.ms/` |
| **Agent Server URL** | `https://8tk2vvls-8080.use.devtunnels.ms/mcp` |
| Service cluster | `use` |
| devtunnel version | `1.0.2094+24665e6583` |
| Tunnel created | 2026-10-09 → lapses on or before **2026-11-08** |

> **The hostname `8tk2vvls` is not derived from the tunnel ID.** The service generates it
> separately. Never construct the URL — read it from `devtunnel show`. A rebuilt tunnel is
> not guaranteed to get the same hostname back.

> `devtunnel show` reports the configured **30-day lifetime**, not a countdown. Track the
> expiry date yourself; nothing will warn you.

> **This kit lives inside a git clone of `HydroCouple/openswmm.mcp`.** It is hidden from
> git by `/host/` in `.git\info\exclude` — local-only, so it never fights a pull and can
> never be pushed upstream. The cost: `git clean -fdx` run from `openswmm.mcp\` **deletes
> this entire folder, `scratch\` included** — `-x` removes ignored files, and an exclude
> entry is no protection. Plain `git clean -fd` is safe. There is nothing in `host\` that
> git can restore.

---

## Part A — One-time setup

**Already done on this machine.** Here for rebuilds and for the record.

```powershell
# 1. Build the Python environment (finds conda, creates env `openswmm.mcp` on 3.13,
#    installs pinned versions, writes .python_path). Safe to re-run.
cd "C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host"
.\setup.cmd

# 2. Install the devtunnel CLI (per-user, no admin).
winget install Microsoft.devtunnel

# 3. Open a NEW terminal — winget only puts the folder on the User PATH for
#    terminals opened after the install — then:
devtunnel --version
devtunnel user login        # add -g for GitHub instead of a work account

# 4. Generate the API key. Save it to the password manager immediately;
#    it is not written to disk anywhere, by design.
.\start_openswmm.cmd /generate

# 5. Create the persistent tunnel + its port. ALREADY DONE — this is the
#    command that produced openswmm-936d58ec.use. Re-running creates a second tunnel.
devtunnel create openswmm-936d58ec --allow-anonymous --expiration 30d
devtunnel port create openswmm-936d58ec.use -p 8080 --protocol http
```

`--allow-anonymous` is required: Copilot Studio calls from Microsoft's cloud with no
browser session, so a tunnel demanding tunnel-level sign-in just serves it a login page.
It means **the API key is the only gate**.

### If `devtunnel` is not found

winget never created the `WinGet\Links` symlink (that needs Developer Mode or admin), so
the package folder itself is on `PATH`. In a brand-new terminal it works. Otherwise call
it by full path:

```powershell
$dt = "C:\Users\KFitzGerald\AppData\Local\Microsoft\WinGet\Packages\Microsoft.devtunnel_Microsoft.Winget.Source_8wekyb3d8bbwe\devtunnel.exe"
& $dt --version
```

---

## Part B0 — The console (the easy path)

```powershell
cd "C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host"
.\Start-Console.cmd
```

`OpenSWMM-Host-Console.ps1` is a small WPF panel over exactly the two commands in Part B.
It does not replace them or reimplement them — it shells out to `start_openswmm.cmd` and
`devtunnel host`, and shows their output in two side-by-side panes. **Part B below remains
the source of truth**; if the console ever disagrees with it, the console is wrong.

What it adds is the rules in this file that otherwise have to be remembered:

- **Open Tunnel stays disabled until the server is genuinely Ready** — process alive *and*
  the banner reported `auth ENABLED` *and* the port is listening. This is the B1 rule
  ("no `auth ENABLED` → do not open the tunnel") as an interlock rather than a warning.
- **Stop Server while hosting** is blocked behind a dialog that closes the tunnel first,
  and if the server dies on its own the console closes hosting automatically — the Part E
  hazard. The tunnel *definition* is never touched; only `devtunnel host` is stopped.
- **Every URL shown is parsed** from `devtunnel` output, never built from the tunnel ID.
- **Days until expiry**, tracked in `console_config.json`, because `devtunnel show` reports
  the configured 30-day lifetime rather than a countdown.
- A warning when something already holds the port, or when a second OpenSWMM tunnel
  already exists, naming what it found.

The API key is typed into a masked box, passed to the child process's environment only
(never a command line), cleared once the server is Ready, and never written to disk.
`console_config.json` holds non-secret settings only and there is deliberately no option
to save the key.

Tunnel creation, deletion and `devtunnel user login` live behind the collapsed
**Setup & Renewal** section — they are monthly maintenance, not session controls.

Local-only: the console is deliberately not in `make_handoff_zip.ps1`'s allowlist, so
colleagues still get the Part B workflow unchanged.

---

## Part B — Every session

**Two windows, in this order.** Server first, then tunnel — so the public URL never points
at a dead port.

### B1. Window 1 — the server

```powershell
cd "C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host"
.\start_openswmm.cmd
```

Paste the key at the hidden prompt. The launcher sets `OPENSWMM_MCP_TRANSPORT=http`,
`OPENSWMM_MCP_HTTP_PORT=8080`, `OPENSWMM_MCP_WORKING_DIR=<root>\scratch`, clears
`OPENSWMM_MCP_ENABLE_PYTHON`, and discards all of it on exit. Nothing is persisted.

Expected banner:

```
OpenSWMM MCP - static API-key auth ENABLED
  bind         http://127.0.0.1:8080/mcp
  working dir  ...\openswmm.mcp\host\scratch
  jail         writes  ...\openswmm.mcp\host\scratch
               reads   ...\openswmm.mcp\host\scratch
  key          43 chars, sha256:2e7002a4
  header       Authorization: Bearer <key>
```

> **No `auth ENABLED` line → stop. Do not open the tunnel.** Without it the server accepts
> unauthenticated requests and the next step makes it public.

Check the `sha256:` fingerprint matches the key you meant to use, and read the `jail`
lines — they are the only place the server states what it can actually touch.

**The first start takes up to a minute** of silence while the engine library loads. Not a
hang. Later starts are quick.

Port already busy? The launcher says so and prints the PID. Either stop that process or
run `.\start_openswmm.cmd 8081` — but note the tunnel is bound to 8080, so a different
port needs a matching `devtunnel port create`.

### B2. Window 2 — the tunnel

```powershell
devtunnel host openswmm-936d58ec.use
```

Leave it open beside the server window. It prints:

```
Connect via browser: https://8tk2vvls-8080.use.devtunnels.ms
```

`Ctrl+C` stops hosting without destroying the tunnel definition.

Lost the window? `devtunnel show openswmm-936d58ec.use` reprints the URL.

---

## Part C — Verify the chain

Run this from a third window. The first call **must** fail and the second **must**
succeed.

```powershell
$url = "https://8tk2vvls-8080.use.devtunnels.ms/mcp"

curl.exe -s -o NUL -w "no key:   %{http_code}`n" -X POST $url

# The body goes through a file deliberately — see the note below.
$body = Join-Path $env:TEMP "mcp_init.json"
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' |
    Out-File -FilePath $body -Encoding ascii

$key = Read-Host "key"
curl.exe -s -o NUL -w "with key: %{http_code}`n" -X POST $url `
    -H "Authorization: Bearer $key" `
    -H "Content-Type: application/json" `
    -H "Accept: application/json, text/event-stream" `
    --data-binary "@$body"

Remove-Item $body
```

**`no key: 401` and `with key: 200` means the whole chain is correct.**

> **Why the temp file.** Windows PowerShell 5.1 rewrites quotes when handing arguments to
> a native `.exe`, so an inline `-d` JSON body arrives mangled. The symptom is a **400**
> with `Parse error: key must be a string at line 1 column 2` — which reads like a
> rejected key but is not. A 400 means the key was *accepted* and the body was the
> problem. PowerShell 7 does not have this defect.

### End-to-end, through the agent

`scratch\site_drainage_example.inp` is staged for exactly this. Ask:

> open `site_drainage_example.inp`, run it, and report flooding

Correct answer: **6 flooded nodes, worst is `J10` at roughly 231,800–231,900 ft³**, then
`J6`, `J2`, `J3`, `J5`, `J9`. Continuity error 0. Check the node count and the ranking,
not the exact digits — the last few vary with the engine build.

---

## Part D — Copilot Studio wiring

One-time, and again **only if the tunnel URL changes**.

Generative orchestration must be **on** or MCP tools are never invoked.

Agent → **Tools** → **Add a tool** → **New tool** → **Model Context Protocol**:

| Field | Value |
|---|---|
| Server name | `OpenSWMM` |
| Server URL | `https://8tk2vvls-8080.use.devtunnels.ms/mcp` |
| Authentication | **API key** → Type **Header** → name `Authorization` |

Then **Create** → **Create a new connection** → connection value `Bearer <your key>` →
**Add to agent**.

> **The connection value must include the literal `Bearer ` prefix.** A bare key returns
> 401 — tested.

Do **not** touch the agent-level setting under **Settings → Security**; leave it on
*Authenticate with Microsoft*. That governs who may talk to the agent, not the MCP
connection, and the M365 Copilot channel requires it.

Publishing is not incremental — every agent edit needs another **Publish**, and existing
chat sessions cache the old version for up to an hour (`start over` forces a refresh).
Full publishing sequence in `TUNNEL_SETUP.md` §5.

---

## Part E — Shutting down

**Tunnel first.** It closes the public door.

```powershell
# 1. Ctrl+C in the `devtunnel host` window.
# 2. Ctrl+C in the server window.

# 3. Confirm nothing is still listening — no output means the port is free.
Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
```

Stopping only the server leaves a public URL pointed at your machine, ready to serve
whatever next binds to 8080.

Retiring it permanently:

```powershell
devtunnel delete openswmm-936d58ec.use
```

---

## Part F — Renewal, on or before 2026-11-08

The 30-day expiration is the service maximum and cannot be extended. Delete and recreate:

```powershell
devtunnel delete openswmm-936d58ec.use

$suffix = -join ((48..57) + (97..102) | Get-Random -Count 8 | ForEach-Object { [char]$_ })
$id     = "openswmm-$suffix"
$id

devtunnel create $id --allow-anonymous --expiration 30d
devtunnel port create "$id.use" -p 8080 --protocol http

# READ the new URL. Do not assume it carried over.
devtunnel show "$id.use"
```

Then **compare the new URL against the old one.** If it moved — which is likely, since
the hostname is service-generated — repoint the agent's Server URL in Copilot Studio
(Part D) and republish. Update the Current configuration table at the top of this file.

The host *login* also lapses independently after a few days. Symptom is `devtunnel host`
failing or prompting; fix is `devtunnel user login`.

---

## Quick reference

```powershell
# Console (wraps both of the below, with the safety interlocks)
cd "C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host"
.\Start-Console.cmd

# Start by hand (two windows, server first)
cd "C:\Users\KFitzGerald\OneDrive - Hazen and Sawyer\Documents\Software Development\openswmm.mcp\host"
.\start_openswmm.cmd
devtunnel host openswmm-936d58ec.use

# Where is it?
devtunnel show openswmm-936d58ec.use
devtunnel list

# Stop (tunnel first, then server)
Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
```

---

## Standing caveats

- **The agent only works while both windows are up and the machine is awake.** Either one
  down, or a sleeping laptop, and the agent fails for everyone using it.
- **Dev tunnels are a Microsoft public preview with no SLA.** Before a demo with a date on
  it, host the tunnel and run Part C the day before — not an hour before.
- **The API key is one shared secret with no per-user identity.** A key holder can run
  unlimited simulations on this CPU. `pathjail.py` confines reads and writes; it does not
  and cannot limit that. Fixing it needs Entra or a hosted deployment, not a setting.
- **Leave `OPENSWMM_MCP_READ_ROOTS` unset** unless there is a specific reason. Every folder
  added is a folder a key holder can read. Writes are always confined to `scratch\` and
  cannot be widened.
- **Only one machine hosts at a time** — the agent holds exactly one address.
</content>
</invoke>
