# Reverse-proxy + HTTPS deployment guide

WobbleBot's `cli/web` daemon ships with **HTTPS termination out of
scope by design** (ADR-016 decision 7). The daemon binds to
`127.0.0.1:8000` by default and speaks plain HTTP on the loopback
interface. HTTPS, when needed, is the reverse proxy's job.

This guide explains the threat model so you can pick the right
posture for your deployment, plus copy-pasteable configs for the
common shapes.

## When you DON'T need HTTPS

**Default loopback-only setup (the v1.0 recommendation).** If
`cli/web` binds to `127.0.0.1:8000` and you access it via
`http://127.0.0.1:8000` in a browser on the same machine, traffic
**never leaves the loopback interface**. No physical network
medium carries the bytes; no Wi-Fi packet sniffer can see your
session cookie; no MITM is possible because there's nothing to be
in the middle of. **HTTPS adds nothing operational in this
scenario.** It's literally encryption of localhost-to-localhost
traffic.

This is the safe-by-default posture v1.0 ships with. If you only
ever interact with cli/web from the same machine where it runs,
stop reading here.

## When you DO need HTTPS

The moment `cli/web` becomes reachable from anything OTHER than
the same machine, you need HTTPS. Concrete triggers:

| Scenario | Why HTTPS |
|---|---|
| Bind to `0.0.0.0` for LAN access | Wi-Fi can be sniffed; LAN-resident threats (a roommate's compromised laptop) see your session cookie |
| Port-forward from your router to cli/web | Public-internet exposure of an auth surface — anyone scanning can attempt the login |
| Access via Tailscale / WireGuard / VPN | Already encrypted by the VPN, but browser warnings about plain HTTP are noisy; HTTPS removes them |
| Access from your phone on home Wi-Fi | LAN exposure even if you didn't realize it; same as the first row |

**The trade-control surface deserves serious attention.** Even
with HSTS + CSRF + bcrypt'd passwords, cli/web is a path from
"login form" to "pause/resume the live engine" to (via the
ADR-013 firewall) actual order placement. Treat its network
exposure with the gravity of any financial-app login.

## Three deployment shapes

The companion file [`Caddyfile.sample`](Caddyfile.sample) contains
ready-to-paste configs for these three options. Pick one:

### Option A — Public domain + Caddy + LetsEncrypt

You own a domain (`wobblebot.your-domain.com`) and want to reach
cli/web from anywhere on the internet. Caddy auto-provisions a
real LetsEncrypt cert and renews it on schedule.

**Required:** inbound ports 80 + 443 reachable from the public
internet (for cert renewal), DNS A-record pointing at your
host's public IP.

**Strongly recommended in addition:** firewall rule restricting
inbound 443 to specific operator IPs (your home / phone /
work). Otherwise the login page is a public attack surface even
behind HTTPS.

### Option B — LAN-only + Caddy internal CA

You want HTTPS for LAN clients (phone, tablet, second laptop)
without exposing cli/web to the public internet. Caddy's
internal CA mints a cert for your LAN IP. Browsers will warn on
first visit until you trust Caddy's root CA on each client
device (Caddy prints instructions on first launch).

**Required:** Caddy installed on the host where cli/web runs.
No public DNS, no LetsEncrypt, no router port-forward.

### Option C — Tailscale Serve (no Caddy needed)

Tailscale provides a free MagicDNS hostname + auto-cert via its
built-in ACME integration. Only devices on your tailnet can
reach cli/web. This is the **recommended posture for solo
operators** because it requires zero cert management, zero
firewall config, and uses Tailscale's identity model (cleaner
than IP allowlists).

```bash
# Single command to expose cli/web on Tailscale:
tailscale serve --https=443 --set-path=/ http://localhost:8000
```

That's it. cli/web stays bound to `127.0.0.1:8000`; Tailscale
serves the reverse proxy itself.

**Trade-off:** locks you into Tailscale's account model.

## Hardening recommendations regardless of option

Apply these on top of whatever reverse proxy you pick:

- **HSTS** — `Strict-Transport-Security: max-age=31536000;
  includeSubDomains` pins HTTPS for a year so accidental
  `http://` URLs don't fall back to plain transit.
- **Frame-busting** — `X-Frame-Options: DENY` prevents
  clickjacking via iframe embedding.
- **MIME-sniffing off** — `X-Content-Type-Options: nosniff`.
- **Referrer policy** — `strict-origin-when-cross-origin` is a
  reasonable default that limits leaked URLs.
- **IP allowlist at the firewall** (Option A only). The Caddy
  layer above doesn't filter; your router or cloud-VM firewall
  does. Locking inbound 443 to a small set of operator IPs
  removes 99% of the attack surface.

## What v1.0 does NOT ship

- App-side TLS in `cli/web`. ADR-016 places HTTPS termination at
  the reverse-proxy layer, not inside uvicorn.
- Cert management. That belongs to your operating environment
  (Caddy + LetsEncrypt, Tailscale's built-in ACME, etc.), not
  to the app.
- A bundled reverse-proxy container. Operators on Synology
  Portainer, bare metal, cloud VMs, and Tailscale-only setups
  all have different needs; bundling one would be wrong for
  most.

If you have feedback or want a fourth deployment shape
documented, open an issue.
