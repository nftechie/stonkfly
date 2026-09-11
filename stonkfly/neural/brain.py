"""Full-graph candidate memory dynamics with explicit stimulation and checkpoints."""

import ctypes as C
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .circuit import identify
from .common import DATA, GRAPH, OUT, digest, save_json
from .state import NativeBrain

SOURCE = Path(__file__).with_name("kernel.cpp")
LIBRARY = (OUT / "physiology-v6") / {
    "darwin": "libmemory.dylib",
    "win32": "memory.dll",
}.get(sys.platform, "libmemory.so")
MODEL = "stonkfly-dual-compartment-v1"
from .rule import PARAMETERS as RULE_PARAMETERS

PARAMETERS = {
    **RULE_PARAMETERS,
    "neural_dt_ms": 0.1,
    "modulator_delivery_trace_ms": 100.0,
    "kc_rest_mV": -60.0,
    "kc_adaptation_jump_mV": 8.0,
    "kc_adaptation_tau_ms": 200.0,
    "interpretation": "Candidate KC adaptation/rest plus a baseline-centered anti-Hebbian rate-rule extension to two compartments. No fitted DAN/MBON background current; lamina bias is a display proxy. Gain, trace constants and transfer to this graph remain unvalidated assumptions.",
}


def build():
    sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    metadata = LIBRARY.with_suffix(LIBRARY.suffix + ".json")
    if LIBRARY.exists() and metadata.exists():
        record = json.loads(metadata.read_text())
        if (
            record["source_sha256"] == sha
            and record["binary_sha256"]
            == hashlib.sha256(LIBRARY.read_bytes()).hexdigest()
        ):
            return record
    LIBRARY.parent.mkdir(parents=True, exist_ok=True)
    temp = LIBRARY.with_suffix(LIBRARY.suffix + ".partial")
    compiler = next(
        (
            candidate
            for candidate in (["clang++", "g++", "c++", "cl"] if os.name == "nt" else ["clang++", "c++", "g++"])
            if shutil.which(candidate)
        ),
        None,
    )
    if compiler is None:
        raise RuntimeError(
            "No C++17 compiler found. Install LLVM/Clang or MinGW on Windows "
            "and ensure clang++ or g++ is on PATH."
        )
    if Path(compiler).name.lower() == "cl.exe":
        command = [
            compiler,
            "/O2",
            "/std:c++17",
            "/LD",
            str(SOURCE),
            f"/Fe:{temp}",
        ]
    else:
        command = [
            compiler,
            "-O3",
            "-std=c++17",
            "-shared",
            *([] if os.name == "nt" else ["-fPIC"]),
            str(SOURCE),
            "-o",
            str(temp),
        ]
    subprocess.run(command, check=True)
    temp.replace(LIBRARY)
    record = {
        "model": MODEL,
        "source_sha256": sha,
        "binary_sha256": hashlib.sha256(LIBRARY.read_bytes()).hexdigest(),
        "compiler": compiler,
        "command": command,
    }
    save_json(metadata, record)
    return record


class MemoryBrain(NativeBrain):
    def __init__(
        self,
        path=GRAPH,
        *,
        eta=0.001,
        circuit=None,
        modulation_mask=None,
        tonic=None,
        dan_baseline_hz=None,
        kc_rest=-60.0,
        adaptation_jump=8.0,
        adaptation_tau=200.0,
    ):
        super().__init__(path)
        self.build = build()
        self.library = C.CDLL(str(LIBRARY))
        self.advance = self.library.memory_advance
        self.advance.argtypes = (
            [C.c_int]
            + [C.c_void_p] * 11
            + [C.c_int, C.c_float]
            + [C.c_void_p] * 5
            + [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_int]
            + [C.c_void_p] * 4
            + [
                C.c_float,
                C.c_float,
                C.c_float,
                C.c_int,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_float,
                C.c_float,
            ]
        )
        self.advance.restype = None
        self.circuit = identify(self) if circuit is None else circuit
        if not math.isfinite(kc_rest) or not -80 <= kc_rest <= -45:
            raise ValueError("Invalid KC resting potential")
        self.rest = np.full(self.n, -52.0, dtype=np.float32)
        self.rest[self.circuit["kc"]] = kc_rest
        self.v[:] = self.rest
        if (
            not math.isfinite(adaptation_jump)
            or adaptation_jump < 0
            or not math.isfinite(adaptation_tau)
            or adaptation_tau <= 20
        ):
            raise ValueError("Invalid adaptation parameters")
        self.adaptation = np.zeros(self.n, dtype=np.float32)
        self.adaptation_jump = float(adaptation_jump)
        self.adaptation_tau = float(adaptation_tau)
        if modulation_mask is None:
            import pyarrow.feather as feather

            neurons = (
                feather.read_table(DATA / "normalized/neurons.feather")
                .to_pandas()
                .set_index("source_id")
                .loc[self.ids]
            )
            modulation_mask = neurons.neurotransmitter.isin(
                ["dopamine", "octopamine", "serotonin"]
            ).to_numpy(dtype=np.uint8)
        self.modulation_mask = np.asarray(modulation_mask, dtype=np.uint8).copy()
        if self.modulation_mask.shape != (self.n,) or np.any(self.modulation_mask > 1):
            raise ValueError("Invalid modulation mask")
        self.eta = float(eta)
        if not math.isfinite(self.eta) or self.eta < 0:
            raise ValueError("Finite nonnegative eta required")
        self.eligibility = np.zeros(self.n, dtype=np.float64)
        self.eligibility_last = np.zeros(self.n, dtype=np.int64)
        self.modulation = np.zeros(self.n, dtype=np.float32)
        self.modulation_last = np.zeros(self.n, dtype=np.int64)
        self.baseline_plastic = self.weight[self.circuit["edges"]].copy()
        self.initial_weight_sha256 = digest(self.weight)
        self.fields = [
            "v",
            "g",
            "refractory",
            "drive",
            "previous_drive",
            "queue",
            "queue_count",
            "counts",
            "luminance",
            "active",
            "active_flag",
            "nactive",
            "last",
            "eligibility",
            "eligibility_last",
            "modulation",
            "modulation_last",
            "adaptation",
        ]
        self.initial = {k: getattr(self, k).copy() for k in self.fields}
        from .rule import PARAMETERS as RULE_PARAMETERS

        self.rule_parameters = RULE_PARAMETERS.copy()
        self.rate_kc = np.zeros(len(self.circuit["edges"]), dtype=np.float64)
        self.rate_dan = np.zeros(len(self.circuit["dan"]), dtype=np.float64)
        self.memory_u = np.zeros_like(self.rate_kc)
        self.memory_w = np.zeros_like(self.rate_kc)
        self.tonic = (
            np.zeros(self.n, dtype=np.float32)
            if tonic is None
            else np.asarray(tonic, dtype=np.float32).copy()
        )
        self.dan_baseline_hz = (
            np.zeros(len(self.circuit["dan"]), dtype=np.float64)
            if dan_baseline_hz is None
            else np.asarray(dan_baseline_hz, dtype=np.float64).copy()
        )
        if self.tonic.shape != (self.n,) or not np.isfinite(self.tonic).all():
            raise ValueError("Invalid tonic current")
        if (
            self.dan_baseline_hz.shape != (len(self.rate_dan),)
            or not np.isfinite(self.dan_baseline_hz).all()
        ):
            raise ValueError("Invalid DAN baseline")
        self.weights_frozen = False
        for k in ["rate_kc", "rate_dan", "memory_u", "memory_w"]:
            self.fields.append(k)
            self.initial[k] = getattr(self, k).copy()

    def reset(self, keep_memory=False):
        if keep_memory:
            saved = (self.memory_u.copy(), self.memory_w.copy())
        for k, v in self.initial.items():
            getattr(self, k)[:] = v
        self.cursor = 0
        self.sim_ms = 0.0
        self.total_spikes = 0
        if not keep_memory:
            self.weight[self.circuit["edges"]] = self.baseline_plastic
        else:
            self.memory_u[:], self.memory_w[:] = saved

    def _neural_step(
        self,
        luminance,
        duration_ms,
        *,
        learning=False,
        stimulation=None,
        lamina_bias=12.0,
    ):
        light = np.asarray(luminance)
        if light.shape != (len(self.retina),) or not np.isfinite(light).all():
            raise ValueError("Invalid retinal input")
        steps = round(duration_ms / self.dt)
        if (
            not math.isfinite(duration_ms)
            or steps < 1
            or not math.isfinite(lamina_bias)
        ):
            raise ValueError("Invalid interval/current")
        self.luminance += (1 - math.exp(-steps * self.dt / 10)) * (
            np.clip(light, 0, 1) - self.luminance
        )
        self.drive.fill(0)
        self.drive[self.lamina] = lamina_bias
        self.drive[self.retina] = 30 * self.luminance / (0.02 + self.luminance)
        self.drive += self.tonic
        if stimulation is not None:
            pulses = stimulation if isinstance(stimulation, list) else [stimulation]
            for indices, current in pulses:
                ix = np.asarray(indices, dtype=np.int32)
                amplitude = np.asarray(current, dtype=np.float32)
                if (
                    ix.ndim != 1
                    or np.any(ix < 0)
                    or np.any(ix >= self.n)
                    or not np.isfinite(amplitude).all()
                    or amplitude.shape not in [(), ix.shape]
                ):
                    raise ValueError("Invalid external stimulation")
                self.drive[ix] += amplitude
        self.counts.fill(0)
        clock = np.asarray([self.cursor], dtype=np.int64)
        c = self.circuit
        arrays = [
            self.ptr,
            self.post,
            self.weight,
            self.v,
            self.g,
            self.refractory,
            self.drive,
            self.previous_drive,
            self.queue,
            self.queue_count,
            clock,
        ]
        start = time.perf_counter()
        self.advance(
            self.n,
            *[x.ctypes.data for x in arrays],
            steps,
            self.dt,
            *[
                getattr(self, k).ctypes.data
                for k in ["counts", "active", "active_flag", "nactive", "last"]
            ],
            c["kc_mask"].ctypes.data,
            c["dan_index"].ctypes.data,
            self.eligibility.ctypes.data,
            self.eligibility_last.ctypes.data,
            len(c["edges"]),
            c["edges"].ctypes.data,
            c["pre"].ctypes.data,
            self.baseline_plastic.ctypes.data,
            c["gain"].ctypes.data,
            self.eta,
            PARAMETERS["trace_kc_seconds"] * 1000,
            PARAMETERS["minimum_fraction"],
            int(learning),
            self.modulation.ctypes.data,
            self.modulation_last.ctypes.data,
            self.modulation_mask.ctypes.data,
            self.rest.ctypes.data,
            self.adaptation.ctypes.data,
            self.adaptation_jump,
            self.adaptation_tau,
        )
        elapsed = time.perf_counter() - start
        self.cursor = int(clock[0])
        self.sim_ms = self.cursor * self.dt
        self.total_spikes += int(self.counts.sum())
        return self.counts.copy(), elapsed

    def step(
        self,
        luminance,
        duration_ms,
        *,
        learning=False,
        stimulation=None,
        lamina_bias=12.0,
    ):
        from .rule import advance

        if not math.isfinite(duration_ms) or duration_ms <= 0:
            raise ValueError("Invalid duration")
        remaining = round(duration_ms / self.dt)
        if remaining < 1:
            raise ValueError("Duration too short")
        total = np.zeros(self.n, dtype=np.int32)
        wall = 0.0
        while remaining:
            ticks = min(100, remaining)
            interval = ticks * self.dt
            # The original LTD update is disabled. Only the centered rule below
            # writes candidate memory efficacies; all neural integration remains.
            c, t = self._neural_step(
                luminance,
                interval,
                learning=False,
                stimulation=stimulation,
                lamina_bias=lamina_bias,
            )
            seconds = interval / 1000
            advance(
                self.rate_kc,
                self.rate_dan,
                self.memory_u,
                self.memory_w,
                c[self.circuit["pre"]] / seconds,
                c[self.circuit["dan"]] / seconds - self.dan_baseline_hz,
                self.circuit["gain"],
                seconds,
                self.eta,
                learning,
                self.weights_frozen,
            )
            if not self.weights_frozen:
                self.weight[self.circuit["edges"]] = self.baseline_plastic * (
                    1 + self.memory_w
                )
            total += c
            wall += t
            remaining -= ticks
        self.counts[:] = total
        return total, wall

    def memory(self):
        w = self.weight[self.circuit["edges"]]
        fraction = w / self.baseline_plastic
        return {
            "plastic_edges": len(w),
            "changed_edges": int(np.count_nonzero(w != self.baseline_plastic)),
            "mean_efficacy": float(fraction.mean()),
            "minimum_efficacy": float(fraction.min()),
            "sha256": digest(w),
            "model": MODEL,
        }

    def checkpoint(self, path):
        metadata = {
            "model": MODEL,
            "build": self.build,
            "eta": self.eta,
            "parameters": PARAMETERS,
            "cursor": self.cursor,
            "weights_frozen": self.weights_frozen,
            "total_spikes": self.total_spikes,
            "graph_ids_sha256": digest(self.ids),
            "graph_ptr_sha256": digest(self.ptr),
            "graph_post_sha256": digest(self.post),
            "plastic_edges_sha256": digest(self.circuit["edges"]),
            "configuration_sha256": self.configuration_signature(),
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                metadata=json.dumps(metadata),
                weight=self.weight,
                **{k: getattr(self, k) for k in self.fields},
            )
        temporary.replace(path)

    def restore(self, path):
        with np.load(path, allow_pickle=False) as a:
            m = json.loads(str(a["metadata"]))
            expected = {
                "model": MODEL,
                "build": self.build,
                "eta": self.eta,
                "parameters": PARAMETERS,
                "graph_ids_sha256": digest(self.ids),
                "graph_ptr_sha256": digest(self.ptr),
                "graph_post_sha256": digest(self.post),
                "plastic_edges_sha256": digest(self.circuit["edges"]),
                "configuration_sha256": self.configuration_signature(),
            }
            if any(m.get(k) != v for k, v in expected.items()):
                raise ValueError("Checkpoint provenance mismatch")
            for k in ["weight", *self.fields]:
                if (
                    a[k].shape != getattr(self, k).shape
                    or a[k].dtype != getattr(self, k).dtype
                ):
                    raise ValueError("Checkpoint array mismatch")
                if a[k].dtype.kind == "f" and not np.isfinite(a[k]).all():
                    raise ValueError("Nonfinite checkpoint state")
            for k in ["weight", *self.fields]:
                getattr(self, k)[:] = a[k]
            self.cursor = int(m["cursor"])
            self.sim_ms = self.cursor * self.dt
            self.total_spikes = int(m["total_spikes"])
            self.weights_frozen = bool(m["weights_frozen"])

    def configuration_signature(self):
        # Equal cell IDs and CSR endpoints alone do not imply equal input
        # geometry, original efficacies or compartment assignment.
        return {
            "initial_weight": self.initial_weight_sha256,
            "modulation_mask": digest(self.modulation_mask),
            "rule": self.rule_parameters,
            "rule_sha256": hashlib.sha256(
                Path(__file__).with_name("rule.py").read_bytes()
            ).hexdigest(),
            "tonic": digest(self.tonic),
            "dan_baseline_hz": digest(self.dan_baseline_hz),
            "rest": digest(self.rest),
            "adaptation_jump": self.adaptation_jump,
            "adaptation_tau": self.adaptation_tau,
            **{
                k: digest(getattr(self, k)) for k in ["retina", "uv", "lamina", "sugar"]
            },
            **{
                k: digest(self.circuit[k])
                for k in ["pre", "gain", "kc_mask", "dan_index"]
            },
        }
