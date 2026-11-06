# sermon-visuals

Turns archived sermon or radio-program audio into artwork, quote cards, a YouTube thumbnail and a
ready-to-upload video, one folder per program. Built for the Born to Win archive but nothing in it is
specific to that program; every name, style rule and guardrail lives in `config.toml`.

The design rule that matters most: **the image model never renders words.** It paints a text-free
background in a fixed house style; this tool overlays the title, the speaker's quote and the
scripture reference with Pillow. That removes the misspelled-verse failure mode and keeps every
card on brand. Pull quotes are checked verbatim against the transcript, and a quote that is not
found word-for-word never makes it onto a card.

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

## Setup

```bash
cd sermon_visuals
pip install -e .            # pulls anthropic, google-genai, openai, pillow, pydantic
python -m sermon_visuals init
```

`init` copies `config.example.toml` to `config.toml` and creates `programs/`, `assets/`, `output/`.
Edit the config: program name, speaker, house-style prompt, the `avoid` list and `depict_christ`.
Put the ministry logo, a speaker photo and (optionally) one or two style reference images in `assets/`.

Keys go in the environment, never in the config:

```bash
export ANTHROPIC_API_KEY=...     # brief
export GEMINI_API_KEY=...        # images, provider = "gemini"
export OPENAI_API_KEY=...        # images, provider = "openai"; transcription
```

ffmpeg must be on PATH for the video stage.

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
