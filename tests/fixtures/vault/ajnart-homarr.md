---
type: repo-dossier
title: ajnart/homarr
url: "https://github.com/ajnart/homarr"
owner: ajnart
repo: homarr
tags: [captured, github, repo, dossier]
languages: [TypeScript, JavaScript]
license: MIT
capability_count: 8
comprehended: true
comprehend_model: claude-sonnet-4-6
comprehend_cost_usd: 1.1336
comprehended_at: 2026-07-12
origin:
  source: github
  source_method: worker
  ingest_id: 1e67dee6f1b5442983a928f37a
  captured_at: "2026-07-12T17:58:57Z"
  captured_by: default
  source_url: "https://github.com/ajnart/homarr"
  extra:
    owner: ajnart
    repo: homarr
---
# ajnart/homarr — project view

Source: <https://github.com/ajnart/homarr>

> Homarr (ajnart/homarr v0.16.0) is a self-hosted, MIT-licensed Next.js 13 dashboard that lets homelab operators organize shortcuts to their services on a drag-and-drop grid and pull live data from those services via a rich set of widgets.

## What it does

Homarr serves as a configurable 'start page' for a self-hosted stack. Boards (stored as JSON files under `data/configs/`) hold **app tiles** — clickable shortcut cards with optional HTTP ping — and **widgets** that actively query services such as Plex/Jellyfin session info, Pi-hole/AdGuard stats, torrent client queues, Sonarr/Radarr release calendars, Home Assistant entity state, Proxmox cluster resources, RSS feeds, and weather. A tRPC API layer mediates all data flow between the Next.js front-end and external services. Authentication supports local credentials, LDAP, and OIDC; roles are owner/admin/user/guest. User and session data live in SQLite (Drizzle ORM); board layout data lives in JSON. The UI uses Mantine 6 + GridStack for the drag-and-drop grid.

## Architecture

**Front-end** (`src/pages/`, `src/components/`, `src/widgets/`): Next.js pages render boards by reading the JSON config from the tRPC `boards` router. The grid is powered by `fily-publish-gridstack` (a custom GridStack publish), wrapped in a Zustand store (`src/components/Dashboard/Wrappers/gridstack/store.tsx`). Each tile — app or widget — is a React component that fires its own tRPC query on mount; widgets re-fetch on an interval.

**API layer** (`src/server/api/`): A single tRPC root router (`src/server/api/root.ts`) composes ~20 sub-routers, one per domain (weather, docker, rss, dnsHole, etc.). Procedures are tiered as `publicProcedure`, `protectedProcedure` (session required), and `adminProcedure` (admin flag required). The API is also exposed as a REST/OpenAPI surface via `trpc-openapi`.

**Data layer**: Two persistence mechanisms coexist. (1) JSON files in `data/configs/<name>.json` store board layouts, app definitions with integration credentials (plaintext), widgets, and visual settings. (2) SQLite via Drizzle ORM stores users, sessions, user settings, and invite tokens. The schema is in `src/server/db/schema.ts`.

**Auth** (`src/server/auth.ts`, `src/utils/auth/`): NextAuth.js with three pluggable providers — local credentials (bcrypt), LDAP (`ldapjs`), and OIDC. Sessions use the database strategy stored in SQLite.

**Service SDKs** (`src/tools/server/sdk/`): Thin HTTP wrappers around Pi-hole, AdGuard, Home Assistant, and Plex APIs. Third-party npm packages handle torrent clients (`@ctrl/*`) and Jellyfin (`@jellyfin/sdk`).

- **Languages:** TypeScript, JavaScript
- **Build system:** npm/yarn + Next.js (tsc-gated, no separate bundler step beyond `next build`)
- **Entry points:** `src/pages/_app.tsx - Next.js app shell, wraps every page with Mantine theme, React Query, tRPC, and i18n providers`, `src/pages/api/[...trpc].ts - tRPC HTTP handler; all API traffic enters here`, `src/pages/api/auth/[...nextauth].ts - NextAuth.js authentication handler`, `src/pages/b/[slug].tsx - Board display page (short-URL alias)`, `src/pages/board/[slug].tsx - Primary board display page`, `src/pages/onboard.tsx - First-run setup flow`, `cli/cli.js - Docker CLI helper for password resets (separate Node process)`

## Capabilities (8)

### Drag-and-Drop Dashboard Grid — portability: high

A responsive, multi-breakpoint (sm/md/lg) drag-and-drop tile grid backed by GridStack. Tile positions and sizes are serialised to JSON per breakpoint and persisted in the board config file.

- **Files:** `src/components/Dashboard/Wrappers/gridstack/init-gridstack.ts`, `src/components/Dashboard/Wrappers/gridstack/store.tsx`, `src/components/Dashboard/Wrappers/gridstack/use-gridstack.ts`, `src/components/Dashboard/Tiles/TileWrapper.tsx`
- **Dependencies:** fily-publish-gridstack, zustand, immer
- **Portability:** The layout schema (`{x, y, width, height}` per breakpoint in JSON) is completely self-contained. A clean-room reimplementation would pick any grid library (react-grid-layout, dnd-kit + CSS grid) and replicate the JSON serialisation pattern. The core idea — store `{sm, md, lg: {location: {x,y}, size: {width,height}}}` per tile — is trivially portable.

### App Tile with HTTP Health-Check Ping — portability: high

Each app tile can display an animated status dot (green/yellow/red) by polling a server-side proxy endpoint that fetches the app's URL with a configurable set of acceptable HTTP status codes.

- **Files:** `src/server/api/routers/app.ts`, `src/components/Dashboard/Tiles/Apps/AppPing.tsx`, `src/components/Dashboard/Tiles/Apps/AppTile.tsx`
- **Dependencies:** axios, next, @trpc/server
- **Portability:** The server-side proxy pattern (`GET /api/app/ping?id=...&configName=...`) is a ~60-line function. Key detail: it intentionally disables TLS cert verification (`rejectUnauthorized: false`) to reach self-signed homelab services — a deliberate choice, not a bug. Status code matching (`isStatusOk` in `AppPing.tsx:76`) allows non-200 codes to count as 'online'.
- **Security:** The ping endpoint accepts an app URL from the JSON config (not from user-supplied query params at request time), so SSRF is limited to what an admin has already configured. TLS verification disabled server-side is an accepted self-hosting tradeoff but worth documenting.

### Multi-Source Icon Library — portability: high

Aggregates icon lists from four sources (local uploads, walkxcode/dashboard-icons via GitHub API, Tabler Icons via unpkg, Papirus + homelab-svg-assets via jsDelivr) and exposes a unified search endpoint. Containers are auto-matched to icons by image name.

- **Files:** `src/server/api/routers/icon.ts`, `src/tools/server/images/abstract-icons-repository.ts`, `src/tools/server/images/github-icons-repository.ts`, `src/tools/server/images/jsdelivr-icons-repository.ts`, `src/tools/server/images/unpkg-icons-repository.ts`, `src/tools/server/images/local-icons-repository.ts`
- **Portability:** Each repository is a class extending `AbstractIconRepository` with a single `fetchInternally()` method that returns `{entries: [{url, name, size}], count, name, copyright}`. The abstraction is clean; adding or removing sources means adding/removing one class. The GitHub source hits the GitHub tree API and constructs CDN URLs — no auth token configured, so it silently fails for rate-limited scenarios.
- **Security:** Icons are loaded as external URLs; no server-side proxying of icon bytes. GitHub API is called without an auth token (rate-limit risk in high-traffic deployments).

### Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring) — portability: medium

A collection of tRPC routers that proxy authenticated requests to 18 external service types and normalise their responses for widget consumption. Covers weather (Open-Meteo), RSS parsing with XSS sanitisation, Pi-hole/AdGuard control, Sonarr/Radarr/Lidarr/Readarr calendars, Jellyfin/Plex active sessions, qBittorrent/Deluge/Transmission/SABnzbd/NZBGet queues, Overseerr/Jellyseerr request lists, Home Assistant entity state and automation triggering, Proxmox cluster stats, and OpenMediaVault system info.

- **Files:** `src/server/api/routers/weather.ts`, `src/server/api/routers/rss.ts`, `src/server/api/routers/dns-hole/router.ts`, `src/server/api/routers/calendar.ts`, `src/server/api/routers/media-server.ts`, `src/server/api/routers/download.ts`, `src/server/api/routers/smart-home/entity-state.ts`, `src/server/api/routers/health-monitoring/router.ts`, `src/server/api/routers/health-monitoring/proxmox.ts`, `src/server/api/routers/health-monitoring/openmediavault.ts`, `src/server/api/routers/media-request.ts`, `src/server/api/routers/indexer-manager.ts`, `src/tools/server/sdk/pihole/piHole.ts`, `src/tools/server/sdk/adGuard/adGuard.ts`, `src/tools/server/sdk/homeassistant/HomeAssistant.ts`, `src/tools/server/sdk/plex/plexClient.ts`
- **Dependencies:** @ctrl/deluge, @ctrl/qbittorrent, @ctrl/transmission, @ctrl/shared-torrent, @jellyfin/sdk, sabnzbd-api, nzbget-api, rss-parser, xss, html-entities, axios, dayjs
- **Portability:** Each integration is a standalone module (~50–200 lines) that takes a URL + credentials and returns a normalised data structure. The weather router (~40 lines, open-meteo.com, no API key) and RSS router (~120 lines, with XSS allow-list sanitisation) are the most self-contained and highest-value lifts. The Home Assistant SDK wrapper (`HomeAssistant.ts`, ~80 lines) is a clean REST client. The torrent normalisation in `download.ts` shows a good 'adapter pattern' for unifying heterogeneous clients behind one interface. The Proxmox cluster query (`proxmox.ts`) is ~110 lines of self-contained API calls.
- **Security:** Pi-hole API token is appended as a URL query parameter (`?auth=<token>`), which leaks into access logs and proxy logs. RSS content uses `xss` with a strict allow-list when the `dangerousAllowSanitizedItemContent` flag is set — safe as implemented, but the flag name is a deliberate warning to operators. Integration credentials (API keys, passwords) are stored in plaintext in JSON board config files on disk.

### Docker Container Management — portability: high

Lists all Docker containers (with auto-matched icons), and allows admins to start, stop, restart, or remove them via the Dockerode SDK connecting to the local Docker socket.

- **Files:** `src/server/api/routers/docker/router.ts`, `src/server/api/routers/docker/DockerSingleton.ts`, `src/components/Manage/Tools/Docker/ContainerTable.tsx`
- **Dependencies:** dockerode
- **Portability:** Dockerode abstracts the Docker socket. The router is ~95 lines; the key pattern is connecting with `new Dockerode({})` (inherits socket from environment) and calling `.listContainers({all: true})`. Clean-room reimplementation needs only a Docker socket client and four action handlers.
- **Security:** Access is gated behind `adminProcedure`. Docker socket access from within a container grants root-equivalent host access — this is standard for homelab dashboard tools but must be documented.

### Auth System (Local / LDAP / OIDC) — portability: medium

NextAuth.js-powered authentication with three providers: local credentials (bcrypt passwords stored in SQLite), LDAP (ldapjs, group-based role mapping to owner/admin), and OIDC. Roles (owner > admin > user > guest) gate tRPC procedures. Invite-link registration flow for new users.

- **Files:** `src/server/auth.ts`, `src/utils/auth/ldap.ts`, `src/utils/auth/oidc.ts`, `src/utils/auth/index.ts`, `src/server/db/schema.ts`, `src/server/api/routers/user.ts`, `src/server/api/routers/invite/invite-router.ts`
- **Dependencies:** next-auth, ldapjs, bcryptjs
- **Portability:** The LDAP integration (~200 lines in `ldap.ts`) is a well-structured reference for group-based role mapping. The tRPC middleware tier (`publicProcedure` / `protectedProcedure` / `adminProcedure` in `src/server/api/trpc.ts`) is a clean pattern for role-gating API endpoints. Invite token generation and expiry (`invite-router.ts`) is ~30 lines and fully self-contained.
- **Security:** Passwords stored with bcrypt+salt. Sessions use database strategy (token in SQLite, not JWT), which allows server-side revocation. The credentials flow uses a custom cookie workaround for NextAuth.js credential sessions (well-documented GitHub issue workaround at `src/server/auth.ts:77`). LDAP filter uses string interpolation of the username, but only after Zod `signInSchema` parsing — injection risk is minimal but worth re-checking in a reimplementation.

### Board Config Persistence (JSON File Store) — portability: high

Board layouts, app definitions (with integration credentials), widgets, and visual settings are stored as versioned JSON files in `data/configs/`. CRUD operations (create, rename, duplicate, delete, write) are handled by a thin file-system layer.

- **Files:** `src/tools/config/getConfig.ts`, `src/tools/config/writeConfig.ts`, `src/tools/config/readConfig.ts`, `src/server/api/routers/board.ts`, `data/configs/default.json`
- **Portability:** The JSON schema (see `data/configs/default.json`) is well-structured and self-documenting: top-level keys are `configProperties`, `categories`, `wrappers`, `apps`, `widgets`, `settings`. Each app/widget has a `shape` object with `sm/md/lg` breakpoint positions. Trivially portable as a data model for any dashboard that needs file-based persistence or import/export.
- **Security:** Board names used as filenames are validated by `configNameSchema` (Zod regex) before any `fs` operations, preventing path traversal. Integration credentials (API keys, passwords) live in plaintext in these files — acceptable for local-only deployments but a risk if config directory is accidentally exposed.

### Rich Text Notebook Widget — portability: high

A Tiptap-based rich text editor embedded as a dashboard tile. Supports headings, bold, italic, lists, task lists, tables, images, highlights, links, code blocks, and custom text colours. Content is stored inside the board JSON config.

- **Files:** `src/widgets/notebook/NotebookEditor.tsx`, `src/widgets/notebook/NotebookWidgetTile.tsx`, `src/server/api/routers/notebook.ts`
- **Dependencies:** @tiptap/react, @tiptap/starter-kit, @tiptap/extension-*, @mantine/tiptap
- **Portability:** Tiptap is framework-agnostic (ProseMirror underneath). The noteworthy implementation detail is that content is saved back to the board JSON config via a tRPC mutation, making the tile self-contained with no separate note storage. A reimplementation could use any ProseMirror-based editor.
- **Security:** HTML content is stored server-side and re-rendered client-side. No server-side sanitisation on save/load — XSS risk if attacker can write the config file directly, but that implies full host compromise anyway.

## Dependencies & license

- **License:** MIT

**Notable dependencies:**
- next@13.5.11 - Next.js, pinned to a specific patch; 13.x reached security-fix-only status in 2024
- next-auth@^4.23.0 - Authentication, v4 (not v5/Auth.js); stable but unmaintained track
- drizzle-orm@^0.28.6 - SQLite ORM via better-sqlite3
- fily-publish-gridstack@^0.0.13 - A custom/forked GridStack publish on npm; extremely low version number, not the upstream gridstack package
- @ctrl/deluge, @ctrl/qbittorrent, @ctrl/transmission - Torrent client adapters
- @jellyfin/sdk@^0.8.0 - Official Jellyfin TypeScript SDK
- ldapjs@^3.0.5 - LDAP client used for LDAP auth
- dockerode@^3.3.2 - Docker socket client
- adm-zip@^0.5.15 - Zip archiving; used in migration tooling, worth checking for zip-slip on extracted paths
- xss@^1.0.14 - HTML sanitiser used on RSS content
- rss-parser@^3.12.0 - RSS/Atom parser
- better-sqlite3@^12.6.2 - Synchronous SQLite driver

**Supply-chain flags:**
- fily-publish-gridstack@0.0.13 — version 0.0.x on npm from an unknown publisher; upstream is gridstack.js by gridstack.io. Not obviously the official package. Low download count, should be verified before adoption.
- next@13.5.11 is pinned to a specific patch behind the current Next.js 14/15 line; known CVEs in older Next.js versions should be checked against this pin.
- No postinstall scripts or vendored binaries observed in package.json.
- Dockerfile installs apt packages (openssl, wget) at build time with no pinned versions.

## Security review

**Concerns:**
- Integration credentials (API keys, passwords) stored in plaintext JSON config files on disk — whoever can read `data/configs/*.json` gets all secrets.
- Pi-hole API token embedded as URL query parameter in every API call (`piHole.ts:17`) — leaks into server access logs and reverse-proxy logs.
- TLS verification disabled (`rejectUnauthorized: false`) for the app-ping endpoint (`src/server/api/routers/app.ts:41`) — intentional for self-signed certs but disables certificate chain validation.
- Docker socket access from the container grants root-equivalent host privileges; no additional sandboxing beyond the `adminProcedure` RBAC gate.
- adm-zip is present in dependencies; zip-slip vulnerability risk if user-controlled archive paths are extracted without normalisation (usage context not fully traced).

**Network egress:** Server makes outbound calls to: `api.open-meteo.com` and `geocoding-api.open-meteo.com` (weather, no key), `api.github.com` (icon repository listing, unauthenticated), `cdn.jsdelivr.net` and `unpkg.com` (icon CDNs), `image.tmdb.org` (Overseerr poster images), and operator-configured internal service URLs (Pi-hole, Sonarr, Jellyfin, etc.).

**Dangerous patterns:**
- RegExp.exec() in `src/server/api/routers/board.ts:201` — this is a JS regex method, NOT shell exec; no actual shell execution found.
- String interpolation used to construct LDAP search filter in `src/utils/auth/ldap.ts:118` — mitigated by Zod schema validation of the username before interpolation, but worth auditing in any reimplementation.
- No eval(), child_process, or shell spawning detected anywhere in the TypeScript source.

## Salvage into your fleet

| Capability | Project | How |
| --- | --- | --- |
| Multi-Source Icon Library | homelab-console | Homelab console needs service icons; the four-repository aggregation pattern (GitHub tree API + jsDelivr + unpkg + local) maps directly, providing thousands of homelab service icons without hosting them yourself. |
| App Tile with HTTP Health-Check Ping | homelab-console | Server-side HTTP proxy ping with configurable accepted status codes is exactly the pattern needed for a homelab status panel — avoids CORS on the client and handles self-signed certs. |
| Docker Container Management | homelab-console | The 95-line Dockerode router (list + start/stop/restart/remove) is a self-contained reference implementation for homelab-console's container management panel. |
| Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring) | homelab-console | The Proxmox cluster monitor, Pi-hole/AdGuard summary+control, and Home Assistant entity state routers are the most relevant to a homelab console; each is a small, self-contained module. |
| App Tile with HTTP Health-Check Ping | uptime-monitor | The configurable-status-code HTTP health-check pattern (with timeout, TLS bypass option, and online/offline state reduction) is the core primitive for an uptime monitor. |
| Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring) | media-hub | The Jellyfin/Plex session normaliser (`media-server.ts`), the Sonarr/Radarr/Lidarr calendar aggregator (`calendar.ts`), and the Overseerr request list (`media-request.ts`) are clean reference implementations for a media hub's 'now playing' and 'upcoming releases' features. |
| Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring) | ops-cockpit | Proxmox cluster resource normalisation (`proxmox.ts`) and OpenMediaVault RPC calls (`openmediavault.ts`) directly address ops-cockpit's need to surface hypervisor and NAS health data. |
| Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring) | family-hub-v2 | The weather widget pattern (Open-Meteo, no API key, free) and the RSS aggregator with XSS sanitisation are immediately useful for a family dashboard showing news feeds and local weather. |
| Rich Text Notebook Widget | second-brain | Tiptap editor configuration with task lists, tables, highlights, and colour — all the extensions wired together in `NotebookEditor.tsx` — is a useful reference for adding an embeddable rich text editor to second-brain. |
| Auth System (Local / LDAP / OIDC) | homelab-console | The three-tier tRPC procedure pattern (public / protected / admin) and the LDAP group-to-role mapping in `ldap.ts` are clean references for adding enterprise auth to a homelab console without NextAuth. |
| Drag-and-Drop Dashboard Grid | homelab-console | The JSON layout schema (`{sm, md, lg: {location: {x,y}, size: {width,height}}}` per tile) and the GridStack initialisation wrapper are direct references for building a configurable homelab dashboard grid. |

**Recommendations:**
- **Weather widget (Open-Meteo)** — `src/server/api/routers/weather.ts` is 68 lines, hits a free API with no key, and returns a clean Zod-validated response. Reimplement this verbatim (sans tRPC wrapper) in any project needing a weather data source. Useful in `family-hub-v2`, `ops-cockpit`.
- **RSS aggregator with XSS sanitisation** — `src/server/api/routers/rss.ts` shows the right way to fetch, parse, and sanitise RSS HTML before surfacing it to a client: `rss-parser` for XML, `xss` with a strict allow-list for HTML content, `html-entities` for plain text fallback. The pattern is directly portable to `family-hub-v2` or `second-brain`.
- **Three-tier RBAC API procedure pattern** — `src/server/api/trpc.ts` (public / protected / admin middleware) is the cleanest implementation of role-gating in a tRPC app. Worth copying verbatim into any fleet project that uses tRPC.
- **HTTP health-check ping proxy** — `src/server/api/routers/app.ts` + `isStatusOk` logic in `AppPing.tsx:76` shows how to do server-side HTTP pinging with configurable acceptable status codes and TLS bypass. Reimplementation target for `uptime-monitor` and `homelab-console`.
- **Icon repository abstraction** — The `AbstractIconRepository` + four concrete implementations in `src/tools/server/images/` demonstrate how to federate icons from GitHub tree API, jsDelivr, and unpkg into a single normalised list. Worth lifting the abstract pattern and the walkxcode/dashboard-icons source for any homelab UI project.
- **Proxmox cluster resource normaliser** — `src/server/api/routers/health-monitoring/proxmox.ts` (~110 lines) fetches `api2/json/cluster/resources` and normalises VMs, LXCs, nodes, and storage into a typed structure. Self-contained and directly useful in `ops-cockpit` or `homelab-console`.
- **Pi-hole / AdGuard control client** — `src/tools/server/sdk/pihole/piHole.ts` and `adGuard.ts` are clean HTTP client wrappers. Note: fix the Pi-hole token-in-URL pattern when reimplementing — pass it as a header or POST body instead.
- **Home Assistant REST client** — `src/tools/server/sdk/homeassistant/HomeAssistant.ts` (~80 lines) covers entity state read, automation trigger, and toggle — the three operations most dashboards need. Lift directly into `homelab-console`.
- **Board layout JSON schema** — The `data/configs/default.json` schema (apps with `{sm,md,lg}` shape, widgets with properties, settings with customization) is a well-designed, human-readable config format for a dashboard. Worth adopting as the storage format for `homelab-console` rather than designing one from scratch.
- **Invite-link registration** — `src/server/api/routers/invite/invite-router.ts` shows a clean invite token generate/consume/expire pattern (random 32-byte hex, 24h expiry, single-use) that is directly applicable to any fleet project with controlled registration.

## Raw analysis (machine-readable)

<details><summary>dossier.json</summary>

```json
{
  "summary": "Homarr (ajnart/homarr v0.16.0) is a self-hosted, MIT-licensed Next.js 13 dashboard that lets homelab operators organize shortcuts to their services on a drag-and-drop grid and pull live data from those services via a rich set of widgets.",
  "what_it_does": "Homarr serves as a configurable 'start page' for a self-hosted stack. Boards (stored as JSON files under `data/configs/`) hold **app tiles** — clickable shortcut cards with optional HTTP ping — and **widgets** that actively query services such as Plex/Jellyfin session info, Pi-hole/AdGuard stats, torrent client queues, Sonarr/Radarr release calendars, Home Assistant entity state, Proxmox cluster resources, RSS feeds, and weather. A tRPC API layer mediates all data flow between the Next.js front-end and external services. Authentication supports local credentials, LDAP, and OIDC; roles are owner/admin/user/guest. User and session data live in SQLite (Drizzle ORM); board layout data lives in JSON. The UI uses Mantine 6 + GridStack for the drag-and-drop grid.",
  "languages": [
    "TypeScript",
    "JavaScript"
  ],
  "build_system": "npm/yarn + Next.js (tsc-gated, no separate bundler step beyond `next build`)",
  "entry_points": [
    "src/pages/_app.tsx - Next.js app shell, wraps every page with Mantine theme, React Query, tRPC, and i18n providers",
    "src/pages/api/[...trpc].ts - tRPC HTTP handler; all API traffic enters here",
    "src/pages/api/auth/[...nextauth].ts - NextAuth.js authentication handler",
    "src/pages/b/[slug].tsx - Board display page (short-URL alias)",
    "src/pages/board/[slug].tsx - Primary board display page",
    "src/pages/onboard.tsx - First-run setup flow",
    "cli/cli.js - Docker CLI helper for password resets (separate Node process)"
  ],
  "architecture": "**Front-end** (`src/pages/`, `src/components/`, `src/widgets/`): Next.js pages render boards by reading the JSON config from the tRPC `boards` router. The grid is powered by `fily-publish-gridstack` (a custom GridStack publish), wrapped in a Zustand store (`src/components/Dashboard/Wrappers/gridstack/store.tsx`). Each tile — app or widget — is a React component that fires its own tRPC query on mount; widgets re-fetch on an interval.\n\n**API layer** (`src/server/api/`): A single tRPC root router (`src/server/api/root.ts`) composes ~20 sub-routers, one per domain (weather, docker, rss, dnsHole, etc.). Procedures are tiered as `publicProcedure`, `protectedProcedure` (session required), and `adminProcedure` (admin flag required). The API is also exposed as a REST/OpenAPI surface via `trpc-openapi`.\n\n**Data layer**: Two persistence mechanisms coexist. (1) JSON files in `data/configs/<name>.json` store board layouts, app definitions with integration credentials (plaintext), widgets, and visual settings. (2) SQLite via Drizzle ORM stores users, sessions, user settings, and invite tokens. The schema is in `src/server/db/schema.ts`.\n\n**Auth** (`src/server/auth.ts`, `src/utils/auth/`): NextAuth.js with three pluggable providers — local credentials (bcrypt), LDAP (`ldapjs`), and OIDC. Sessions use the database strategy stored in SQLite.\n\n**Service SDKs** (`src/tools/server/sdk/`): Thin HTTP wrappers around Pi-hole, AdGuard, Home Assistant, and Plex APIs. Third-party npm packages handle torrent clients (`@ctrl/*`) and Jellyfin (`@jellyfin/sdk`).",
  "capabilities": [
    {
      "name": "Drag-and-Drop Dashboard Grid",
      "what": "A responsive, multi-breakpoint (sm/md/lg) drag-and-drop tile grid backed by GridStack. Tile positions and sizes are serialised to JSON per breakpoint and persisted in the board config file.",
      "files": [
        "src/components/Dashboard/Wrappers/gridstack/init-gridstack.ts",
        "src/components/Dashboard/Wrappers/gridstack/store.tsx",
        "src/components/Dashboard/Wrappers/gridstack/use-gridstack.ts",
        "src/components/Dashboard/Tiles/TileWrapper.tsx"
      ],
      "dependencies": [
        "fily-publish-gridstack",
        "zustand",
        "immer"
      ],
      "portability": "high",
      "portability_notes": "The layout schema (`{x, y, width, height}` per breakpoint in JSON) is completely self-contained. A clean-room reimplementation would pick any grid library (react-grid-layout, dnd-kit + CSS grid) and replicate the JSON serialisation pattern. The core idea — store `{sm, md, lg: {location: {x,y}, size: {width,height}}}` per tile — is trivially portable.",
      "security_notes": ""
    },
    {
      "name": "App Tile with HTTP Health-Check Ping",
      "what": "Each app tile can display an animated status dot (green/yellow/red) by polling a server-side proxy endpoint that fetches the app's URL with a configurable set of acceptable HTTP status codes.",
      "files": [
        "src/server/api/routers/app.ts",
        "src/components/Dashboard/Tiles/Apps/AppPing.tsx",
        "src/components/Dashboard/Tiles/Apps/AppTile.tsx"
      ],
      "dependencies": [
        "axios",
        "next",
        "@trpc/server"
      ],
      "portability": "high",
      "portability_notes": "The server-side proxy pattern (`GET /api/app/ping?id=...&configName=...`) is a ~60-line function. Key detail: it intentionally disables TLS cert verification (`rejectUnauthorized: false`) to reach self-signed homelab services — a deliberate choice, not a bug. Status code matching (`isStatusOk` in `AppPing.tsx:76`) allows non-200 codes to count as 'online'.",
      "security_notes": "The ping endpoint accepts an app URL from the JSON config (not from user-supplied query params at request time), so SSRF is limited to what an admin has already configured. TLS verification disabled server-side is an accepted self-hosting tradeoff but worth documenting."
    },
    {
      "name": "Multi-Source Icon Library",
      "what": "Aggregates icon lists from four sources (local uploads, walkxcode/dashboard-icons via GitHub API, Tabler Icons via unpkg, Papirus + homelab-svg-assets via jsDelivr) and exposes a unified search endpoint. Containers are auto-matched to icons by image name.",
      "files": [
        "src/server/api/routers/icon.ts",
        "src/tools/server/images/abstract-icons-repository.ts",
        "src/tools/server/images/github-icons-repository.ts",
        "src/tools/server/images/jsdelivr-icons-repository.ts",
        "src/tools/server/images/unpkg-icons-repository.ts",
        "src/tools/server/images/local-icons-repository.ts"
      ],
      "dependencies": [],
      "portability": "high",
      "portability_notes": "Each repository is a class extending `AbstractIconRepository` with a single `fetchInternally()` method that returns `{entries: [{url, name, size}], count, name, copyright}`. The abstraction is clean; adding or removing sources means adding/removing one class. The GitHub source hits the GitHub tree API and constructs CDN URLs — no auth token configured, so it silently fails for rate-limited scenarios.",
      "security_notes": "Icons are loaded as external URLs; no server-side proxying of icon bytes. GitHub API is called without an auth token (rate-limit risk in high-traffic deployments)."
    },
    {
      "name": "Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring)",
      "what": "A collection of tRPC routers that proxy authenticated requests to 18 external service types and normalise their responses for widget consumption. Covers weather (Open-Meteo), RSS parsing with XSS sanitisation, Pi-hole/AdGuard control, Sonarr/Radarr/Lidarr/Readarr calendars, Jellyfin/Plex active sessions, qBittorrent/Deluge/Transmission/SABnzbd/NZBGet queues, Overseerr/Jellyseerr request lists, Home Assistant entity state and automation triggering, Proxmox cluster stats, and OpenMediaVault system info.",
      "files": [
        "src/server/api/routers/weather.ts",
        "src/server/api/routers/rss.ts",
        "src/server/api/routers/dns-hole/router.ts",
        "src/server/api/routers/calendar.ts",
        "src/server/api/routers/media-server.ts",
        "src/server/api/routers/download.ts",
        "src/server/api/routers/smart-home/entity-state.ts",
        "src/server/api/routers/health-monitoring/router.ts",
        "src/server/api/routers/health-monitoring/proxmox.ts",
        "src/server/api/routers/health-monitoring/openmediavault.ts",
        "src/server/api/routers/media-request.ts",
        "src/server/api/routers/indexer-manager.ts",
        "src/tools/server/sdk/pihole/piHole.ts",
        "src/tools/server/sdk/adGuard/adGuard.ts",
        "src/tools/server/sdk/homeassistant/HomeAssistant.ts",
        "src/tools/server/sdk/plex/plexClient.ts"
      ],
      "dependencies": [
        "@ctrl/deluge",
        "@ctrl/qbittorrent",
        "@ctrl/transmission",
        "@ctrl/shared-torrent",
        "@jellyfin/sdk",
        "sabnzbd-api",
        "nzbget-api",
        "rss-parser",
        "xss",
        "html-entities",
        "axios",
        "dayjs"
      ],
      "portability": "medium",
      "portability_notes": "Each integration is a standalone module (~50–200 lines) that takes a URL + credentials and returns a normalised data structure. The weather router (~40 lines, open-meteo.com, no API key) and RSS router (~120 lines, with XSS allow-list sanitisation) are the most self-contained and highest-value lifts. The Home Assistant SDK wrapper (`HomeAssistant.ts`, ~80 lines) is a clean REST client. The torrent normalisation in `download.ts` shows a good 'adapter pattern' for unifying heterogeneous clients behind one interface. The Proxmox cluster query (`proxmox.ts`) is ~110 lines of self-contained API calls.",
      "security_notes": "Pi-hole API token is appended as a URL query parameter (`?auth=<token>`), which leaks into access logs and proxy logs. RSS content uses `xss` with a strict allow-list when the `dangerousAllowSanitizedItemContent` flag is set — safe as implemented, but the flag name is a deliberate warning to operators. Integration credentials (API keys, passwords) are stored in plaintext in JSON board config files on disk."
    },
    {
      "name": "Docker Container Management",
      "what": "Lists all Docker containers (with auto-matched icons), and allows admins to start, stop, restart, or remove them via the Dockerode SDK connecting to the local Docker socket.",
      "files": [
        "src/server/api/routers/docker/router.ts",
        "src/server/api/routers/docker/DockerSingleton.ts",
        "src/components/Manage/Tools/Docker/ContainerTable.tsx"
      ],
      "dependencies": [
        "dockerode"
      ],
      "portability": "high",
      "portability_notes": "Dockerode abstracts the Docker socket. The router is ~95 lines; the key pattern is connecting with `new Dockerode({})` (inherits socket from environment) and calling `.listContainers({all: true})`. Clean-room reimplementation needs only a Docker socket client and four action handlers.",
      "security_notes": "Access is gated behind `adminProcedure`. Docker socket access from within a container grants root-equivalent host access — this is standard for homelab dashboard tools but must be documented."
    },
    {
      "name": "Auth System (Local / LDAP / OIDC)",
      "what": "NextAuth.js-powered authentication with three providers: local credentials (bcrypt passwords stored in SQLite), LDAP (ldapjs, group-based role mapping to owner/admin), and OIDC. Roles (owner > admin > user > guest) gate tRPC procedures. Invite-link registration flow for new users.",
      "files": [
        "src/server/auth.ts",
        "src/utils/auth/ldap.ts",
        "src/utils/auth/oidc.ts",
        "src/utils/auth/index.ts",
        "src/server/db/schema.ts",
        "src/server/api/routers/user.ts",
        "src/server/api/routers/invite/invite-router.ts"
      ],
      "dependencies": [
        "next-auth",
        "ldapjs",
        "bcryptjs"
      ],
      "portability": "medium",
      "portability_notes": "The LDAP integration (~200 lines in `ldap.ts`) is a well-structured reference for group-based role mapping. The tRPC middleware tier (`publicProcedure` / `protectedProcedure` / `adminProcedure` in `src/server/api/trpc.ts`) is a clean pattern for role-gating API endpoints. Invite token generation and expiry (`invite-router.ts`) is ~30 lines and fully self-contained.",
      "security_notes": "Passwords stored with bcrypt+salt. Sessions use database strategy (token in SQLite, not JWT), which allows server-side revocation. The credentials flow uses a custom cookie workaround for NextAuth.js credential sessions (well-documented GitHub issue workaround at `src/server/auth.ts:77`). LDAP filter uses string interpolation of the username, but only after Zod `signInSchema` parsing — injection risk is minimal but worth re-checking in a reimplementation."
    },
    {
      "name": "Board Config Persistence (JSON File Store)",
      "what": "Board layouts, app definitions (with integration credentials), widgets, and visual settings are stored as versioned JSON files in `data/configs/`. CRUD operations (create, rename, duplicate, delete, write) are handled by a thin file-system layer.",
      "files": [
        "src/tools/config/getConfig.ts",
        "src/tools/config/writeConfig.ts",
        "src/tools/config/readConfig.ts",
        "src/server/api/routers/board.ts",
        "data/configs/default.json"
      ],
      "dependencies": [],
      "portability": "high",
      "portability_notes": "The JSON schema (see `data/configs/default.json`) is well-structured and self-documenting: top-level keys are `configProperties`, `categories`, `wrappers`, `apps`, `widgets`, `settings`. Each app/widget has a `shape` object with `sm/md/lg` breakpoint positions. Trivially portable as a data model for any dashboard that needs file-based persistence or import/export.",
      "security_notes": "Board names used as filenames are validated by `configNameSchema` (Zod regex) before any `fs` operations, preventing path traversal. Integration credentials (API keys, passwords) live in plaintext in these files — acceptable for local-only deployments but a risk if config directory is accidentally exposed."
    },
    {
      "name": "Rich Text Notebook Widget",
      "what": "A Tiptap-based rich text editor embedded as a dashboard tile. Supports headings, bold, italic, lists, task lists, tables, images, highlights, links, code blocks, and custom text colours. Content is stored inside the board JSON config.",
      "files": [
        "src/widgets/notebook/NotebookEditor.tsx",
        "src/widgets/notebook/NotebookWidgetTile.tsx",
        "src/server/api/routers/notebook.ts"
      ],
      "dependencies": [
        "@tiptap/react",
        "@tiptap/starter-kit",
        "@tiptap/extension-*",
        "@mantine/tiptap"
      ],
      "portability": "high",
      "portability_notes": "Tiptap is framework-agnostic (ProseMirror underneath). The noteworthy implementation detail is that content is saved back to the board JSON config via a tRPC mutation, making the tile self-contained with no separate note storage. A reimplementation could use any ProseMirror-based editor.",
      "security_notes": "HTML content is stored server-side and re-rendered client-side. No server-side sanitisation on save/load — XSS risk if attacker can write the config file directly, but that implies full host compromise anyway."
    }
  ],
  "dependencies": {
    "license": "MIT",
    "notable": [
      "next@13.5.11 - Next.js, pinned to a specific patch; 13.x reached security-fix-only status in 2024",
      "next-auth@^4.23.0 - Authentication, v4 (not v5/Auth.js); stable but unmaintained track",
      "drizzle-orm@^0.28.6 - SQLite ORM via better-sqlite3",
      "fily-publish-gridstack@^0.0.13 - A custom/forked GridStack publish on npm; extremely low version number, not the upstream gridstack package",
      "@ctrl/deluge, @ctrl/qbittorrent, @ctrl/transmission - Torrent client adapters",
      "@jellyfin/sdk@^0.8.0 - Official Jellyfin TypeScript SDK",
      "ldapjs@^3.0.5 - LDAP client used for LDAP auth",
      "dockerode@^3.3.2 - Docker socket client",
      "adm-zip@^0.5.15 - Zip archiving; used in migration tooling, worth checking for zip-slip on extracted paths",
      "xss@^1.0.14 - HTML sanitiser used on RSS content",
      "rss-parser@^3.12.0 - RSS/Atom parser",
      "better-sqlite3@^12.6.2 - Synchronous SQLite driver"
    ],
    "supply_chain_flags": [
      "fily-publish-gridstack@0.0.13 — version 0.0.x on npm from an unknown publisher; upstream is gridstack.js by gridstack.io. Not obviously the official package. Low download count, should be verified before adoption.",
      "next@13.5.11 is pinned to a specific patch behind the current Next.js 14/15 line; known CVEs in older Next.js versions should be checked against this pin.",
      "No postinstall scripts or vendored binaries observed in package.json.",
      "Dockerfile installs apt packages (openssl, wget) at build time with no pinned versions."
    ]
  },
  "security": {
    "concerns": [
      "Integration credentials (API keys, passwords) stored in plaintext JSON config files on disk — whoever can read `data/configs/*.json` gets all secrets.",
      "Pi-hole API token embedded as URL query parameter in every API call (`piHole.ts:17`) — leaks into server access logs and reverse-proxy logs.",
      "TLS verification disabled (`rejectUnauthorized: false`) for the app-ping endpoint (`src/server/api/routers/app.ts:41`) — intentional for self-signed certs but disables certificate chain validation.",
      "Docker socket access from the container grants root-equivalent host privileges; no additional sandboxing beyond the `adminProcedure` RBAC gate.",
      "adm-zip is present in dependencies; zip-slip vulnerability risk if user-controlled archive paths are extracted without normalisation (usage context not fully traced)."
    ],
    "network_egress": "Server makes outbound calls to: `api.open-meteo.com` and `geocoding-api.open-meteo.com` (weather, no key), `api.github.com` (icon repository listing, unauthenticated), `cdn.jsdelivr.net` and `unpkg.com` (icon CDNs), `image.tmdb.org` (Overseerr poster images), and operator-configured internal service URLs (Pi-hole, Sonarr, Jellyfin, etc.).",
    "dangerous_patterns": [
      "RegExp.exec() in `src/server/api/routers/board.ts:201` — this is a JS regex method, NOT shell exec; no actual shell execution found.",
      "String interpolation used to construct LDAP search filter in `src/utils/auth/ldap.ts:118` — mitigated by Zod schema validation of the username before interpolation, but worth auditing in any reimplementation.",
      "No eval(), child_process, or shell spawning detected anywhere in the TypeScript source."
    ]
  },
  "fit_to_fleet": [
    {
      "project": "homelab-console",
      "capability": "Multi-Source Icon Library",
      "how": "Homelab console needs service icons; the four-repository aggregation pattern (GitHub tree API + jsDelivr + unpkg + local) maps directly, providing thousands of homelab service icons without hosting them yourself."
    },
    {
      "project": "homelab-console",
      "capability": "App Tile with HTTP Health-Check Ping",
      "how": "Server-side HTTP proxy ping with configurable accepted status codes is exactly the pattern needed for a homelab status panel — avoids CORS on the client and handles self-signed certs."
    },
    {
      "project": "homelab-console",
      "capability": "Docker Container Management",
      "how": "The 95-line Dockerode router (list + start/stop/restart/remove) is a self-contained reference implementation for homelab-console's container management panel."
    },
    {
      "project": "homelab-console",
      "capability": "Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring)",
      "how": "The Proxmox cluster monitor, Pi-hole/AdGuard summary+control, and Home Assistant entity state routers are the most relevant to a homelab console; each is a small, self-contained module."
    },
    {
      "project": "uptime-monitor",
      "capability": "App Tile with HTTP Health-Check Ping",
      "how": "The configurable-status-code HTTP health-check pattern (with timeout, TLS bypass option, and online/offline state reduction) is the core primitive for an uptime monitor."
    },
    {
      "project": "media-hub",
      "capability": "Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring)",
      "how": "The Jellyfin/Plex session normaliser (`media-server.ts`), the Sonarr/Radarr/Lidarr calendar aggregator (`calendar.ts`), and the Overseerr request list (`media-request.ts`) are clean reference implementations for a media hub's 'now playing' and 'upcoming releases' features."
    },
    {
      "project": "ops-cockpit",
      "capability": "Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring)",
      "how": "Proxmox cluster resource normalisation (`proxmox.ts`) and OpenMediaVault RPC calls (`openmediavault.ts`) directly address ops-cockpit's need to surface hypervisor and NAS health data."
    },
    {
      "project": "family-hub-v2",
      "capability": "Service Integration Hub (Weather, RSS, DNS Hole, Media Calendar, Media Server, Download Clients, Smart Home, Health Monitoring)",
      "how": "The weather widget pattern (Open-Meteo, no API key, free) and the RSS aggregator with XSS sanitisation are immediately useful for a family dashboard showing news feeds and local weather."
    },
    {
      "project": "second-brain",
      "capability": "Rich Text Notebook Widget",
      "how": "Tiptap editor configuration with task lists, tables, highlights, and colour — all the extensions wired together in `NotebookEditor.tsx` — is a useful reference for adding an embeddable rich text editor to second-brain."
    },
    {
      "project": "homelab-console",
      "capability": "Auth System (Local / LDAP / OIDC)",
      "how": "The three-tier tRPC procedure pattern (public / protected / admin) and the LDAP group-to-role mapping in `ldap.ts` are clean references for adding enterprise auth to a homelab console without NextAuth."
    },
    {
      "project": "homelab-console",
      "capability": "Drag-and-Drop Dashboard Grid",
      "how": "The JSON layout schema (`{sm, md, lg: {location: {x,y}, size: {width,height}}}` per tile) and the GridStack initialisation wrapper are direct references for building a configurable homelab dashboard grid."
    }
  ],
  "salvage_recommendations": [
    "**Weather widget (Open-Meteo)** — `src/server/api/routers/weather.ts` is 68 lines, hits a free API with no key, and returns a clean Zod-validated response. Reimplement this verbatim (sans tRPC wrapper) in any project needing a weather data source. Useful in `family-hub-v2`, `ops-cockpit`.",
    "**RSS aggregator with XSS sanitisation** — `src/server/api/routers/rss.ts` shows the right way to fetch, parse, and sanitise RSS HTML before surfacing it to a client: `rss-parser` for XML, `xss` with a strict allow-list for HTML content, `html-entities` for plain text fallback. The pattern is directly portable to `family-hub-v2` or `second-brain`.",
    "**Three-tier RBAC API procedure pattern** — `src/server/api/trpc.ts` (public / protected / admin middleware) is the cleanest implementation of role-gating in a tRPC app. Worth copying verbatim into any fleet project that uses tRPC.",
    "**HTTP health-check ping proxy** — `src/server/api/routers/app.ts` + `isStatusOk` logic in `AppPing.tsx:76` shows how to do server-side HTTP pinging with configurable acceptable status codes and TLS bypass. Reimplementation target for `uptime-monitor` and `homelab-console`.",
    "**Icon repository abstraction** — The `AbstractIconRepository` + four concrete implementations in `src/tools/server/images/` demonstrate how to federate icons from GitHub tree API, jsDelivr, and unpkg into a single normalised list. Worth lifting the abstract pattern and the walkxcode/dashboard-icons source for any homelab UI project.",
    "**Proxmox cluster resource normaliser** — `src/server/api/routers/health-monitoring/proxmox.ts` (~110 lines) fetches `api2/json/cluster/resources` and normalises VMs, LXCs, nodes, and storage into a typed structure. Self-contained and directly useful in `ops-cockpit` or `homelab-console`.",
    "**Pi-hole / AdGuard control client** — `src/tools/server/sdk/pihole/piHole.ts` and `adGuard.ts` are clean HTTP client wrappers. Note: fix the Pi-hole token-in-URL pattern when reimplementing — pass it as a header or POST body instead.",
    "**Home Assistant REST client** — `src/tools/server/sdk/homeassistant/HomeAssistant.ts` (~80 lines) covers entity state read, automation trigger, and toggle — the three operations most dashboards need. Lift directly into `homelab-console`.",
    "**Board layout JSON schema** — The `data/configs/default.json` schema (apps with `{sm,md,lg}` shape, widgets with properties, settings with customization) is a well-designed, human-readable config format for a dashboard. Worth adopting as the storage format for `homelab-console` rather than designing one from scratch.",
    "**Invite-link registration** — `src/server/api/routers/invite/invite-router.ts` shows a clean invite token generate/consume/expire pattern (random 32-byte hex, 24h expiry, single-use) that is directly applicable to any fleet project with controlled registration."
  ]
}
```
</details>

## Related
- [[homarr-labs-homarr]]
- [[2026-05-20-ajnart-homarr]]
- [[2026-05-20-homarr-labs-homarr]]
