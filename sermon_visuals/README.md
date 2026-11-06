# sermon-visuals

Turns archived sermon or radio-program audio into artwork, quote cards, a YouTube thumbnail and a
ready-to-upload video, one folder per program. Built for the Born to Win archive but nothing in it is
specific to that program; every name, style rule and guardrail lives in `config.toml`.

The design rule that matters most: **the image model never renders words.** It paints a text-free
background in a fixed house style; this tool overlays the title, the speaker's quote and the
scripture reference with Pillow. That removes the misspelled-verse failure mode and keeps every
card on brand. Pull quotes are checked verbatim against the transcript, and a quote that is not
found word-for-word never makes it onto a card.

## Quick start (from zero)

You need a terminal (Terminal on Mac, PowerShell on Windows), Python 3.11 or newer, git, and ffmpeg.

- Python: https://www.python.org/downloads/ (on Windows tick "Add python.exe to PATH" in the installer)
- git: https://git-scm.com/downloads
- ffmpeg: Mac `brew install ffmpeg`; Windows `winget install ffmpeg`; Ubuntu `sudo apt install ffmpeg`

**No git?** Download the code as a ZIP instead:
https://github.com/blakesilvers/Collector/archive/refs/heads/claude/serene-mendel-0btcz1.zip
Unzip it, open a terminal inside the `sermon_visuals` folder, and skip to `pip install -e .` below.

With git, paste these one at a time. The first three fetch the code; the rest set it up.

```bash
git clone https://github.com/blakesilvers/Collector.git
```

```bash
cd Collector
```

```bash
git checkout claude/serene-mendel-0btcz1
```

```bash
cd sermon_visuals
```

```bash
pip install -e .
```

```bash
python -m sermon_visuals init
```

`init` writes two files next to each other: `config.toml` (names, house style, guardrails) and `.env`
(API keys). Open `.env` in any text editor and paste your keys after the `=` signs:

- `ANTHROPIC_API_KEY` from https://console.anthropic.com/settings/keys (the brief)
- `GEMINI_API_KEY` from https://aistudio.google.com/apikey (the paintings)
- `OPENAI_API_KEY` only if you switch `images.provider` to `openai` or need transcription

Put one MP3 in `programs/` (and its transcript as a `.txt` with the same name, if you have one). Then:

```bash
python -m sermon_visuals doctor
```

`doctor` prints OK or FIX for Python, ffmpeg, fonts, keys and inputs, and says what to do about each
FIX. When everything is OK:

```bash
python -m sermon_visuals run
```

Open `output/review.html` in a browser. Everything the program made is in `output/<name>/`.

On Windows, if `python` is not found, try `py` in place of `python`. If `pip` is not found, use `python -m pip`.

## Pipeline

| Stage | What happens | Service |
|---|---|---|
| `transcript` | Uses `<stem>.txt` next to the audio if present, else transcribes | OpenAI transcription (optional) |
| `brief` | Transcript -> title, summary, scripture refs, pull quotes, two image concepts, thumbnail hook, review flags | Claude (structured output) |
| `images` | Each concept -> N candidate paintings in 16:9, house style + optional reference images | Gemini (Nano Banana) or OpenAI Images |
| `compose` | Chosen art + text -> YouTube frame, thumbnail, square and vertical quote cards | Pillow |
| `video` | Frame + audio (+ optional `.srt` burned in) -> MP4 | ffmpeg |

Everything is idempotent. A `manifest.json` in each program folder records what is done; re-running
skips finished stages, and `--force` redoes them.

## Configuration

`config.toml` holds the program name, speaker, house-style prompt, the `avoid` list and
`depict_christ`. Put the ministry logo, a speaker photo and (optionally) one or two style reference
images in `assets/` and name them in the config. Fonts are found automatically (Georgia, DejaVu
Serif or Liberation Serif); set `compose.font_serif` to a `.ttf` path to use something else.

Keys live in `.env` next to the config. Environment variables with the same names take precedence,
so the tool also works in CI or on a server without the file.

## Daily use

```bash
# drop audio (and transcripts / captions if you have them) into programs/
#   programs/btw-2024-03-11.mp3
#   programs/btw-2024-03-11.txt   (optional, skips transcription)
#   programs/btw-2024-03-11.srt   (optional, burned into the video)

python -m sermon_visuals run                      # all programs, all stages
python -m sermon_visuals run --only btw-2024-03-11 --stages brief,images
python -m sermon_visuals list                     # progress and errors per program
```

`run` writes `output/review.html`. Open it, look at the candidate paintings and the quotes, then pick
the painting you want on the cards:

```bash
python -m sermon_visuals choose btw-2024-03-11 concept2_1.png
python -m sermon_visuals run --only btw-2024-03-11 --stages compose,video
```

Output per program:

```
output/btw-2024-03-11/
  transcript.txt            (only if transcribed here)
  brief.json
  manifest.json
  art/concept1_1.png ...    candidate paintings, no text
  cards/youtube_frame.png   1920x1080 title frame used in the video
  cards/thumbnail.png       1920x1080 with the short hook instead of the title
  cards/quote1_square.png   1080x1080
  cards/quote1_vertical.png 1080x1920
  btw-2024-03-11.mp4
```

## Review before publishing

The review page flags, per program:

- quotes not found verbatim in the transcript (red; they are also excluded from cards);
- `content_flags` from the brief: anything the extractor thought a human should check;
- any stage that failed and why.

Look at every painting before it goes out. Models still produce odd hands, mangled symbols and
accidental lettering. The `negative` prompt reduces that; it does not eliminate it.

## Switching providers and models

`images.provider` is `gemini` or `openai`. Model IDs are plain strings in the config so they can be
changed without touching code. Reference images, when given, are sent with every request so the
look stays consistent across hundreds of programs; generate a few paintings you like, drop them in
`assets/`, list them under `style.reference_images`.

Adding a provider means implementing one method, `generate(prompt, aspect, references) -> bytes`, in
`sermon_visuals/imagegen.py` and adding it to `make_provider`.

## Rough cost per program

Figures from the vendors' pricing pages on 2026-11-06; check them before budgeting.

- Brief: one Claude call over a ~30-minute transcript, a few cents.
- Images: 2 concepts x 2 candidates = 4 images. Nano Banana 2 at 2K is about $0.10 per image,
  so roughly $0.40. GPT Image 2 at medium quality is in the same range.
- Transcription (only when no transcript exists): under $0.01 per minute of audio on the mini model.
- Compose and video: free, local.

## Tests

```bash
pip install pytest
python -m pytest tests
```

The tests stub Claude and the image providers, so they run with no keys and no network. The video
test is skipped when ffmpeg is absent.
