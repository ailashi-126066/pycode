from pycode.ui.agents import AgentsPanel


def test_agents_panel_shows_each_subagent_by_name():
    # 11.1 section 39 #5: /agents lists the name the main agent gave.
    agent = {
        "name": "n3",
        "mode": "readonly",
        "status": "running",
        "background": False,
        "description": "review",
        "workspace": "/w",
        "worktree": None,
    }
    lines = [
        str(item.content)
        for item in AgentsPanel({"running": 1, "limit": 4, "agents": [agent]}).compose()
    ]
    assert lines[1].startswith("n3 · readonly · running · review")
