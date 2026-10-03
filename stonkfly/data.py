"""Fetch released MaleCNS inputs and verify both sources and prepared arrays."""

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

import numpy as np

from .neural.common import DATA, GRAPH, digest

PACKAGE = Path(__file__).with_name("neural")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify():
    lock = json.loads((PACKAGE / "sources.lock.json").read_text())
    if sha(DATA / "annotations.feather") != lock["annotations.feather"]["sha256"]:
        raise RuntimeError("Annotation checksum mismatch")
    expected = json.loads((PACKAGE / "arrays.lock.json").read_text())
    with np.load(GRAPH, allow_pickle=False) as a:
        if set(a.files) != set(expected):
            raise RuntimeError("Graph fields mismatch")
        for k, h in expected.items():
            if digest(a[k]) != h:
                raise RuntimeError("Graph array checksum mismatch: " + k)
        if len(a["ids"]) != 166700 or len(a["post"]) != 25582938:
            raise RuntimeError("Wrong retained graph")
    import pyarrow.feather as f

    # Match normalized transmitter identities to the checksum-locked released file.
    if not (DATA / "normalized/neurons.feather").exists():
        raise RuntimeError("Normalized neuron metadata missing")
    n = f.read_table(DATA / "normalized/neurons.feather").to_pandas()
    transmitter_values = json.dumps(
        n.neurotransmitter.fillna("").astype(str).tolist(), separators=(",", ":")
    ).encode()
    nt_expected = json.loads((PACKAGE / "neurons.lock.json").read_text())[
        "neurotransmitter_values_sha256"
    ]
    if hashlib.sha256(transmitter_values).hexdigest() != nt_expected:
        raise RuntimeError("Normalized transmitter values mismatch")
    with np.load(GRAPH, allow_pickle=False) as a:
        if not np.array_equal(n.source_id.to_numpy(), a["ids"]):
            raise RuntimeError("Normalized neuron order mismatch")
    return {
        "release": "MaleCNS v1.0",
        "neurons": 166700,
        "directed_edges": 25582938,
        "arrays_verified": True,
    }


def prepare(reuse=None):
    DATA.mkdir(parents=True, exist_ok=True)
    if reuse:
        root = Path(reuse)
        mapping = {
            root / "outputs/doom/malecns_v1/graph.npz": GRAPH,
            root / "connectome_data/malecns_v1/annotations.feather": DATA
            / "annotations.feather",
            root / "connectome_data/malecns_v1/normalized/neurons.feather": DATA
            / "normalized/neurons.feather",
        }
        for source, target in mapping.items():
            if not source.is_file():
                raise RuntimeError("Missing reuse source: " + str(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    else:
        lock = json.loads((PACKAGE / "sources.lock.json").read_text())
        for name, info in lock.items():
            path = DATA / name
            if not path.exists():
                print("Downloading", name, flush=True)
                tmp = path.with_suffix(".partial")
                urllib.request.urlretrieve(info["url"], tmp)
                if sha(tmp) != info["sha256"]:
                    raise RuntimeError("Downloaded checksum mismatch: " + name)
                tmp.replace(path)
            if sha(path) != info["sha256"]:
                raise RuntimeError("Source checksum mismatch: " + name)
        shutil.copyfile(PACKAGE / "sources.lock.json", DATA / "source.lock.json")
        from .neural.connectome import import_graph
        from .neural.prepare import prepare as compile_graph

        import_graph()
        compile_graph()
    print(json.dumps(verify()), flush=True)
