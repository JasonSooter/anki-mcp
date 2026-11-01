# Task register — durable, permanent record

**Why this file exists:** the Claude Code session task list is **not file-backed**. A task
created with `TaskCreate` and never written here is gone at `/clear` or session end. This
file is the durable copy. Rehydrate the session list from it; never the reverse.

Started 2026-11-01 with tasks moved from the
[home-media-server](https://github.com/JasonSooter/home-media-server) register, which
deploys this server; each row names its old ID there. Code work for this server is tracked
here; hosting it (compose, Tailscale, secrets on the box) stays in home-media-server.

**Next ID: T-008**

---

## 🎯 Priority

None set. Pick from the register.

---

## Register

| ID | Status | Subject | Detail |
|----|--------|---------|--------|
| T-001 | open | Tools for the remaining hand-scripted work | Two things were done by ad-hoc scripts and are still not reachable through the MCP: (a) bulk audio regeneration across a deck (today only per note: `update_note(..., regenerate_audio=True)`), (b) the empty-card sweep after clearing a gating field (`col.get_empty_cards()`). Same shape as two gaps already closed (`Test Spelling` default, `create_deck`). No work started. (Was home-media-server T-012.) |
| T-002 | open | Berg/Zug/gelb final bursts still well below native after 24 takes | Parked pending Jason's listening verdict (T-003). Ruled out (measured): `language_code`, turbo v2.5, v3 (noise tail), stability/speed/style, vowel-initial carrier ("der Berg ist gemeint"), tail margin (burst is measured inside the slice). Not tried: targeted gain on the release region (DSP) — would push toward −9 dB but may sound artificial; needs a listen test before shipping. gelb's [p] is inherently weak even natively (native De-gelb −33.8 dB). (Was home-media-server T-035.) |
| T-003 | open | Get Jason's listen verdict on the new word audio | **Owner: Jason.** He listened to every word on 2026-09-24 but gave no verdict. Ask two things: (1) is the final [k]/[p]/[t] audible on der Berg, der Zug and gelb (the three weakest by the metric: −15.6, −18.6, −20.6 dB); (2) do the -ig words (wenig, billig and ruhig first; then Honig, Essig, Käfig, fertig, wichtig, lustig, richtig, König) end in [ç], not [k]? Judge only the bare -ig word: [ɡ] in *wichtige* etc. is correct. All fine → close this and T-002. Any weak stop → T-002 (release-region gain, A/B listen before shipping). Any -ig [k] → `update_note(note_id, regenerate_audio=True)` re-records it under the hiss check. (Was home-media-server T-039.) |
| T-004 | open | Stay in Python: make the code readable instead | **Decision 2026-09-26:** NOT porting to TypeScript. The server is built on `anki` (Anki's own Rust-backed library), which has no Node equivalent, and a reimplementation risks corrupting the collection. Instead: a module-by-module "how it works" section in `README.md` (checked 2026-11-01: none yet), plus plain-English comments on the densest parts (`tools/write.py` ~1.2k lines, `pronunciation.py`, `images.py`). (Was home-media-server T-047.) |
| T-005 | open | Make one image serve several people (per-instance configuration) | **Decided 2026-09-26:** host one anki-mcp per family member, each with its own collection, AnkiWeb account, OAuth login and public URL. This repo's part: per-instance config (data dir, port, public URL, secrets) via environment only; a voice/image budget per instance or shared (ElevenLabs cost); and the deck/card conventions made configurable where they are specific to Jason's deck. Running the instances (compose, Tailscale) is home-media-server's half. (Was part of home-media-server T-053.) |
| T-006 | open | Decide `AuthSettings.validate_token_resource` before mcp SDK 3.0 | Every server logs `MCPDeprecationWarning: AuthSettings.validate_token_resource is not set`. In mcp SDK 3.0 it defaults to True whenever `resource_server_url` is set, and our providers don't record the token's `resource`, so a silent SDK bump could reject every token. Decide in all three repos together (anki-mcp, obsidian-mcp, ynab-mcp; the OAuth code is shared). While there, update the `oauth/*.py` provenance headers, which still say "the two are". No work started; checked 2026-11-01: not set anywhere. (Was home-media-server T-060.) |
| T-007 | blocked | Local (stdio) mode + Fly.io template | **Deferred by Jason 2026-09-26** ("skip that for now"). Serverless (Vercel, Lambda, Workers) doesn't fit either server as built: anki-mcp needs a persistent disk, a single writer and Anki's Rust-backed Python library; obsidian-mcp needs the vault on disk. The easiest paths for others: (1) stdio mode for Claude Desktop (a few lines each, both use FastMCP `streamable_http_app`, and stdio skips OAuth); (2) a Fly.io/Railway template with one volume and max 1 instance. A Workers version of obsidian-mcp would need a storage-API redesign; revisit during the TypeScript port. Applies to anki-mcp and obsidian-mcp. (Was home-media-server T-054.) |

---

## 📌 Standing facts that save re-derivation

- The OAuth code is shared by anki-mcp, obsidian-mcp and ynab-mcp: fix bugs in all three.

---

## ⚠️ False premises and method traps

- None recorded yet.

---

## 🗓️ Time-bound checks

- None.
