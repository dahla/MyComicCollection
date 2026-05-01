# ComicVault

A self-hosted Disney comic collection tracker, backed by the
[Inducks](https://inducks.org/) catalogue (~7000 publications, 250 000+
issues). Browse by country, mark which issues you own, where you store
them, and what condition they're in. Free-text search across publications,
issues, and stories.

Runs as a single small Docker container — Python + SQLite, no separate
database, no Node build step.

## Features

- Browse the full Inducks catalogue by country, then by publication
- Click any publication to see its issues laid out as a cover grid
  (covers proxied from inducks.org and cached on disk)
- Mark issues as owned, with location + condition + free-form notes
- **Bulk-add mode** on a publication page — pick a location/condition,
  then click each issue you have to add a copy in one click
- Filter dropdown applied to country / publication / issue lists:
  *All*, *Owned only*, *Missing only*
- Free-text search across publication titles, issue titles, story titles
- Defaults the next "Add a copy" to your last-used location and condition
- Settings page for Inducks credentials, locations, and grading conditions
  (all editable)

## Quick start (using the prebuilt image)

A multi-arch image (amd64 + arm64) is published to GitHub Container
Registry on every push to `main`:

```yaml
# docker-compose.yml
services:
  comicvault:
    image: ghcr.io/dahla/mycomiccollection:latest
    container_name: comicvault
    restart: unless-stopped
    ports:
      - "3111:8000"
    volumes:
      - ./data:/data
```

Then:

```sh
docker compose pull
docker compose up -d
```

Open <http://localhost:3111>. The first visit shows a setup screen:

1. Enter your free [inducks.org](https://inducks.org/login.php) account
   credentials. These are only used to fetch cover images — Inducks
   requires login for image hot-linking.
2. Click **Download catalogue**. The ~100 MB Inducks ISV dump is fetched
   and imported into SQLite. Takes 2–5 minutes on first run.
3. After sync completes, browse → mark issues → search.

## Building from source

```sh
git clone https://github.com/dahla/MyComicCollection.git
cd MyComicCollection
docker compose up --build -d
```

The provided [Dockerfile](Dockerfile) builds the same image the CI
workflow publishes.

## Configuration

| Env var      | Default          | Purpose                                        |
| ------------ | ---------------- | ---------------------------------------------- |
| `DATA_DIR`   | `/data`          | SQLite DB + Inducks session cookie live here.  |
| `COVER_DIR`  | `$DATA_DIR/covers` | Where fetched cover images are cached.       |

Mount `$DATA_DIR` to a host directory or named volume for persistence.
The container is otherwise stateless — re-create it freely.

## Reusing an existing cover cache

Cover filenames are `md5(issuecode).{jpg,gif,png}`. If you already have a
cover cache from a previous Inducks-based app (for example
[duckvault](https://gitlab.com/stanstrup/duckvault) — same hashing
scheme), point `COVER_DIR` at it to skip re-downloading. Example
`docker-compose.override.yml`:

```yaml
services:
  comicvault:
    volumes:
      - /mnt/nas/inducks/covers:/covers
    environment:
      COVER_DIR: /covers
```

Override files are gitignored by default — keep them per-host.

## API surface

All endpoints under `/api/`. The frontend is the only consumer; consider
this contract internal.

- `GET /api/setup`, `POST /api/login`, `POST /api/logout`
- `POST /api/sync/start`, `GET /api/sync/status`
- `GET /api/countries?filter=all|owned|missing`
- `GET /api/publications?country=…&q=…&filter=…`
- `GET /api/publications/{publicationcode}`
- `GET /api/issues/{issuecode}`
- `GET /api/search?q=…`
- `GET /api/collection`, `POST /api/collection`,
  `PATCH /api/collection/{id}`, `DELETE /api/collection/{id}`
- `GET/POST/DELETE /api/locations`
- `GET/POST/DELETE /api/conditions`
- `GET /api/covers/issue/{issuecode}`,
  `GET /api/covers/publication/{publicationcode}`

Cover responses carry `Cache-Control: public, max-age=31536000, immutable`,
so each cover is fetched at most once per browser.

## How the GitHub Actions workflow works

[`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)
runs on every push to `main`. It:

1. Builds the image for `linux/amd64` and `linux/arm64` (so it runs on
   both x86 servers and Raspberry Pi).
2. Tags the image as `:latest` and `:sha-<short-commit>`.
3. Pushes to `ghcr.io/<owner>/<repo>` using the workflow's automatic
   `GITHUB_TOKEN` — no manual secrets needed.

**One-time:** after the first successful run, go to your GitHub profile
→ *Packages* → click the package → *Package settings* → *Change
visibility* → **Public** if you want others to `docker pull` without
authenticating.

To trigger a build manually use *Actions → Build and publish Docker
image → Run workflow*.

## Tech stack

- **Backend:** Python 3.12, FastAPI, stdlib `sqlite3`, `httpx`. Four pip
  dependencies total.
- **Frontend:** Single HTML file, Vue 3 + Tailwind CSS via CDN. No build
  step, no `node_modules`.
- **Storage:** SQLite (one file). The Inducks ISV dump is parsed and
  loaded into ~9 tables; only what's needed for browsing, searching, and
  cover URL resolution is imported.

## License

The code in this repo is yours. The Inducks data this app downloads at
runtime belongs to the Inducks project; please respect their
[terms of use](https://inducks.org/).
