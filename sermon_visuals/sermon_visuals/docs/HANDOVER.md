# Handover: sermon-visuals

Written for a Claude Code session taking this project over on Blake's Windows PC. You have no
other context, so this is everything that matters. Read it all before touching anything.

## 1. What this is and who it is for

Blake Silvers leads a Christian nonprofit that carries on the work of **Ronald L. Dart** (died
2016): the *Born to Win* radio program, Christian Educational Ministries, Whitehouse, Texas. The
archive is audio programs of about 28 minutes, most with an edited PDF transcript. The goal is to
turn each program into video that holds attention on YouTube and teaches well.

Blake is comfortable with ideas and not with a terminal. Give him one command per code block, say
what he should see, and never assume git knowledge. His input is often voice-to-text. He wants
accuracy over agreement: say what is unverified, lead with the strongest counterargument, give
your own numbers before reacting to his.

A **board** approves each escalation in complexity, resolution and spend. Approvals arrive one at
a time. The process must be able to stop at any rung and still have produced something finished.

## 2. Rules that are not negotiable

- **Ron's audio is never altered.** No cuts, no reordering, no synthetic voice, no synthetic
  likeness of him. His real photos may be used for branding.
- **Words on screen are his words or scripture.** Pull quotes and key phrases are checked verbatim
  against the transcript or the word timings and dropped if they do not match. Scripture blocks,
  other authors' quotations and `[bracketed]` editor notes are never attributed to him.
- **Image models never render text.** Text is set by our code (Pillow) so a verse is never misspelled.
- **`depict_christ = false`.** No depiction of Jesus or God in any form, including as an infant.
  The `avoid` list in `config.toml` also applies. The ministry is in the Church of God tradition
  (Sabbath, biblical Holy Days, does not keep Christmas or Easter); do not flag that teaching as
  sensitive and do not use generic Christmas or church imagery.
- **Money.** Hard cap `studio.budget_usd = 200` per full program, pro-rated for clips. Nothing is
  generated without the `Spend about $X? [y/N]` prompt or an explicit `--yes` from Blake. Every
  new paid path you add must go through the same estimate, cap and ledger (`spend.json`).
- **Secrets.** Keys live in `.env` beside `config.toml`. Never print, commit or paste them.
- **Blake likes the scripture overlays as they are.** Whenever Ron reads a passage, the reference,
  version and words appear in a band for the length of the reading and page through long passages.
  Keep that behaviour in anything new.

## 3. Where things are

- Repo `blakesilvers/Collector`, branch `claude/serene-mendel-0btcz1`, open draft pull request #2
  into `main`. The project is the `sermon_visuals/` folder (own `pyproject.toml`). The rest of the
  repo is an unrelated bid scraper; leave it alone.
- On the PC the working copy is an unzipped download, **not a git clone**:
  `C:\Users\Blake\Downloads\Collector-claude-serene-mendel-0btcz1\Collector-claude-serene-mendel-0btcz1\sermon_visuals`
  It holds `config.toml`, `.env`, `programs\about_christmas.mp3`, `.pdf` and `.txt`, and `output\`.
- **First job:** make a real clone so you can commit and push. Clone the branch to a clean folder
  such as `C:\Users\Blake\sermon-visuals`, copy `.env`, `config.toml`, `programs\`, `assets\` and
  `output\` into its `sermon_visuals\` folder, run `python -m pip install -e .` there, and confirm
  `python -m sermon_visuals doctor` passes. From then on that clone is the single source of truth.
  A cloud session has been pushing to this branch; pull before you start and treat yourself as the
  owner from here.
- Environment: Windows 11, PowerShell, Python 3.12 from the Microsoft Store (always
  `python -m ...`; scripts are not on PATH), ffmpeg on PATH, Blender installable. PowerShell treats
  `[ ]` in filenames as wildcards: use `-LiteralPath`.
- Keys present: Anthropic and Gemini. **OpenAI has no credit**; do not route anything to it unless
  Blake funds it.

## 4. What exists and how well it is proven

Commands (`python -m sermon_visuals <command>`): `init`, `doctor`, `list`, `run`, `choose`,
`review`, `illustrate`, `studio`, `update`. Tests: `python -m pytest tests` (22, all offline).

| Piece | Module | Proven on Blake's PC with live APIs? |
|---|---|---|
| Transcript from `.txt` / `.pdf`, Gemini fallback | `transcribe.py` | `.txt` yes; `.pdf` and Gemini path only in tests |
| Brief: title, quotes, concepts, flags (Claude) | `extract.py` | yes |
| Paintings (Gemini image) and re-framing per format | `imagegen.py`, `pipeline.py` | yes |
| Cards, thumbnail, still-frame video with verses | `compose.py`, `video.py`, `verses.py` | yes |
| Illustrated video, one sketch per minute | `illustrate.py` | yes (18 sketches, 36 verse pages) |
| **Studio**: word timing, storyboard, budget, layered render | `studio/` | **no, never run by Blake** |

Studio is the current frontier. Verified only here: the frame compositor, budget logic, caching,
and real Whisper alignment on synthetic speech. **Never run live:** the Claude storyboard call
(`studio/plan.py`, streamed structured output) and the Gemini Omni image-to-video call
(`studio/assets.py`, Interactions API). Expect to fix one or both on first contact. Read the SDK's
actual signatures before changing them; do not guess.

Studio in one paragraph: local faster-whisper timestamps every word; Claude plans chapters, one
picture per beat, verbatim key phrases, info cards (list, timeline, definition) and scripture
readings, all referenced by sentence id so no time is invented; the plan is priced and trimmed to
budget; pictures are drawn in parallel from two style anchors; chosen shots are animated from
their own still; `studio/render.py` builds every frame in Python (camera move, ink-in transition,
then text layers) and pipes raw frames to ffmpeg. Assets are cached by content hash, so re-runs
are free and editing one scene in `storyboard_L<n>.json` redraws one picture. Each run writes a
**contact sheet** JPEG of 24 frames.

Levels (`studio/levels.py`), with estimates at November 2026 prices, which drift and must be
re-checked: L1 picture every 40 s (about $5 a program); L2 every 20 s plus key phrases ($9);
L3 every 12 s plus info cards ($15); L4 every 9 s, captions, 30% animated ($55); L5 every 7 s,
70% animated ($110). Pictures about $0.10 each, animation about $0.07 a second at 720p.

Things already learned the hard way:
- Google retired Veo from the Gemini API on 2026-10-22. Video is `gemini-omni-1.2-flash` through
  `client.interactions.create(model=, input=[image, text], response_format={"type": "video", ...})`,
  3 to 15 seconds per clip, audio always generated (we discard it).
- faster-whisper's bundled audio decode breaks on newer PyAV. We decode the WAV ourselves and pass
  samples. Keep it that way.
- Long ffmpeg filtergraphs hit the Windows command-line limit. That is why studio composites
  frames in Python. Do not go back to one giant filtergraph.
- PDF text extraction can put a quoted block out of order. The brief once flagged that as an
  error in the source. It is an extraction artifact.
- The model's default review flags were mostly noise until `ministry.audience_notes` was added.

## 5. What Blake has asked for next

1. **A process that climbs.** More complexity and resolution as board approvals come in.
2. **3D.** He saw a demo where Claude wrote Blender code for a room, rendered 13 shots in 4K on 10
   rented RunPod GPUs in 52 minutes for $5.75. He can install Blender.
3. **Spreading the work across models**, since your session can reach several.

### 5a. The approval ladder (build this first)

Make escalation a recorded decision instead of a flag someone remembers. Proposed:

| Tier | What the board sees | Ceiling | Unlocks |
|---|---|---|---|
| 0 Draft | 5-min clip, placeholder art (`--draft`), real timing and text layers | $1 | pacing and chapter structure |
| 1 Proof | 5-min clip, level 3, half-size (`--preview`) | $5 | the look: style, palette, cards |
| 2 Program | one full program, level 3, 1080p | $20 | publishing at level 3; batch of 5 |
| 3 Motion | 5-min clip at level 5, then one full program at level 4 | $60 | animation in production |
| 4 Sets | reusable 3D sets cut into a level 4 program | $120 | 3D in production |
| 5 Showcase | level 5 plus 3D, 4K masters | $200 | the full treatment, per program |

Implement as an `approvals.toml` beside `config.toml` (tier, approved by, date, note) and a
`--tier` option on `studio` that sets level, resolution and budget from the tier and **refuses to
run above the highest approved tier**. Add an `approve` command that writes the record, and a
`packet` command that assembles what the board needs for the next tier: the clip, the contact
sheet, the cost so far from `spend.json`, the projected cost per program, and a one-page summary
(HTML is fine). Blake forwards packets; keep them self-explanatory and free of jargon.

### 5b. 3D track

Do not replace the illustrated pipeline with 3D. Renting GPUs is cheap (the demo works out to
about $0.66 a GPU-hour; 28 minutes at 4K is roughly $80 to $320 depending on scene weight). The
real cost is building scenes, and code-built Blender is weak at believable robed people, which is
most of a Bible teaching program. Use 3D where it beats a drawing **and gets reused across many
programs**:

- Second Temple: Holy Place with the altar of incense (appears in this very program), courts.
- The wilderness tabernacle.
- A terrain map of Israel for journeys (Nazareth to Bethlehem) and regions.
- Abstract teaching sets: the priestly-course calendar wheel, a festival-year ring.

Build each as a parametric Python script run with `blender --background --python`, kept in a new
`sets/` folder with a manifest (name, cameras, frame ranges, render settings). Render a
low-sample 540p turntable first for approval, then final. Shots enter studio as ordinary clips:
`ShotSpec.clip` already takes any MP4, so add a storyboard field naming a set and camera and have
the asset stage render or fetch it. Local GPU first; RunPod only for finals, by splitting frame
ranges across pods and collecting frames. Price it through the same estimate, cap and ledger. No
human figures in 3D unless Blake approves a specific look.

### 5c. Spreading work across models

The seams already exist: `ImageProvider` (`imagegen.py`) and `MotionProvider`
(`studio/assets.py`) are small protocols, generation is a thread pool, and assets are cached by
content hash, so any model can fill any slot. Add a provider registry and a routing table in
config (task to ordered list of providers with a price each), with fallback on failure and the
price fed into the estimate. Keep one model per program for the pictures themselves: mixing image
models inside one program breaks the single-hand look, which matters more than throughput. Good
candidates for routing elsewhere: transcription, storyboard critique (a second model reviewing the
first one's plan against the rules in section 2), contact-sheet review, animation, upscaling.

You can do something the cloud session could not: **look at the output.** Read the contact sheets
and pull frames with ffmpeg, judge them against the storyboard, and fix what is wrong before
Blake sees it.

## 6. Evidence already gathered (do not redo)

- Education research (Mayer; Brame 2016; Guo, Kim and Rubin 2014): signaling and segmenting help
  reliably; decorative visuals unrelated to the point hurt; engagement fades past about six
  minutes. Expect level 3 to teach best and levels 4 and 5 to hold casual viewers longer. Only
  retention data from published videos will settle it. Consider publishing chapters as their own
  six-minute videos; the storyboard already has chapter boundaries.
- Comparables: CS Lewis Doodle (a dead teacher's radio talks, illustrated, the closest match);
  BibleProject (short animated explainers); Desiring God's *Look at the Book* (text on screen,
  marked up while narrated). After Skool is the stylistic reference Blake named.
- AI image output has unsettled copyright status; Adobe Firefly is the only option with
  indemnity. The ministry has not set an AI disclosure policy. Raise it before publishing.

## 7. First session, in order

1. Clone, migrate, `doctor`, run the tests.
2. `python -m sermon_visuals studio about_christmas --minutes 5 --draft` to prove word timing and
   the storyboard call for cents. Fix what breaks.
3. Same clip at `--level 3`. Read the contact sheet yourself. Check verse timing against the
   audio, that no picture violates section 2, and that key phrases are his exact words.
4. Same clip at `--level 5` to prove the animation call. Watch the spend prompt.
5. Show Blake levels 3 and 5 side by side with costs. Record his reaction in `direction` in
   `config.toml`.
6. Build the approval ladder (5a), then the first 3D set (5b), then routing (5c).

Commit small, push to the branch, keep pull request #2 current, and tell Blake plainly what was
verified and what was not after each step.

## 8. Decisions that belong to Blake or the board

- House style: `graphic` (current default), `ink`, `woodcut`, `painterly`, or a custom sentence.
- Whether captions stay on at level 4 and above (they help muted viewing, and duplicate narration).
- Whether to publish whole programs, six-minute chapters, or both.
- An AI disclosure line for descriptions.
- Whether 3D may ever include human figures.
- Ministry logo and a speaker photo for `assets\` (both currently missing).
