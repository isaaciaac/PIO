from __future__ import annotations

from pathlib import Path

from vibe.repo import ensure_vibe_dirs
from vibe.schemas.events import new_event
from vibe.storage.ledger import Ledger


def test_ledger_append_and_read_branch(tmp_path: Path) -> None:
    ensure_vibe_dirs(tmp_path, agent_ids=["router"])

    main = Ledger(tmp_path, branch_id="main")
    evt1 = new_event(agent="pm", type="REQ_CREATED", summary="req", branch_id="main")
    main.append(evt1)

    branch = Ledger(tmp_path, branch_id="feature1")
    evt2 = new_event(agent="qa", type="TEST_RUN", summary="run", branch_id="feature1")
    branch.append(evt2)

    assert main.count_lines() == 1
    assert branch.count_lines() == 1

    main_events = list(main.iter_events())
    assert main_events[0].id == evt1.id

    branch_events = list(branch.iter_events())
    assert branch_events[0].id == evt2.id

