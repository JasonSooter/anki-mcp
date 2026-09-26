# anki-mcp

An MCP server that exposes an Anki collection to Claude clients as tools, over
Streamable HTTP. It opens `collection.anki2` directly with the [`anki`][anki]
Python package — there is no AnkiConnect and no Anki GUI anywhere in the stack.

The primary use case is capturing German vocabulary from a phone: talk to
Claude, and the note lands in the deck. The card design (below) is opinionated
towards learning German, but the server works with any deck and note type.

[anki]: https://pypi.org/project/anki/

## Quick start

You need Docker, an [AnkiWeb](https://ankiweb.net) account (without 2FA), and a
way to reach the server over HTTPS from your Claude client -- the examples use
[Tailscale](https://tailscale.com).

```bash
mkdir anki-mcp && cd anki-mcp
curl -fsSLO https://raw.githubusercontent.com/JasonSooter/anki-mcp/main/secrets.env.example
cp secrets.env.example secrets.env
openssl rand -hex 32          # paste into ANKI_MCP_BEARER_TOKEN in secrets.env
mkdir -p data config && sudo chown 1000:1000 data config
```

`compose.yaml`:

```yaml
services:
  anki-mcp:
    image: ghcr.io/jasonsooter/anki-mcp:main
    container_name: anki-mcp
    env_file: secrets.env
    environment:
      - TZ=Europe/Berlin          # drives Anki's daily rollover -- use yours
    volumes:
      - ./data:/data              # collection.anki2 + collection.media
      - ./config:/config          # AnkiWeb session key, OAuth state
    ports:
      - "127.0.0.1:8770:8770"     # localhost only; publish via Tailscale
    restart: unless-stopped
```

```bash
docker compose up -d
curl -s http://127.0.0.1:8770/healthz
# {"status":"ok","profile":"full","collection_open":true}
```

Then [seed the collection from AnkiWeb](#first-run-seeding-from-ankiweb) and
[connect a Claude client](#connecting-a-claude-client). Images are published
for `linux/amd64` and `linux/arm64`; `:main` tracks the main branch, and every
build is also tagged `sha-<commit>` if you want to pin one.

## Configuration

All configuration is environment variables, normally in `secrets.env`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANKI_MCP_AUTH_MODE` | no | `bearer` | `bearer` (one static token) or `oauth` (login page; needed for Claude custom connectors / mobile). See [Auth modes](#auth-modes). |
| `ANKI_MCP_BEARER_TOKEN` | bearer mode | -- | At least 32 characters; the server refuses to start without it. |
| `ANKI_MCP_LOGIN_PASSWORD` | oauth mode | -- | Login page password, at least 12 characters. |
| `ANKI_MCP_TOTP_SECRET` | oauth mode | -- | Base32 TOTP setup key (not the 6-digit code). |
| `ANKI_MCP_PUBLIC_URL` | oauth mode | `http://localhost:<port>` in bearer mode | The externally reachable `https://` address; OAuth redirects and discovery metadata carry it. |
| `ANKI_MCP_PROFILE` | no | `full` | `full`, or `public` for a read-mostly tool set. See [Profiles](#profiles). |
| `ANKIWEB_USERNAME` / `ANKIWEB_PASSWORD` | first sync | -- | Exchanged for a cached session key on the first sync; blank them afterwards. |
| `ANKIWEB_ENDPOINT` | no | AnkiWeb | Custom sync server URL, for a self-hosted sync server. |
| `ANKI_MCP_IMAGE_PROVIDER` | no | `auto` | `auto`, `pixabay`, `openverse`, `wikimedia`, or `google`. See [Pictures](#pictures). |
| `PIXABAY_API_KEY` | no | -- | Free key; the best image relevance. |
| `GOOGLE_CSE_API_KEY` / `GOOGLE_CSE_ENGINE_ID` | no | -- | Legacy Google Custom Search engine. |
| `ANKI_MCP_TTS_PROVIDER` | no | `google` | `elevenlabs` or `google`, for audio Wiktionary doesn't have. |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | for ElevenLabs | -- | No default voice: every default is an English speaker. |
| `GOOGLE_TTS_API_KEY` | for Google TTS | -- | Text-to-Speech API key. |
| `ANKI_MCP_TTS_VOICE` | no | `de-DE-Neural2-F` | Google TTS voice. |
| `ANKI_MCP_PREFER_TTS` | no | `false` | Re-synthesise all audio with TTS, for one consistent voice. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` | no | -- | Ship structured logs over OTLP. Unset = stdout only. |
| `ANKI_MCP_ENVIRONMENT` | no | `production` | Tags every telemetry record. |
| `ANKI_MCP_HOST` / `ANKI_MCP_PORT` | no | `0.0.0.0` / `8770` | Listen address. |
| `ANKI_MCP_DATA_DIR` / `ANKI_MCP_STATE_DIR` | no | `/data` / `/config` in the image | Where the collection and the state live. |

## The rule that makes this safe

**This server is the single writer to the collection.**

```
  Claude client ──► anki-mcp (your server) ──► AnkiWeb ──► phone (review only)
```

Anki's collection is a SQLite database with an exclusive lock and its own sync
protocol. Two consequences you must not design around:

1. **Never put the data directory in a Dropbox / Syncthing / iCloud path.**
   File-sync tools copy SQLite files mid-write and corrupt them. The collection
   lives only on the server.
2. **Never run two copies of this container against the same directory.** The
   second one will fail to start (by design — see below), but don't try.

Edit on the phone only if you accept that a conflict will require a full sync
in one direction, which discards the other side.

## Tools

Read-only:

| Tool | What it does |
|---|---|
| `list_decks` | Deck names, card counts, and what's due today |
| `list_note_types` | Note types and their field names, in order |
| `search_notes` | Anki search syntax; truncated field previews |
| `get_note` | One note in full, plus its cards |
| `due_counts` | Cards due today, whole collection or one deck |
| `review_summary` | Reviews, time, again-rate, mature retention over N days |
| `find_images` | Search every image library and return the pictures to look at ([Pictures](#pictures)) |

Writes:

| Tool | What it does |
|---|---|
| `create_deck` | Create a deck (idempotent; `::` nests) ([Decks](#decks)) |
| `delete_deck` | Delete a deck; refuses to destroy cards unless told ([Decks](#decks)) |
| `add_note` | Add a note to a deck (rejects duplicates, and refuses one it can't illustrate) |
| `update_note` | Edit fields and tags, move the note to another deck, replace the picture via `image_url`, or re-record the audio ([Moving a note](#moving-a-note)) |
| `delete_notes` | Delete every note matching a search; count and selection must match ([Deleting notes](#deleting-notes)) |
| `sync_to_ankiweb` | Push to AnkiWeb — explicit, never automatic |

Adding or editing a note saves locally but does **not** sync. Call
`sync_to_ankiweb` when you want the phone to see it.

### Profiles

`ANKI_MCP_PROFILE` decides which tools are registered:

- `full` (default) — everything.
- `public` — the read-only tools (including `find_images`) plus `add_note`.
  `create_deck`, `delete_deck`, `update_note`, `delete_notes` and
  `sync_to_ankiweb` are not registered, so they don't exist on the wire; a
  client calling one gets `Unknown tool`.

Run a second container with `ANKI_MCP_PROFILE=public` to expose a narrower
endpoint. Note that it needs its own collection directory — two containers
cannot share one (see the lock rule above).

## First run: seeding from AnkiWeb

The collection directory starts empty. On first start the server creates a new,
empty collection; you then pull your real one down from AnkiWeb.

1. **Create the secrets file** (see [Quick start](#quick-start)).

   Put your AnkiWeb username and password in the same file. They are needed
   only for the first sync; the server caches a session key at
   `config/ankiweb_hkey` and you can blank the password afterwards.
   **AnkiWeb accounts with 2FA enabled will not work** — there's no interactive
   step available to satisfy the second factor.

2. **Start it:**

   ```bash
   docker compose up -d anki-mcp
   curl -s http://127.0.0.1:8770/healthz
   # {"status":"ok","profile":"full","collection_open":true}
   ```

3. **Seed the collection.** The new empty collection and your AnkiWeb account
   have no shared history, so AnkiWeb will demand a *full* sync. `sync_to_ankiweb`
   will refuse and tell you so — that refusal is the safety feature. Do the
   full download deliberately, with the container stopped:

   ```bash
   docker compose stop anki-mcp
   docker compose run --rm --entrypoint python anki-mcp -c "
   from anki.collection import Collection
   col = Collection('/data/collection.anki2')
   auth = col.sync_login('YOUR_ANKIWEB_EMAIL', 'YOUR_PASSWORD', None)
   col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
   print('downloaded')
   "
   docker compose up -d anki-mcp
   ```

   `upload=False` means **AnkiWeb overwrites local**, which is what you want
   when seeding. Getting this backwards would upload your empty collection over
   your real one — read the next section before you ever pass `upload=True`.

   Alternatively, copy an existing `collection.anki2` into `data/`
   while the container is stopped. Copy the `collection.media` folder too.

## Full sync: what it means and how to resolve one

A *full* sync is not a merge. One side is discarded wholesale. AnkiWeb asks for
one when the two collections have diverged beyond reconciliation — different
schema versions, or a `Check Database`/restore on one side.

`sync_to_ankiweb` never resolves this on its own. It reports which direction
AnkiWeb wants and changes nothing. To resolve it, stop the container and run
the snippet above with:

- `upload=False` — **AnkiWeb wins.** Local changes since the divergence are lost.
- `upload=True` — **This server wins.** Anything that reached AnkiWeb from
  another device is lost.

Because the server is the single writer, `upload=True` is usually right — but
confirm the phone has nothing unsynced first. Back up `data/` before either.

## Connecting a Claude client

The port is bound to `127.0.0.1` so nothing is exposed by simply starting the
container. Publish it on the tailnet with Tailscale, which also gives it a
real TLS certificate:

```bash
sudo tailscale serve --bg --https=8770 http://127.0.0.1:8770
sudo tailscale serve status
```

The server is then at `https://<your-host>.<your-tailnet>.ts.net:8770/mcp`,
reachable from any device on the tailnet.

**Claude Code:**

```bash
claude mcp add --transport http anki \
  https://<your-host>.<your-tailnet>.ts.net:8770/mcp \
  --header "Authorization: Bearer <ANKI_MCP_BEARER_TOKEN>"
```

**Claude Desktop** — in Settings → Connectors → Add custom connector, use the
same URL and add the `Authorization: Bearer <token>` header.

Verify with: *"list my Anki decks"*.

## Exposing it via Tailscale Funnel

Funnel puts the endpoint on the public internet. What changes:

```bash
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8770
```

1. **Switch to the public profile.** Set `ANKI_MCP_PROFILE=public` so the
   internet-facing endpoint cannot edit notes or trigger syncs.
2. **Set `ANKI_MCP_PUBLIC_URL`** to the Funnel hostname, so the OAuth
   protected-resource metadata the server publishes points at the right host.
3. **If you stay on the bearer token, understand what it is.** On the tailnet
   it's defence in depth; on Funnel it would be the *only* thing between the
   internet and your collection, and it never rotates or expires. Treat a leak
   as total compromise, and rotate by changing `secrets.env` and restarting.
4. **Use OAuth, not the bearer token.** This is configuration, not code: set
   `ANKI_MCP_AUTH_MODE=oauth` along with `ANKI_MCP_LOGIN_PASSWORD`,
   `ANKI_MCP_TOTP_SECRET` and `ANKI_MCP_PUBLIC_URL`, and the server refuses to
   start unless all three are present. See [Auth modes](#auth-modes). It is
   also not optional for Claude custom connectors -- a static header is not
   something they can send, so the mobile apps need OAuth regardless.

Funnel also means every request is logged by Tailscale's infrastructure and the
hostname is publicly resolvable. For a personal vocab collection that is
probably fine; decide deliberately rather than by default.

### Why port 8770

Not 8765 — that is **AnkiConnect's** default port. This server deliberately
does not use AnkiConnect, but anyone running Anki desktop on the same machine
would get a silent port collision, so the default sidesteps it.

## Auth modes

`ANKI_MCP_AUTH_MODE` picks how callers authenticate.

**`bearer`** — one static token in `ANKI_MCP_BEARER_TOKEN`, checked with a
constant-time compare. Good enough on the tailnet, where the network is already
the perimeter, and it is what Claude Code and Claude Desktop use because they
run locally and can send an `Authorization` header.

> **If the deployment ever stops being tailnet-only, change this back to
> `oauth` in the same commit.** A bearer endpoint left on Funnel does not fail
> — it serves happily, protected by one token that never rotates. Every other
> misconfiguration here announces itself: the wrong port fails the connector
> dialog, a missing TOTP secret refuses to boot, a stale issuer breaks
> discovery. This one is silent, which is why it gets a warning rather than a
> test — the code is correct in both modes.

**`oauth`** — a full OAuth 2.1 authorization server: dynamic client
registration, PKCE, authorization codes, rotating refresh tokens, and a login
page that wants a password *and* a TOTP code. State lives in
`config/oauth.db`, so a container restart does not break the
connector.

You need `oauth` for Claude custom connectors — and therefore for **Claude on
Android**, which is the only way to reach this server from a phone. Anthropic's
servers make that connection, so the endpoint must be publicly reachable and a
static header is not on offer.

The login page is the only thing between the internet and your collection when
Funnel is on. It fails closed: identical wording whether the password or the
code was wrong, five attempts per source address per 15 minutes, and a TOTP
code cannot be replayed inside its own window.

Set `ANKI_MCP_TOTP_SECRET` to the base32 *setup key* (what a password
manager's one-time-password field or an authenticator app is set up from), not
the 6-digit code.

## Telemetry and the Grafana dashboard

Optional. Structured logs ship over OTLP (for example to Grafana Cloud), and
every field is queryable as Loki structured metadata:

```logql
{service_name="anki-mcp", deployment_environment_name="production"}
  | tool="add_note" | status="error"
```

Set `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` to turn it
on. With no endpoint configured it is a no-op — the collection must stay
servable whether or not Grafana is reachable.

Import `dashboards/anki-mcp.json` (uid `anki-mcp`; it expects a Loki data
source with uid `grafanacloud-logs`, Grafana Cloud's default). It covers health and error
rate, per-tool latency, collection size, capture rate by deck and note type,
AnkiWeb sync outcomes — including the full-sync refusals — and a security row
tracking auth failures and login attempts by source address.

`dashboards/alerts-ankiweb-sync.json` holds two alert rules (full sync needed,
sync erroring). Replace `REPLACE_WITH_YOUR_FOLDER_UID` with a folder of yours,
then import them through Grafana's alert-rule provisioning API.

## Decks

`create_deck` makes one; `add_note` refuses an unknown deck rather than
inventing it, because a typo would otherwise scatter cards into a deck nobody
reviews. `"::"` nests, and the parents are created with the child.

Fill in the description. `list_decks` shows it and `add_note`'s own guidance is
to read it before choosing where a note belongs, so a deck without one is a deck
the next caller has to guess about.

Creating a deck twice is safe -- the existing one is reported back with
`created: false`. That matters because "make sure this deck exists" is what
every caller actually means, and a retry after a dropped connection should be
harmless.

`delete_deck` is the other direction. It and `delete_notes` are the two tools
here that destroy work rather than adding it.
Its intended use is tidying up: an empty deck made by a typo, or two decks that
turned out to be the same topic under different spellings. Two guards, because
Anki's own `remove()` has neither:

- **Cards are refused by default.** A deck holding cards raises instead, saying
  how many are at stake; `delete_cards=True` is required to go through. Those
  cards carry review history that no sync brings back.
- **Subdecks count toward that total**, because deleting a parent deletes them.
  Counting only the parent's own cards would call a tree holding hundreds
  "empty".

Note that deck names are case-sensitive and `&` is literal -- a client that
HTML-escapes it leaves you with `German Time &amp; Dates` sitting next to
`German Time & Dates` as two separate decks.

## Moving a note

`update_note`'s `deck` argument moves the note's cards to another deck. It is
the repair for a note that went into the wrong one -- which is a real failure
mode here, since `add_note` takes the deck by name and a model choosing that
name can choose badly.

**Moving preserves each card's scheduling and review history.** That is the
reason this exists rather than leaving the job to `delete_notes` plus a fresh
`add_note`: deleting and re-adding resets every card to new and throws the
history away, and it re-fetches the audio and picture for a note that already
had good ones.

The deck must already exist. Like `add_note`, this refuses an unknown name and
lists the real ones rather than inventing a deck, because a typo would
otherwise file the note where nobody reviews it -- and `&` is literal, so
`German Time &amp; Dates` is a different deck from `German Time & Dates`. Call
`create_deck` first if it does not exist yet.

The result reports `moved: {from, to, cards}`, with `from` read before the
move, so a caller who named the wrong note can see where its cards actually
came from.

## Deleting notes

`delete_notes` takes an Anki search and deletes everything it matches. It is the
cleanup path for a batch that went in wrong -- a session that filed notes into
the wrong deck, an import that should not have happened.

**Both `expect_count` and `selection` are required.** Run `search_notes` with
the same query, pass its `total_matches` as `expect_count` and its `selection`
value through unchanged. Never construct a selection token; it is the proof
that you looked.

A search is the most dangerous way to choose what to destroy: `tag:zeit`,
`tag:zeit*` and a typo'd `tag:ziet` all look equally reasonable in a tool call,
and in a collection that has `zeit::3-monate` the first two differ by every
month note. The two arguments answer different questions, and fail differently
on purpose:

| Refusal | Means |
|---|---|
| count mismatch | the query is broader or narrower than you meant |
| digest mismatch | notes were added, removed or **edited** since you looked |
| token expired | your decision is older than the facts it rested on |

The **age** check is not belt-and-braces. A delete planned against this
collection on one day was still a perfectly accurate query two days later --
same count, same note ids -- but by then the notes had been superseded and
deleting them was wrong. Nothing about *which* notes matched could have caught
that; only *when* they were looked at. Selections expire after 15 minutes, and
re-running the search costs nothing.

The digest covers each matched note's **fields and tags**, not just its id.
Ids alone were tried first and are not enough: Anki hands out note ids from a
millisecond clock and reuses a freed one, so deleting the newest note and
adding another in the same millisecond yields a different note wearing the same
id -- exactly the swap the token exists to catch. Hashing contents also means an
edit to a matched note invalidates the selection, which is the desired answer
for a caller about to destroy something they reviewed in a different form.

The token covers **every** match, not the rendered page: `search_notes`
paginates and `delete_notes` does not, so a page-only token would quietly stop
protecting the remainder of a large cleanup.

Two smaller guards: an empty query is refused rather than matching the whole
collection, and a query matching *nothing* is an error rather than a successful
deletion of zero notes, because a misspelled tag matches nothing rather than
erroring and "deleted 0" would read as "the cleanup happened".

If the count comes back wrong, look at what the query matches before re-issuing
with the number from the error. The mismatch is usually telling you the query is
wrong, not the count.

Deleting a note deletes its cards and their review history. Once synced it is
gone from every device; AnkiWeb has no undo.

## The three cards

Every note of `German (Fluent Forever)` makes three cards, and they are three
different questions rather than three views of one:

| card | prompt | task |
|---|---|---|
| **1. Wort erkennen** | word + IPA + audio | recall the meaning; then record and compare your own voice |
| **2. Wort produzieren** | picture or digits + the sentence with the word blanked | **type** the word |
| **3. Schreibweise** | audio + definition, the word hidden | **type what you hear** — dictation |

Card 1 cannot have a typing box: it *shows* the word, so there would be nothing
to recall. That asymmetry is why only two of the three are typed.

Cards 2 and 3 look similar and are not. Card 2 gives you the meaning and asks
for the form; card 3 gives you only the sound. That distinction depends
entirely on the audio being right — while the synthesiser was mispronouncing
isolated words, card 3 was worse than useless, because it drilled the wrong
sound and then marked your correct spelling against it. It became worth having
on the same day the audio did.

**Card 3 is gated on `Test Spelling`.** Fill it (any value) and the card
generates; clear it and the card stops being produced. Two things follow:

- Clearing the field does **not** delete the card. Anki leaves it as an *empty
  card* until `col.get_empty_cards()` is run, so a flag cleared in bulk quietly
  inflates deck counts until someone sweeps.
- It is the per-note escape hatch. Dictating a 25-character compound
  (`neunzehnhundertfünfundachtzig`) is a different exercise from dictating
  `neun`, and if it stops being worth the keystrokes, clearing the flag on
  those notes alone drops the card without touching the rest.

**Speaking practice** lives on card 1's answer, where the model audio plays:
`Shift+V` records, `V` replays. Never on card 2's prompt, for the same reason
the word audio never plays there — a card that asks you to produce a word must
not first say it.

**`{{type:}}` renders only in the reviewer and the card-layout screen.** The
Browse → Preview window never shows the input box no matter what the template
says. Check a template by reading it in *Manage Note Types → Cards*, or study it
in a filtered deck (`"note:German (Fluent Forever)" card:3 is:new`).

## The English field

`English` is required on every note, and it renders on the answer side of both
cards behind `{{hint:English}}` -- never on a prompt, so retrieval still happens in
German. It holds one line per piece of German the answer shows, separated by
`<br>`:

```
pocket money
Money that children get regularly from their parents and may spend freely.
My daughter gets 25 euros pocket money every month.
```

A bare gloss was the original rule and it left most of the answer side
unreadable: the German definition and the example sentence are written at a
level above where the learner is, which is the point of them as *material* but
useless as *feedback*. `add_note` counts the lines and refuses a note whose
gloss does not cover the `Definition (DE)` and `Example (DE)` it was given.

These are lines in one field rather than `Definition (EN)` and `Example (EN)`
fields on purpose: adding fields is a note type schema change, and AnkiWeb
answers a schema change by demanding a full sync in one direction -- which
`sync_to_ankiweb` refuses to do on its own.

## Pronunciation

IPA and audio come from German Wiktionary; audio is re-synthesised by TTS when
`ANKI_MCP_PREFER_TTS=true`, so the deck has one voice rather than a mix of
volunteer recordings at different volumes. Nothing invents an IPA transcription
— for a phrase there simply is none, and a hand-assembled guess presented as
fact is worse than a blank field.

### Word-final consonants

With ElevenLabs, a word's audio is not synthesised on its own. Said alone, the
word ends the utterance, and the voice says an utterance-final stop weakly: a
long closure, then a faint release. On `der Berg` the [k] was inaudible, which
is fatal on a deck that teaches final devoicing. So for the `Word` field:

- the word is spoken inside a carrier, `„der Berg“, sagte sie.`, via
  `/with-timestamps`, and cut back out using the character alignment, keeping
  120 ms past the last letter for the release and adding 300 ms of silence;
- for a word ending in an obstruent (b d g p t k s ß f v z x ch), up to 24 takes
  are made, three at a time (the plan's concurrency limit), and the one with the strongest final release is
  kept. The strength of the burst varies by 8 dB between identical requests,
  and no request setting moved it, so selection is the fix. The `pronunciation`
  note in the tool result says how many takes were made and the winner's score;
- a word ending in **-ig** (König, wenig, zwanzig) is not ranked by burst
  strength: standard German says [ɪç] there, not [k] (g-Spirantisierung), and
  ranking by burst could favour a [k] take. Instead each take is checked for a
  sustained final hiss (an unbroken run of at least 90 ms; calibrated on live
  takes, where [ç] held 110-170 ms and a real [k] 30-70 ms). Takes come in
  rounds of 3, up to 6; the longest hiss in the first round with a qualifying
  take is kept, or the longest overall if none qualifies. `ei` + g (Zweig, Teig) is ordinary devoicing and
  is ranked like any other stop;
- example sentences are synthesised as before.

`scripts/tts_tail_diagnostic.py` measures a clip's final release with the same
code the server selects on, against stored clips or a reference recording such
as Commons `De-Berg2.ogg` (a native [k] at -9.4 dB relative to the vowel).

### Compounds

German builds compounds freely, so a word can be completely ordinary and still
have no Wiktionary entry. `das Budgetgeld` is real, is used by the DJI, and
returns nothing.

When the whole word misses, `compound_ipa` looks up its **parts** and joins
their real transcriptions:

```
das Budgetgeld   ->  Budget (byˈd͡ʒeː) + Geld  ->  byˈd͡ʒeːˌɡɛlt
das Kleidergeld  ->  Kleider (ˈklaɪ̯dɐ) + Geld ->  ˈklaɪ̯dɐˌɡɛlt
```

Measured against live Wiktionary: `das Taschengeld` and `der Dauerauftrag` are
answered directly and never reach the splitter, and `die Entmystifizierung`
resolves to nothing — it is a prefix/suffix derivation rather than a compound,
and no split of it has two real parts. A blank IPA field is the correct answer
there.

Three things keep this a derivation rather than a guess:

- **Every symbol comes from Wiktionary.** Only the seam is ours, and the
  `pronunciation` field in `add_note`'s result names the parts, so a derived
  transcription is never mistaken for a dictionary one.
- **Both parts must have real entries.** A nonsense split finds nothing and is
  dropped, which is what stops the splitter from inventing a plausible-looking
  word boundary.
- **The stress is the actual rule, not a flourish.** German compounds carry
  primary stress on the first element and secondary on the rest, which is how
  Wiktionary writes the compounds it *does* have — `Taschengeld` is
  `ˈtaʃn̩ˌɡɛlt`. So a later part's primary mark is demoted, and an unmarked
  monosyllable gains a secondary one.

The seam can differ slightly from a transcription the dictionary has lexicalised
— derived `Tasche + Geld` gives `ˈtaʃəˌɡɛlt` where Wiktionary's own entry reads
`ˈtaʃn̩ˌɡɛlt`, because the compound reduces the schwa. That only matters for
words Wiktionary already has, and those never reach the splitter.

Linking elements are stripped only when the unstripped head has no entry:
`Taschen|geld` needs the `n` peeled to reach Tasche, but `Kleidergeld` is
Kleider + Geld — the plural, not a linked `Kleid` — and peeling first would
transcribe the wrong word. The search runs longest-first-element first, because German heads are
usually the short tail — `-geld`, `-zeit`, `-haus`. Part lookups are capped per
word so an unsplittable compound cannot turn into a minute of throttled
requests.

## Pictures

On this collection the picture is not decoration. Fluent Forever's argument --
and the line from the book that drove the design -- is that a card should carry
meaning without an English word on it, so the image *is* the definition for
anything concrete. That is why `add_note` refuses a note it cannot illustrate.

### How to choose one

```
find_images(queries=[...])   ->  look at the results
add_note(..., image_url=...) ->  attach the one that actually depicts the word
```

`find_images` runs every query against every image library, pools the results,
removes duplicates and returns the pictures themselves. **Look at them.** This
is the whole point of the tool, because word matching cannot judge a picture:

- a green ceramic frog is tagged `winken, abschied` and is a flawless textual
  match for waving goodbye
- a photograph of paperclips is tagged `feierabend`

Both were chosen automatically, and both were wrong. Scoring candidates by how
well their tags matched the query was tried and measured: the paperclip photo
carries *both* query words and scores 1.00 while the images genuinely about
Feierabend score 0.50, and six candidates tied at 1.00 for another search. Tags
are user-supplied and match promiscuously. The approach was abandoned rather
than shipped.

### Writing good queries

Pass several at once and vary them deliberately -- the thing itself, the scene
it belongs to, and what a person *does*:

```python
find_images(queries=["Hausschuh", "Pantoffel Filz", "Fuesse Hausschuhe Sofa"])
```

Traps worth knowing, all learned the hard way:

- **Emotive nouns pull the wrong register.** Searching `Abschied` returns
  funerals and mourning roses, because that is what people tag with it. Search
  the concrete action instead -- `Frau winkt`, `Kinder winken`.
- **Compound queries invite spurious matches.** `Buero Feierabend` let a picture
  of paperclips match on both words. One concrete noun is usually better.
- **The first hit is rarely the best one.** Providers rank by popularity.

### The libraries

| Provider | Key | Character |
|---|---|---|
| Pixabay | free | Photographs taken on purpose: everyday objects and scenes, German tags. The best single source. |
| Openverse | none | Flickr and friends. Slower and patchier, but holds candid photography the stock libraries do not -- it supplied the children waving and the theatre curtain. |
| Wikimedia | none | Fast, but its `File:` namespace is largely digitised books; results are filtered to real images because scanned newspaper pages were taking a third of every search. |
| Google CSE | key | Largely moot: Google withdrew "search the entire web" in March 2026, capping new engines at 50 listed domains. Kept for legacy engines. |

`ANKI_MCP_IMAGE_PROVIDER=auto` chains them best-first for the one-shot
`image_query` path. `find_images` ignores the chain and searches all of them,
because when something is going to *look* at the results, breadth beats speed.

### When there is genuinely no picture

Some words cannot be depicted unambiguously. A photograph of vegetables does
not mean *Gesundheit* -- it means vegetables, and the card would accept several
different answers. Wyner splits `Picture Words` from the `All-Purpose Card` for
exactly this reason.

For those, `add_note(abstract=true)` skips the image and requires
`Definition (DE)` instead, so the German definition carries the meaning.

**This is a last resort, not a first move.** Try several genuinely different
queries first. "Wir sehen uns!" survived three failed searches before a wide
pooled scan found children waving -- giving up early would have cost a real
picture.

### A note on deleting media

`store_image` names files from the search query, and Anki reuses an existing
name when the bytes match, so **two notes can point at the same file**. The
server never deletes media, so this is latent — but anything that cleans up
superseded images must resolve which notes reference a file *before* unlinking
it, not after. Deleting by filename would silently blank the picture on every
other note sharing it.

`Tools → Check Media` in Anki reports both halves: files referenced by no note,
and notes referencing missing files.

## Operations

**Backup.** The collection is a live SQLite database — copy it stopped, not hot:

```bash
docker compose stop anki-mcp
cp -a data ~/backups/anki-$(date +%F)
docker compose up -d anki-mcp
```

**The lock.** The server opens the collection once, on one dedicated thread,
and holds it for its lifetime. If the file is already open, startup fails with
exit code 3 and an explicit message — it does not retry, because a container
that keeps restarting is visible while one that silently serves errors is not.
Exit code 2 means a configuration problem (usually a missing or too-short
bearer token). The server will not start unauthenticated.

**Timezone.** Set `TZ` on the container; it drives Anki's daily rollover. If it's wrong, `due_counts` disagrees with the phone.

## Upgrading the `anki` package

`anki` is pinned exactly in `pyproject.toml` (currently **26.8.1**, validated
against AnkiDroid as the downstream client).

This pin is not a routine dependency. The `anki` library owns the collection's
**schema version**: opening the collection with a newer version can upgrade the
schema, and a schema AnkiDroid doesn't understand forces a full sync on the
phone — or refuses to sync at all. Anki's Python API also has no stability
guarantee and moves between releases.

Before bumping:

1. **Check whether the schema changed.** Compare `SCHEMA_MAX_VERSION` in
   `rslib/src/storage/upgrades/mod.rs` and `SYNC_VERSION_MAX` in
   `rslib/src/sync/version.rs` between the two tags of
   [`ankitects/anki`](https://github.com/ankitects/anki) (the git tags are
   zero-padded: `26.08.1`, not `26.8.1`). If both are unchanged, the phone's
   AnkiDroid version doesn't matter; if either changed, AnkiDroid on the phone
   must support the new version first.
2. Back up the data directory.
3. Bump, rebuild, run the tools once, then `sync_to_ankiweb` and confirm the
   phone still syncs normally rather than demanding a full sync.

Treat it as a migration, not an upgrade.

## License

MIT -- see [LICENSE](LICENSE).
