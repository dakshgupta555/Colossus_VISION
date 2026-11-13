"""Dataset generation, training, benchmark (rules vs AI), save/load."""
import json
import os
import time

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.metrics import confusion_matrix, f1_score

from . import config as C
from . import features as F
from . import pv_sim as S
from .rules import rule_diagnose

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(ROOT, "models", "model.joblib")
BENCH_PATH = os.path.join(ROOT, "models", "benchmark.json")


def make_dataset(n_per_class, seed, hard_fraction=0.0, only_hard=False):
    rng = np.random.default_rng(seed)
    X, y, feats = [], [], []
    for fault in C.CLASSES:
        for k in range(n_per_class):
            hard = only_hard or (rng.random() < hard_fraction)
            s = S.make_scenario(fault, rng, hard=hard)
            p = S.simulate(s, rng)
            f, *_ = F.extract(p)
            X.append(F.vector(f))
            y.append(fault)
            feats.append(f)
    return np.array(X), np.array(y), feats


def evaluate(clf, X, y, feats):
    pred_ai = clf.predict(X)
    pred_rule = np.array([rule_diagnose(f) for f in feats])
    out = {}
    for name, pred in [("rules", pred_rule), ("ai", pred_ai)]:
        out[name] = dict(
            macro_f1=round(float(f1_score(y, pred, average="macro")), 3),
            accuracy=round(float(np.mean(pred == y)), 3),
            confusion=confusion_matrix(y, pred, labels=C.CLASSES).tolist(),
        )
    return out


def train(n_per_class=1500, seed=11, verbose=True):
    t0 = time.time()
    # training data covers normal AND hard conditions (dim light, heat, mild faults)
    X, y, _ = make_dataset(n_per_class, seed, hard_fraction=0.3)
    if verbose:
        print(f"generated {len(y)} simulated training curves in {time.time() - t0:.0f}s")
    rf = RandomForestClassifier(n_estimators=300, min_samples_leaf=2, class_weight="balanced",
                                n_jobs=-1, random_state=0)
    clf = CalibratedClassifierCV(rf, method="sigmoid", cv=3)
    clf.fit(X, y)
    iso = IsolationForest(n_estimators=200, contamination="auto", random_state=0).fit(X)
    iso_thr = float(np.percentile(iso.score_samples(X), 0.5))
    imp_rf = RandomForestClassifier(n_estimators=200, min_samples_leaf=2, n_jobs=-1, random_state=0).fit(X, y)
    importances = dict(sorted(zip(F.FEATURE_NAMES, imp_rf.feature_importances_.round(4).tolist()),
                              key=lambda kv: -kv[1]))
    bundle = dict(clf=clf, iso=iso, iso_thr=iso_thr, features=F.FEATURE_NAMES, classes=C.CLASSES,
                  importances=importances, trained_on="simulated (pvlib twin)", n_train=int(len(y)))
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump(bundle, MODEL_PATH)
    if verbose:
        print(f"model trained and saved in {time.time() - t0:.0f}s")

    # benchmark on UNSEEN seeds
    bench = {}
    for name, kw in [("standard", dict(seed=101)), ("hard", dict(seed=202, only_hard=True))]:
        Xt, yt, ft = make_dataset(300, **kw)
        bench[name] = evaluate(clf, Xt, yt, ft)
        if verbose:
            print(f"[{name:8s}] rules macro-F1={bench[name]['rules']['macro_f1']:.3f}   "
                  f"AI macro-F1={bench[name]['ai']['macro_f1']:.3f}")
    bench["classes"] = C.CLASSES
    bench["note"] = ("Simulated test sets (unseen random seeds). 'hard' = dim light 15-35 %, "
                     "55-68 C panels, mild faults. Real-bench results are added after data collection.")
    with open(BENCH_PATH, "w") as fh:
        json.dump(bench, fh, indent=1)
    return bundle


def load_or_train():
    if os.path.exists(MODEL_PATH):
        try:
            return joblib.load(MODEL_PATH)
        except Exception as exc:  # e.g. scikit-learn version change
            print("could not load model, retraining:", exc)
    return train()
