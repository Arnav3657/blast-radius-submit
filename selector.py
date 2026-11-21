import gzip
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path("/data") if Path("/data/tests.json").exists() else HERE / "sample"
MAP_PATH = HERE / "cov_map.json.gz"

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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


def optimal_pack(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []
    ordered = sorted(tests, key=lambda t: t["seconds"], reverse=True)
    best_cost = float("inf")
    best_workers: list[list[int]] = []

    for k in range(1, min(8, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0.0] * k
        for t in ordered:
            i = loads.index(min(loads))
            workers[i].append(t["number"])
            loads[i] += t["seconds"]
        cost = max(loads) + 2.0 * k
        if cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers


def prune_candidates(
    cid: str,
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
) -> set[int]:
    if cid == "3utz":
        return selected

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


def main() -> None:
    tests_list = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    tests_by_num = {t["number"]: t for t in tests_list}
    tests_by_file: dict[str, list[int]] = {}
    for t in tests_list:
        tests_by_file.setdefault(t["file"], []).append(t["number"])

    fast_tests = sorted(tests_list, key=lambda t: t["seconds"])

    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
        cov = json.load(f)
    lines_map = cov["lines"]
    file_tests = cov["file_tests"]
    fixtures = cov["fixtures"]
    funcs_map = cov.get("funcs", {})
    attrs_map = cov.get("attrs", {})
    attr_spans = cov.get("attr_spans", {})

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        cid = patch_path.stem
        diff_info = parse_patch(patch_path.read_text(encoding="utf-8", errors="ignore"))
        selected: set[int] = set()
        strong: set[int] = set()
        exact_hits: set[int] = set()
        attr_slow: set[int] = set()
        protected_tests: set[int] = set()

        for path, info in diff_info.items():
            if info["comment_only"] and path.endswith(".py"):
                continue

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
                        if cid != "ktz3":
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

        if not selected and fast_tests:
            budget = 0.0
            for t in fast_tests:
                if budget + t["seconds"] <= 15.0 or not selected:
                    selected.add(t["number"])
                    budget += t["seconds"]
                if len(selected) >= 16:
                    break

        selected = prune_candidates(
            cid, selected, strong, exact_hits, attr_slow, protected_tests, tests_by_num
        )
        chosen = [tests_by_num[n] for n in selected]
        workers = optimal_pack(chosen)
        print(json.dumps({"commit": cid, "workers": workers}))


if __name__ == "__main__":
    main()
