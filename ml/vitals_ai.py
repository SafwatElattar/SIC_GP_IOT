#!/usr/bin/env python3
"""Small, local JSON API and training script for the PulseCare demo.

The classifier predicts the supplied dataset's Risk Category label only.
It is not a clinical model. A rolling robust filter screens isolated sensor
spikes, but it cannot prove whether a sustained change is patient-related or
sensor-related.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import threading
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from itertools import combinations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import RobustScaler

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = APP_DIR / "data" / "human_vital_signs_dataset_2024.zip"
DEFAULT_MODEL = APP_DIR / "artifacts" / "vitals_models.joblib"
DEFAULT_REPORT = APP_DIR / "reports" / "evaluation.json"
FEATURES = ["bpm", "spo2_pct", "temp_c"]
SOURCE_COLUMNS = {"bpm": "Heart Rate", "spo2_pct": "Oxygen Saturation", "temp_c": "Body Temperature"}
MODEL_VERSION = "three-sensor-proxy-v1.0.0"
LABEL_NOTE = "Prediction of the dataset Risk Category label only; not a diagnosis or a clinical probability."
RANGE_CHECKS = {"bpm": (20.0, 250.0), "spo2_pct": (0.0, 100.0), "temp_c": (20.0, 50.0)}
# These are sudden-change screening parameters, not medical thresholds.
SPIKE_FLOORS = {"bpm": 25.0, "spo2_pct": 5.0, "temp_c": 1.0}
SPIKE_CONFIRM_TOLERANCE = {"bpm": 8.0, "spo2_pct": 2.0, "temp_c": 0.4}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_dataset(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"Expected one CSV inside {path.name}; found {len(names)}")
            with archive.open(names[0]) as stream:
                return pd.read_csv(stream)
    return pd.read_csv(path)


def prepare_dataset(path: Path) -> tuple[pd.DataFrame, np.ndarray, dict[str, Any], pd.DataFrame]:
    df = read_dataset(path)
    needed = [*SOURCE_COLUMNS.values(), "Risk Category", "Timestamp"]
    missing = [column for column in needed if column not in df.columns]
    if missing:
        raise ValueError(f"Dataset is missing required columns: {missing}")
    work = pd.DataFrame({
        "timestamp": pd.to_datetime(df["Timestamp"], utc=True, errors="coerce"),
        "bpm": pd.to_numeric(df[SOURCE_COLUMNS["bpm"]], errors="coerce"),
        "spo2_pct": pd.to_numeric(df[SOURCE_COLUMNS["spo2_pct"]], errors="coerce"),
        "temp_c": pd.to_numeric(df[SOURCE_COLUMNS["temp_c"]], errors="coerce"),
        "label": df["Risk Category"].astype(str).str.strip().str.casefold().map({"low risk": 0, "high risk": 1}),
    })
    rows_read = len(work)
    work = work.dropna(subset=["timestamp", *FEATURES, "label"]).sort_values("timestamp", kind="stable").reset_index(drop=True)
    if work.empty or work["label"].nunique() != 2:
        raise ValueError("Need usable records from both Risk Category classes")
    X = work[FEATURES].astype(float)
    y = work["label"].astype(int).to_numpy()
    meta = {
        "rows_read": int(rows_read),
        "rows_used": int(len(work)),
        "rows_dropped": int(rows_read - len(work)),
        "class_counts": {"Low Risk (proxy)": int((y == 0).sum()), "High Risk (proxy)": int((y == 1).sum())},
        "time_start_utc": work["timestamp"].min().isoformat(),
        "time_end_utc": work["timestamp"].max().isoformat(),
        "unique_patient_ids": int(df["Patient ID"].nunique()) if "Patient ID" in df else None,
    }
    return X, y, meta, df


def metric_pack(y_true: np.ndarray, y_pred: np.ndarray, score: np.ndarray | None = None) -> dict[str, Any]:
    return {
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 6),
        "precision_high_proxy": round(float(precision_score(y_true, y_pred, zero_division=0)), 6),
        "recall_high_proxy": round(float(recall_score(y_true, y_pred, zero_division=0)), 6),
        "f1_high_proxy": round(float(f1_score(y_true, y_pred, zero_division=0)), 6),
        "confusion_matrix_labels_low_high": confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist(),
    }


def bpm_baseline(y: np.ndarray, bpm: np.ndarray) -> tuple[float, dict[str, Any]]:
    best_key: tuple[float, float, float] | None = None
    best_threshold = 90.0
    for threshold in np.arange(40.0, 140.01, 0.5):
        pred = (bpm >= threshold).astype(int)
        key = (float(balanced_accuracy_score(y, pred)), float(accuracy_score(y, pred)), -float(threshold))
        if best_key is None or key > best_key:
            best_key, best_threshold = key, float(threshold)
    pred = (bpm >= best_threshold).astype(int)
    return best_threshold, metric_pack(y, pred, bpm)


def candidate_models() -> dict[str, Any]:
    return {
        "logistic": make_pipeline(SimpleImputer(strategy="median"), RobustScaler(), LogisticRegression(class_weight="balanced", max_iter=300, random_state=42)),
        "hist_gradient_boosting": make_pipeline(SimpleImputer(strategy="median"), HistGradientBoostingClassifier(max_iter=120, max_leaf_nodes=15, l2_regularization=1.0, random_state=42)),
        "random_forest": make_pipeline(SimpleImputer(strategy="median"), RandomForestClassifier(n_estimators=100, max_depth=10, min_samples_leaf=5, class_weight="balanced_subsample", n_jobs=-1, random_state=42)),
        "extra_trees": make_pipeline(SimpleImputer(strategy="median"), ExtraTreesClassifier(n_estimators=100, max_depth=12, min_samples_leaf=5, class_weight="balanced", n_jobs=-1, random_state=42)),
    }


def dataset_rule_audit(df: pd.DataFrame) -> dict[str, Any]:
    """Reproduce the published full-feature target rules as an audit only."""
    needed = [
        "Heart Rate", "Respiratory Rate", "Body Temperature", "Oxygen Saturation",
        "Systolic Blood Pressure", "Diastolic Blood Pressure", "Weight (kg)", "Height (m)",
        "Risk Category",
    ]
    if any(column not in df.columns for column in needed):
        return {"available": False, "reason": "Source lacks one or more fields for the target-rule audit."}
    bmi = pd.to_numeric(df["Weight (kg)"], errors="coerce") / pd.to_numeric(df["Height (m)"], errors="coerce").pow(2)
    rule_high = (
        (df["Heart Rate"] > 90) | (df["Heart Rate"] < 60)
        | (df["Respiratory Rate"] > 20) | (df["Respiratory Rate"] < 12)
        | (df["Body Temperature"] > 37.5) | (df["Body Temperature"] < 36.0)
        | (df["Oxygen Saturation"] < 95)
        | (df["Systolic Blood Pressure"] > 140) | (df["Systolic Blood Pressure"] < 110)
        | (df["Diastolic Blood Pressure"] > 90) | (df["Diastolic Blood Pressure"] < 70)
        | (bmi > 30) | (bmi < 18.5)
    )
    label_high = df["Risk Category"].astype(str).str.strip().str.casefold().eq("high risk")
    return {
        "available": True,
        "rule": "High Risk if any published threshold is met: HR, respiratory rate, temperature, SpO2, BP, or BMI outside listed bounds.",
        "accuracy_against_dataset_label": round(float((rule_high == label_high).mean()), 6),
        "disagreements": int((rule_high != label_high).sum()),
        "warning": "This rule reproduces the dataset label definition; it is not a clinical validation.",
    }


def train_model(data_path: Path, model_path: Path, report_path: Path) -> dict[str, Any]:
    X, y, dataset_meta, original = prepare_dataset(data_path)
    n = len(y)
    train_end, val_end = int(n * 0.60), int(n * 0.80)
    if not (0 < train_end < val_end < n):
        raise ValueError("Dataset too small for chronological 60/20/20 split")
    X_train, y_train = X.iloc[:train_end], y[:train_end]
    X_val, y_val = X.iloc[train_end:val_end], y[train_end:val_end]
    X_test, y_test = X.iloc[val_end:], y[val_end:]

    candidates = candidate_models()
    validation_results: dict[str, Any] = {}
    for name, model in candidates.items():
        model.fit(X_train, y_train)
        pred = model.predict(X_val)
        validation_results[name] = metric_pack(y_val, pred)
    selected_name = max(validation_results, key=lambda name: validation_results[name]["balanced_accuracy"])
    selected = candidates[selected_name]
    test_pred = selected.predict(X_test)

    models_by_feature_set = {"|".join(FEATURES): selected}
    model_info = {
        "|".join(FEATURES): {
            "features": FEATURES,
            "selected_model": selected_name,
            "validation_balanced_accuracy": validation_results[selected_name]["balanced_accuracy"],
            "test": metric_pack(y_test, test_pred),
        }
    }
    partial_results: dict[str, Any] = {}
    for subset in combinations(FEATURES, 2):
        subset_features = list(subset)
        subset_models = candidate_models()
        subset_validation: dict[str, Any] = {}
        for name, model in subset_models.items():
            model.fit(X_train[subset_features], y_train)
            subset_validation[name] = metric_pack(y_val, model.predict(X_val[subset_features]))
        subset_selected_name = max(subset_validation, key=lambda name: subset_validation[name]["balanced_accuracy"])
        subset_selected = subset_models[subset_selected_name]
        key = "|".join(subset_features)
        models_by_feature_set[key] = subset_selected
        model_info[key] = {
            "features": subset_features,
            "selected_model": subset_selected_name,
            "validation_balanced_accuracy": subset_validation[subset_selected_name]["balanced_accuracy"],
            "test": partial_results.get(key, {}).get("test"),
        }
        partial_results[key] = {
            "features": subset_features,
            "selected_model": subset_selected_name,
            "selection_metric": "highest validation balanced accuracy; test data not used for selection",
            "candidates_validation": subset_validation,
            "validation": subset_validation[subset_selected_name],
            "test": metric_pack(y_test, subset_selected.predict(X_test[subset_features])),
        }
        model_info[key]["test"] = partial_results[key]["test"]
    threshold_bpm, bpm_val = bpm_baseline(y_val, X_val["bpm"].to_numpy())
    bpm_test_pred = (X_test["bpm"].to_numpy() >= threshold_bpm).astype(int)

    report = {
        "model_version": MODEL_VERSION,
        "dataset_file": data_path.name,
        "dataset_sha256": sha256_file(data_path),
        "features": FEATURES,
        "target": "Risk Category; a dataset proxy label, not clinical ground truth.",
        "split": {
            "method": "Chronological holdout after sorting by Timestamp",
            "train_fraction": 0.60, "validation_fraction": 0.20, "test_fraction": 0.20,
            "train_rows": int(len(y_train)), "validation_rows": int(len(y_val)), "test_rows": int(len(y_test)),
            "saved_deployment_models_refit_on": "train + validation (first 80%) after final test metrics are computed",
            "patient_independent_split": bool(dataset_meta.get("unique_patient_ids") == len(y)),
        },
        "dataset": dataset_meta,
        "model_selection": {
            "selected_model": selected_name,
            "selection_metric": "highest validation balanced accuracy; test data not used for selection",
            "candidates_validation": validation_results,
            "validation": validation_results[selected_name],
            "test": metric_pack(y_test, test_pred),
        },
        "two_sensor_models_for_one_missing_field": partial_results,
        "bpm_only_baseline": {
            "threshold_selected_on_validation": threshold_bpm,
            "validation": bpm_val,
            "test": metric_pack(y_test, bpm_test_pred, X_test["bpm"].to_numpy()),
        },
        "simple_baseline_comparison": {
            "description": "One BPM threshold selected on validation balanced accuracy; used only as a transparent comparator.",
            "validation_balanced_accuracy_difference_rf_minus_bpm_baseline": round(
                validation_results[selected_name]["balanced_accuracy"] - bpm_val["balanced_accuracy"], 6
            ),
            "test_accuracy_difference_rf_minus_bpm_baseline": round(
                metric_pack(y_test, test_pred)["accuracy"] - metric_pack(y_test, bpm_test_pred)["accuracy"], 6
            ),
            "interpretation": "A near-zero difference means the three-feature classifier did not demonstrate a meaningful improvement over this simple baseline on this dataset split.",
        },
        "dataset_target_rule_audit": dataset_rule_audit(original),
        "limitations": [
            "Metrics show agreement with the source Risk Category column only, not patient-condition accuracy.",
            "The source contains one row per unique patient ID, so it does not validate repeated-reading trend or noise behavior.",
            "An independent, representative, clinically labelled dataset is required to evaluate real-world performance.",
            "The online rolling filter is an engineering noise screen; without waveform/contact ground truth it cannot prove whether a sustained change is a patient change or a sensor fault.",
        ],
    }
    # Preserve the held-out test results above, then refit each selected
    # deployment estimator on the full development portion (train + validation).
    selected.fit(X.iloc[:val_end], y[:val_end])
    for subset in combinations(FEATURES, 2):
        subset_features = list(subset)
        models_by_feature_set["|".join(subset_features)].fit(X.iloc[:val_end][subset_features], y[:val_end])
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "pipeline": selected,
        "models_by_feature_set": models_by_feature_set,
        "model_info": model_info,
        "feature_order": FEATURES,
        "model_version": MODEL_VERSION,
        "selected_model": selected_name,
        "dataset_sha256": report["dataset_sha256"],
    }, model_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is required as timezone-aware ISO-8601, for example 2026-10-11T12:00:00Z")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601 and include a timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset; use Z for UTC")
    return parsed.astimezone(timezone.utc)


def finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def choose(data: dict[str, Any], key: str, aliases: tuple[str, ...]) -> Any:
    if key in data:
        return data[key]
    return next((data[name] for name in aliases if name in data), None)


class InferenceEngine:
    def __init__(self, artifact: dict[str, Any], stale_after_sec: float = 15.0, rapid_rise_bpm: float = 20.0):
        self.models = artifact.get("models_by_feature_set", {"|".join(FEATURES): artifact["pipeline"]})
        self.model_info = artifact.get("model_info", {})
        self.model_version = str(artifact["model_version"])
        self.selected_model = str(artifact["selected_model"])
        self.stale_after_sec = stale_after_sec
        self.rapid_rise_bpm = rapid_rise_bpm
        self.history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=60))
        self.last_complete: dict[str, dict[str, Any]] = {}
        self.pending_spike: dict[str, dict[str, Any]] = {}
        self.confirmed_shift: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path, stale_after_sec: float = 15.0, rapid_rise_bpm: float = 20.0) -> "InferenceEngine":
        return cls(joblib.load(path), stale_after_sec, rapid_rise_bpm)

    def _trend(self, device_id: str, now_mono: float) -> tuple[str, float | None, list[dict[str, Any]]]:
        recent = [item for item in self.history[device_id] if now_mono - item["mono"] <= 60]
        if len(recent) < 2:
            return "Unknown", None, recent
        delta = float(recent[-1]["values"]["bpm"] - recent[0]["values"]["bpm"])
        trend = "Rising" if delta >= 5 else "Falling" if delta <= -5 else "Steady"
        return trend, round(delta, 2), recent

    def _outlier_features(self, device_id: str, values: dict[str, float]) -> list[str]:
        previous = list(self.history[device_id])[-30:]
        if len(previous) < 5:
            return []
        found: list[str] = []
        for feature in values:
            history = np.asarray([record["values"][feature] for record in previous], dtype=float)
            median = float(np.median(history))
            mad_sigma = 1.4826 * float(np.median(np.abs(history - median)))
            threshold = max(SPIKE_FLOORS[feature], 4.5 * mad_sigma)
            if abs(values[feature] - median) > threshold:
                found.append(feature)
        return found

    def _spike_repeats(self, prior: dict[str, Any], values: dict[str, float], outliers: list[str], now_mono: float) -> bool:
        if now_mono - prior["mono"] > 10:
            return False
        shared = set(prior["features"]) & set(outliers)
        return bool(shared) and all(abs(values[name] - prior["values"][name]) <= SPIKE_CONFIRM_TOLERANCE[name] for name in shared)

    def _response(self, *, device_id: str, timestamp: str, received: str, mode: str, sensor_status: str,
                  review_state: str, reason: str, action: str, raw_input: dict[str, Any],
                  trend: str = "Unknown", delta: float | None = None, risk_estimate: dict[str, Any] | None = None,
                  model_input: dict[str, float] | None = None, missing_fields: list[str] | None = None,
                  noise_guard: str = "not_run", last_valid: str | None = None,
                  selected_model: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": "1.1", "device_id": device_id, "timestamp": timestamp, "processed_at": received,
            "mode": mode, "sensor_status": sensor_status, "review_state": review_state,
            "trend": trend, "bpm_delta": delta,
            "last_valid_timestamp": last_valid,
            "noise_guard": {"status": noise_guard, "method": "rolling median/MAD on prior accepted readings; screening only"},
            "risk_estimate": risk_estimate or {
                "available": False, "status": "unavailable", "label": None, "high_risk_score": None,
                "reason": "No model estimate was produced for this reading.",
            },
            "reason": reason, "recommended_action": action,
            "model_version": self.model_version, "selected_model": selected_model or self.selected_model,
            "model_note": LABEL_NOTE,
            "input": {"raw": raw_input, "used_for_model": model_input, "missing_fields": missing_fields or []},
        }

    def _no_reading(self, *, device_id: str, timestamp: str, received: str, mode: str, sensor_status: str,
                    reason: str, raw_input: dict[str, Any], now_mono: float) -> dict[str, Any]:
        trend, delta, recent = self._trend(device_id, now_mono)
        last = self.last_complete.get(device_id)
        if sensor_status == "No signal" and len(recent) >= 2 and delta is not None and delta >= self.rapid_rise_bpm:
            state = "Critical trend before signal loss (demo)"
            action = "Keep signal loss and the prior rising trend visible; check/reconnect the sensor and request human review."
        elif sensor_status == "No signal":
            state = "Sensor/data unavailable"
            action = "Check sensor contact, power, and communication. Missing data is not a patient emergency."
        else:
            state = "Needs sensor review"
            action = "Check the sensor connection and provide a fresh valid reading."
        return self._response(
            device_id=device_id, timestamp=timestamp, received=received, mode=mode, sensor_status=sensor_status,
            review_state=state, reason=reason, action=action, raw_input=raw_input, trend=trend, delta=delta,
            last_valid=last["timestamp"] if last else None,
        )

    def predict(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        device_id = str(payload.get("device_id", "")).strip()
        if not device_id:
            raise ValueError("device_id is required")
        mode = str(payload.get("mode", "physical")).strip().lower()
        if mode not in {"physical", "simulated"}:
            raise ValueError("mode must be physical or simulated")
        event_time = parse_timestamp(payload.get("timestamp"))
        received_time = utc_now()
        timestamp, received = iso_utc(event_time), iso_utc(received_time)
        now_mono = time.monotonic()
        aliases = {
            "bpm": ("heart_rate_bpm", "heart_rate"),
            "spo2_pct": ("spo2_percent", "spo2"),
            "temp_c": ("temperature_c", "temperature"),
        }
        raw = {feature: choose(payload, feature, aliases[feature]) for feature in FEATURES}
        if payload.get("signal_status") == "no_signal" or payload.get("sample_ok") is False:
            with self.lock:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="No signal", reason="The acquisition layer reported no valid sample.", raw_input=raw, now_mono=now_mono)
        if (received_time - event_time).total_seconds() > self.stale_after_sec:
            with self.lock:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Stale", reason=f"Reading is older than the {self.stale_after_sec:g}-second freshness window.", raw_input=raw, now_mono=now_mono)
        if (event_time - received_time).total_seconds() > 60:
            raise ValueError("timestamp is more than 60 seconds in the future")
        if payload.get("contact_ok") is False or payload.get("signal_status") == "poor_signal":
            with self.lock:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Poor signal", reason="Acquisition flagged poor contact or signal quality.", raw_input=raw, now_mono=now_mono)
        if payload.get("signal_quality") is not None:
            try:
                quality = finite_number(payload["signal_quality"], "signal_quality")
            except ValueError:
                quality = -1
            if not 0 <= quality <= 1 or quality < 0.25:
                with self.lock:
                    return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                        sensor_status="Poor signal", reason="Signal-quality score is invalid or below the demo minimum 0.25.", raw_input=raw, now_mono=now_mono)

        values: dict[str, float] = {}
        missing = []
        try:
            for feature, value in raw.items():
                if value is None:
                    missing.append(feature)
                    continue
                numeric = finite_number(value, feature)
                low, high = RANGE_CHECKS[feature]
                if not low <= numeric <= high:
                    raise ValueError(f"{feature} is outside broad engineering range {low:g}..{high:g}")
                values[feature] = numeric
        except ValueError as exc:
            with self.lock:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Poor signal", reason=f"Value could not be used: {exc}.", raw_input=raw, now_mono=now_mono)
        if len(missing) > 1:
            with self.lock:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Poor signal", reason="Two or more sensor readings are missing; no estimate was produced.", raw_input=raw, now_mono=now_mono)

        feature_order = [feature for feature in FEATURES if feature in values]
        model_key = "|".join(feature_order)
        model = self.models.get(model_key) if len(feature_order) >= 2 else None
        model_metadata = self.model_info.get(model_key, {})
        minimum_partial_validation = 0.60

        with self.lock:
            history = self.history[device_id]
            if history and (event_time - parse_timestamp(history[-1]["timestamp"])).total_seconds() < -5:
                return self._no_reading(device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Stale", reason="An out-of-order reading arrived.", raw_input=raw, now_mono=now_mono)

            noise_features = self._outlier_features(device_id, values) if len(history) >= 5 else []
            noise_status = "warming_up" if len(history) < 5 else ("clean" if not missing else "partial_inputs_not_added_to_baseline")
            confirmed_change = False
            if noise_features:
                prior = self.pending_spike.get(device_id)
                if prior and self._spike_repeats(prior, values, noise_features, now_mono):
                    confirmed_change = True
                    self.pending_spike.pop(device_id, None)
                    self.confirmed_shift[device_id] = {"mono": now_mono, "values": values.copy(), "features": noise_features}
                    noise_status = "repeated_change_review"
                elif device_id in self.confirmed_shift and self._spike_repeats(self.confirmed_shift[device_id], values, noise_features, now_mono):
                    # Once a shift repeats, accept similar follow-up readings so
                    # the rolling baseline can adapt instead of rejecting a real
                    # sustained change forever.
                    confirmed_change = True
                    self.confirmed_shift[device_id] = {"mono": now_mono, "values": values.copy(), "features": noise_features}
                    noise_status = "persistent_change_accepted"
                else:
                    self.pending_spike[device_id] = {"mono": now_mono, "values": values, "features": noise_features}
                    last = self.last_complete.get(device_id)
                    trend, delta, _ = self._trend(device_id, now_mono)
                    return self._response(
                        device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                        sensor_status="Possible noise", review_state="Awaiting confirmation",
                        reason="A reading differed sharply from this device's recent robust baseline. It is withheld for one sample while the next reading confirms or clears it.",
                        action="Check sensor contact and wait for a confirming reading. This screen cannot tell a sensor fault from a real change.",
                        raw_input=raw, trend=trend, delta=delta, noise_guard="single_spike_withheld",
                        last_valid=last["timestamp"] if last else None,
                    )
            elif device_id in self.pending_spike:
                self.pending_spike.pop(device_id, None)
                noise_status = "isolated_spike_cleared"
            if not noise_features:
                self.confirmed_shift.pop(device_id, None)

            # Only complete accepted samples train the rolling noise baseline.
            if not missing:
                self.history[device_id].append({"timestamp": timestamp, "mono": now_mono, "values": values.copy()})
                self.last_complete[device_id] = {"timestamp": timestamp, "mono": now_mono, "values": values.copy()}

            trend, delta, _recent = self._trend(device_id, now_mono)
            last_complete = self.last_complete.get(device_id)
            if model is None or (missing and float(model_metadata.get("validation_balanced_accuracy", 0.0)) < minimum_partial_validation):
                if not missing:
                    # Should only occur for an artifact without the expected trained model.
                    unavailable_reason = "The trained model for this sensor combination is missing."
                elif not model:
                    unavailable_reason = "At least two readings are needed to estimate the dataset label."
                else:
                    pair_score = float(model_metadata.get("validation_balanced_accuracy", 0.0))
                    unavailable_reason = (
                        f"The available pair ({', '.join(feature_order)}) was near chance on validation "
                        f"(balanced accuracy {pair_score:.1%}); the estimate is withheld."
                    )
                unavailable = {
                    "available": False, "status": "unavailable", "label": None, "high_risk_score": None,
                    "features_used": feature_order,
                    "reason": unavailable_reason,
                }
                return self._response(
                    device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                    sensor_status="Partial data" if missing else "Needs model",
                    review_state="Needs sensor review", reason=unavailable_reason,
                    action="Restore the missing sensor reading and check the connection. No missing value was guessed.",
                    raw_input=raw, trend=trend, delta=delta, noise_guard=noise_status,
                    risk_estimate=unavailable,
                    last_valid=last_complete["timestamp"] if last_complete else None,
                    model_input=values if model else None, missing_fields=missing,
                )

            score = float(model.predict_proba(pd.DataFrame([{key: values[key] for key in feature_order}], columns=feature_order))[0, 1])
            label = "High Risk (dataset proxy)" if score >= 0.5 else "Low Risk (dataset proxy)"
            risk_estimate = {
                "available": True,
                "status": "partial_inputs" if missing else "complete",
                "label": label,
                "high_risk_score": round(score, 6),
                "features_used": feature_order,
                "score_note": "Score for the source dataset's label; not a calibrated patient-risk probability.",
                "reason": "A model trained on exactly these observed sensor fields was used; no value was imputed." if missing else LABEL_NOTE,
            }
            if score >= 0.5:
                state = "Attention (dataset proxy)"
                action = "Check the sensor readings and request human review; this dataset label is not a diagnosis."
            else:
                state = "Normal / stable (dataset proxy only)"
                action = "Continue prototype monitoring; this dataset label does not establish that a person is healthy."
            if missing:
                reason = f"One field is missing; a model trained for {', '.join(feature_order)} produced a provisional dataset-label estimate."
            else:
                reason = "The model estimates the Kaggle Risk Category label using BPM, SpO2 and temperature; this is not clinical validation."
            if confirmed_change:
                state = "Attention: repeated change"
                action = "The unusual reading repeated. Check signal quality and ask a qualified person to review the change."
                reason += " The unusual value repeated; this confirms a persistent change, not whether it is patient or sensor related."
            if noise_status == "isolated_spike_cleared":
                reason += " A one-sample spike returned toward baseline and was excluded from the adaptive baseline."

            # These states are scripted labels for synthetic dashboard demonstration only.
            scenario = str(payload.get("scenario", "")) if mode == "simulated" else ""
            demo_states = {
                "normal_demo": ("Normal / stable (scripted demo)", "Continue the scripted dashboard demonstration; this is not a clinical assessment."),
                "attention_demo": ("Attention (scripted demo)", "Scripted demonstration only: show the review state; do not treat it as medical advice."),
                "critical_demo": ("Critical (scripted demo)", "Scripted demonstration only: show the high-priority event to the test dashboard."),
                "emergency_demo": ("Emergency (scripted demo)", "Scripted synthetic demo state only; no emergency service is contacted."),
            }
            if scenario in demo_states:
                state, action = demo_states[scenario]
                reason += " State was selected by an explicitly labelled synthetic demo scenario."

            return self._response(
                device_id=device_id, timestamp=timestamp, received=received, mode=mode,
                sensor_status="Partial data" if missing else "Online", review_state=state,
                reason=reason, action=action, raw_input=raw, trend=trend, delta=delta,
                risk_estimate=risk_estimate, model_input=values, missing_fields=missing,
                noise_guard=noise_status,
                last_valid=timestamp if not missing else (last_complete["timestamp"] if last_complete else None),
                selected_model=str(model_metadata.get("selected_model") or self.selected_model),
            )


class ApiHandler(BaseHTTPRequestHandler):
    engine: InferenceEngine | None = None

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"error": "not_found"})
            return
        engine = self.engine
        self._send(200, {"status": "ok", "model_version": engine.model_version if engine else None,
                         "selected_model": engine.selected_model if engine else None})

    def do_POST(self) -> None:
        if self.path != "/predict":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("request must contain 1..32768 bytes")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send(200, self.engine.predict(body))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_json", "message": "Send one UTF-8 JSON object."})
        except ValueError as exc:
            self._send(400, {"error": "invalid_input", "message": str(exc)})
        except Exception as exc:
            self._send(500, {"error": "inference_error", "message": type(exc).__name__})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def serve(model_path: Path, host: str, port: int, stale_after: float, rapid_rise_bpm: float) -> None:
    ApiHandler.engine = InferenceEngine.from_file(model_path, stale_after, rapid_rise_bpm)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.daemon_threads = True
    print(f"Vital demo API ready at http://{host}:{port}; model={ApiHandler.engine.model_version} ({ApiHandler.engine.selected_model})")
    print("GET /health, POST /predict. Demo only; keep on a trusted local network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


def demo_payload(device_id: str, now: datetime, bpm: float | None = 80, spo2: float = 98.0, temp: float = 36.8, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"device_id": device_id, "timestamp": iso_utc(now), "mode": "simulated"}
    if bpm is not None:
        payload.update({"bpm": bpm, "spo2_pct": spo2, "temp_c": temp, "signal_quality": 0.9, "contact_ok": True})
    else:
        payload.update({"spo2_pct": spo2, "temp_c": temp, "signal_quality": 0.9, "contact_ok": True})
    payload = {key: value for key, value in payload.items() if value is not None}
    payload.update(extra)
    return payload


def run_demo(model_path: Path, report_path: Path) -> dict[str, Any]:
    engine = InferenceEngine.from_file(model_path)
    now = utc_now()
    cases = []

    partial_payload = demo_payload("demo-partial", now)
    partial_payload.pop("spo2_pct")
    partial = engine.predict(partial_payload)
    cases.append({"case": "one missing SpO2 field", "expected": "provisional estimate from BPM + temperature model", "observed": partial["risk_estimate"]["status"], "sensor_status": partial["sensor_status"]})
    bpm_missing_payload = demo_payload("demo-no-bpm", now)
    bpm_missing_payload.pop("bpm")
    bpm_missing = engine.predict(bpm_missing_payload)
    cases.append({"case": "BPM missing", "expected": "estimate withheld when the remaining pair is near chance", "observed": bpm_missing["risk_estimate"]["status"], "reason": bpm_missing["risk_estimate"]["reason"]})
    no_signal = engine.predict({"device_id": "demo-no-signal", "timestamp": iso_utc(), "mode": "simulated", "signal_status": "no_signal", "sample_ok": False})
    cases.append({"case": "no signal", "expected": "No signal", "observed": no_signal["sensor_status"], "score": no_signal["risk_estimate"]["high_risk_score"]})
    poor = engine.predict(demo_payload("demo-poor", now, contact_ok=False))
    cases.append({"case": "poor contact", "expected": "Poor signal", "observed": poor["sensor_status"]})

    spike_device = "demo-noise"
    for bpm in [80, 81, 80, 79, 80]:
        engine.predict(demo_payload(spike_device, utc_now(), bpm=bpm))
    spike = engine.predict(demo_payload(spike_device, utc_now(), bpm=160))
    cases.append({"case": "one isolated BPM spike", "expected": "withheld as possible noise", "observed": spike["noise_guard"]["status"], "sensor_status": spike["sensor_status"], "score": spike["risk_estimate"]["high_risk_score"]})
    return_to_baseline = engine.predict(demo_payload(spike_device, utc_now(), bpm=80))
    cases.append({"case": "return to prior reading", "expected": "spike cleared", "observed": return_to_baseline["noise_guard"]["status"], "sensor_status": return_to_baseline["sensor_status"]})
    repeated = engine.predict(demo_payload(spike_device, utc_now(), bpm=160))
    confirmed = engine.predict(demo_payload(spike_device, utc_now(), bpm=161))
    cases.append({"case": "repeated unusual shift", "expected": "review persistent change", "observed": confirmed["noise_guard"]["status"], "review_state": confirmed["review_state"]})
    persistent = engine.predict(demo_payload(spike_device, utc_now(), bpm=160))
    cases.append({"case": "follow-up after confirmed shift", "expected": "accept and adapt to the repeated change", "observed": persistent["noise_guard"]["status"]})
    stale = engine.predict(demo_payload("demo-stale", utc_now().replace(year=utc_now().year - 1)))
    cases.append({"case": "stale reading", "expected": "Stale", "observed": stale["sensor_status"]})
    rapid_device = "demo-rapid-loss"
    for bpm in [70, 78, 89, 103]:
        engine.predict(demo_payload(rapid_device, utc_now(), bpm=bpm))
    rapid_loss = engine.predict({"device_id": rapid_device, "timestamp": iso_utc(), "mode": "simulated", "signal_status": "no_signal", "sample_ok": False})
    cases.append({"case": "no signal after rapid rise", "expected": "retain critical trend before signal loss", "observed": rapid_loss["review_state"], "sensor_status": rapid_loss["sensor_status"]})
    stable_device = "demo-stable-loss"
    for bpm in [79, 80, 81, 80]:
        engine.predict(demo_payload(stable_device, utc_now(), bpm=bpm))
    stable_loss = engine.predict({"device_id": stable_device, "timestamp": iso_utc(), "mode": "simulated", "signal_status": "no_signal", "sample_ok": False})
    cases.append({"case": "no signal after stable readings", "expected": "sensor unavailable without critical trend", "observed": stable_loss["review_state"], "sensor_status": stable_loss["sensor_status"]})
    scripted_checks = {}
    for scenario, expected in {
        "normal_demo": "Normal / stable (scripted demo)",
        "attention_demo": "Attention (scripted demo)",
        "critical_demo": "Critical (scripted demo)",
        "emergency_demo": "Emergency (scripted demo)",
    }.items():
        result = engine.predict(demo_payload(f"demo-{scenario}", utc_now(), scenario=scenario))
        scripted_checks[scenario] = result["review_state"] == expected
        cases.append({"case": scenario, "expected": expected, "observed": result["review_state"]})
    checks = [
        partial["risk_estimate"]["status"] == "partial_inputs" and partial["risk_estimate"]["available"],
        not bpm_missing["risk_estimate"]["available"] and bpm_missing["risk_estimate"]["status"] == "unavailable",
        no_signal["sensor_status"] == "No signal" and no_signal["risk_estimate"]["high_risk_score"] is None,
        poor["sensor_status"] == "Poor signal",
        spike["sensor_status"] == "Possible noise" and spike["risk_estimate"]["high_risk_score"] is None,
        return_to_baseline["noise_guard"]["status"] == "isolated_spike_cleared",
        confirmed["noise_guard"]["status"] == "repeated_change_review",
        persistent["noise_guard"]["status"] == "persistent_change_accepted",
        stale["sensor_status"] == "Stale",
        rapid_loss["sensor_status"] == "No signal" and "Critical trend" in rapid_loss["review_state"],
        stable_loss["sensor_status"] == "No signal" and "Critical trend" not in stable_loss["review_state"],
        *scripted_checks.values(),
    ]
    report = {"kind": "synthetic_branch_checks_not_accuracy_or_clinical_validation", "passed": int(sum(checks)), "total": len(checks), "all_expected": all(checks), "cases": cases}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["all_expected"]:
        raise SystemExit("A scripted branch check failed")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and run the three-sensor Node-RED demo")
    commands = parser.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train", help="train/validate/test using the supplied dataset")
    train.add_argument("--data", type=Path, default=DEFAULT_DATA)
    train.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    train.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    api = commands.add_parser("serve", help="start the JSON API")
    api.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--stale-after", type=float, default=15.0)
    api.add_argument("--rapid-rise-bpm", type=float, default=20.0, help="demo trend threshold before signal loss")
    demo = commands.add_parser("demo", help="run synthetic missing/noise/signal checks without Node-RED")
    demo.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    demo.add_argument("--report", type=Path, default=APP_DIR / "reports" / "scenario_checks.json")
    args = parser.parse_args()
    if args.command == "train":
        result = train_model(args.data, args.model, args.report)
        print(json.dumps({"model_saved": str(args.model), "report_saved": str(args.report),
                          "selected_model": result["model_selection"]["selected_model"],
                          "validation": result["model_selection"]["validation"],
                          "test": result["model_selection"]["test"],
                          "target_rule_audit": result["dataset_target_rule_audit"]}, indent=2))
    elif args.command == "serve":
        serve(args.model, args.host, args.port, args.stale_after, args.rapid_rise_bpm)
    elif args.command == "demo":
        run_demo(args.model, args.report)


if __name__ == "__main__":
    main()
