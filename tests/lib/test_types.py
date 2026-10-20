import pytest

from audioshake.client import planned_outputs
from audioshake.types import Model, Target, Task

from .conftest import load_fixture


def _model(models_json, model_id):
    return next(Model.from_api(m) for m in models_json["models"] if m["id"] == model_id)


class TestTarget:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (Target("vocals"), {"model": "vocals", "formats": ["wav"]}),
            (
                Target("multi_voice", formats=("mp3",), residual=True, num_speakers=3),
                {"model": "multi_voice", "formats": ["mp3"], "residual": True, "numSpeakers": 3},
            ),
            (
                Target("transcription", language="en"),
                {"model": "transcription", "formats": ["wav"], "language": "en"},
            ),
        ],
        ids=["minimal", "options in camelCase", "language only when set"],
    )
    def test_to_api_sends_only_what_is_set_in_camel_case(self, target, expected):
        assert target.to_api() == expected


class TestModel:
    @pytest.mark.parametrize(
        ("model_id", "default_format", "produces_media"),
        [("vocals", "wav", True), ("transcription", "json", False)],
    )
    def test_default_format_is_the_first_media_format_else_the_first(
        self, models_json, model_id, default_format, produces_media
    ):
        model = _model(models_json, model_id)
        assert model.default_format == default_format
        assert model.produces_media is produces_media

    def test_limits_and_access(self, models_json):
        assert _model(models_json, "multi_voice").max_input_seconds == 5400
        music = _model(models_json, "music")
        assert music.access == "requestAccess"
        assert not music.enabled
        assert music.credits_per_minute is None

    def test_unknown_keys_survive_in_raw(self):
        model = Model.from_api({"id": "x", "futureField": 1})
        assert model.raw["futureField"] == 1


class TestTask:
    def test_parses_completed_task(self):
        task = Task.from_api(load_fixture("task_completed.json"))
        assert task.id == "task_0123456789abcdef"
        assert task.done and task.succeeded
        assert task.cost == 3.0
        assert [t.model for t in task.targets] == ["vocals", "drums"]
        assert [o.name for o in task.outputs] == [
            "vocals",
            "vocals_residual",
            "drums",
            "drums_residual",
        ]

    @pytest.mark.parametrize(
        ("targets", "done", "succeeded", "errors", "cost"),
        [
            ([{"model": "vocals", "status": "processing"}], False, False, [], 0),
            (
                [
                    {"model": "vocals", "status": "failed", "message": "unsupported input"},
                    {"model": "drums", "status": "completed", "cost": "1.5"},
                ],
                True,
                False,
                ["unsupported input"],
                1.5,
            ),
        ],
        ids=["still processing", "one failed, one completed"],
    )
    def test_done_and_succeeded_follow_the_targets(self, targets, done, succeeded, errors, cost):
        task = Task.from_api({"id": "t", "targets": targets})
        assert task.done is done and task.succeeded is succeeded
        assert [t.error for t in task.failed] == errors
        assert task.cost == cost

    def test_api_error_status_and_json_error_are_normalised(self):
        task = Task.from_api(
            {
                "id": "t",
                "targets": [
                    {"model": "vocals", "status": "error", "error": {"message": "audio too short"}},
                    {"model": "drums", "status": "created"},
                    {"model": "bass", "status": "PROCESSING"},
                ],
            }
        )
        assert [t.status for t in task.targets] == ["failed", "pending", "processing"]
        assert task.failed[0].error == "audio too short"
        assert not task.done

    def test_unknown_status_is_treated_as_running_not_success(self, caplog):
        from audioshake import types

        types._unknown_statuses_seen.clear()
        with caplog.at_level("WARNING", logger="audioshake.tasks"):
            task = Task.from_api({"id": "t", "targets": [{"model": "vocals", "status": "paused"}]})
            Task.from_api({"id": "t", "targets": [{"model": "vocals", "status": "paused"}]})
        assert task.targets[0].status == "processing" and not task.done
        assert caplog.text.count("unknown target status") == 1


def _task_with_outputs(*targets):
    return Task.from_api(
        {
            "id": "t",
            "targets": [
                {
                    "model": model,
                    "status": "completed",
                    "output": [
                        {"name": n, "format": f, "link": f"https://s/{i}"}
                        for i, (n, f) in enumerate(outputs)
                    ],
                }
                for model, outputs in targets
            ],
        }
    )


def _file_names(task):
    return [f"{stem}.{output.format}" for output, _, stem in planned_outputs(task)]


def test_planned_outputs_compare_names_after_sanitising():
    # "a/vocals" and "vocals" both sanitise to "vocals".
    task = _task_with_outputs(("m1", [("vocals", "wav")]), ("m2", [("a/vocals", "wav")]))
    names = _file_names(task)
    assert names == ["m1_vocals.wav", "m2_vocals.wav"]


def test_planned_outputs_ignore_case_when_comparing():
    task = _task_with_outputs(("m1", [("Vocals", "wav")]), ("m2", [("vocals", "WAV")]))
    assert len({n.casefold() for n in _file_names(task)}) == 2


def test_one_output_in_two_formats_keeps_the_plain_name():
    # The extension already tells them apart, so no model prefix is needed.
    task = _task_with_outputs(("alignment", [("alignment", "json"), ("alignment", "srt")]))
    assert _file_names(task) == ["alignment.json", "alignment.srt"]


def test_prefixed_name_cannot_overwrite_a_real_output():
    # Both models' "residual" become <model>_residual, which m3 already produces.
    task = _task_with_outputs(
        ("vocals", [("vocals", "wav"), ("residual", "wav")]),
        ("drums", [("residual", "wav")]),
        ("m3", [("vocals_residual", "wav")]),
    )
    names = _file_names(task)
    assert len(set(names)) == len(names)
    assert names[:3] == ["vocals.wav", "vocals_residual.wav", "drums_residual.wav"]
