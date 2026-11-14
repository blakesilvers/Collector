"""Offline tests: no API keys needed. Image generation and Claude are stubbed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from sermon_visuals import compose, env, extract, pipeline, video
from sermon_visuals.cli import main
from sermon_visuals.config import load_config
from sermon_visuals.models import Brief, PullQuote, VisualConcept

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent / "sermon_visuals" / "config.example.toml"

TRANSCRIPT = (
    "Good morning. Today I want to talk about the beatitudes, Matthew chapter five. "
    "Blessed are the poor in spirit. Now, people hear that and they think it means something it doesn't. "
    "The kingdom of God isn't a place you go; it's a government you live under. Think about that for a minute.\n"
    "And so we come to the end of the program for today. Until next time, this is Ron Dart."
)


def fake_brief() -> Brief:
    return Brief(
        title="A Government You Live Under",
        one_line="What the beatitudes really say about the kingdom.",
        description="Ron Dart walks through Matthew 5 and what 'poor in spirit' means.",
        primary_scripture="Matthew 5:3",
        themes=["kingdom of God", "beatitudes"],
        pull_quotes=[
            PullQuote(text="The kingdom of God isn't a place you go; it's a government you live under.", why="Memorable reframing."),
            PullQuote(text="The kingdom is a government you choose", why="Paraphrase, should fail verification."),
        ],
        visual_concepts=[
            VisualConcept(name="Hillside at dawn", image_prompt="A Galilean hillside at first light, wild grass, distant lake.", rationale="Sermon on the Mount setting."),
        ],
        thumbnail_text="Not a place, a government",
        content_flags=[],
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    cfg_text = re.sub(r'^provider = "gemini"         # gemini.*$', 'provider = "none"', EXAMPLE.read_text(encoding="utf-8"), count=1, flags=re.M)
    (tmp_path / "config.toml").write_text(cfg_text, encoding="utf-8")
    (tmp_path / "programs").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "programs" / "btw-0001.txt").write_text(TRANSCRIPT, encoding="utf-8")
    return tmp_path


def make_art(path: Path, size=(1536, 864)) -> Path:
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(0, size[0], 4):
            c = (120 + y * 100 // size[1], 90, 60 + x * 120 // size[0])
            for dx in range(4):
                if x + dx < size[0]:
                    px[x + dx, y] = c
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt: str, aspect: str, references) -> bytes:
        self.calls.append(prompt)
        import io
        buf = io.BytesIO()
        Image.new("RGB", (768, 432), (140, 100, 70)).save(buf, "PNG")
        return buf.getvalue()


def test_config_loads_example(workspace: Path):
    cfg = load_config(workspace / "config.toml")
    assert cfg.ministry.program_name == "Born to Win"
    assert cfg.images.provider == "gemini"
    assert cfg.transcribe.provider == "none"
    assert "No text" in cfg.style.negative or "no text" in cfg.style.negative.lower()
    assert cfg.style.depict_christ is False


def test_verify_quotes_requires_verbatim():
    checks = extract.verify_quotes(fake_brief(), TRANSCRIPT)
    assert [c.verified for c in checks] == [True, False]


def test_normalize_ignores_punctuation_and_case():
    assert extract._normalize("It’s a Government—you live under!") == "it's a government you live under"


def test_quote_card_and_title_frame(tmp_path: Path):
    art = make_art(tmp_path / "art.png")
    fonts = compose.Fonts.resolve()
    long_quote = " ".join(["word"] * 60)
    for name, fmt in compose.FORMATS.items():
        out = compose.render_quote_card(art, tmp_path / f"q_{name}.png", fmt, fonts, long_quote, "— Ron Dart", "Born to Win")
        with Image.open(out) as im:
            assert im.size == (fmt.width, fmt.height)
    out = compose.render_title_frame(art, tmp_path / "t.png", compose.FORMATS["youtube"], fonts, "A Government You Live Under", "Ron Dart · Matthew 5:3", "Born to Win")
    with Image.open(out) as im:
        assert im.size == (1920, 1080)


def test_fit_text_shrinks_to_fit(tmp_path: Path):
    from PIL import ImageDraw
    img = Image.new("RGB", (400, 200))
    draw = ImageDraw.Draw(img)
    make = lambda s: compose.ImageFont.truetype(compose.Fonts.resolve().sans, s)
    font, lines, line_h = compose.fit_text(draw, " ".join(["lorem"] * 40), make, 360, 180, start=60, minimum=10)
    assert len(lines) * line_h <= 180
    assert all(draw.textlength(l, font=font) <= 360 for l in lines)


def test_ffmpeg_command_escapes_caption_path(tmp_path: Path):
    cmd = video.build_command(tmp_path / "f.png", tmp_path / "a.mp3", tmp_path / "o.mp4", Path("C:/x/it's.srt"))
    vf = cmd[cmd.index("-vf") + 1]
    assert "subtitles='C\\:/x/it\\'s.srt'" in vf
    assert "-shortest" in cmd


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_video_end_to_end(tmp_path: Path):
    frame = make_art(tmp_path / "frame.png", (1920, 1080))
    audio = tmp_path / "a.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1", str(audio)], check=True)
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    out = video.render_video(frame, audio, tmp_path / "out.mp4", srt)
    assert out.exists() and out.stat().st_size > 1000


def test_pipeline_offline(workspace: Path, monkeypatch):
    cfg = load_config(workspace / "config.toml")
    monkeypatch.setattr(extract, "extract_brief", lambda cfg, text: fake_brief())
    provider = FakeProvider()
    monkeypatch.setattr(pipeline.imagegen, "make_provider", lambda cfg: provider)

    m = pipeline.run_program(cfg, "btw-0001", ["transcript", "brief", "images", "compose"])
    assert m.errors == {}
    assert m.done == ["transcript", "brief", "images", "compose"]
    assert len(m.art) == cfg.images.candidates  # one concept x candidates
    assert len(provider.calls) == cfg.images.candidates + 2  # plus square + vertical re-framings
    assert all("no text" in p.lower() for p in provider.calls)
    assert "Do not depict Jesus" in provider.calls[0]
    assert "Recompose this exact scene as a 1:1 image" in provider.calls[cfg.images.candidates]
    assert m.chosen_art == m.art[0].path
    assert Path(m.chosen_art).with_name("concept1_1_square.png").exists()
    # quote 1 verified -> square + vertical cards; quote 2 unverified -> no cards
    assert {"youtube_frame", "thumbnail", "quote1_square", "quote1_vertical"} == set(m.cards)
    brief = json.loads(Path(m.brief).read_text(encoding="utf-8"))
    assert brief["title"] == "A Government You Live Under"

    # Re-running does nothing new.
    calls_before = len(provider.calls)
    m2 = pipeline.run_program(cfg, "btw-0001", ["images"])
    assert len(provider.calls) == calls_before and len(m2.art) == cfg.images.candidates

    # video stage fails cleanly without audio, and leaves earlier stages intact.
    m3 = pipeline.run_program(cfg, "btw-0001", ["video"])
    assert "video" in m3.errors and "compose" in m3.done


def test_cli_choose_and_review(workspace: Path, monkeypatch, capsys):
    cfg = load_config(workspace / "config.toml")
    monkeypatch.setattr(extract, "extract_brief", lambda cfg, text: fake_brief())
    monkeypatch.setattr(pipeline.imagegen, "make_provider", lambda cfg: FakeProvider())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.chdir(workspace)
    assert main(["run", "--stages", "transcript,brief,images,compose"]) == 0
    assert (workspace / "output" / "review.html").exists()
    assert main(["choose", "btw-0001", "concept1_2.png"]) == 0
    m = pipeline.load_manifest(cfg, "btw-0001")
    assert m.chosen_art.endswith("concept1_2.png") and "compose" not in m.done
    assert main(["choose", "btw-0001", "nope.png"]) == 1
    assert main(["list"]) == 0
    assert "btw-0001" in capsys.readouterr().out


def test_dotenv_and_key_preflight(workspace: Path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(workspace / "config.toml")
    assert env.missing_keys(cfg, ["transcript", "brief", "images"]) == ["ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
    assert env.required_keys(cfg, ["compose"]) == ["GEMINI_API_KEY"]  # re-framing square/vertical cards
    cfg.images.recompose = False
    assert env.required_keys(cfg, ["compose"]) == []
    cfg.images.recompose = True
    cfg.transcribe.provider = "openai"
    assert env.needs_transcription(cfg, ["btw-0001"]) is False  # sidecar .txt exists
    assert env.needs_transcription(cfg, ["btw-0002"]) is True
    assert "OPENAI_API_KEY" in env.missing_keys(cfg, ["transcript"], transcription=True)
    assert env.missing_keys(cfg, ["transcript"], transcription=False) == []
    cfg.transcribe.provider = "gemini"
    assert env.required_keys(cfg, ["transcript"], transcription=True) == ["GEMINI_API_KEY"]
    (workspace / ".env").write_text("# keys\nANTHROPIC_API_KEY=sk-test\nGEMINI_API_KEY='g-test'\nOPENAI_API_KEY=\n", encoding="utf-8")
    assert env.load_dotenv(workspace / ".env") == ["ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
    assert env.missing_keys(cfg, ["brief", "images"]) == []
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    assert main(["run", "--stages", "brief"]) == 2  # refuses before calling anything


def test_init_and_doctor(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert main(["init"]) == 0
    assert (tmp_path / "config.toml").exists() and (tmp_path / ".env").exists()
    assert (tmp_path / "programs").is_dir()
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FIX  ANTHROPIC_API_KEY" in out and "0 program(s)" in out


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a real xref table, so pypdf can read it."""
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_transcript_is_used(workspace: Path, monkeypatch):
    from sermon_visuals import transcribe
    (workspace / "programs" / "btw-0001.txt").unlink()
    (workspace / "programs" / "btw-0001.pdf").write_bytes(minimal_pdf("Blessed are the poor in spirit."))
    cfg = load_config(workspace / "config.toml")
    assert env.needs_transcription(cfg, ["btw-0001"]) is False
    path = transcribe.get_transcript(cfg, "btw-0001", None, workspace / "output" / "btw-0001")
    assert path.read_text(encoding="utf-8").strip() == "Blessed are the poor in spirit."
    assert "btw-0001" in pipeline.discover_stems(cfg)


def test_validate_segments_fixes_gaps_overlaps_and_slivers():
    from sermon_visuals.illustrate import Segment, validate_segments
    raw = [
        Segment(start=0, end=30, gist="a", illustration="x"),
        Segment(start=28, end=61, gist="b", illustration="y"),   # overlaps previous
        Segment(start=61, end=64, gist="c", illustration="z"),   # sliver -> merged into b
        Segment(start=70, end=500, gist="d", illustration="w"),  # gap before, runs past the end
    ]
    fixed = validate_segments(raw, duration=120.0, minimum=8.0)
    assert [(s.start, s.end) for s in fixed] == [(0.0, 30.0), (30.0, 64.0), (64.0, 120.0)]
    assert fixed[1].gist == "b"


def test_assembly_command_shape(tmp_path: Path):
    from sermon_visuals.illustrate import build_assembly_command
    frames = [(tmp_path / "a.png", 10.0), (tmp_path / "b.png", 5.0), (tmp_path / "c.png", 7.0)]
    cmd = build_assembly_command(frames, tmp_path / "a.mp3", tmp_path / "o.mp4", fps=24, fade=0.5)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("zoompan") == 3 and fc.count("xfade") == 2
    assert "offset=9.500" in fc and "offset=14.000" in fc
    assert cmd[cmd.index("-map") + 3] == "3:a"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_illustrate_stage_offline(workspace: Path, monkeypatch):
    from sermon_visuals.illustrate import Segment
    cfg = load_config(workspace / "config.toml")
    audio = workspace / "programs" / "btw-0001.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "6", str(audio)], check=True)
    monkeypatch.setattr(extract, "extract_brief", lambda cfg, text: fake_brief())
    provider = FakeProvider()
    monkeypatch.setattr(pipeline.imagegen, "make_provider", lambda cfg: provider)
    m = pipeline.run_program(cfg, "btw-0001", ["transcript"])
    segments = [
        Segment(start=0, end=2.5, gist="opening", illustration="a lamp on a table"),
        Segment(start=2.5, end=6, gist="closing", illustration="a road at dusk"),
    ]
    cfg.illustrate.title_seconds = 0
    cfg.illustrate.crossfade = 0.4
    from sermon_visuals.illustrate import write_scriptures
    from sermon_visuals.verses import Scripture
    write_scriptures([Scripture(start=1.0, end=4.0, reference="Luke 2:10", version="AKJV",
                                text="Fear not: for, behold, I bring you good tidings of great joy, which shall be to all people.")],
                     workspace / "output" / "btw-0001" / "illustrated" / "verses.json")
    pipeline.stage_illustrate(cfg, m, provider=provider, segments=segments)
    assert (workspace / "output" / "btw-0001" / "illustrated" / "verse_overlays_bottom" / "verse_001_1.png").exists()
    assert len(m.sketches) == 2 and all(Path(p).exists() for p in m.sketches)
    assert "ink marker sketch" in provider.calls[0] and "a lamp on a table" in provider.calls[0]
    out = Path(m.illustrated)
    assert out.exists() and out.stat().st_size > 1000
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, check=True).stdout.strip())
    assert 5.0 <= dur <= 6.5


def test_paginate_and_validate_scriptures():
    from sermon_visuals.verses import Scripture, paginate, validate_scriptures
    text = " ".join(f"word{i}." for i in range(1, 121))  # 120 one-word sentences
    pages = paginate(text, 55)
    assert len(pages) == 3 and all(len(p.split()) <= 55 for p in pages)
    assert paginate("short passage", 55) == ["short passage"]
    items = [
        Scripture(start=10, end=20, reference="John 1:1", text="In the beginning was the Word"),
        Scripture(start=15, end=25, reference="John 1:2", text="The same was in the beginning with God"),
        Scripture(start=30, end=30.5, reference="x", text="too short"),
        Scripture(start=40, end=50, reference="", text="no reference"),
    ]
    fixed = validate_scriptures(items, duration=45.0)
    assert [(s.reference, s.start, s.end) for s in fixed] == [("John 1:1", 10.0, 20.0), ("John 1:2", 20.0, 25.0)]


def test_verse_overlay_render_and_filter(tmp_path: Path):
    from sermon_visuals.verses import Overlay, Scripture, build_overlays, overlay_filter
    fonts = compose.Fonts.resolve()
    long_text = " ".join(["And the angel said unto them, Fear not."] * 20)  # ~160 words -> pages
    ovs = build_overlays([Scripture(start=10, end=22, reference="Luke 2:10", text=long_text)], tmp_path, 1920, 1080, fonts, 55, 4.0)
    assert len(ovs) == 3 and ovs[0].start == 10 and abs(ovs[-1].end - 22) < 1e-6
    # A short window never drops words: pages get denser, then faster.
    short = build_overlays([Scripture(start=0, end=6, reference="Luke 2:10", text=long_text)], tmp_path / "short", 1920, 1080, fonts, 55, 4.0)
    shown = " ".join(p for p in [Path(o.png).name for o in short])
    assert len(short) >= 2 and abs(short[-1].end - 6) < 1e-6
    top = build_overlays([Scripture(start=0, end=6, reference="John 1:1", text="In the beginning was the Word")], tmp_path / "top", 1920, 1080, fonts, position="top")
    with Image.open(top[0].png) as im:
        assert im.getpixel((960, 60))[3] > 0 and im.getpixel((960, 1000))[3] == 0  # band at the top, not the bottom
    with Image.open(ovs[0].png) as im:
        assert im.size == (1920, 1080) and im.mode == "RGBA"
    inputs, flt, last = overlay_filter("v9", 4, ovs)
    assert inputs.count("-i") == 3 and last == "vo2" and "between(t,10.000,14.000)" in flt
    assert overlay_filter("v9", 4, []) == ([], "", "v9")
    cmd = video.build_command(tmp_path / "f.png", tmp_path / "a.mp3", tmp_path / "o.mp4", overlays=ovs)
    assert "-filter_complex" in cmd and cmd[cmd.index("-map") + 1] == "[vo2]"


def test_update_applies_zip_without_touching_user_files(tmp_path: Path):
    import io, zipfile
    from sermon_visuals import update
    root = tmp_path / "install"
    (root / "sermon_visuals").mkdir(parents=True)
    (root / "sermon_visuals" / "old.py").write_text("old")
    (root / "config.toml").write_text("mine")
    (root / ".env").write_text("secret")
    (root / "programs").mkdir(); (root / "programs" / "a.mp3").write_bytes(b"x")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Collector-branch/README.md", "repo readme")
        zf.writestr("Collector-branch/sermon_visuals/pyproject.toml", "[project]\nname='x'")
        zf.writestr("Collector-branch/sermon_visuals/README.md", "new readme")
        zf.writestr("Collector-branch/sermon_visuals/sermon_visuals/__init__.py", "new")
        zf.writestr("Collector-branch/sermon_visuals/sermon_visuals/config.example.toml", "example")
        zf.writestr("Collector-branch/sermon_visuals/tests/test_x.py", "pass")
    replaced = update.apply_zip(buf.getvalue(), root)
    assert set(replaced) == {"sermon_visuals", "tests", "pyproject.toml", "README.md"}
    assert not (root / "sermon_visuals" / "old.py").exists()
    assert (root / "sermon_visuals" / "__init__.py").read_text() == "new"
    assert (root / "README.md").read_text() == "new readme"
    assert (root / "config.toml").read_text() == "mine" and (root / ".env").read_text() == "secret"
    assert (root / "programs" / "a.mp3").exists()


# ---------- studio ----------

def _studio_words():
    from sermon_visuals.studio.plan import Word
    text = ("What did the first Christians believe about Christmas? They had no idea what you were talking about. "
            "The conception of John was in June. The angel came to Mary in December. And the birth of Jesus was in late September.")
    words, t = [], 0.2
    for w in text.split():
        words.append(Word(w=w, s=round(t, 2), e=round(t + 0.25, 2)))
        t += 0.32 + (0.5 if w[-1] in ".?" else 0)
    return words


def _studio_board():
    from sermon_visuals.studio import plan
    return plan.Storyboard(
        title="About Christmas",
        chapters=[plan.Chapter(first=0, title="A loaded question"), plan.Chapter(first=2, title="Counting the months")],
        beats=[
            plan.Beat(first=0, last=0, scene="A house church at night.", motion="Lamps flicker.", hero=True, key_phrase="the first Christians believe"),
            plan.Beat(first=1, last=1, scene="A puzzled fisherman.", motion="He shrugs.", hero=False, key_phrase="words he never said"),
            plan.Beat(first=2, last=4, scene="A ring of seasons.", motion="Slow turn.", hero=False, key_phrase="",
                      card=plan.Card(type="timeline", title="The months", items=[
                          plan.CardItem(label="June", detail="John conceived"), plan.CardItem(label="December", detail="The angel comes"),
                          plan.CardItem(label="Late September", detail="Jesus born")])),
        ],
        readings=[],
    )


def test_studio_levels_and_overrides():
    from sermon_visuals.studio.levels import LEVELS, layers_for
    assert [LEVELS[k].beat_seconds for k in range(1, 6)] == sorted((LEVELS[k].beat_seconds for k in range(1, 6)), reverse=True)
    assert LEVELS[3].motion_share == 0 and LEVELS[5].motion_share > LEVELS[4].motion_share > 0
    assert layers_for(5, {"captions": False, "motion_share": 0.2}).captions is False
    with pytest.raises(ValueError):
        layers_for(3, {"sparkles": True})


def test_studio_sentences_phrases_and_repair():
    from sermon_visuals.studio import plan
    words = _studio_words()
    sents = plan.sentences_from_words(words)
    assert len(sents) == 5 and sents[0].text.endswith("Christmas?") and sents[1].first == sents[0].last + 1
    timed = plan.phrase_times("the first Christians believe", words, sents[0].first, sents[0].last)
    assert [w for w, _ in timed] == ["the", "first", "Christians", "believe"] and timed[0][1] == words[2].s
    assert plan.phrase_times("words he never said", words, 0, len(words) - 1) == []
    messy = plan.Storyboard(title="T", chapters=[plan.Chapter(first=3, title="Late")], readings=[], beats=[
        plan.Beat(first=1, last=1, scene="a", motion="", hero=False, key_phrase=""),
        plan.Beat(first=1, last=2, scene="b", motion="", hero=False, key_phrase=""),
        plan.Beat(first=9, last=12, scene="c", motion="", hero=False, key_phrase=""),
    ])
    fixed = plan.repair(messy, 5)
    assert [(b.first, b.last) for b in fixed.beats] == [(0, 1), (2, 2), (3, 4)]
    assert fixed.chapters[0].first == 0 and fixed.chapters[-1].first == 3


def test_studio_animation_choice_and_budget(tmp_path: Path):
    from sermon_visuals.studio import assets, plan
    beats = [plan.Beat(first=i, last=i, scene=f"s{i}", motion="m", hero=(i % 3 == 0), key_phrase="") for i in range(10)]
    shots = [assets.Shot(index=i, start=i * 8.0, end=(i + 1) * 8.0, beat=b) for i, b in enumerate(beats)]
    assets.choose_animated(shots, 0.5)
    chosen = [s.index for s in shots if s.animate]
    assert len(chosen) == 5 and {0, 3, 6, 9} <= set(chosen)  # heroes first
    assert all(s.clip_seconds == 8 for s in shots if s.animate)
    assets.assign_paths(shots, tmp_path, "style", "guard", "m")
    est = assets.estimate(shots, 0.10, 0.07, 0.0)
    assert est.new_images == 10 and est.new_clip_seconds == 40 and abs(est.total - (1.0 + 2.8)) < 1e-6
    dropped = assets.fit_to_budget(shots, 2.0, 0.10, 0.07, 0.0)
    kept = [s.index for s in shots if s.animate]
    assert dropped == 4 and len(kept) == 1 and shots[kept[0]].beat.hero  # non-heroes dropped first
    assert assets.estimate(shots, 0.10, 0.07, 0.0).total <= 2.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_studio_end_to_end_offline(workspace: Path):
    from sermon_visuals.studio import assets
    from sermon_visuals.studio.run import run_studio
    cfg = load_config(workspace / "config.toml")
    audio = workspace / "programs" / "btw-0001.mp3"
    words = _studio_words()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", f"{words[-1].e + 1:.2f}", str(audio)], check=True)

    class FakeMotion:
        model = "fake"
        calls = 0

        def animate(self, image, prompt, seconds):
            FakeMotion.calls += 1
            out = workspace / f"fake_{FakeMotion.calls}_{seconds}.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(image), "-t", str(seconds), "-vf", "scale=320:180",
                            "-r", "12", "-pix_fmt", "yuv420p", str(out)], check=True)
            return out.read_bytes()

    cfg.studio.layers = {"motion_share": 0.3}
    asked = []
    res = run_studio(cfg, "btw-0001", level=5, preview=True, provider=assets.DraftArt(), motion=FakeMotion(), words=words, board=_studio_board(),
                     confirm=lambda q: asked.append(q) or "y")
    assert asked and "Spend about" in asked[0]
    video = Path(res["video"])
    assert video.exists() and Path(res["contact_sheet"]).exists() and res["animated"] >= 1 and FakeMotion.calls >= 1
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True, check=True).stdout.strip())
    assert abs(dur - (words[-1].e + 1)) < 0.6
    spent = res["spent"]
    # Second run: everything is cached, nothing is generated or charged, and "no" is never asked.
    res2 = run_studio(cfg, "btw-0001", level=5, preview=True, provider=assets.DraftArt(), motion=FakeMotion(), words=words,
                      confirm=lambda q: (_ for _ in ()).throw(AssertionError("should not ask")))
    assert res2["spent"] == spent and res2["estimate"] == 0
    # Declining the prompt spends nothing.
    res3 = run_studio(cfg, "btw-0001", level=5, minutes=0.2, preview=True, provider=assets.DraftArt(), motion=FakeMotion(), words=words,
                      board=_studio_board(), confirm=lambda q: "n")
    assert "video" not in res3


# ---------- content guards ----------

NO_SCRIPT = "No written words, letters, script, inscriptions or scroll text in any language"
NO_DIVINE = "Do not depict Jesus, God, or any divine figure in any form, including as an infant."


def _all_prompts(cfg) -> dict[str, str]:
    """The text each of the five prompt builders would send to an image or video model."""
    from sermon_visuals import illustrate, imagegen
    from sermon_visuals.studio import assets, plan
    beat = plan.Beat(first=0, last=0, scene="A scroll covered in inscribed script.", motion="The camera drifts.", hero=False, key_phrase="")
    guard = imagegen.content_guard(cfg)
    return {
        "build_prompt": imagegen.build_prompt(cfg, "A hillside at dawn."),
        "recompose_prompt": imagegen.recompose_prompt(cfg, "1:1"),
        "sketch_prompt": illustrate.sketch_prompt(cfg, "A lamp on a table."),
        "image_prompt": assets.image_prompt("Bold graphic style.", beat.scene, guard),
        "motion_prompt": assets.motion_prompt(beat, guard),
    }


def test_every_prompt_carries_the_content_guards(workspace: Path):
    cfg = load_config(workspace / "config.toml")
    assert cfg.style.negative and cfg.style.avoid and not cfg.style.depict_christ
    avoid = "Do not show: " + "; ".join(cfg.style.avoid) + "."
    for name, text in _all_prompts(cfg).items():
        for guard in (cfg.style.negative, NO_SCRIPT, NO_DIVINE, avoid):
            assert guard in text, f"{name} is missing: {guard}"


def test_content_guard_fallback_and_depict_switch(workspace: Path):
    from sermon_visuals import imagegen
    cfg = load_config(workspace / "config.toml")
    cfg.style.negative = ""
    for name, text in _all_prompts(cfg).items():
        assert imagegen.NO_TEXT_FALLBACK in text and NO_SCRIPT in text and NO_DIVINE in text, name
    cfg = load_config(workspace / "config.toml")
    cfg.style.depict_christ = True
    for name, text in _all_prompts(cfg).items():
        assert NO_DIVINE not in text and "divine" not in text, name
        assert cfg.style.negative in text and NO_SCRIPT in text and "Do not show: " in text, name
    cfg.style.avoid = []
    assert all("Do not show" not in t for t in _all_prompts(cfg).values())


def test_studio_sends_guards_and_cache_key_follows_them(workspace: Path, monkeypatch):
    from sermon_visuals import imagegen
    from sermon_visuals.studio import assets, plan
    cfg = load_config(workspace / "config.toml")
    beats = [plan.Beat(first=i, last=i, scene=f"scene {i}", motion="m", hero=True, key_phrase="") for i in range(2)]
    shots = [assets.Shot(index=i, start=i * 5.0, end=(i + 1) * 5.0, beat=b, animate=True, clip_seconds=5) for i, b in enumerate(beats)]
    assets.assign_paths(shots, workspace / "a", "style", imagegen.content_guard(cfg), "m")
    first = [(s.image, s.clip) for s in shots]

    sent: list[str] = []

    class Pictures(assets.DraftArt):
        def generate(self, prompt, aspect, references):
            sent.append(prompt)
            return super().generate(prompt, aspect, references)

    class Motion:
        model = "m"

        def animate(self, image, prompt, seconds):
            sent.append(prompt)
            return b"clip"

    assets.generate_assets(cfg, shots, "style", Pictures(), Motion(), assets.Ledger(workspace / "spend.json"), 0.1, 0.07, workers=1)
    assert len(sent) == 4 and all(NO_DIVINE in p and NO_SCRIPT in p and cfg.style.negative in p and "Do not show: " in p for p in sent)

    # Changing any guard changes where the picture and clip are cached, so they are drawn again with the new wording.
    changes = {"avoid": lambda c: setattr(c.style, "avoid", c.style.avoid + ["candles"]),
               "negative": lambda c: setattr(c.style, "negative", "No lettering at all."),
               "depict": lambda c: setattr(c.style, "depict_christ", True)}
    for name, change in changes.items():
        changed = load_config(workspace / "config.toml")
        change(changed)
        assets.assign_paths(shots, workspace / "a", "style", imagegen.content_guard(changed), "m")
        assert all(s.image != a and s.clip != b for s, (a, b) in zip(shots, first)), name
        sent.clear()
        assets.generate_assets(changed, shots, "style", Pictures(), Motion(), assets.Ledger(workspace / "spend.json"), 0.1, 0.07, workers=1)
        assert len(sent) == 4, name  # drawn and animated again, not reused from the old files


# ---------- animation model missing ----------

def _animated_shots(workspace: Path, n: int):
    from sermon_visuals import imagegen
    from sermon_visuals.studio import assets, plan
    cfg = load_config(workspace / "config.toml")
    beats = [plan.Beat(first=i, last=i, scene=f"scene {i}", motion="m", hero=True, key_phrase="") for i in range(n)]
    shots = [assets.Shot(index=i, start=i * 5.0, end=(i + 1) * 5.0, beat=b, animate=True, clip_seconds=5) for i, b in enumerate(beats)]
    assets.assign_paths(shots, workspace / "a", "style", imagegen.content_guard(cfg), "omni-missing")
    return cfg, shots


def test_missing_animation_model_stops_animation_once(workspace: Path, monkeypatch):
    import httpx2
    from google.genai import _interactions as gi

    from sermon_visuals.studio import assets
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    cfg, shots = _animated_shots(workspace, 4)
    req = httpx2.Request("POST", "https://generativelanguage.googleapis.com/v1beta/interactions")

    class Missing:
        model = "omni-missing"
        calls = 0

        def animate(self, image, prompt, seconds):
            Missing.calls += 1
            raise gi.NotFoundError("models/omni-missing is not found", response=httpx2.Response(404, request=req), body=None)

    report = assets.generate_assets(cfg, shots, "style", assets.DraftArt(), Missing(), assets.Ledger(workspace / "spend.json"), 0.0, 0.07, workers=1)
    assert Missing.calls == 1  # no retry, and the other three clips are not attempted
    assert sorted(report.clip_failures) == [0, 1, 2, 3] and report.clips_made == 0
    assert "omni-missing" in report.motion_stopped and "motion_model" in report.motion_stopped
    assert "Nothing was charged" not in report.motion_stopped  # not provable when other clips may already have been made
    assert all(not s.animate and s.clip is None for s in shots)
    assert json.loads((workspace / "spend.json").read_text(encoding="utf-8")).get("clip_seconds", 0) == 0


def test_missing_animation_model_with_parallel_workers(workspace: Path, monkeypatch):
    import threading

    from sermon_visuals.studio import assets
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    cfg, shots = _animated_shots(workspace, 12)
    gate = threading.Barrier(4, timeout=5)

    class Missing:
        model = "omni-missing"
        calls = 0
        lock = threading.Lock()

        def animate(self, image, prompt, seconds):
            with Missing.lock:
                Missing.calls += 1
                n = Missing.calls
            if n <= 4:
                gate.wait()  # four clips are in flight at once before the first failure comes back
            raise RuntimeError("404 NOT_FOUND. models/omni-missing is not found for API version v1beta")

    report = assets.generate_assets(cfg, shots, "style", assets.DraftArt(), Missing(), assets.Ledger(workspace / "spend.json"), 0.0, 0.07, workers=4)
    assert Missing.calls == 4  # the four already in flight; none retried, none of the other eight started
    assert sorted(report.clip_failures) == list(range(12)) and report.motion_stopped.count("omni-missing") == 1


def test_passing_animation_errors_are_still_retried(workspace: Path, monkeypatch):
    from sermon_visuals.studio import assets
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    cfg, shots = _animated_shots(workspace, 2)

    class Flaky:
        model = "m"
        calls = 0

        def animate(self, image, prompt, seconds):
            Flaky.calls += 1
            raise RuntimeError("the video model returned no video")

    report = assets.generate_assets(cfg, shots, "style", assets.DraftArt(), Flaky(), assets.Ledger(workspace / "spend.json"), 0.0, 0.07, workers=1)
    assert Flaky.calls == 4 and report.motion_stopped == ""  # two tries per clip, as before


# ---------- storyboard call failures ----------

class _FakeStream:
    def __init__(self, fail_on_enter=None, fail_on_final=None, message=None):
        self.fail_on_enter, self.fail_on_final, self.message = fail_on_enter, fail_on_final, message

    def __enter__(self):
        if self.fail_on_enter:
            raise self.fail_on_enter
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if self.fail_on_final:
            raise self.fail_on_final
        return self.message


class _FakeClaude:
    def __init__(self, stream: _FakeStream):
        outer = self

        class Messages:
            def stream(self, **kwargs):
                outer.kwargs = kwargs
                return stream

        self.messages = Messages()


def _plan_inputs(workspace: Path):
    from sermon_visuals.studio import plan
    from sermon_visuals.studio.levels import layers_for
    cfg = load_config(workspace / "config.toml")
    return cfg, plan.sentences_from_words(_studio_words()), layers_for(3, {})


def _api_errors():
    import anthropic
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(cls, code):
        return cls("boom", response=httpx2.Response(code, request=req), body=None)

    return {
        "rate": status(anthropic.RateLimitError, 429),
        "auth": status(anthropic.AuthenticationError, 401),
        "model": status(anthropic.NotFoundError, 404),
        "overloaded": status(anthropic.InternalServerError, 529),
        "bad": status(anthropic.BadRequestError, 400),
        "network": anthropic.APIConnectionError(request=req),
    }


JARGON = ("ValidationError", "pydantic", "Traceback", "json_invalid", "APIStatusError", "httpx")


def test_storyboard_unreadable_reply_is_a_plain_error(workspace: Path):
    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    try:  # exactly what the SDK raises when a cut-off reply is parsed at the end of the text block
        plan.Storyboard.model_validate_json('{"title": "About Chri')
    except Exception as exc:  # noqa: BLE001
        truncated = exc
    with pytest.raises(plan.StoryboardFailed) as info:
        plan.make_storyboard(cfg, sentences, "", layers, client=_FakeClaude(_FakeStream(fail_on_final=truncated)))
    msg = str(info.value)
    assert isinstance(info.value, RuntimeError) and info.value.charged
    assert "cut off" in msg and "charged" in msg and "--minutes" in msg
    assert not any(j in msg for j in JARGON)


@pytest.mark.parametrize("kind", ["rate", "auth", "model", "overloaded", "bad", "network"])
def test_storyboard_api_errors_are_plain(workspace: Path, kind):
    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    err = _api_errors()[kind]
    for stream in (_FakeStream(fail_on_enter=err), _FakeStream(fail_on_final=err)):
        with pytest.raises(plan.StoryboardFailed) as info:
            plan.make_storyboard(cfg, sentences, "", layers, client=_FakeClaude(stream))
        msg = str(info.value)
        assert "charged" in msg and not any(j in msg for j in JARGON), msg
    if kind == "auth":
        assert "ANTHROPIC_API_KEY" in msg and not info.value.charged
    if kind == "model":
        assert cfg.extract.model in msg and not info.value.charged


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _real_sdk_client(handler):
    """The installed anthropic SDK, talking to a fake server inside the test (no network, no key)."""
    import anthropic
    import httpx2
    return anthropic.Anthropic(api_key="test-not-a-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_storyboard_cut_off_through_the_real_sdk(workspace: Path):
    import httpx2

    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    body = _sse(
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": cfg.extract.model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": {"input_tokens": 1000, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"title": "About Chri'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 64000}},
        {"type": "message_stop"},
    )
    client = _real_sdk_client(lambda req: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body))
    with pytest.raises(plan.StoryboardFailed) as info:
        plan.make_storyboard(cfg, sentences, "", layers, client=client)
    assert "cut off" in str(info.value) and info.value.charged

    err = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    client = _real_sdk_client(lambda req: httpx2.Response(429, json=err))
    with pytest.raises(plan.StoryboardFailed) as info:
        plan.make_storyboard(cfg, sentences, "", layers, client=client)
    assert "Wait a few minutes" in str(info.value) and not info.value.charged


# ---------- spend gate before the storyboard ----------

def _gated_studio(workspace: Path, monkeypatch, cost=0.0123, fail=None):
    """run_studio with the audio length, transcript and the paid storyboard call replaced by recorders."""
    from sermon_visuals.studio import plan, run
    calls: list[str] = []

    def fake_storyboard(*args, **kwargs):
        calls.append("storyboard")
        if fail:
            raise fail
        return _studio_board(), cost

    monkeypatch.setattr(run, "_duration", lambda p: 20.0)
    monkeypatch.setattr(run, "get_transcript", lambda *a, **k: calls.append("transcript") or (workspace / "programs" / "btw-0001.txt"))
    monkeypatch.setattr(plan, "make_storyboard", fake_storyboard)
    (workspace / "programs" / "btw-0001.mp3").write_bytes(b"")
    cfg = load_config(workspace / "config.toml")
    return cfg, run, calls, run.studio_dir(cfg, "btw-0001", 0.0, None)


def _no_prompt(q):
    raise AssertionError(f"should not ask: {q}")


@pytest.mark.parametrize("plan_only,replan", [(False, False), (True, False), (True, True), (False, True)])
def test_plan_gate_declined_spends_nothing(workspace: Path, monkeypatch, plan_only, replan):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    old_board = work / "storyboard_L3.json"
    if replan:  # a saved storyboard exists but --replan asks for a new, paid one
        work.mkdir(parents=True, exist_ok=True)
        old_board.write_text(_studio_board().model_dump_json(), encoding="utf-8")
    before = old_board.read_bytes() if replan else None
    asked: list[str] = []
    res = run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), plan_only=plan_only, replan=replan,
                         confirm=lambda q: asked.append(q) or "n")
    assert asked == ["  Spend about $0.06 on the plan? [y/N] "]  # $0.60 per half hour, pro-rated, floor of a tenth
    assert calls == [] and "video" not in res
    assert not (work / "spend.json").exists()
    assert (old_board.read_bytes() == before) if replan else not old_board.exists()


def test_plan_gate_refuses_over_budget_even_with_yes(workspace: Path, monkeypatch):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    work.mkdir(parents=True, exist_ok=True)
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd}), encoding="utf-8")
    before = (work / "spend.json").read_bytes()
    with pytest.raises(RuntimeError) as info:
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), assume_yes=True, confirm=_no_prompt)
    assert "budget" in str(info.value) and "Nothing was spent" in str(info.value)
    assert calls == [] and (work / "spend.json").read_bytes() == before


def test_plan_gate_records_the_real_cost(workspace: Path, monkeypatch, capsys):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch, cost=0.0123)
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), plan_only=True, confirm=lambda q: "y")
    assert calls == ["transcript", "storyboard"]
    spend = json.loads((work / "spend.json").read_text(encoding="utf-8"))
    assert spend["usd"] == 0.0123 and spend["storyboards"] == 1
    assert "estimate" in capsys.readouterr().out


def _ceiling(cfg, reference: str | None = None) -> float:
    """The gate's upper bound for this test program: before the transcript is read, or for a given one."""
    from sermon_visuals.studio import plan
    from sermon_visuals.studio.levels import layers_for
    sents = plan.sentences_from_words(_studio_words())
    return round(plan.storyboard_ceiling(cfg, sents, layers_for(3, cfg.studio.layers), cfg.studio.direction, reference), 4)


def test_plan_gate_yes_skips_prompt_and_unknown_cost_counts_the_ceiling(workspace: Path, monkeypatch):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch, cost=None)
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), plan_only=True, assume_yes=True, confirm=_no_prompt)
    assert calls == ["transcript", "storyboard"]
    reference = (workspace / "programs" / "btw-0001.txt").read_text(encoding="utf-8")
    assert json.loads((work / "spend.json").read_text(encoding="utf-8"))["usd"] == _ceiling(cfg, reference)


def test_storyboard_ceiling_bounds_the_request(workspace: Path):
    from sermon_visuals.studio import plan
    from sermon_visuals.studio.levels import layers_for
    cfg = load_config(workspace / "config.toml")
    st, sents, layers = cfg.studio, plan.sentences_from_words(_studio_words()), layers_for(3, {})
    long_ref = "“word” — " * 30_000  # a long reference with curly quotes, beyond the allowance made before it is read
    request = plan.SYSTEM + plan._request(cfg, sents, long_ref, layers, "")
    # independent bound: a byte-level tokenizer makes at most one token per UTF-8 byte; price it all at the dearest
    # input rate and add a reply that runs to the full MAX_TOKENS
    worst = (len(request.encode("utf-8")) * max(st.claude_input_usd_per_mtok, st.claude_cache_write_usd_per_mtok)
             + plan.MAX_TOKENS * st.claude_output_usd_per_mtok) / 1e6
    assert plan.storyboard_ceiling(cfg, sents, layers, "", long_ref) >= worst
    assert plan.storyboard_ceiling(cfg, sents, layers, "", None) >= plan.storyboard_ceiling(cfg, sents, layers, "", "x" * 100_000)


def test_missing_model_check_is_narrow():
    from sermon_visuals.studio import assets
    assert assets.model_missing(RuntimeError("404 NOT_FOUND. models/omni-x is not found for API version v1beta"))
    assert assets.model_missing(type("E", (Exception,), {"status_code": 404})("gone"))
    assert not assets.model_missing(RuntimeError("duration 14s is not supported for this model"))
    assert not assets.model_missing(RuntimeError("503 the model is overloaded"))


def test_plan_gate_refuses_when_full_length_reply_would_overrun(workspace: Path, monkeypatch):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    ceiling = _ceiling(cfg)
    work.mkdir(parents=True, exist_ok=True)
    left = (0.06 + ceiling) / 2  # more than the estimate, less than the most the call could cost
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd - left}), encoding="utf-8")
    with pytest.raises(RuntimeError) as info:
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), assume_yes=True, confirm=_no_prompt)
    assert "Nothing was spent" in str(info.value) and calls == []


def test_plan_gate_rechecks_after_reading_a_long_transcript(workspace: Path, monkeypatch):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    long_ref = workspace / "programs" / "long.txt"
    long_ref.write_text("word " * 200_000, encoding="utf-8")
    monkeypatch.setattr(run, "get_transcript", lambda *a, **k: calls.append("transcript") or long_ref)
    before, after = _ceiling(cfg), _ceiling(cfg, long_ref.read_text(encoding="utf-8"))
    assert after > before
    work.mkdir(parents=True, exist_ok=True)
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd - (before + after) / 2}), encoding="utf-8")
    with pytest.raises(RuntimeError) as info:
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), assume_yes=True, confirm=_no_prompt)
    assert "budget" in str(info.value) and calls == ["transcript"]  # read the transcript, never called Claude


@pytest.mark.parametrize("usd,expect", [(None, "ceiling"), (0.8123, 0.8123)])
def test_plan_gate_failed_paid_call_is_still_counted(workspace: Path, monkeypatch, usd, expect):
    from sermon_visuals.studio import plan
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch, fail=plan.StoryboardFailed("cut off", charged=True, usd=usd))
    with pytest.raises(plan.StoryboardFailed):
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), assume_yes=True, confirm=_no_prompt)
    if expect == "ceiling":  # cost unknown (a cut-off reply may have run to full length): count the most it could have been
        expect = _ceiling(cfg, (workspace / "programs" / "btw-0001.txt").read_text(encoding="utf-8"))
    assert json.loads((work / "spend.json").read_text(encoding="utf-8"))["usd"] == expect


def test_plan_gate_uncharged_failure_is_not_counted(workspace: Path, monkeypatch):
    from sermon_visuals.studio import plan
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch, fail=plan.StoryboardFailed("bad key", charged=False))
    with pytest.raises(plan.StoryboardFailed):
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), assume_yes=True, confirm=_no_prompt)
    assert not (work / "spend.json").exists()


def test_storyboard_cost_from_token_counts(workspace: Path):
    from types import SimpleNamespace

    from sermon_visuals.studio import plan
    st = load_config(workspace / "config.toml").studio
    usage = SimpleNamespace(input_tokens=10_000, output_tokens=30_000, cache_creation_input_tokens=2_000, cache_read_input_tokens=None)
    expected = (10_000 * st.claude_input_usd_per_mtok + 30_000 * st.claude_output_usd_per_mtok
                + 2_000 * st.claude_cache_write_usd_per_mtok) / 1e6
    assert plan.storyboard_cost(usage, st) == pytest.approx(expected)
    assert plan.storyboard_cost(None, st) is None
    assert plan.storyboard_cost(SimpleNamespace(), st) is None  # missing counts are unknown, not free
    assert plan.storyboard_cost(SimpleNamespace(input_tokens=5, output_tokens=None), st) is None


def test_storyboard_unreadable_fallback_keeps_known_cost(workspace: Path):
    from types import SimpleNamespace

    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=2000, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    msg = SimpleNamespace(stop_reason="end_turn", parsed_output=None, usage=usage,
                          content=[SimpleNamespace(type="text", text='{"title": "half')])
    with pytest.raises(plan.StoryboardFailed) as info:
        plan.make_storyboard(cfg, sentences, "", layers, client=_FakeClaude(_FakeStream(message=msg)))
    assert info.value.charged and info.value.usd == pytest.approx(plan.storyboard_cost(usage, cfg.studio))


def test_small_picture_estimate_still_asks(workspace: Path, monkeypatch):
    from sermon_visuals.studio import assets
    from sermon_visuals.studio import plan as P
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    one = P.Storyboard(title="T", chapters=[], readings=[],
                       beats=[P.Beat(first=0, last=99, scene="A field.", motion="", hero=False, key_phrase="")])
    asked: list[str] = []
    res = run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=one, provider=assets.DraftArt(),
                         confirm=lambda q: asked.append(q) or "n")
    assert asked == ["  Spend about $0.10? [y/N] "] and "video" not in res and calls == []


def test_storyboard_stop_reasons_are_plain(workspace: Path):
    from types import SimpleNamespace

    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    for reason, words in (("max_tokens", "cut off"), ("refusal", "declined")):
        msg = SimpleNamespace(stop_reason=reason, content=[], parsed_output=None, usage=None)
        with pytest.raises(plan.StoryboardFailed) as info:
            plan.make_storyboard(cfg, sentences, "", layers, client=_FakeClaude(_FakeStream(message=msg)))
        assert words in str(info.value) and info.value.charged


# ---------- attribution: only his own words are shown as his ----------

# Copied from the ministry's edited transcript of "About Christmas" (programs/about_christmas.txt), in its own layout:
# a reading of another author's article (lines 18-34 there) that ends with a credit line, scripture blocks with a
# reference line and a version label, verse lines with no reference line, and [bracketed] notes. The real file
# uses no-break spaces between words and the Greek question mark (U+037E) for semicolons.
EXCERPT_LINES = """About Christmas
by Ronald L. Dart
So there's not a hint in the New Testament about believing in Christmas or celebrating Jesus' birth. It
could be argued that this fact does not militate against modern celebration, but that's not what interests
me at this point. We can argue about that later. My question is what the first Christians believed and
practiced relative to Jesus' nativity. (The word “nativity” just means the circumstances of one's birth.)
Now, I came across an interesting item published by the American Presbyterian Church. Apparently it
was published shortly after last Christmas. They said:
And they are, of course, dead right. That said, though, I think we can safely assume that the first
Christians knew when Jesus was born. We don't. They did not see fit to record it for us, but I think they
must've known roughly. And the reason I say that is because Luke records events that, at least
seasonally, place Jesus' birth. Luke begins his gospel with the story of another birth. (And you might
wonder, in a way , “Why?”, and we'll talk about that.) His name was John the Baptist. The story begins
in the very first verse of Luke 1:
In the recent holiday season many Christians have been merrily, joyously, and some perhaps
even seriously, solemnly, and reverently, celebrating the birth of Jesus Christ. However, one
wonders how many gave any significant thought to the issue of when was Christ born. If they
had they would quickly have discovered that despite all the mythology and legend that
surrounds this holiday season, we really do not know when Christ was born. We know neither
the year, nor the month, nor the day. For that matter we don’t know the time of day either. God
in his wisdom has chosen not to reveal to us anything concerning the exact date of this
momentous event so long foretold by the prophets and awaited by the faithful.
American Presbyterian Church  Christ's Birth
Now, before I go on, I take this to mean that we can safely consider that in Luke's gospel we have a
statement of things that were widely reported among the first Christians. “Many have taken in hand to
set forth in order a declaration of those things which are most surely believed.” So Luke, then, has
taken pen in hand and pad (or whatever he used for writing tools) and he—good reporter, a good
historian—put the whole thing together for us.
Luke 1
1 For as much as many have taken in hand to set forth in order a declaration of those things
which are most surely believed among us,
AKJV
Luke 1
2 Even as they delivered them to us, which from the beginning were eyewitnesses, and
ministers of the word;
AKJV
Now, we can add to this that many Jewish rabbis teach that the only example of a birthday in the Bible
is one observed in honor of Pharaoh [Genesis 40], and since the Jews were exhorted against observing
the customs of the Egyptians, it was their custom not to observe birthdays.
Luke 1
8 And it came to pass, that while he executed the priest's office before God in the order of his
course,
9 According to the custom of the priest's office, his lot [his job] was to burn incense when he
went into the temple of the Lord. [That's his job. When he went into the Temple, he burned
incense.]
AKJV
Mind you, the Word became flesh and dwelt among us. The Greek word for “dwelt” [skénoó, σκηνόω, Strong's G4637] could just as
easily have been translated “tabernacled among us.” Why is that important? I'll explain.
Why is all this important? Because it was important for everyone to know that Jesus came into the
world just like they did. He was born. He was a baby. You could actually pick him and hold him.
5 To be taxed with Mary his espoused wife, being great with child.
6 And so it was, that, while they were there, the days were accomplished that she should be
delivered.
Luke 2
8 And there were in the same country shepherds abiding in the field, keeping watch over their
flock by night.
AKJV"""
EXCERPT = EXCERPT_LINES.replace(" ", " ").replace("word;", "word;")

ARTICLE_PHRASES = ["we really do not know when Christ was born", "so long foretold by the prophets"]
HIS_PHRASES = ["not a hint in the New Testament", "the first Christians knew when Jesus was born",
               "at least seasonally, place Jesus' birth", "were widely reported among the first Christians",
               "it was their custom not to observe birthdays", "Jesus came into the world just like they did",
               "could just as easily have been translated"]


def test_splitter_keeps_a_credited_article_off_his_words():
    from sermon_visuals import attribution as A
    spans = A.split_transcript(EXCERPT)
    for phrase in ARTICLE_PHRASES:  # the real case: a live plan chose both of these as his key phrases
        assert A.whose(phrase, spans) == A.OTHER_AUTHOR, phrase
    for phrase in HIS_PHRASES:
        assert A.whose(phrase, spans) == A.SPEAKER, phrase
    assert A.whose("Presbyterian Church Christ's Birth", spans) == A.OTHER_AUTHOR  # the credit line itself
    assert A.whose("words he never said", spans) is None
    assert A.whose("About Christmas by Ronald L. Dart", spans) != A.SPEAKER  # title and byline are not spoken


def test_splitter_scripture_blocks_are_not_his():
    from sermon_visuals import attribution as A
    spans = A.split_transcript(EXCERPT)
    assert A.whose("which from the beginning were eyewitnesses", spans) == A.SCRIPTURE
    assert A.whose("ministers of the word", spans) == A.SCRIPTURE  # ends in U+037E
    assert A.whose("keeping watch over their flock by night", spans) == A.SCRIPTURE
    assert A.whose("being great with child", spans) == A.SCRIPTURE  # verse lines with no reference line above
    # also quoted inline inside his paragraph, in quotation marks: still not his
    assert A.whose("many have taken in hand to set forth in order", spans) != A.SPEAKER


def test_splitter_bracketed_notes_are_not_his():
    from sermon_visuals import attribution as A
    spans = A.split_transcript(EXCERPT)
    assert A.whose("Genesis 40", spans) == A.EDITOR_NOTE
    assert A.whose("Strong's G4637", spans) == A.EDITOR_NOTE
    assert A.whose("when he went into the Temple, he burned incense", spans) != A.SPEAKER
    kinds = {s.kind for s in spans}
    assert kinds == {A.SPEAKER, A.SCRIPTURE, A.OTHER_AUTHOR, A.EDITOR_NOTE}


def test_pull_quotes_must_be_his_own_words():
    brief = fake_brief().model_copy(update={"pull_quotes": [
        PullQuote(text="We really do not know when Christ was born.", why="from the article he reads"),
        PullQuote(text="Which from the beginning were eyewitnesses", why="scripture"),
        PullQuote(text="So there's not a hint in the New Testament about believing in Christmas", why="his"),
    ]})
    assert [c.verified for c in extract.verify_quotes(brief, EXCERPT)] == [False, False, True]


def _words_from(text: str):
    from sermon_visuals.studio.plan import Word
    words, t = [], 0.2
    for w in text.split():
        words.append(Word(w=w, s=round(t, 2), e=round(t + 0.25, 2)))
        t += 0.32 + (0.5 if w[-1] in ".?" else 0)
    return words


SPOKEN = ("So there's not a hint in the New Testament about believing in Christmas. "
          "If they had they would quickly have discovered that we really do not know when Christ was born. "
          "The story begins in the very first verse of Luke.")


def _attribution_board():
    from sermon_visuals.studio import plan
    return plan.Storyboard(title="About Christmas", chapters=[], readings=[], beats=[
        plan.Beat(first=0, last=0, scene="A shelf of rolled scrolls.", motion="m", hero=False, key_phrase="not a hint in the New Testament"),
        plan.Beat(first=1, last=1, scene="Misty hills.", motion="m", hero=False, key_phrase="we really do not know when Christ was born"),
        plan.Beat(first=2, last=2, scene="A lamp.", motion="m", hero=False, key_phrase="words he never said"),
    ])


def test_key_phrases_need_his_words_and_the_audio():
    from sermon_visuals import attribution as A
    from sermon_visuals.studio import plan
    words = _words_from(SPOKEN)
    sents = plan.sentences_from_words(words)
    board = _attribution_board()
    timed, dropped = plan.vet_key_phrases(board, words, sents, A.split_transcript(EXCERPT), cards=True)
    assert list(timed) == [0]
    assert [(i, why) for i, _, why in dropped] == [(1, A.OTHER_AUTHOR), (2, None)]  # None: nowhere in his words
    # with no reference transcript the old behaviour stands: the audio check alone
    timed, dropped = plan.vet_key_phrases(board, words, sents, None, cards=True)
    assert list(timed) == [0, 1] and [i for i, _, _ in dropped] == [2]
    # his own words in the transcript, but not spoken during this beat: the audio check alone drops it
    beats = list(board.beats)
    beats[2] = beats[2].model_copy(update={"key_phrase": "the first Christians knew when Jesus was born"})
    timed, dropped = plan.vet_key_phrases(board.model_copy(update={"beats": beats}), words, sents,
                                          A.split_transcript(EXCERPT), cards=True)
    assert A.whose("the first Christians knew when Jesus was born", A.split_transcript(EXCERPT)) == A.SPEAKER
    assert (2, "the first Christians knew when Jesus was born", plan.NOT_HEARD) in dropped


def test_timeline_shows_only_his_key_phrases(tmp_path: Path):
    from sermon_visuals import attribution as A
    from sermon_visuals.studio import plan, run
    from sermon_visuals.studio.levels import layers_for
    words = _words_from(SPOKEN)
    sents = plan.sentences_from_words(words)
    board = _attribution_board()
    duration = words[-1].e + 1
    shots = run.shots_from(board, sents, duration)
    tl = run.build_timeline(board, words, sents, duration, shots, layers_for(4, {}), tmp_path, 640, 360, 55, 4.0,
                            spans=A.split_transcript(EXCERPT))
    shown = [" ".join(w for w, _ in c.words) for c in tl.callouts]
    assert shown == ["not a hint in the New Testament"]


def test_studio_prints_the_attribution_check(workspace: Path, monkeypatch, capsys):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    (workspace / "programs" / "btw-0001.txt").write_text(EXCERPT, encoding="utf-8")
    run.run_studio(cfg, "btw-0001", level=4, words=_words_from(SPOKEN), board=_attribution_board(), plan_only=True,
                   confirm=_no_prompt)
    out = capsys.readouterr().out
    assert "1 will be shown, 2 dropped" in out
    assert "we really do not know when Christ was born" in out and "another author" in out
    assert "could not be checked" not in out
    (workspace / "programs" / "btw-0001.txt").unlink()
    run.run_studio(cfg, "btw-0001", level=4, words=_words_from(SPOKEN), board=_attribution_board(), plan_only=True,
                   confirm=_no_prompt)
    out = capsys.readouterr().out
    assert sum("could not be checked" in line for line in out.splitlines()) == 1
    assert "2 will be shown, 1 dropped" in out


def test_quote_cards_recheck_against_his_words(workspace: Path, monkeypatch):
    """Cards are made only from quotes that are his own words in the transcript as it is now: not from a stale
    check made under the old whole-transcript rule, and not from a quote edited into brief.json by hand."""
    (workspace / "programs" / "btw-0001.txt").write_text(EXCERPT, encoding="utf-8")
    brief = fake_brief().model_copy(update={"pull_quotes": [
        PullQuote(text="So there's not a hint in the New Testament about believing in Christmas", why="his"),
        PullQuote(text="We really do not know when Christ was born.", why="the article he reads"),
    ]})
    monkeypatch.setattr(extract, "extract_brief", lambda cfg, text: brief)
    monkeypatch.setattr(pipeline.imagegen, "make_provider", lambda cfg: FakeProvider())
    cfg = load_config(workspace / "config.toml")
    m = pipeline.run_program(cfg, "btw-0001", ["transcript", "brief", "images"])
    from sermon_visuals.models import QuoteCheck
    m.quote_checks = [QuoteCheck(text=q.text, verified=True) for q in brief.pull_quotes]  # stale: all passed
    # the owner adds a quote to brief.json by hand after the brief stage; it has no check at all
    on_disk = json.loads(Path(m.brief).read_text(encoding="utf-8"))
    on_disk["pull_quotes"].append({"text": "So long foretold by the prophets", "why": "added by hand"})
    Path(m.brief).write_text(json.dumps(on_disk), encoding="utf-8")
    pipeline.stage_compose(cfg, m, FakeProvider())
    assert {k for k in m.cards if k.startswith("quote")} == {"quote1_square", "quote1_vertical"}
    assert [c.verified for c in m.quote_checks] == [True, False, False]


def test_storyboard_prompt_states_the_attribution_rule(workspace: Path):
    from sermon_visuals.studio import plan
    text = " ".join(plan.SYSTEM.split())
    for needle in ("scripture", "another author", "[bracketed]", "leave it empty"):
        assert needle in text, needle


_WIDE = " and the words run on for a good while so that this line is as wide as a real wrapped line"


def test_splitter_review_gaps_are_closed():
    from sermon_visuals import attribution as A

    def who(phrase, *lines):
        return A.whose(phrase, A.split_transcript("\n".join(lines)))

    lead = "I read it to you now, and listen closely to every word the gospel writer puts down here for us all:"
    # a verse wrapped over more lines than the continuation limit, then another verse and the label
    long_verse = (["Luke 9", "1 And it came to pass that he went up into a mountain to pray" + _WIDE]
                  + [f"continuation line number {k}" + _WIDE for k in range(8)]
                  + ["2 And a voice came out of the cloud saying This is my beloved Son" + _WIDE, "AKJV"])
    for k in range(8):
        assert who(f"continuation line number {k}", lead, *long_verse) == A.SCRIPTURE, k
    # a quotation running over two paragraphs: the first paragraph has an opening mark and no closing one
    letter = ["I want to read this to you from a letter that came in the post this week, it is long:" + _WIDE,
              "“First paragraph of somebody else writing a long letter to the programme" + _WIDE, "",
              "“Second paragraph of the letter ends here, with many thanks to you all.”",
              "That is what he wrote to me, and I think he is right, so let us go on with Luke." + _WIDE]
    assert who("somebody else writing a long letter", *letter) == A.OTHER_AUTHOR
    assert who("the letter ends here, with many thanks", *letter) == A.OTHER_AUTHOR
    assert who("That is what he wrote to me", *letter) == A.SPEAKER  # his words again after the closing mark
    # single curly quotation marks; the apostrophe inside does not end the quotation
    assert who("another author's actual words", "So then he wrote, ‘These are another author’s actual words, "
               "not mine at all.’ And that was that." + _WIDE) == A.OTHER_AUTHOR
    # credit lines that end in a full stop: a dash credit and a title-case credit
    for credit in ("— C. S. Lewis, Mere Christianity.", "American Presbyterian Church, Christ's Birth."):
        assert who("the date is lost to history forever",
                   "I came across this in a book, and it is worth reading to you now, so here it is." + _WIDE,
                   "The writer says that we cannot know the hour of his birth at all, whatever we try" + _WIDE,
                   "and that the date is lost to history forever, as far as anyone can tell.",
                   credit, "Now back to Luke, and what he tells us about the shepherds." + _WIDE) == A.OTHER_AUTHOR, credit
    # ...also when the last line of the quotation is short and has no closing punctuation
    assert who("we cannot know the hour of his birth",
               "I came across this in a book, and it is worth reading to you now, so here it is." + _WIDE,
               "The writer says that we cannot know the hour of his birth at all, whatever we try" + _WIDE,
               "and the date is lost to history forever", "— C. S. Lewis, Mere Christianity.",
               "Now back to Luke, and what he tells us about the shepherds." + _WIDE) == A.OTHER_AUTHOR
    # reference lines in other common shapes open a scripture block on their own (no version label after them
    # to rescue the block, and his own words after it)
    mine = "Now that is the verse, and I say this in my own words to you here." + _WIDE
    for ref in ("LUKE 1", "Luke 1:1, 17", "Ps. 23", "Luke 1:1-4 (KJV)", "St. John 1", "John 3:16 (ESV)."):
        lines = ["Some words of my own come first here, and plenty of them." + _WIDE, ref,
                 "Even as they delivered them to us, which from the beginning were eyewitnesses" + _WIDE, mine]
        assert who("which from the beginning were eyewitnesses", *lines) == A.SCRIPTURE, ref
        # (with no label to close the block, up to MAX_VERSE_CONTINUATION lines after it are also treated as
        # scripture, so his next line is not claimed as his: conservative by design)
    # "2. Even as ..." is a verse line too, again with no label after it
    assert who("which from the beginning were eyewitnesses", "Some words of my own come first here, and plenty of them." + _WIDE,
               "2. Even as they delivered them to us, which from the beginning were eyewitnesses" + _WIDE, mine) == A.SCRIPTURE
    # a short wrapped line inside an over-long verse does not stop the cut-off lines being given back
    long_verse = (["Luke 9", "1 And it came to pass that he went up into a mountain to pray" + _WIDE]
                  + [f"continuation line number {k}" + _WIDE for k in range(7)] + ["short wrap here"]
                  + ["late continuation line" + _WIDE, "2 And a voice came out of the cloud" + _WIDE, "AKJV"])
    for phrase in ("continuation line number 6", "late continuation line"):
        assert who(phrase, lead, *long_verse) == A.SCRIPTURE, phrase


def test_a_phrase_found_both_in_his_words_and_a_reading_is_not_his():
    """If he also says, in his own words, a line from the article he reads, the phrase could be the article's
    at the moment it is shown; with no way to tell which occurrence is on screen, it is not treated as his."""
    from sermon_visuals import attribution as A
    echoed = EXCERPT + "\n" + "And that is the point: we really do not know when Christ was born, and that matters." + _WIDE
    spans = A.split_transcript(echoed)
    assert A.whose("And that is the point", spans) == A.SPEAKER  # the added line really is his
    assert A.whose("we really do not know when Christ was born", spans) == A.OTHER_AUTHOR
    # directly, in both orders: the old first-match rule returned SPEAKER when his span came first
    his = A.Span(A.SPEAKER, "we really do not know when Christ was born", 1, 1)
    article = A.Span(A.OTHER_AUTHOR, "we really do not know when Christ was born", 2, 2)
    assert A.whose("we really do not know", [his, article]) == A.OTHER_AUTHOR
    assert A.whose("we really do not know", [article, his]) == A.OTHER_AUTHOR


def test_unreadable_or_unmarked_transcript_is_said_plainly(workspace: Path, monkeypatch, capsys):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    (workspace / "programs" / "btw-0001.txt").unlink()
    (workspace / "programs" / "btw-0001.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    run.run_studio(cfg, "btw-0001", level=3, words=_words_from(SPOKEN), board=_attribution_board(), plan_only=True,
                   confirm=_no_prompt)
    out = capsys.readouterr().out
    assert "could not be read" in out and "0 will be shown, 3 dropped" in out  # fails closed: nothing shown as his
    (workspace / "programs" / "btw-0001.pdf").unlink()
    (workspace / "programs" / "btw-0001.txt").write_text(SPOKEN + "\n" + TRANSCRIPT, encoding="utf-8")  # no marks at all
    run.run_studio(cfg, "btw-0001", level=3, words=_words_from(SPOKEN), board=_attribution_board(), plan_only=True,
                   confirm=_no_prompt)
    out = capsys.readouterr().out
    assert sum("marks no scripture" in line for line in out.splitlines()) == 1


# ---------- hard budget stop for pictures ----------

def test_fit_to_budget_refuses_when_stills_alone_overrun(tmp_path: Path):
    from sermon_visuals.studio import assets, plan
    beats = [plan.Beat(first=i, last=i, scene=f"s{i}", motion="m", hero=True, key_phrase="") for i in range(4)]
    shots = [assets.Shot(index=i, start=i * 8.0, end=(i + 1) * 8.0, beat=b) for i, b in enumerate(beats)]
    assets.choose_animated(shots, 1.0)
    assets.assign_paths(shots, tmp_path, "style", "guard", "m")
    with pytest.raises(assets.OverBudget) as info:
        assets.fit_to_budget(shots, 0.30, 0.10, 0.07, 0.0)  # four stills alone are $0.40
    assert not any(s.animate for s in shots)  # animation was trimmed first
    assert info.value.cost == pytest.approx(0.40) and info.value.left == pytest.approx(0.30)
    assert "$0.40" in str(info.value) and "$0.30" in str(info.value)
    assert "No pictures were generated and nothing was spent on them" in str(info.value)


def _one_picture_board():
    from sermon_visuals.studio import plan as P
    return P.Storyboard(title="T", chapters=[], readings=[],
                        beats=[P.Beat(first=0, last=99, scene="A field.", motion="", hero=False, key_phrase="")])


@pytest.mark.parametrize("assume_yes", [True, False])
def test_picture_budget_stop_holds_even_with_yes(workspace: Path, monkeypatch, assume_yes):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    work.mkdir(parents=True, exist_ok=True)
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd - 0.04}), encoding="utf-8")
    before = (work / "spend.json").read_bytes()
    provider = FakeProvider()
    with pytest.raises(RuntimeError) as info:
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=_one_picture_board(), provider=provider,
                       assume_yes=assume_yes, confirm=_no_prompt)
    message = str(info.value)
    assert "$0.10" in message and "$0.04" in message and "No pictures were generated and nothing was spent on them" in message
    assert provider.calls == [] and (work / "spend.json").read_bytes() == before
    assert not list(work.glob("assets/*.png"))


def test_picture_budget_stop_in_plan_only_mode_is_a_note(workspace: Path, monkeypatch, capsys):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    work.mkdir(parents=True, exist_ok=True)
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd - 0.04}), encoding="utf-8")
    before = (work / "spend.json").read_bytes()
    provider = FakeProvider()
    res = run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=_one_picture_board(), provider=provider,
                         plan_only=True, assume_yes=True, confirm=_no_prompt)
    assert provider.calls == [] and "video" not in res and (work / "spend.json").read_bytes() == before
    assert "No pictures were generated and nothing was spent on them" in capsys.readouterr().out


def test_generation_never_spends_past_what_is_left(workspace: Path):
    """Second line of defence: even if more pictures turn out to be needed than were estimated (say a cached
    picture was deleted after the question was answered), generation stops at what is left of the budget."""
    from sermon_visuals import imagegen
    from sermon_visuals.studio import assets, plan
    cfg = load_config(workspace / "config.toml")
    beats = [plan.Beat(first=i, last=i, scene=f"scene {i}", motion="m", hero=False, key_phrase="") for i in range(5)]
    shots = [assets.Shot(index=i, start=i * 5.0, end=(i + 1) * 5.0, beat=b) for i, b in enumerate(beats)]
    assets.assign_paths(shots, workspace / "a", "style", imagegen.content_guard(cfg), "m")
    provider = FakeProvider()
    ledger = assets.Ledger(workspace / "spend.json")
    report = assets.generate_assets(cfg, shots, "style", provider, None, ledger, 0.10, 0.07, workers=3, budget_left=0.25)
    assert len(provider.calls) == 2 and ledger.data["usd"] == pytest.approx(0.20)
    assert report.budget_stopped and "budget" in report.budget_stopped


def test_every_paid_attempt_counts_against_the_budget(workspace: Path, monkeypatch):
    """A retry is another request the provider may bill (a timed-out reply may still have been charged), so each
    attempt reserves its own cost; and pictures and clips draw on one allowance."""
    from sermon_visuals import imagegen
    from sermon_visuals.studio import assets, plan
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    cfg = load_config(workspace / "config.toml")
    beats = [plan.Beat(first=i, last=i, scene=f"scene {i}", motion="m", hero=True, key_phrase="") for i in range(3)]

    def fresh(tag, animate=False):
        shots = [assets.Shot(index=i, start=i * 5.0, end=(i + 1) * 5.0, beat=b, animate=animate, clip_seconds=5 if animate else 0)
                 for i, b in enumerate(beats)]
        assets.assign_paths(shots, workspace / tag, "style", imagegen.content_guard(cfg), "m")
        return shots

    class Flaky(FakeProvider):
        attempts = 0

        def generate(self, prompt, aspect, references):
            Flaky.attempts += 1
            if Flaky.attempts == 1:
                raise TimeoutError("the reply timed out (it may still have been billed)")
            return super().generate(prompt, aspect, references)

    assets.generate_assets(cfg, fresh("r"), "style", Flaky(), None, assets.Ledger(workspace / "r.json"), 0.10, 0.07,
                           workers=1, budget_left=0.25)
    assert Flaky.attempts == 2  # the failed attempt and one success use up $0.20; a third attempt would pass $0.25

    class Motion:
        model = "m"
        calls = 0

        def animate(self, image, prompt, seconds):
            Motion.calls += 1
            return b"clip"

    provider = FakeProvider()
    ledger = assets.Ledger(workspace / "m.json")
    assets.generate_assets(cfg, fresh("m", animate=True), "style", provider, Motion(), ledger, 0.10, 0.07, workers=1,
                           budget_left=0.70)  # 3 pictures ($0.30) + one 5-second clip ($0.35) fit; a second clip does not
    assert len(provider.calls) == 3 and Motion.calls == 1 and ledger.data["usd"] <= 0.70 + 1e-9


def test_picture_budget_within_reach_still_asks(workspace: Path, monkeypatch):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    work.mkdir(parents=True, exist_ok=True)
    (work / "spend.json").write_text(json.dumps({"usd": cfg.studio.budget_usd - 0.10}), encoding="utf-8")  # exactly enough
    asked: list[str] = []
    provider = FakeProvider()
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=_one_picture_board(), provider=provider,
                   confirm=lambda q: asked.append(q) or "n")
    assert asked == ["  Spend about $0.10? [y/N] "] and provider.calls == []


# ---------- storyboard prompt and the scene lint ----------

def test_storyboard_prompt_keeps_writing_out_of_pictures():
    from sermon_visuals.studio import plan
    text = " ".join(plan.SYSTEM.split())
    for needle in ("closed, rolled, blank or turned away", "newsletters", "calendars", "signs", "labels", "diagrams",
                   "notes covered in script", "in full every time", "\"the same man\"",
                   "somewhere the speaker says they were not"):
        assert needle in text, needle


def test_close_up_faces_rule_follows_the_house_style(workspace: Path):
    from sermon_visuals.studio import plan
    from sermon_visuals.studio.levels import layers_for
    cfg = load_config(workspace / "config.toml")
    sents, layers = plan.sentences_from_words(_studio_words()), layers_for(3, {})
    assert plan.forbids_close_faces("Soft light. No people's faces in close-up. Nothing cartoonish.")
    assert plan.forbids_close_faces("Never show a face in close up.")
    assert not plan.forbids_close_faces("Close-up of hands kneading bread; faces lit by lamplight.")
    assert "No people's faces in close-up" in cfg.style.prompt  # the example house style forbids them
    assert "no close-up faces" in plan._request(cfg, sents, "", layers, "")
    cfg.style.prompt = "Painterly editorial illustration, warm palette."
    assert "no close-up faces" not in plan._request(cfg, sents, "", layers, "")
    assert "the manger" in plan._request(cfg, sents, "", layers, "")  # owner's pending wording left alone


def test_scene_lint_flags_writing_prone_things():
    from sermon_visuals.studio import plan
    assert plan.lint_scene("An open scroll covered in Hebrew script beside a wall calendar.") == ["scroll", "script", "calendar"]
    assert plan.lint_scene("A stack of ancient letters and codices; Luke writing on a wax tablet.") == ["letters", "codices", "writing", "tablet"]
    assert plan.lint_scene("A newsletter pinned under a signpost with a label.") == ["newsletter", "signpost", "label"]
    # shown closed, rolled, blank or turned away: no warning
    assert plan.lint_scene("Several rolled scrolls tied with cord and a closed book; a blank sign turned away.") == []
    assert plan.lint_scene("A leather book lying closed on a table.") == []
    assert plan.lint_scene("Misty hills near Bethlehem at dawn.") == []


def test_scene_lint_warns_before_the_picture_question(workspace: Path, monkeypatch, capsys):
    from sermon_visuals.studio import plan as P
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    board = P.Storyboard(title="T", chapters=[], readings=[], beats=[
        P.Beat(first=0, last=1, scene="Misty hills.", motion="", hero=False, key_phrase=""),
        P.Beat(first=2, last=99, scene="An open scroll covered in script.", motion="", hero=False, key_phrase="")])
    seen: list[str] = []
    provider = FakeProvider()
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=board, provider=provider,
                   confirm=lambda q: seen.append(capsys.readouterr().out) or "n")
    assert len(seen) == 1 and "Beat 2: scroll, script" in seen[0] and "Beat 1" not in seen[0]
    assert provider.calls == []  # a warning, not a block: the question is still asked and "n" stops it


def test_picture_progress_counts_against_the_real_total(workspace: Path, caplog):
    import logging

    from sermon_visuals import imagegen
    from sermon_visuals.studio import assets, plan
    cfg = load_config(workspace / "config.toml")
    beats = [plan.Beat(first=i, last=i, scene=f"scene {i}", motion="m", hero=False, key_phrase="") for i in range(5)]
    shots = [assets.Shot(index=i, start=i * 5.0, end=(i + 1) * 5.0, beat=b) for i, b in enumerate(beats)]
    assets.assign_paths(shots, workspace / "a", "style", imagegen.content_guard(cfg), "m")
    with caplog.at_level(logging.INFO, logger=assets.log.name):
        assets.generate_assets(cfg, shots, "style", assets.DraftArt(), None, assets.Ledger(workspace / "spend.json"), 0.1, 0.07, workers=1)
    done = [m for m in caplog.messages if m.startswith("Picture ")]
    assert done == [f"Picture {k}/5 done" for k in range(1, 6)]


# ---------- the look of the dark styles is pinned ----------

GOLDEN = HERE / "fixtures" / "dark_theme_frames.json"
GOLDEN_TIMES = [0.5, 3.35, 4.5, 5.3, 6.0, 8.0, 10.5, 13.5, 15.8]


def _golden_picture(path: Path, seed: int) -> Path:
    import numpy as np
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:432, 0:768].astype(np.float32)
    base = np.stack([60 + 120 * xx / 768, 80 + 90 * yy / 432, 140 - 60 * xx / 768], -1)
    for _ in range(6):
        cx, cy, r = rng.integers(0, 768), rng.integers(0, 432), rng.integers(30, 120)
        base[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = rng.integers(0, 255, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(base.clip(0, 255).astype(np.uint8)).save(path)
    return path


def _golden_timeline(tmp: Path, **extra):
    from sermon_visuals.studio import plan, render
    from sermon_visuals.verses import Scripture, build_overlays
    a, b = _golden_picture(tmp / "a.png", 1), _golden_picture(tmp / "b.png", 2)
    card = plan.Card(type="list", title="Three reasons", items=[
        plan.CardItem(label="No date given", detail="Scripture is silent"), plan.CardItem(label="No feast kept", detail=""),
        plan.CardItem(label="Shepherds outdoors", detail="Not in December")])
    verses = build_overlays([Scripture(start=13.0, end=15.5, reference="Luke 2:8", version="KJV",
                                       text="And there were in the same country shepherds abiding in the field.")],
                            tmp / "verses", 640, 360, compose.Fonts.resolve())
    return render.Timeline(
        duration=16.0,
        shots=[render.ShotSpec(0.0, 5.0, a), render.ShotSpec(5.0, 10.0, b), render.ShotSpec(10.0, 16.0, a)],
        callouts=[render.Callout(words=[("the", 3.0), ("first", 3.3), ("Christians", 3.6)], end=4.9)],
        cards=[render.CardSpec(card=card, start=5.5, end=9.5, item_times=[5.8, 6.6, 7.4])],
        chapters=[render.ChapterMark(1, "A loaded question", 0.0), render.ChapterMark(2, "Counting the months", 10.0)],
        verses=verses, caption_words=[plan.Word(w=w, s=s, e=s + 0.25) for w, s in
                                      [("What", 3.0), ("did", 3.3), ("they", 3.6), ("believe?", 3.9), ("Nobody", 15.4), ("knows.", 15.7)]],
        **extra)


def _golden_hashes(tmp: Path, **extra) -> dict:
    import hashlib

    from sermon_visuals.studio import render
    r = render.Renderer(_golden_timeline(tmp, **extra), 640, 360, 24)
    out = {f"{t:.2f}": hashlib.sha256(r.frame(t).tobytes()).hexdigest() for t in GOLDEN_TIMES}
    r.close()
    return out


def _golden_env() -> dict:
    import PIL

    from sermon_visuals.studio import render
    t = render.Type()
    return {"fonts": [t.display, t.bold, t.sans, t.serif, compose.Fonts.resolve().serif], "pillow": PIL.__version__}


def test_dark_styles_look_exactly_as_before(tmp_path: Path):
    """Frames of a fixed timeline (callout, card, both chapter treatments, scripture band, progress bar, ink-in)
    must match, pixel for pixel, what the renderer drew before the ink theme existed."""
    import os
    if os.environ.get("SV_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps({"env": _golden_env(), "frames": _golden_hashes(tmp_path)}, indent=1), encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if golden["env"] != _golden_env():
        pytest.skip("different fonts or Pillow version from the machine that pinned the frames")
    assert _golden_hashes(tmp_path) == golden["frames"]


# ---------- planner rules for the whiteboard (ink) style ----------

WHITEBOARD_NEEDLES = ("one idea per picture", "two or three clearly separated subjects", "plain white",
                      "wide empty margins", "never mention lighting, time of day, camera shots or framing, or colours",
                      "dot eyes", "never portraits", "single central robed figure", "shared-meal table",
                      "never glow, rays or light", "clay oil lamp")
CINEMATIC_NEEDLES = ("light and framing", "Vary the framing", "in full every time", "\"the same man\"")


def test_planner_rules_follow_the_style():
    from sermon_visuals.studio import plan
    ink = " ".join(plan.system_for("ink").split())
    for needle in WHITEBOARD_NEEDLES:
        assert needle in ink, needle
    for needle in ("light and framing", "Vary the framing", "in full every time"):
        assert needle not in ink, needle
    # the rules every style shares are still there
    for needle in ("closed, rolled, blank or turned away", "somewhere the speaker says they were not", "Never draw the speaker",
                   "point back to an earlier beat"):
        assert needle in ink, needle
    for style in ("graphic", "painterly", "woodcut", "A custom watercolour style.", ""):
        assert plan.system_for(style) == plan.SYSTEM, style
    text = " ".join(plan.SYSTEM.split())
    for needle in CINEMATIC_NEEDLES:
        assert needle in text, needle
    for needle in WHITEBOARD_NEEDLES[2:]:
        assert needle not in text, needle


def test_scene_field_description_follows_the_style():
    from sermon_visuals.studio import plan
    scene = lambda model: model.model_json_schema()["$defs"][  # noqa: E731
        next(k for k in model.model_json_schema()["$defs"] if "Beat" in k)]["properties"]["scene"]["description"]
    assert plan.storyboard_model("graphic") is plan.Storyboard
    assert "light, framing" in scene(plan.Storyboard)
    ink = scene(plan.storyboard_model("ink"))
    assert "plain white" in ink and "No lighting, time of day, camera or colour words" in ink and "light, framing" not in ink


def test_whiteboard_storyboard_call_uses_the_whiteboard_rules(workspace: Path):
    from types import SimpleNamespace

    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    reply = _studio_board().model_dump_json()
    for style, model in (("ink", plan.storyboard_model("ink")), ("graphic", plan.Storyboard)):
        cfg.studio.style = style
        msg = SimpleNamespace(stop_reason="end_turn", parsed_output=None, usage=None,
                              content=[SimpleNamespace(type="text", text=reply)])
        client = _FakeClaude(_FakeStream(message=msg))
        board, _ = plan.make_storyboard(cfg, sentences, "", layers, client=client)
        assert client.kwargs["system"][0]["text"] == plan.system_for(style)
        assert client.kwargs["output_format"] is model
        assert type(board) is plan.Storyboard and board == _studio_board()  # saved in the one shape, whatever the style
        # the spending ceiling is worked out from the text that is really sent
        worst = (len((plan.system_for(style) + plan._request(cfg, sentences, "", layers, "")).encode("utf-8"))
                 * max(cfg.studio.claude_input_usd_per_mtok, cfg.studio.claude_cache_write_usd_per_mtok)
                 + plan.MAX_TOKENS * cfg.studio.claude_output_usd_per_mtok) / 1e6
        assert plan.storyboard_ceiling(cfg, sentences, layers, "", "") >= worst


def test_no_light_as_a_stand_in_for_god(workspace: Path):
    from sermon_visuals.studio import plan
    cfg, sentences, layers = _plan_inputs(workspace)
    for style in ("graphic", "ink"):
        cfg.studio.style = style
        req = plan._request(cfg, sentences, "", layers, "")
        line = next(x for x in req.splitlines() if x.startswith("depict_christ"))
        assert "the manger" in line and "never use light, glow or rays as a stand-in" in line
        assert "hands, light" not in line and "light, the manger" not in line


def test_whiteboard_scene_lint():
    from sermon_visuals.studio import plan
    scene = ("Wide shot of a village at dusk, warm golden light on a man in a blue tunic; "
             "a halo of rays glows behind him, close-up of his hands.")
    assert plan.lint_style(scene, "ink") == ["wide shot", "dusk", "golden", "light", "blue", "halo", "rays", "glows", "close-up"]
    for style in ("graphic", "painterly", "woodcut"):
        assert plan.lint_style(scene, style) == []
    assert plan.lint_style("Two hands pass a rolled scroll across a plain table; a clay oil lamp beside it.", "ink") == []
    assert plan.lint_style("A shepherd with a lamp, overhead view, at night under moonlight.", "ink") == [
        "overhead", "night", "moonlight"]
    assert plan.lint_scene(scene) == []  # the writing lint is unchanged


def test_whiteboard_lint_warns_before_the_picture_question(workspace: Path, monkeypatch, capsys):
    from sermon_visuals.studio import plan as P
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    cfg.studio.image_usd = 0.10
    board = P.Storyboard(title="T", chapters=[], readings=[], beats=[
        P.Beat(first=0, last=1, scene="Two hands and a rolled scroll on plain white.", motion="", hero=False, key_phrase=""),
        P.Beat(first=2, last=99, scene="Wide shot of a robed man at dusk with a halo.", motion="", hero=False, key_phrase="")])
    for style, expect in (("ink", True), ("graphic", False)):
        cfg.studio.style = style
        seen: list[str] = []
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=board, provider=FakeProvider(),
                       confirm=lambda q: seen.append(capsys.readouterr().out) or "n")
        assert ("Beat 2: wide shot, dusk, halo" in seen[0]) is expect and "Beat 1:" not in seen[0], style


# ---------- ink clean-up and picture checks ----------

def _sketchbook_photo(dark_fill: bool = True) -> Image.Image:
    """A synthetic photo of an ink drawing in a sketchbook: warm paper with a vignette, the neighbouring page and the
    binding on the left, a strip of wooden desk on the right, black line work, an orange accent, a blue patch, a grey
    wash and (optionally) a large black fill."""
    import numpy as np
    from PIL import ImageDraw
    W, H = 960, 540
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    vignette = 1.0 - 0.14 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / 2
    paper = np.array([238, 228, 210], np.float32)
    img = paper[None, None, :] * vignette[..., None]
    img[:, :14] = np.array([214, 205, 190])           # the neighbouring page
    img[:, 14:17] = np.array([120, 112, 100])         # the binding
    rng = np.random.default_rng(3)
    desk = np.array([150, 92, 48], np.float32) + rng.normal(0, 10, (H, 26, 1))
    img[:, W - 26:] = desk                            # wooden desk
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(8):                                # line work
        d.line((200 + 30 * k, 150, 200 + 30 * k, 420), fill=(20, 20, 20), width=4)
    d.rectangle((480, 300, 620, 420), outline=(25, 25, 25), width=5)
    d.ellipse((650, 160, 720, 230), fill=(204, 92, 32))   # orange accent
    d.rectangle((500, 160, 600, 230), fill=(40, 70, 160))  # stray blue
    d.rectangle((640, 330, 780, 420), fill=(150, 146, 140))  # grey wash
    if dark_fill:
        d.rectangle((120, 440, 420, 500), fill=(18, 18, 22))  # a deliberate black fill (a night sky, say)
    return im


def _patch_lum(im: Image.Image, box) -> float:
    import numpy as np
    a = np.asarray(im.crop(box).convert("RGB"), np.float32) / 255.0
    return float((a @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean())


def test_ink_normalize_cleans_the_paper_and_keeps_the_drawing(tmp_path: Path):
    import hashlib

    import numpy as np

    from sermon_visuals.studio import ink
    src = tmp_path / "pic.png"
    _sketchbook_photo().save(src)
    before_bytes = hashlib.sha256(src.read_bytes()).hexdigest()
    with Image.open(src) as im:
        raw = im.convert("RGB")
        out = ink.normalize(im)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before_bytes  # the cached file is never touched
    assert out.size == raw.size and out.mode == "RGB"
    q0, q1 = ink.qc(raw), ink.qc(out)
    assert q0.border_lum < 0.9 and q1.border_lum >= 0.97
    assert q0.off_palette > 0.01 and q1.off_palette < 0.005
    # paper, the neighbouring page, the binding and the desk are white now
    for box in [(60, 60, 160, 120), (0, 200, 12, 300), (14, 200, 17, 300), (936, 200, 960, 300), (820, 40, 900, 100)]:
        assert _patch_lum(out, box) >= 0.97, box
    # line work, the black fill and the grey wash keep their tone
    assert _patch_lum(out, (201, 260, 203, 300)) < 0.15
    assert _patch_lum(out, (140, 450, 400, 490)) < 0.12
    assert abs(_patch_lum(out, (660, 345, 760, 405)) - _patch_lum(raw, (660, 345, 760, 405)) / 0.88) < 0.12
    # the orange accent keeps its colour; the stray blue turns grey at about the same darkness
    a = np.asarray(out, np.float32) / 255.0
    hue, sat = ink._hue_sat(a[175:215, 665:705])
    assert sat.mean() > 0.6 and 15 <= float(np.median(hue)) <= 40
    _, sat = ink._hue_sat(a[175:215, 515:585])
    assert sat.mean() < 0.05 and abs(_patch_lum(out, (515, 175, 585, 215)) - _patch_lum(raw, (515, 175, 585, 215))) < 0.15


def _figure(d, cx, cy, halo: bool):
    d.ellipse((cx - 28, cy - 28, cx + 28, cy + 28), outline=(20, 20, 20), width=4)        # head
    d.line((cx, cy + 28, cx, cy + 120), fill=(20, 20, 20), width=4)                     # body
    if halo:
        d.arc((cx - 48, cy - 48, cx + 48, cy + 48), 180, 360, fill=(235, 125, 40), width=9)


def test_ink_qc_measures_wash_colour_border_and_halos():
    from PIL import ImageDraw

    from sermon_visuals.studio import ink
    white = Image.new("RGB", (960, 540), (255, 255, 255))
    q = ink.qc(white)
    assert q.grey_wash == 0 and q.off_palette == 0 and q.border_lum > 0.99 and not q.halo and q.reasons() == []
    im = white.copy()
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 479, 269), fill=(140, 140, 140))      # a quarter of the picture in grey wash
    d.rectangle((480, 270, 959, 539), fill=(30, 60, 170))     # a quarter in blue
    q = ink.qc(im)
    assert abs(q.grey_wash - 0.25) < 0.02 and abs(q.off_palette - 0.25) < 0.02 and q.border_lum < 0.9
    assert any("grey" not in r and "edges" in r for r in q.reasons())
    # an orange ring over a head near the top is flagged; the same low in the picture, a head without one, or an
    # orange lamp with a round handle beside it is not
    for cy, halo, expect in ((90, True, True), (420, True, False), (90, False, False)):
        im = white.copy()
        _figure(ImageDraw.Draw(im), 300, cy, halo)
        q = ink.qc(im)
        assert q.halo is expect, (cy, halo)
        if expect:
            x0, y0, x1, y1 = q.halo_boxes[0]
            assert x0 < 300 < x1 and y0 < cy and "halo" in q.reasons()[0]
    im = white.copy()
    d = ImageDraw.Draw(im)
    d.ellipse((240, 70, 340, 110), fill=(200, 100, 45), outline=(20, 20, 20), width=3)   # lamp body
    d.ellipse((338, 74, 368, 104), outline=(20, 20, 20), width=4)                         # its handle
    d.polygon([(260, 70), (268, 40), (276, 70)], fill=(240, 150, 40))                      # flame
    assert not ink.qc(im).halo


def _real_ink_pictures() -> list[Path]:
    from sermon_visuals.imagegen import content_guard
    from sermon_visuals.studio import assets, plan
    from sermon_visuals.studio.levels import STYLES
    from sermon_visuals.studio.run import shots_from
    root = HERE.parent
    work = root / "output" / "about_christmas" / "studio" / "clip_0000_0300"
    if not (root / "config.toml").exists() or not (work / "storyboard_L3.json").exists():
        return []
    cfg = load_config(root / "config.toml")
    board = plan.Storyboard.model_validate_json((work / "storyboard_L3.json").read_text(encoding="utf-8"))
    sents = plan.sentences_from_words(plan.load_words(work / "words.json"))
    shots = shots_from(plan.repair(board, len(sents)), sents, 300.0)
    assets.assign_paths(shots, work / "assets", STYLES["ink"], content_guard(cfg), cfg.studio.motion_model)
    found = [s.image for s in shots if s.image.exists()]
    return found if len(found) == len(shots) else []


def test_ink_normalize_on_the_real_ink_pictures():
    """Runs only on the machine that holds the about_christmas ink clip (21 pictures); skipped elsewhere."""
    from sermon_visuals.studio import ink
    pictures = _real_ink_pictures()
    if not pictures:
        pytest.skip("the about_christmas ink pictures are not on this computer")
    assert len(pictures) == 21
    for path in pictures:
        with Image.open(path) as im:
            small = im.convert("RGB").resize((im.width // 2, im.height // 2), Image.BOX)
        q = ink.qc(ink.normalize(small))
        assert q.border_lum >= 0.97 and q.off_palette < 0.005, (path.name, q)


# ---------- redrawing chosen beats, and the picture check after generation ----------

class _ColourProvider:
    """Pictures in a new colour on every call, so a redraw is visible in the file."""
    name, model = "fake", "fake-1"

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt: str, aspect: str, references) -> bytes:
        import io
        self.calls.append(prompt)
        buf = io.BytesIO()
        Image.new("RGB", (320, 180), (255, 255, 255 - 20 * len(self.calls))).save(buf, "PNG")
        return buf.getvalue()


def _two_beat_board():
    from sermon_visuals.studio import plan as P
    return P.Storyboard(title="T", chapters=[], readings=[], beats=[
        P.Beat(first=0, last=1, scene="Two hands and a rolled scroll.", motion="", hero=False, key_phrase=""),
        P.Beat(first=2, last=99, scene="A clay oil lamp beside a closed jar.", motion="", hero=False, key_phrase="")])


def test_redraw_chosen_beats_goes_through_the_estimate_and_the_ledger(workspace: Path, monkeypatch, capsys):
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    monkeypatch.setattr(run.render, "render", lambda tl, audio, out, **k: out)
    cfg.studio.image_usd = 0.10
    provider = _ColourProvider()
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=_two_beat_board(), provider=provider,
                   confirm=lambda q: "y")
    assert len(provider.calls) == 2
    pictures = sorted((work / "assets").glob("img_*.png"))
    first = {p.name: p.read_bytes() for p in pictures}
    spent = json.loads((work / "spend.json").read_text(encoding="utf-8"))["usd"]

    # declining the question changes nothing
    asked: list[str] = []
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), provider=provider, redraw=[2],
                   confirm=lambda q: asked.append(q) or "n")
    assert asked == ["  Spend about $0.10? [y/N] "] and len(provider.calls) == 2
    assert {p.name: p.read_bytes() for p in (work / "assets").glob("img_*.png")} == first

    # saying yes redraws only beat 2, counts it, and keeps the old picture beside the new one
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), provider=provider, redraw=[2], confirm=lambda q: "y")
    assert len(provider.calls) == 3 and "A clay oil lamp beside a closed jar." in provider.calls[-1]
    assert json.loads((work / "spend.json").read_text(encoding="utf-8"))["usd"] == pytest.approx(spent + 0.10)
    now = {p.name: p.read_bytes() for p in (work / "assets").glob("img_*.png")}
    changed = [n for n in first if now[n] != first[n]]
    assert len(changed) == 1
    backups = list((work / "assets").glob("*.before-redraw*.png"))
    assert len(backups) == 1 and backups[0].read_bytes() == first[changed[0]]
    out = capsys.readouterr().out
    assert "Redrawing beat 2" in out

    # without --redraw nothing is drawn again
    run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), provider=provider, confirm=_no_prompt)
    assert len(provider.calls) == 3
    with pytest.raises(ValueError) as info:
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), provider=provider, redraw=[7], confirm=_no_prompt)
    assert "7" in str(info.value) and "2 beats" in str(info.value)


def test_redraw_option_on_the_command_line():
    from sermon_visuals.cli import build_parser
    args = build_parser().parse_args(["studio", "about_christmas", "--redraw", "5, 17"])
    assert args.redraw == [5, 17]
    assert build_parser().parse_args(["studio", "about_christmas"]).redraw is None
    for bad in ("five", "0", "3,,x"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["studio", "about_christmas", "--redraw", bad])


def test_ink_picture_check_is_printed_after_generation(workspace: Path, monkeypatch, capsys):
    import io

    from PIL import ImageDraw
    cfg, run, calls, work = _gated_studio(workspace, monkeypatch)
    monkeypatch.setattr(run.render, "render", lambda tl, audio, out, **k: out)
    cfg.studio.image_usd = 0.10

    class Pictures:
        name, model = "fake", "fake-1"

        def generate(self, prompt, aspect, references):
            im = Image.new("RGB", (960, 540), (255, 255, 255))
            d = ImageDraw.Draw(im)
            if "lamp" in prompt:  # the second beat: murky grey wash over most of the picture
                d.rectangle((60, 40, 900, 500), fill=(130, 130, 130))
            d.line((300, 200, 600, 300), fill=(20, 20, 20), width=5)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()

    for style, expect in (("ink", True), ("graphic", False)):
        cfg.studio.style = style
        run.run_studio(cfg, "btw-0001", level=3, words=_studio_words(), board=_two_beat_board(), provider=Pictures(),
                       confirm=lambda q: "y")
        out = capsys.readouterr().out
        assert ("Picture check" in out) is expect, style
        if expect:
            check = out[out.index("Picture check"):]
            assert "Beat 2: heavy grey shading" in check and "Beat 1:" not in check and "--redraw 2" in check


# ---------- the ink (light paper) theme for the text layers ----------

INK_FIXTURES = sorted((HERE / "fixtures" / "ink").glob("*.jpg"))  # six real ink pictures, downscaled


def _rel_lum(rgb):
    import numpy as np
    c = np.asarray(rgb, np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin @ np.array([0.2126, 0.7152, 0.0722])


def _grow(mask, r: int):
    import numpy as np
    from PIL import ImageFilter
    im = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * r + 1))
    return np.asarray(im) > 0


def _contrast(with_text, without_text, colour, others=(), tol=40, ring=(3, 7), inset=0):
    """WCAG contrast measured from pixels: the pixels a layer drew in `colour` against the frame right around them
    (a ring 3 to 7 pixels out, leaving out anything drawn in the layer's other colours). `inset` keeps only pixels
    that far inside the layer's box, so a panel's ink rule is not counted as type. Returns (median, worst decile)
    of the ratio, and how many pixels were drawn in the colour."""
    import numpy as np
    a, b = with_text.astype(np.int32), without_text.astype(np.int32)
    changed = np.abs(a - b).sum(-1) > 24
    if inset:
        ys, xs = np.nonzero(changed)
        keep = np.zeros_like(changed)
        keep[ys.min() + inset:ys.max() - inset + 1, xs.min() + inset:xs.max() - inset + 1] = True
        changed &= keep

    def near(c):
        return np.abs(a - np.array(c)).max(-1) <= tol

    glyph = changed & near(colour)
    drawn = glyph.copy()
    for c in others:
        drawn |= changed & near(c)
    bg = _grow(glyph, ring[1]) & ~_grow(drawn, ring[0])
    if glyph.sum() < 20 or bg.sum() < 20:
        return 0.0, 0.0, int(glyph.sum())
    fg = float(np.median(_rel_lum(with_text[glyph])))
    lum_bg = _rel_lum(with_text[bg])

    def ratio(lum):
        return (max(lum, fg) + 0.05) / (min(lum, fg) + 0.05)

    return ratio(float(np.median(lum_bg))), ratio(float(np.percentile(lum_bg, 10))), int(glyph.sum())


def _ink_timeline(pic, tmp: Path, *, callouts=(), cards=(), verses=False, chapters=(), shots=None, theme=None):
    from sermon_visuals.studio import render
    from sermon_visuals.studio.theme import INK
    from sermon_visuals.verses import PAPER_BAND, Scripture, build_overlays
    ovs = build_overlays([Scripture(start=1.0, end=7.0, reference="Luke 2:8", version="KJV",
                                    text="And there were in the same country shepherds abiding in the field, keeping watch "
                                         "over their flock by night.")],
                         tmp / "verses", 960, 540, compose.Fonts.resolve(), band=PAPER_BAND) if verses else []
    return render.Timeline(duration=10.0, shots=shots or [render.ShotSpec(0.0, 10.0, pic)], callouts=list(callouts),
                           cards=list(cards), chapters=list(chapters), verses=ovs, theme=theme or INK)


def _frames(tl, times, W=960, H=540):
    from sermon_visuals.studio import render
    r = render.Renderer(tl, W, H, 24)
    out = [r.frame(t) for t in times]
    r.close()
    return out


def _phrase(t0=1.0):
    from sermon_visuals.studio import render
    return render.Callout(words=[("Nobody", t0), ("kept", t0 + 0.3), ("it", t0 + 0.6), ("then.", t0 + 0.9)], end=t0 + 5.0)


def _card(start=1.0, end=7.0):
    from sermon_visuals.studio import plan, render
    card = plan.Card(type="list", title="Three reasons", items=[
        plan.CardItem(label="No date given", detail="Scripture is silent"), plan.CardItem(label="No feast kept", detail="")])
    return render.CardSpec(card=card, start=start, end=end, item_times=[start + 0.2, start + 0.6])


def test_ink_text_layers_meet_contrast_over_real_ink_pictures(tmp_path: Path):
    from sermon_visuals.studio import render
    from sermon_visuals.studio.theme import INK
    assert len(INK_FIXTURES) >= 5
    for pic in INK_FIXTURES:
        # key phrase: charcoal on the picture, at least 7:1; its orange underline at least 4.5:1
        with_text, = _frames(_ink_timeline(pic, tmp_path, callouts=[_phrase()]), [4.0])
        bare, = _frames(_ink_timeline(pic, tmp_path), [4.0])
        med, low, n = _contrast(with_text, bare, INK.ink, others=[INK.accent])
        assert n > 500 and med >= 7.0 and low >= 4.5, (pic.name, "key phrase", med, low)
        med, low, n = _contrast(with_text, bare, INK.accent, others=[INK.ink])
        assert n > 100 and med >= 4.5, (pic.name, "underline", med)
        # scripture band: charcoal serif at least 7:1, the orange reference at least 4.5:1
        with_band, = _frames(_ink_timeline(pic, tmp_path, verses=True), [4.0])
        med, low, n = _contrast(with_band, bare, INK.ink, others=[INK.accent], inset=10)
        assert n > 500 and med >= 7.0 and low >= 7.0, (pic.name, "scripture", med, low)
        med, low, n = _contrast(with_band, bare, INK.accent, others=[INK.ink], inset=10)
        assert n > 50 and med >= 4.5, (pic.name, "reference", med)
        # info card: labels at least 7:1, the orange title at least 4.5:1
        with_card, = _frames(_ink_timeline(pic, tmp_path, cards=[_card()]), [4.0])
        med, low, n = _contrast(with_card, bare, INK.ink, others=[INK.accent, INK.muted], inset=10)
        assert n > 200 and med >= 7.0 and low >= 7.0, (pic.name, "card", med, low)
        med, low, n = _contrast(with_card, bare, INK.accent, others=[INK.ink, INK.muted], inset=10)
        assert n > 50 and med >= 4.5, (pic.name, "card title", med)
    # chapter card and chip
    pic = INK_FIXTURES[0]
    chapters = [render.ChapterMark(1, "Nobody knows the date", 0.0)]
    title, chip = _frames(_ink_timeline(pic, tmp_path, chapters=chapters), [1.2, 4.0])
    bare1, bare4 = _frames(_ink_timeline(pic, tmp_path), [1.2, 4.0])
    med, low, n = _contrast(title, bare1, INK.ink, others=[INK.accent])
    assert n > 500 and med >= 7.0, ("chapter", med)
    med, low, n = _contrast(chip, bare4, INK.ink, others=[INK.accent], inset=4)
    assert n > 100 and med >= 7.0, ("chip", med)


def test_ink_key_phrase_is_whole_and_its_underline_draws_on(tmp_path: Path):
    from sermon_visuals.studio.theme import INK
    pic = INK_FIXTURES[1]
    times = [1.05, 1.5, 2.6]
    shown = _frames(_ink_timeline(pic, tmp_path, callouts=[_phrase()]), times)
    bare = _frames(_ink_timeline(pic, tmp_path), times)
    ink_px = [_contrast(a, b, INK.ink, others=[INK.accent])[2] for a, b in zip(shown, bare)]
    accent_px = [_contrast(a, b, INK.accent, others=[INK.ink])[2] for a, b in zip(shown, bare)]
    assert ink_px[1] > 500 and abs(ink_px[1] - ink_px[2]) <= 0.02 * ink_px[2]  # every word is up from the start
    assert accent_px[0] < accent_px[1] < accent_px[2]                          # the underline follows the words


def test_ink_key_phrase_shows_the_vetted_words():
    from sermon_visuals.studio import render
    c = render.Callout(words=[("“Not", 1.0), ("a", 1.2), ("hint", 1.4), ("in", 1.6), ("Acts,", 1.8)], end=5.0)
    assert render.callout_words(c) == ["Not", "a", "hint", "in", "Acts"]  # his words, in order, as spoken; marks dropped


def test_ink_text_waits_for_its_picture(tmp_path: Path):
    """A key phrase or card on a new picture appears only once that picture has finished inking in."""
    from sermon_visuals.studio import render
    a = _golden_picture(tmp_path / "a.png", 1)
    b = _golden_picture(tmp_path / "b.png", 2)
    shots = [render.ShotSpec(0.0, 5.0, a), render.ShotSpec(5.0, 10.0, b)]
    times = [4.9, 5.3, 5.7, 5.9]
    for layer in ({"callouts": [_phrase(5.05)]}, {"cards": [_card(5.3, 9.0)]}):
        shown = _frames(_ink_timeline(None, tmp_path, shots=shots, **layer), times)
        bare = _frames(_ink_timeline(None, tmp_path, shots=shots), times)
        assert (shown[1] == bare[1]).all() and (shown[2] == bare[2]).all(), layer  # picture still inking in
        assert not (shown[3] == bare[3]).all(), layer                               # drawn: the text follows


def test_ink_key_phrase_avoids_the_busy_side_and_never_sits_on_the_band(tmp_path: Path):
    import numpy as np
    from PIL import ImageDraw

    from sermon_visuals.studio.theme import INK
    for busy_left in (True, False):
        im = Image.new("RGB", (1536, 864), (255, 255, 255))
        d = ImageDraw.Draw(im)
        x0 = 60 if busy_left else 800
        for k in range(0, 680, 9):  # dense hatching over one upper half
            d.line((x0 + k, 60, x0 + k - 40, 560), fill=(25, 25, 25), width=4)
        pic = tmp_path / f"busy_{busy_left}.png"
        im.save(pic)
        shown, = _frames(_ink_timeline(pic, tmp_path, callouts=[_phrase()]), [3.0])
        bare, = _frames(_ink_timeline(pic, tmp_path), [3.0])
        a = shown.astype(int)
        glyph = (np.abs(a - bare.astype(int)).sum(-1) > 24) & (np.abs(a - np.array(INK.ink)).max(-1) <= 40)
        xs = np.nonzero(glyph)[1]
        assert len(xs) > 500 and ((xs.mean() > 480) if busy_left else (xs.mean() < 480)), busy_left
    # during a scripture reading the band is shown and the key phrase is not
    pic = INK_FIXTURES[0]
    both, = _frames(_ink_timeline(pic, tmp_path, callouts=[_phrase()], verses=True), [3.0])
    band, = _frames(_ink_timeline(pic, tmp_path, verses=True), [3.0])
    assert (both == band).all()


def test_ink_band_keeps_the_same_size_place_and_timing(tmp_path: Path):
    import numpy as np

    from sermon_visuals.verses import DARK_BAND, PAPER_BAND, Scripture, build_overlays, render_overlay
    fonts = compose.Fonts.resolve()
    text = " ".join(["And the angel said unto them, Fear not."] * 20)
    for start, end in ((10, 22), (0, 6)):
        s = [Scripture(start=start, end=end, reference="Luke 2:10", version="KJV", text=text)]
        dark = build_overlays(s, tmp_path / f"d{start}", 1920, 1080, fonts, 55, 4.0)
        paper = build_overlays(s, tmp_path / f"p{start}", 1920, 1080, fonts, 55, 4.0, band=PAPER_BAND)
        assert [(o.start, o.end) for o in dark] == [(o.start, o.end) for o in paper]
    for band, name in ((DARK_BAND, "d.png"), (PAPER_BAND, "p.png")):
        render_overlay("Luke 2:10", "KJV", text[:300], 1920, 1080, fonts, tmp_path / name, band=band)
    with Image.open(tmp_path / "d.png") as d, Image.open(tmp_path / "p.png") as p:
        assert d.getchannel("A").getbbox() == p.getchannel("A").getbbox()
        centre = np.asarray(p.convert("RGBA"))[1080 - 60, 120]
        assert tuple(centre) == (252, 251, 248, 255)  # opaque paper


def test_theme_follows_the_style(tmp_path: Path):
    from sermon_visuals.studio import plan, run
    from sermon_visuals.studio.levels import layers_for
    from sermon_visuals.studio.theme import DARK, INK, theme_for
    assert theme_for("ink") is INK and theme_for("INK ") is INK
    for style in ("graphic", "painterly", "woodcut", "My own watercolour sentence.", ""):
        assert theme_for(style) is DARK
    words = _studio_words()
    sents = plan.sentences_from_words(words)
    board = _studio_board().model_copy(update={"readings": [plan.Reading(first=1, last=1, reference="Luke 1:1", version="",
                                                                          text="They had no idea.")]})
    shots = run.shots_from(board, sents, words[-1].e + 1)
    for style, theme in (("ink", INK), ("graphic", DARK), (None, DARK)):
        kw = {} if style is None else {"style": style}
        tl = run.build_timeline(board, words, sents, words[-1].e + 1, shots, layers_for(3, {}), tmp_path / str(style),
                                640, 360, 55, 4.0, spans=None, **kw)
        assert tl.theme is theme
        with Image.open(tl.verses[0].png) as im:
            px = im.convert("RGBA").getpixel((40, 360 - 20))
        assert (px == (252, 251, 248, 255)) is (theme is INK), style
