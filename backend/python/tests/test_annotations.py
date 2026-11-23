from __future__ import annotations

from syrinx_backend.annotations.formats import export_annotations, import_annotations
from syrinx_backend.annotations.syrinx_io import (
    filter_annotations,
    find_nearest_annotation,
    find_next_annotation,
    find_prev_annotation,
    parse_syrinx_log,
    serialize_syrinx_log,
)
from syrinx_backend.models import AnnotationFilter


def test_syrinx_fieldkey_roundtrip_extra_fields_and_notes() -> None:
    sample = "\n".join(
        [
            "# Custom Syrinx header comment",
            "fieldkey:\tsoundfile\tchannel\tlefttimesec\trighttimesec\ttopfreqhz\tbottomfreqhz\tcomment\tnotes\tquality\tspecies",
            "an:\tC:\\Audio\\bird_song_01.wav\t1\t0.250000\t0.850000\t6500.000000\t1800.000000\tSong Type A\tLine1@Line2\tQ3\tSongSparrow",
            "an:\t\t2\t1.100000\t1.750000\t7200.000000\t2200.000000\tCall B\tWith\x02SOH\tQ1\tChickadee",
        ]
    )
    doc = parse_syrinx_log(sample)
    assert doc.non_annotation_lines == ["# Custom Syrinx header comment"]
    assert len(doc.annotations) == 2

    a0, a1 = doc.annotations
    assert a0.soundfile == "bird_song_01.wav"
    assert a0.channel == 1
    assert a0.start_time_sec == 0.25
    assert a0.end_time_sec == 0.85
    assert a0.low_freq_hz == 1800.0
    assert a0.high_freq_hz == 6500.0
    assert a0.comment == "Song Type A"
    assert a0.notes == "Line1\nLine2"
    assert a0.extra_fields["quality"] == "Q3"
    assert a0.extra_fields["species"] == "SongSparrow"

    # Empty soundfile inherits previous row's filename (classic CAnnotationList behavior)
    assert a1.soundfile == "bird_song_01.wav"
    assert a1.channel == 2
    assert a1.notes == "With\nSOH"

    serialized = serialize_syrinx_log(doc.annotations, non_annotation_lines=doc.non_annotation_lines)
    reparsed = parse_syrinx_log(serialized)
    assert reparsed.non_annotation_lines == ["# Custom Syrinx header comment"]
    assert len(reparsed.annotations) == 2
    assert reparsed.annotations[0].notes == "Line1\nLine2"
    assert reparsed.annotations[0].extra_fields["species"] == "SongSparrow"
    assert reparsed.annotations[1].notes == "With\nSOH"


def test_syrinx_legacy_key_value_pair_format() -> None:
    legacy = (
        "selected:\tD:\\recordings\\193a.wav\tchannel:\t1\t"
        "lefttime_abs:\t0.5\trighttime_abs:\t1.25\t"
        "bottomfreq:\t1500\ttopfreq:\t5500\t"
        "comment:\tTrill\tnotes:\tfirst@second\tcustomtag:\t42"
    )
    doc = parse_syrinx_log(legacy)
    assert len(doc.annotations) == 1
    ann = doc.annotations[0]
    assert ann.soundfile == "193a.wav"
    assert ann.start_time_sec == 0.5
    assert ann.end_time_sec == 1.25
    assert ann.low_freq_hz == 1500.0
    assert ann.high_freq_hz == 5500.0
    assert ann.comment == "Trill"
    assert ann.notes == "first\nsecond"
    assert ann.extra_fields["customtag"] == "42"


def test_filtering_and_navigation() -> None:
    sample = "\n".join(
        [
            "fieldkey:\tsoundfile\tchannel\tlefttimesec\trighttimesec\ttopfreqhz\tbottomfreqhz\tcomment\tnotes",
            "an:\ta.wav\t1\t0.5\t1.0\t4000\t1000\tSong A\t",
            "an:\ta.wav\t1\t2.0\t2.5\t5000\t1500\t\t",
            "an:\ta.wav\t2\t3.0\t3.5\t6000\t2000\tNoise\t",
            "an:\ta.wav\t1\t4.0\t4.5\t5500\t1200\tSong B\t",
        ]
    )
    anns = parse_syrinx_log(sample).annotations

    flt = AnnotationFilter(include_filter=True, include_text="Song", exclude_empty=True, channels=[1])
    filtered = filter_annotations(anns, flt, filename="C:\\dir\\a.wav")
    assert [a.comment for a in filtered] == ["Song A", "Song B"]

    assert find_next_annotation(filtered, 0.5).comment == "Song B"  # type: ignore[union-attr]
    assert find_prev_annotation(filtered, 4.0).comment == "Song A"  # type: ignore[union-attr]
    assert find_nearest_annotation(filtered, 3.6).comment == "Song B"  # type: ignore[union-attr]


def test_raven_csv_json_roundtrip() -> None:
    sample = "\n".join(
        [
            "fieldkey:\tsoundfile\tchannel\tlefttimesec\trighttimesec\ttopfreqhz\tbottomfreqhz\tcomment\tnotes",
            "an:\tsong.wav\t1\t0.10\t0.90\t7000\t2000\tWhistle\tClear",
        ]
    )
    anns = parse_syrinx_log(sample).annotations

    for fmt in ("raven_txt", "csv", "json"):
        exported = export_annotations(anns, fmt)  # type: ignore[arg-type]
        reimported = import_annotations(exported, fmt)  # type: ignore[arg-type]
        assert len(reimported.annotations) == 1
        rec = reimported.annotations[0]
        assert rec.comment == "Whistle"
        assert abs(rec.start_time_sec - 0.10) < 1e-4
        assert abs(rec.high_freq_hz - 7000.0) < 1e-1
