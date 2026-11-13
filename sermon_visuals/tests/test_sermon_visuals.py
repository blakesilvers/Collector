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
    assets.assign_paths(shots, tmp_path, "style", "neg", False, "m")
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
