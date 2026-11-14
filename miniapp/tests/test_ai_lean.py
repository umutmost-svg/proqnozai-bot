"""The model's forecast on every match with probabilities ("lean"): deterministic, from the shown percents,
"model" confidence only in the validated population, separate from the strict pick."""
from __future__ import annotations

from miniapp.ai.analysis import AnalysisResult, lean_of


def test_the_top_outcome_wins() -> None:
    assert lean_of(54, 26, 20, "estimate") == {"market": "1", "probability": 54, "confidence": "estimate"}
    assert lean_of(20, 26, 54, "model") == {"market": "2", "probability": 54, "confidence": "model"}
    assert lean_of(30, 40, 30, "estimate")["market"] == "X"


def test_a_tied_top_two_is_their_double_chance() -> None:
    assert lean_of(40, 20, 40, "estimate") == {"market": "12", "probability": 80, "confidence": "estimate"}
    assert lean_of(38, 38, 24, "estimate") == {"market": "1X", "probability": 76, "confidence": "estimate"}
    assert lean_of(24, 38, 38, "estimate") == {"market": "X2", "probability": 76, "confidence": "estimate"}


def _result(kind: str, estimated: bool, gate: bool | None, probabilities=None) -> AnalysisResult:
    forecast = None if gate is None else {"gateEligible": gate}
    return AnalysisResult("fd:1", kind, (), probabilities=probabilities or {"home": 50, "draw": 30, "away": 20},
                          estimated=estimated, forecast=forecast)


def test_model_confidence_only_for_a_ready_analysis_in_the_validated_population() -> None:
    assert _result("ready", False, True).to_json()["lean"]["confidence"] == "model"
    assert _result("ready", False, False).to_json()["lean"]["confidence"] == "estimate"   # e.g. not match day
    assert _result("partial", True, True).to_json()["lean"]["confidence"] == "estimate"   # PARTIAL is an estimate
    assert _result("partial", True, None).to_json()["lean"]["confidence"] == "estimate"   # market estimate


def test_no_probabilities_no_lean() -> None:
    assert AnalysisResult("fd:1", "insufficient", ("no_verified_form",)).to_json()["lean"] is None
    assert AnalysisResult("fd:1", "partial", (), probabilities=None, estimated=True).to_json()["lean"] is None


def test_the_forecast_says_whether_it_is_in_the_validated_population(clock) -> None:
    from miniapp.tests.test_ai_analysis import FakeModel, make_ai, run

    ai, _ = make_ai(clock, FakeModel())
    body = run(ai.analyse("fd:7", user_id=1)).to_json()
    assert isinstance(body["forecast"]["gateEligible"], bool)
    assert body["lean"] is not None and body["lean"]["market"] in {"1", "X", "2", "1X", "X2", "12"}
