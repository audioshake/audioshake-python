import json

import httpx
import pytest

from audioshake import (
    Asset,
    AsyncAudioShake,
    Model,
    Target,
    TaskFailedError,
    ValidationError,
    build_targets,
)
from audioshake._run import source_stem

from .conftest import BASE_URL


def _catalog(models_json):
    return {m["id"]: Model.from_api(m) for m in models_json["models"]}


class TestBuildTargets:
    @pytest.mark.parametrize(
        ("models", "expected"),
        [
            ("vocals", ["vocals"]),  # a str is a Sequence[str]: must not become v,o,c,a,l,s
            ("vocals,drums", ["vocals", "drums"]),
            (Target("vocals"), ["vocals"]),
        ],
        ids=["one-id", "comma-list", "one-target"],
    )
    def test_a_single_value_is_one_model_list(self, models, expected):
        assert [t.model for t in build_targets(models)] == expected

    def test_a_generator_of_targets_is_read_once(self):
        # build_targets reads models twice; a generator must not lose its Targets.
        targets = build_targets(Target(m) for m in ("vocals", "drums"))
        assert [t.model for t in targets] == ["vocals", "drums"]

    def test_defaults_from_catalog(self, models_json):
        targets = build_targets(
            ["vocals", "transcription", "multi_voice"],
            catalog=_catalog(models_json),
            language="en",
            num_speakers=3,
        )
        by_model = {t.model: t for t in targets}
        assert by_model["vocals"].formats == ("wav",)
        assert by_model["vocals"].language is None and by_model["vocals"].num_speakers is None
        assert by_model["transcription"].formats == ("json",)
        assert by_model["transcription"].language == "en"
        assert by_model["multi_voice"].num_speakers == 3
        assert by_model["multi_voice"].language is None

    @pytest.mark.parametrize(
        ("models", "format", "expected"),
        [
            (["vocals", "transcription"], "mp3", [("mp3",), ("json",)]),
            (["alignment"], "srt", [("srt",)]),
            (["alignment"], "txt", [("txt",)]),
            (["transcription"], "txt", [("txt",)]),
            (["vocals", "alignment"], "srt", [("wav",), ("srt",)]),
            (["vocals", "alignment"], "srt,mp3", [("mp3",), ("srt",)]),
            (["alignment"], ("json", "srt"), [("json", "srt")]),
            (["vocals", "alignment"], "SRT", [("wav",), ("srt",)]),
        ],
        ids=[
            "audio-format-skips-text-model",
            "srt-reaches-alignment",
            "txt-reaches-alignment",
            "txt-reaches-transcription",
            "text-format-leaves-audio-on-default",
            "comma-list-split-per-model",
            "one-model-several-files",
            "case-insensitive",
        ],
    )
    def test_each_model_takes_the_requested_formats_it_offers(
        self, models_json, models, format, expected
    ):
        targets = build_targets(models, catalog=_catalog(models_json), format=format)
        assert [t.formats for t in targets] == expected

    def test_catalog_may_be_the_list_models_list_returns(self, models_json):
        as_list = [Model.from_api(m) for m in models_json["models"]]
        kwargs = {"format": "mp3", "language": "en", "num_speakers": 3}
        targets = build_targets(
            ["vocals", "transcription", "multi_voice"], catalog=as_list, **kwargs
        )
        assert targets == build_targets(
            ["vocals", "transcription", "multi_voice"], catalog=_catalog(models_json), **kwargs
        )
        # Validation reads the list form too, not only the defaults.
        with pytest.raises(ValidationError, match=r"cannot output 'ogg'"):
            build_targets(["vocals"], catalog=as_list, format="ogg")

    @pytest.mark.parametrize(
        ("models", "format", "match"),
        [
            (["vocals"], "ogg", r"cannot output 'ogg'.*aiff, wav"),
            (["vocals"], "srt", r"cannot output 'srt'.*wav"),
            (["transcription"], "srt", r"cannot output 'srt'.*json, txt"),
            (["vocals", "drums"], "mp3,srt", "No selected model can output 'srt'"),
        ],
        ids=["unknown-format", "text-format-for-audio", "audio-format-for-text", "one-of-a-list"],
    )
    def test_a_format_no_selected_model_offers_is_rejected_with_choices(
        self, models_json, models, format, match
    ):
        with pytest.raises(ValidationError, match=match):
            build_targets(models, catalog=_catalog(models_json), format=format)

    def test_unsupported_language_is_rejected(self, models_json):
        with pytest.raises(ValidationError, match="does not support language 'xx'"):
            build_targets(["transcription"], catalog=_catalog(models_json), language="xx")

    def test_language_is_ignored_for_models_without_languages(self, models_json):
        targets = build_targets(["vocals"], catalog=_catalog(models_json), language="xx")
        assert targets[0].language is None

    @pytest.mark.parametrize(
        ("models", "catalog", "format", "language", "formats", "languages"),
        [
            (["alignment"], "empty", "mp3", None, [("json",)], [None]),
            (["brand_new_model"], "listed", "ogg", None, [("ogg",)], [None]),
            (
                ["vocals", "music_detection", "alignment"],
                None,
                "flac",
                "fr",
                [("flac",), ("json",), ("json",)],
                [None, None, "fr"],
            ),
            (["alignment"], None, "srt", None, [("srt",)], [None]),
            (["alignment"], None, "mp3", None, [("json",)], [None]),
        ],
        ids=[
            "model-missing-from-catalog",
            "unknown-model-takes-the-format",
            "no-catalog-per-model-defaults",
            "no-catalog-text-format-honoured",
            "no-catalog-audio-format-ignored-for-text",
        ],
    )
    def test_without_a_catalog_entry_the_fallback_decides(
        self, models_json, models, catalog, format, language, formats, languages
    ):
        """A model the catalog does not describe gets the built-in default, with
        the requested format applied only where the fallback knows it fits."""
        catalogs = {None: None, "empty": {}, "listed": _catalog(models_json)}
        targets = build_targets(models, catalog=catalogs[catalog], format=format, language=language)
        assert [t.formats for t in targets] == formats
        assert [t.language for t in targets] == languages

    def test_explicit_targets_pass_through_and_mix(self):
        custom = Target("multi_voice", formats=("flac",), num_speakers=2)
        targets = build_targets(["vocals", custom])
        assert targets == [Target("vocals"), custom]

    @pytest.mark.parametrize(
        "models",
        [
            [Target("vocals"), Target("vocals")],
            [Target("vocals"), Target("vocals", formats=("mp3",))],  # different options
            ["drums", Target("vocals"), Target("vocals")],
            ["effects,vocals", Target("effects")],  # through a comma list
        ],
        ids=["two-targets", "two-targets-different-formats", "mixed", "comma-list"],
    )
    def test_a_model_listed_twice_in_any_form_is_rejected(self, models):
        with pytest.raises(ValidationError, match="listed more than once"):
            build_targets(models)

    @pytest.mark.parametrize(
        ("typo", "listed"),
        [
            ("multi-voice", "multi_voice"),
            ("MULTI-VOICE", "multi_voice"),
            ("lead-vocals", "vocals_lead"),
            ("lead vocals", "vocals_lead"),
            ("electric_guitar", "guitar_electric"),
            ("lead_vocal", "vocals_lead"),  # singular, and reordered
            ("drum", "drums"),
            (Target("multi-voice"), "multi_voice"),
        ],
        ids=[
            "hyphen",
            "case",
            "reordered-hyphen",
            "reordered-space",
            "reordered",
            "singular-reordered",
            "singular",
            "ready-made-target",
        ],
    )
    def test_a_certain_misspelling_of_a_listed_model_is_stopped(self, models_json, typo, listed):
        """Only certain matches are stopped: the listed spelling after lowercasing
        and underscores, the same words in another order, or a word in the singular."""
        with pytest.raises(ValidationError, match=f"Did you mean '{listed}'"):
            build_targets([typo], catalog=_catalog(models_json))

    def test_words_shared_by_several_listed_models_are_not_guessed(self):
        # Reordering is only certain when one listed id has those words.
        catalog = {
            m: Model.from_api({"id": m, "outputFormats": ["wav"]})
            for m in ("guitar_bass", "bass_guitar_x", "guitar_bass_x")
        }
        assert build_targets(["x_bass_guitar"], catalog=catalog)[0].model == "x_bass_guitar"

    def test_an_unlisted_model_still_passes_through(self, models_json):
        # Pre-release models are callable without being in the catalog, so the
        # API, not the client, decides whether an unknown id exists.
        targets = build_targets(["music_removal_beta"], catalog=_catalog(models_json))
        assert targets[0].model == "music_removal_beta"

    def test_without_a_catalog_nothing_is_second_guessed(self):
        assert build_targets(["multi-voice"])[0].model == "multi-voice"

    @pytest.mark.parametrize(
        ("models", "match"),
        [
            ([Target("vocals"), Target("vocals")], "listed more than once"),
            (["multi-voice"], "multi_voice"),
        ],
        ids=["duplicate", "misspelling"],
    )
    def test_validation_stops_the_run_before_any_upload(
        self, client, api, models_json, tmp_path, models, match
    ):
        api.get("/models").respond(200, json=models_json)
        upload = api.post("/assets")
        source = tmp_path / "song.wav"
        source.write_bytes(b"riff")
        with pytest.raises(ValidationError, match=match):
            client.run(source, models)
        assert not upload.called

    def test_empty(self):
        with pytest.raises(ValidationError):
            build_targets([])


def test_source_stem():
    assert source_stem("song.final.wav") == "song.final"
    assert source_stem("https://cdn.test/media/track.mp3?sig=1") == "track"
    assert source_stem("https://cdn.test/") == "url"


def test_safe_name_strips_paths_and_odd_characters():
    from audioshake._run import safe_name

    assert safe_name("../../etc/passwd", "x") == "passwd"
    assert safe_name("..", "x") == "x"
    assert safe_name("", "x") == "x"
    assert safe_name("Live Take (v2).m4a", "x") == "Live Take _v2_.m4a"
    assert safe_name("C:\\music\\song", "x") == "song"
    assert source_stem("../weird name!.wav") == "weird name_"


def _wire(api, storage, task_json, models_json):
    api.get("/models").respond(200, json=models_json)
    api.post("/assets").respond(200, json={"id": "asset_1", "name": "song.wav"})
    create = api.post("/tasks").respond(200, json={"id": task_json["id"], "targets": []})
    pending = {
        "id": task_json["id"],
        "targets": [
            {"model": "vocals", "status": "processing"},
            {"model": "drums", "status": "pending"},
        ],
    }
    api.get(f"/tasks/{task_json['id']}").side_effect = [
        httpx.Response(200, json=pending),
        httpx.Response(200, json=task_json),
    ]
    for name in ("vocals.wav", "vocals_residual.wav", "drums.wav", "drums_residual.wav"):
        storage.get(f"/{name}").respond(200, content=b"pcm")
    return create


class TestRunSync:
    def test_full_flow_from_file(self, client, api, storage, task_json, models_json, tmp_path):
        create = _wire(api, storage, task_json, models_json)
        source = tmp_path / "song.wav"
        source.write_bytes(b"riff" * 100)
        events = []

        # One bare comma list is one model list, not one model per letter.
        result = client.run(
            source, "vocals,drums", output_dir=tmp_path / "out", on_progress=events.append
        )

        sent = json.loads(create.calls.last.request.content)
        assert sent["assetId"] == "asset_1"
        assert [t["model"] for t in sent["targets"]] == ["vocals", "drums"]
        assert result.task.succeeded and result.task.cost == 3.0
        assert [p.name for p in result.files] == [
            "vocals.wav",
            "vocals_residual.wav",
            "drums.wav",
            "drums_residual.wav",
        ]
        assert result.files[0].parent == tmp_path / "out"
        stages = [e.stage for e in events]
        assert stages[0] == "upload" and stages[-1] == "done"
        assert "queued" in stages and "processing" in stages and "download" in stages
        processing = [e for e in events if e.stage == "processing"]
        assert (processing[0].done, processing[0].total) == (0, 2)
        assert (processing[-1].done, processing[-1].total) == (2, 2)
        assert events[-1].task is result.task
        # The asset outlives the run; the result and the upload event both name it.
        assert result.asset is not None and result.asset.id == "asset_1"
        uploaded = [e for e in events if e.stage == "upload" and e.asset is not None]
        assert [e.asset.id for e in uploaded] == ["asset_1"]
        assert uploaded[0].done == uploaded[0].total == source.stat().st_size

    def test_url_source_skips_upload_and_download_can_be_disabled(
        self, client, api, storage, task_json, models_json
    ):
        create = _wire(api, storage, task_json, models_json)
        upload = api.post("/assets")
        result = client.run("https://cdn.test/track.mp3", ["vocals"], download=False)
        assert json.loads(create.calls.last.request.content)["url"] == "https://cdn.test/track.mp3"
        assert not upload.called
        assert result.files == ()
        assert result.asset is None  # nothing was uploaded
        assert not storage.calls

    def test_an_asset_source_is_not_uploaded_again(
        self, client, api, storage, task_json, models_json, tmp_path
    ):
        create = _wire(api, storage, task_json, models_json)
        upload = api.post("/assets")
        events = []
        earlier = Asset(id="asset_1", name="song.wav")

        result = client.run(
            earlier, ["vocals,drums"], output_dir=tmp_path / "out", on_progress=events.append
        )

        assert json.loads(create.calls.last.request.content)["assetId"] == "asset_1"
        assert not upload.called
        assert result.asset is earlier
        assert "upload" not in [e.stage for e in events]
        assert len(result.files) == 4

    def test_options_reach_the_payload(self, client, api, storage, task_json, models_json):
        create = _wire(api, storage, task_json, models_json)
        client.run(
            "https://cdn.test/a.wav",
            ["multi_voice", "transcription"],
            format="mp3",
            residual=True,
            language="de",
            num_speakers=4,
            download=False,
        )
        sent = json.loads(create.calls.last.request.content)
        assert sent["targets"] == [
            {"model": "multi_voice", "formats": ["mp3"], "residual": True, "numSpeakers": 4},
            {"model": "transcription", "formats": ["json"], "residual": True, "language": "de"},
        ]

    def test_catalog_failure_falls_back_to_defaults(
        self, client, api, storage, task_json, no_sleep
    ):
        api.get("/models").respond(500)
        create = api.post("/tasks").respond(200, json={"id": task_json["id"], "targets": []})
        api.get(f"/tasks/{task_json['id']}").respond(200, json=task_json)
        client.run("https://cdn.test/a.wav", ["transcription"], download=False)
        sent = json.loads(create.calls.last.request.content)
        assert sent["targets"][0]["formats"] == ["json"]


def _with_failed(task_json, *models):
    """The fixture task with ``models`` failed: no outputs, hold released (cost 0)."""
    targets = [
        {**t, "status": "failed", "cost": 0, "output": [], "error": {"message": "worker died"}}
        if t["model"] in models
        else t
        for t in task_json["targets"]
    ]
    return {**task_json, "targets": targets}


class TestRunPartialFailure:
    """A failed target's hold is released; completed ones are charged, so keep them."""

    def test_completed_targets_are_downloaded_when_another_fails(
        self, client, api, storage, task_json, models_json, tmp_path, caplog
    ):
        _wire(api, storage, _with_failed(task_json, "drums"), models_json)

        with caplog.at_level("WARNING", logger="audioshake"):
            result = client.run(
                "https://cdn.test/a.wav", ["vocals", "drums"], output_dir=tmp_path / "out"
            )

        assert [p.name for p in result.files] == ["vocals.wav", "vocals_residual.wav"]
        assert all(p.read_bytes() == b"pcm" for p in result.files)
        assert [t.model for t in result.failed] == ["drums"]
        assert result.task.cost == 1.5  # vocals only; the failed target cost nothing
        assert any("drums failed" in m for m in caplog.messages)

    def test_a_task_where_everything_failed_still_raises(
        self, client, api, storage, task_json, models_json, tmp_path
    ):
        _wire(api, storage, _with_failed(task_json, "vocals", "drums"), models_json)

        with pytest.raises(TaskFailedError) as failed:
            client.run("https://cdn.test/a.wav", ["vocals", "drums"], output_dir=tmp_path / "out")

        assert [t.model for t in failed.value.task.failed] == ["vocals", "drums"]
        assert not storage.calls

    async def test_async_run_keeps_the_completed_targets_too(
        self, api, storage, task_json, models_json, tmp_path, monkeypatch
    ):
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("audioshake._retry.async_sleep", fake_sleep)
        _wire(api, storage, _with_failed(task_json, "drums"), models_json)
        async with AsyncAudioShake(api_key="k", base_url=BASE_URL) as aclient:
            result = await aclient.run(
                "https://cdn.test/a.wav", ["vocals", "drums"], output_dir=tmp_path / "out"
            )
        assert [p.name for p in result.files] == ["vocals.wav", "vocals_residual.wav"]
        assert [t.model for t in result.failed] == ["drums"]


class TestRunAsync:
    async def test_an_asset_source_is_not_uploaded_again(
        self, api, storage, task_json, models_json, tmp_path
    ):
        create = _wire(api, storage, task_json, models_json)
        upload = api.post("/assets")
        async with AsyncAudioShake(api_key="k", base_url=BASE_URL) as aclient:
            result = await aclient.run(
                Asset(id="asset_1", name="song.wav"), ["vocals,drums"], output_dir=tmp_path / "out"
            )
        assert json.loads(create.calls.last.request.content)["assetId"] == "asset_1"
        assert not upload.called
        assert result.asset is not None and result.asset.id == "asset_1"

    async def test_full_flow(self, api, storage, task_json, models_json, tmp_path, monkeypatch):
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("audioshake._retry.async_sleep", fake_sleep)
        _wire(api, storage, task_json, models_json)
        source = tmp_path / "song.wav"
        source.write_bytes(b"riff")
        events = []
        async with AsyncAudioShake(api_key="k", base_url=BASE_URL) as aclient:
            result = await aclient.run(
                source, ["vocals", "drums"], output_dir=tmp_path / "out", on_progress=events.append
            )
        assert len(result.files) == 4
        assert events[0].stage == "upload"
        assert events[-1].stage == "done"
        assert result.asset is not None and result.asset.id == "asset_1"
        assert [e.asset.id for e in events if e.asset is not None] == ["asset_1"]
