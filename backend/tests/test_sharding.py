"""The CI shard split must run every test file exactly once across the shards."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import sharding

_FILES = sorted(f"tests/{p.name}" for p in Path(__file__).parent.glob("test_*.py"))


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_every_file_lands_in_exactly_one_shard(n):
    shards = sharding.partition(_FILES, n, sharding.load_weights())
    flat = [f for s in shards for f in s]
    assert sorted(flat) == _FILES and len(flat) == len(set(flat))


def test_a_file_with_no_weight_still_lands_somewhere():
    files = [*_FILES, "tests/test_brand_new_file.py"]
    shards = sharding.partition(files, 3, sharding.load_weights())
    assert sum(s.count("tests/test_brand_new_file.py") for s in shards) == 1


def test_the_split_is_balanced_by_measured_weight():
    w = sharding.load_weights()
    loads = [sum(w.get(f, 0) for f in s) for s in sharding.partition(_FILES, 3, w)]
    assert max(loads) <= 1.15 * (sum(loads) / 3), loads


def test_every_process_computes_the_same_split():
    """Each CI shard is a separate process and they never talk, so "exactly one shard" holds
    only if the partition is a pure function of the file list. Anything seeded per process
    (`hash()` under PYTHONHASHSEED, set iteration order) would let a file land in two shards
    or none, and every shard would still pass this file's in-process checks."""
    code = ("import json; from tests import sharding as s; "
            "print(json.dumps(s.partition(json.loads(input()), 3, s.load_weights())))")
    backend = Path(__file__).parent.parent
    files = [*_FILES, "tests/test_brand_new_file.py"]
    runs = {
        subprocess.run([sys.executable, "-c", code], input=json.dumps(files), cwd=backend,
                       env={**os.environ, "PYTHONHASHSEED": seed},
                       capture_output=True, text=True, check=True).stdout
        for seed in ("1", "2", "3")
    }
    assert len(runs) == 1, "the shard split depends on the process — shards would disagree"


@pytest.mark.parametrize("bad", ["0/3", "4/3", "x/3", "3", "1/0"])
def test_a_malformed_shard_is_an_error_not_an_empty_run(bad):
    with pytest.raises(ValueError):
        sharding.parse(bad)


def test_unset_means_everything():
    assert sharding.parse(None) is None and sharding.parse("  ") is None


def test_every_test_file_has_a_weight():
    """Not a correctness rule (unknown files get the median), a freshness nudge: a file
    with no weight means the weights are due a refresh from a timed run."""
    missing = [f for f in _FILES if f not in sharding.load_weights()]
    assert len(missing) <= 25, f"{len(missing)} files have no shard weight; refresh shard_weights.json"
