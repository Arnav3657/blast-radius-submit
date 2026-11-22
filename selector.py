import gzip
import json
import marshal
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path("/data") if Path("/data/tests.json").exists() else HERE / "sample"
MAP_PATH = HERE / "cov_map.json.gz"
BIN_PATH = HERE / "cov_map.bin"  # optional fast-loading copy (python selector.py --convert)

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# If True, patches that only change comments in .py files run nothing (cost 0).
# Default False: the task says comment-only changes are "not promised harmless".
# Flip to True only after confirming no sample comment-only commit has failing tests.
EMPTY_FOR_COMMENT_ONLY = False

# Safety margins (a miss costs 300 s, so a little extra runtime is cheap insurance).
# Tests this fast are always re-added after pruning.
SAFETY_CAP = 0.05
# When a non-prose change maps to nothing (config, conftest, unknown file), run all tests this fast.
UNMAPPED_CAP = 0.1

# Patches touching only these prose files can't affect tests.
PROSE_EXT = (".md", ".rst", ".txt")


def parse_patch(text: str) -> dict[str, dict]:
    files: dict[str, dict] = {}
    cur_file = None
    old_line = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            if len(parts) >= 4:
                cur_file = parts[3][2:]
                files.setdefault(cur_file, {"lines": set(), "comment_only": True, "attrs": set()})
            continue
        if cur_file is None or raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        m = HUNK_RE.match(raw)
        if m:
            old_line = int(m.group(1))
            continue
        if raw.startswith("-"):
            content = raw[1:].strip()
            if content and not content.startswith("#"):
                files[cur_file]["comment_only"] = False
            files[cur_file]["lines"].add(old_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am:
                files[cur_file]["attrs"].add(am.group(1))
            old_line += 1
        elif raw.startswith("+"):
            content = raw[1:].strip()
            if content and not content.startswith("#"):
                files[cur_file]["comment_only"] = False
            files[cur_file]["lines"].add(old_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am:
                files[cur_file]["attrs"].add(am.group(1))
        else:
            old_line += 1

    return files


def _lpt(items: list[tuple[int, int]], k: int):
    workers: list[list[tuple[int, int]]] = [[] for _ in range(k)]
    loads = [0] * k
    for it in items:
        i = loads.index(min(loads))
        workers[i].append(it)
        loads[i] += it[0]
    return workers, loads


def _improve(workers, loads) -> None:
    """Local search: move/swap items off the busiest worker while it strictly helps."""
    k = len(workers)
    for _ in range(2000):
        mi = max(range(k), key=lambda i: loads[i])
        mx = loads[mi]
        best = None  # (pair_max, kind, j, a_idx, b_idx)
        for j in range(k):
            if j == mi:
                continue
            lj = loads[j]
            for ai, a in enumerate(workers[mi]):
                # move a -> j
                pm = max(mx - a[0], lj + a[0])
                if pm < mx and (best is None or pm < best[0]):
                    best = (pm, "m", j, ai, -1)
                # swap a with a smaller b in j
                for bi, b in enumerate(workers[j]):
                    if b[0] < a[0]:
                        pm = max(mx - a[0] + b[0], lj + a[0] - b[0])
                        if pm < mx and (best is None or pm < best[0]):
                            best = (pm, "s", j, ai, bi)
        if best is None:
            return
        _, kind, j, ai, bi = best
        a = workers[mi][ai]
        if kind == "m":
            workers[mi].pop(ai)
            workers[j].append(a)
            loads[mi] -= a[0]
            loads[j] += a[0]
        else:
            b = workers[j][bi]
            workers[mi][ai] = b
            workers[j][bi] = a
            loads[mi] += b[0] - a[0]
            loads[j] += a[0] - b[0]


def optimal_pack(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []
    # whole milliseconds, same as the scorer
    items = sorted(((round(t["seconds"] * 1000), t["number"]) for t in tests), reverse=True)
    best_cost = None
    best_workers: list[list[int]] = []

    for k in range(1, min(8, len(items)) + 1):
        workers, loads = _lpt(items, k)
        _improve(workers, loads)
        cost = max(loads) + 2000 * sum(1 for w in workers if w)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = [[n for _, n in w] for w in workers if w]

    return best_workers


def prune_candidates(
    selected: set[int],
    strong: set[int],
    exact_hits: set[int],
    attr_slow: set[int],
    protected_tests: set[int],
    tests_by_num: dict[int, dict],
    attr_slow_cap: float = 0.5,
    non_protected_slow_cap: float = 2.0,
    large_set_threshold: int = 25,
    large_set_slow_cap: float = 0.2,
    huge_set_threshold: int = 150,
    huge_set_slow_cap: float = 0.01,
) -> set[int]:
    protected = strong | exact_hits
    kept = {
        n for n in selected
        if (
            (n in exact_hits)
            or (n in strong and not (n in attr_slow and tests_by_num[n]["seconds"] >= attr_slow_cap))
            or (tests_by_num[n]["seconds"] < 0.01)
        )
        and not (n not in protected_tests and tests_by_num[n]["seconds"] > non_protected_slow_cap)
    }

    if len(kept) >= large_set_threshold:
        kept = {
            n for n in kept
            if (
                (n in exact_hits)
                or (n not in protected_tests and tests_by_num[n]["seconds"] <= large_set_slow_cap)
                or (n in protected_tests and tests_by_num[n]["seconds"] <= non_protected_slow_cap)
            )
        }

    if len(kept) >= huge_set_threshold:
        kept = {
            n for n in kept
            if (n in exact_hits) or (tests_by_num[n]["seconds"] <= huge_set_slow_cap)
        }

    if len(kept) > 100:
        kept = {
            n for n in kept
            if (
                (n in exact_hits)
                or (n in strong and not (n in attr_slow and tests_by_num[n]["seconds"] >= attr_slow_cap))
                or (tests_by_num[n]["seconds"] < 0.002)
            )
            and not (n not in protected_tests and tests_by_num[n]["seconds"] > non_protected_slow_cap)
        }

    return kept if kept else (protected or selected)


def load_map() -> dict:
    if BIN_PATH.exists():
        try:
            with open(BIN_PATH, "rb") as f:
                return marshal.load(f)
        except Exception:
            pass  # different python version etc. -> fall back to the gz json
    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
        return json.load(f)


def convert() -> None:
    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
        cov = json.load(f)
    with open(BIN_PATH, "wb") as f:
        marshal.dump(cov, f)
    print("wrote", BIN_PATH, file=sys.stderr)


def is_prose_only(diff_info: dict, fixtures: dict, tests_by_file: dict) -> bool:
    if not diff_info:
        return False
    for path in diff_info:
        if path in fixtures or path in tests_by_file:
            return False
        if not path.lower().endswith(PROSE_EXT):
            return False
    return True


def main() -> None:
    tests_list = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    tests_by_num = {t["number"]: t for t in tests_list}
    tests_by_file: dict[str, list[int]] = {}
    for t in tests_list:
        tests_by_file.setdefault(t["file"], []).append(t["number"])

    fast_tests = sorted(tests_list, key=lambda t: t["seconds"])

    cov = load_map()
    lines_map = cov["lines"]
    file_tests = cov["file_tests"]
    fixtures = cov["fixtures"]
    funcs_map = cov.get("funcs", {})
    attrs_map = cov.get("attrs", {})
    attr_spans = cov.get("attr_spans", {})

    out = []
    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        cid = patch_path.stem
        diff_info = parse_patch(patch_path.read_text(encoding="utf-8", errors="ignore"))

        if is_prose_only(diff_info, fixtures, tests_by_file):
            out.append(json.dumps({"commit": cid, "workers": []}))
            continue

        if (
            EMPTY_FOR_COMMENT_ONLY
            and diff_info
            and all(i["comment_only"] and p.endswith(".py") for p, i in diff_info.items())
        ):
            out.append(json.dumps({"commit": cid, "workers": []}))
            continue

        selected: set[int] = set()
        strong: set[int] = set()
        exact_hits: set[int] = set()
        attr_slow: set[int] = set()
        protected_tests: set[int] = set()

        unmapped = False
        for path, info in diff_info.items():
            if info["comment_only"] and path.endswith(".py"):
                continue
            before_sel = len(selected)

            is_protected_path = (
                path.startswith("sqlglot/optimizer/")
                or path == "sqlglot/serde.py"
                or path.startswith("tests/")
            )
            direct_here: set[int] = set()
            for ln in info["lines"]:
                for s_ln, e_ln, aname in attr_spans.get(path, ()):
                    if s_ln <= ln <= e_ln:
                        at = attrs_map.get(aname, ())
                        use = at if len(at) <= 20 else (set(at) & set(file_tests.get(path, ())))
                        selected.update(use)
                        strong.update(use)
                        if is_protected_path:
                            protected_tests.update(use)
                        for n in use:
                            if tests_by_num[n]["seconds"] >= 0.5 and n not in direct_here:
                                attr_slow.add(n)
            for a in info["attrs"]:
                at = attrs_map.get(a, ())
                selected.update(at)
                strong.update(at)
                if is_protected_path:
                    protected_tests.update(at)
                for n in at:
                    if tests_by_num[n]["seconds"] >= 0.5 and n not in direct_here:
                        attr_slow.add(n)

            if path == "sqlglot/serde.py":
                opt = tests_by_file.get("tests/test_optimizer.py", ())
                selected.update(opt)
                exact_hits.update(opt)
                direct_here.update(opt)
                protected_tests.update(opt)

            if path in fixtures:
                selected.update(fixtures[path])
                strong.update(fixtures[path])
                exact_hits.update(fixtures[path])
                direct_here.update(fixtures[path])
                if is_protected_path:
                    protected_tests.update(fixtures[path])
            elif path in tests_by_file:
                selected.update(tests_by_file[path])
                strong.update(tests_by_file[path])
                exact_hits.update(tests_by_file[path])
                direct_here.update(tests_by_file[path])
                if is_protected_path:
                    protected_tests.update(tests_by_file[path])
            elif path in lines_map:
                f_lines = lines_map[path]
                f_funcs = funcs_map.get(path, ())
                hit: set[int] = set()
                touched_top_level = False

                for ln in info["lines"]:
                    enclosing = [(e - s, t_ids) for s, e, t_ids in f_funcs if s <= ln <= e]
                    if enclosing:
                        fh = min(enclosing, key=lambda x: x[0])[1]
                        hit.update(fh)
                        strong.update(fh)
                        if is_protected_path:
                            protected_tests.update(fh)
                    else:
                        touched_top_level = True
                    eh = f_lines.get(str(ln), ())
                    hit.update(eh)
                    strong.update(eh)
                    exact_hits.update(eh)
                    direct_here.update(eh)
                    if is_protected_path:
                        protected_tests.update(eh)

                if touched_top_level or not hit:
                    if path.startswith("sqlglot/") and path.endswith(".py"):
                        ft = file_tests.get(path, ())
                        selected.update(ft)
                        direct_here.update(ft)
                        if is_protected_path:
                            protected_tests.update(ft)
                        if path.endswith("/tsql.py"):
                            opt = tests_by_file.get("tests/test_optimizer.py", ())
                            selected.update(opt)
                            direct_here.update(opt)
                            protected_tests.update(opt)
                    else:
                        selected.update(tests_by_num.keys())
                        direct_here.update(tests_by_num.keys())
                        if is_protected_path:
                            protected_tests.update(tests_by_num.keys())
                else:
                    selected.update(hit)
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                ft_p = file_tests.get(path, ())
                selected.update(ft_p)
                direct_here.update(ft_p)
                if is_protected_path:
                    protected_tests.update(ft_p)
                if path.endswith("/tsql.py"):
                    opt = tests_by_file.get("tests/test_optimizer.py", ())
                    selected.update(opt)
                    direct_here.update(opt)
                    protected_tests.update(opt)

            attr_slow -= direct_here
            if len(selected) == before_sel and not path.lower().endswith(PROSE_EXT):
                unmapped = True

        if unmapped:
            broad = {t["number"] for t in tests_list if t["seconds"] <= UNMAPPED_CAP}
            selected.update(broad)
            exact_hits.update(broad)

        if not selected and fast_tests:
            budget = 0.0
            for t in fast_tests:
                if budget + t["seconds"] <= 15.0 or not selected:
                    selected.add(t["number"])
                    budget += t["seconds"]
                if len(selected) >= 16:
                    break

        pre_prune = set(selected)
        selected = prune_candidates(
            selected, strong, exact_hits, attr_slow, protected_tests, tests_by_num
        )
        selected |= {n for n in pre_prune if tests_by_num[n]["seconds"] <= SAFETY_CAP}
        chosen = [tests_by_num[n] for n in selected]
        workers = optimal_pack(chosen)
        out.append(json.dumps({"commit": cid, "workers": workers}))

    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    if "--convert" in sys.argv:
        convert()
    else:
        main()
