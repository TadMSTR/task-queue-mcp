"""
The fast read path (vikunja#1003): libyaml's parser, and the lookup of one task by filename.

Two properties, each of which fails in a way no other suite would notice:

- The C loader must parse every shape the live queue holds exactly as the pure-Python one
  did. A difference would change what every reader sees, silently.
- The lookup by filename must never answer `not found` for a record on disk. The live queue
  holds at least one file whose name does not match its id, so the full-scan fallback is
  load-bearing, not defensive.
"""

import logging
import os
import uuid

import pytest
import yaml

import src.tools.queue as q
from src.tools.queue import (
    DEAD_LETTER_REFUSAL,
    get_task_handler,
    park_task_handler,
    set_task_status_handler,
    submit_task_handler,
    update_task_handler,
)

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

# Hand-written to cover what yaml.dump in submit_task_handler never produces, but other
# direct-YAML writers (the dispatcher, the audit launcher) do: a tz-aware timestamp written
# as a bare scalar, a date, a literal block scalar, a nested history list and non-ASCII.
LIVE_SHAPES = """\
id: 3f0e8a52-0000-4000-8000-000000000001
created: 2026-09-30T16:19:21.039681Z
due: 2026-10-01
status: approved
summary: "Fix the tab — lag ≥ 2 s, ümlaut, 日本語, emoji 🚀"
payload:
  description: |
    Line one.
      Indented line two.

    Paragraph after a blank line.
  folded: >
    folded text
    continues here
  context_refs:
    - /home/ted/a.md
    - /home/ted/b.md
  priority: high
history:
  - timestamp: '2026-09-30T16:19:21.039681Z'
    status: submitted
    actor: research
    note: Task submitted via task-queue-mcp
  - timestamp: 2026-09-30 16:20:02.109259+00:00
    status: approved
    actor: dispatcher
    note: "Auto-approved: requires_approval=false (explicit)"
result: {output: null, completed_by: null, completed_at: null}
retry_policy:
  next_retry_at: ~
  retry_count: 0
flags: [true, false, yes, no, on, off]
numbers: [1, 1.5, 0x1f, 1e3, .inf]
"""


def test_libyaml_loader_is_the_one_in_use():
    # CI's wheels carry libyaml. If this fails, the published image's own assertion will
    # too, and every read is back to the ~10x slower pure-Python parser.
    assert yaml.__with_libyaml__
    assert q.YAML_LOADER_NAME == "CSafeLoader"


def test_c_and_python_safe_loaders_agree_on_live_shapes():
    py = yaml.load(LIVE_SHAPES, Loader=yaml.SafeLoader)
    c = yaml.load(LIVE_SHAPES, Loader=yaml.CSafeLoader)
    assert c == py
    # Guard the fixture itself: if these shapes stopped parsing as typed values the
    # equality above would be comparing two strings.
    assert py["created"].tzinfo is not None
    assert py["payload"]["description"].count("\n") == 4
    assert len(py["history"]) == 2


def test_c_and_python_safe_loaders_agree_on_a_submitted_record(tmp_path):
    r = submit_task_handler(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="unicode — ✓",
        description="multi\nline\n\ttabbed",
        context_refs=["/a", "/b"],
        queue_dir=str(tmp_path),
    )
    text = (tmp_path / r["filename"]).read_text()
    assert yaml.load(text, Loader=yaml.CSafeLoader) == yaml.load(text, Loader=yaml.SafeLoader)


def test_c_safe_loader_still_refuses_python_tags(tmp_path):
    # The switch from safe_load to load(Loader=...) must not have widened what parses.
    path = tmp_path / "20260930-000000-deadbeef.yml"
    path.write_text("id: !!python/object/apply:os.system ['true']\n")
    assert q._load_task_file(str(path)) is None


# ---------------------------------------------------------------------------
# Lookup by filename, with a full-scan fallback
# ---------------------------------------------------------------------------


def _submit(tmp_path, **kw) -> dict:
    defaults = dict(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    defaults.update(kw)
    return submit_task_handler(**defaults)


def _set_status(path, status):
    data = yaml.safe_load(path.read_text())
    data["status"] = status
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def _move(tmp_path, filename, subdir, new_name=None):
    dest = tmp_path / subdir
    dest.mkdir(exist_ok=True)
    target = dest / (new_name or filename)
    os.replace(tmp_path / filename, target)
    return target


def _misname(name: str) -> str:
    """Same timestamp, a suffix that is not the id's prefix."""
    return name[: -len("xxxxxxxx.yml")] + "856b868d.yml"


@pytest.fixture
def parse_count(monkeypatch):
    """How many task files were parsed. The count is the assertion; timing is not."""
    calls = []
    real = q._load_task_file

    def counting(path):
        calls.append(os.path.basename(path))
        return real(path)

    monkeypatch.setattr(q, "_load_task_file", counting)
    return calls


def _padding(tmp_path, n=20):
    """Other records in queue and archive, so a full scan is distinguishable from a lookup."""
    for i in range(n):
        r = _submit(tmp_path, summary=f"pad {i}")
        if i % 2:
            _move(tmp_path, r["filename"], "archive")


def test_correctly_named_archive_record_parses_at_most_two_files(tmp_path, parse_count):
    _padding(tmp_path)
    r = _submit(tmp_path)
    _move(tmp_path, r["filename"], "archive")
    parse_count.clear()

    task = get_task_handler(r["task_id"], queue_dir=str(tmp_path))

    assert task["id"] == r["task_id"]
    assert task["queue_location"] == "archive"
    assert len(parse_count) <= 2, parse_count


def test_misnamed_archive_record_is_found_and_logged(tmp_path, parse_count, caplog):
    # Reproduces the live archive/20260603-121702-856b868d.yml, which holds 741bf127-….
    _padding(tmp_path)
    r = _submit(tmp_path)
    moved = _move(tmp_path, r["filename"], "archive", _misname(r["filename"]))
    assert not moved.name.endswith(f"-{r['task_id'][:8]}.yml")
    parse_count.clear()

    with caplog.at_level(logging.WARNING, logger="src.tools.queue"):
        task = get_task_handler(r["task_id"], queue_dir=str(tmp_path))

    assert task["id"] == r["task_id"]
    assert task["queue_location"] == "archive"
    # The fallback is a full scan, not a lucky guess.
    assert len(parse_count) > 20
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any(r["task_id"] in m and moved.name in m for m in warnings), warnings


def test_misnamed_live_record_is_found_by_a_mutating_handler(tmp_path):
    r = _submit(tmp_path)
    renamed = tmp_path / _misname(r["filename"])
    os.replace(tmp_path / r["filename"], renamed)

    result = set_task_status_handler(
        r["task_id"], "approved", actor="dispatcher", queue_dir=str(tmp_path)
    )

    assert result["ok"] is True, result
    # Written back to the file it was read from, not to a new correctly-named one.
    assert yaml.safe_load(renamed.read_text())["status"] == "approved"
    assert sorted(p.name for p in tmp_path.glob("*.yml")) == [renamed.name]


def test_absent_id_is_not_found_after_a_full_scan(tmp_path, parse_count):
    _padding(tmp_path)
    parse_count.clear()

    result = get_task_handler(str(uuid.uuid4()), queue_dir=str(tmp_path))

    assert result == {"ok": False, "error": "not found"}
    assert len(parse_count) == 20


def test_dead_lettered_id_is_still_refused_by_a_mutating_handler(tmp_path):
    r = _submit(tmp_path)
    _set_status(tmp_path / r["filename"], "approved")
    _move(tmp_path, r["filename"], "dead-letters")

    result = park_task_handler(r["task_id"], actor="operator", queue_dir=str(tmp_path))

    assert result == {"ok": False, "error": DEAD_LETTER_REFUSAL}


def test_misnamed_dead_letter_is_still_refused(tmp_path):
    r = _submit(tmp_path)
    _move(tmp_path, r["filename"], "dead-letters", _misname(r["filename"]))

    result = update_task_handler(
        r["task_id"], "in-progress", actor="developer", queue_dir=str(tmp_path)
    )

    assert result == {"ok": False, "error": DEAD_LETTER_REFUSAL}


def test_mutations_do_not_reach_into_dead_letters(tmp_path):
    # Mutating handlers search queue + archive only. A lookup that widened to dead-letters
    # would mutate a dead letter in place.
    r = _submit(tmp_path)
    moved = _move(tmp_path, r["filename"], "dead-letters")
    before = moved.read_text()

    assert q._find_task(str(tmp_path), r["task_id"], include_archived=True) is None
    set_task_status_handler(r["task_id"], "approved", actor="dispatcher", queue_dir=str(tmp_path))

    assert moved.read_text() == before


def test_shared_eight_char_suffix_returns_the_exact_id(tmp_path):
    prefix = "abcdef01"
    wanted = f"{prefix}-1111-4111-8111-111111111111"
    other = f"{prefix}-2222-4222-8222-222222222222"
    for i, tid in enumerate((other, wanted)):
        (tmp_path / f"20260930-00000{i}-{prefix}.yml").write_text(
            yaml.dump({"id": tid, "status": "submitted", "target_agent": "developer"})
        )

    assert get_task_handler(wanted, queue_dir=str(tmp_path))["id"] == wanted
    assert get_task_handler(other, queue_dir=str(tmp_path))["id"] == other


def test_queue_copy_wins_over_archive_copy(tmp_path):
    # Directory order is the same as _load_all_tasks: queue, then archive.
    r = _submit(tmp_path)
    live = tmp_path / r["filename"]
    archived = tmp_path / "archive" / r["filename"]
    archived.parent.mkdir()
    archived.write_text(live.read_text())
    _set_status(archived, "completed")

    task = q._find_task(str(tmp_path), r["task_id"], include_archived=True)

    assert task["_location"] == "queue"
    assert task["status"] == "submitted"


def test_tmp_files_are_never_read(tmp_path):
    r = _submit(tmp_path)
    stale = tmp_path / (r["filename"] + ".tmp")
    stale.write_text(yaml.dump({"id": r["task_id"], "status": "failed"}))
    os.remove(tmp_path / r["filename"])

    assert get_task_handler(r["task_id"], queue_dir=str(tmp_path))["error"] == "not found"


def test_a_non_hex_prefix_never_reaches_a_glob_pattern(tmp_path, monkeypatch):
    # Callers validate ids as UUIDs; _find_task does not rely on it. A prefix carrying path
    # or glob syntax skips the fast path entirely and is only ever compared, never globbed.
    patterns = []
    real_glob = q.glob.glob

    def spy(pattern, *a, **kw):
        patterns.append(pattern)
        return real_glob(pattern, *a, **kw)

    monkeypatch.setattr(q.glob, "glob", spy)
    (tmp_path / "20260930-000000-deadbeef.yml").write_text(yaml.dump({"id": "../*/x-y"}))

    task = q._find_task(str(tmp_path), "../*/x-y", include_archived=True)

    assert task is not None and task["id"] == "../*/x-y"
    assert all(p.endswith("*.yml") for p in patterns), patterns
