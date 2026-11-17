"""LangGraph orchestration of the agents.

Lead-generation-only mode (default, `sending.enabled: false`):

    research -> enrich -> qualify -> match -> write -> END      (drafts wait for human review)

The send node is not even added to the graph unless `sending.enabled: true`; then
`run --send` adds  write -> send -> END, and with human approval ON the send node
only sends drafts already approved.

Each node works on the leads in SQLite whose status it owns, so runs are
resumable: if a run stops halfway, the next run carries on from the DB state.
Falls back to a plain sequential runner if langgraph isn't installed.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from app.agents.email_writer import EmailAgent
from app.agents.enrichment import EnrichmentAgent
from app.agents.matching import MatchingAgent
from app.agents.qualification import QualificationAgent
from app.agents.research import ResearchAgent
from app.agents.sender import SenderAgent
from app.config import Config
from app.db import DB
from app.llm import LLM
from app.tools.geo import Geo
from app.tools.http import PoliteFetcher
from app.tools.search import SearchClient

log = logging.getLogger("graph")
STAGES = ["research", "enrich", "qualify", "match", "write"]   # default run: lead generation only
SEND_STAGE = "send"


class PipelineState(TypedDict, total=False):
    stages: list[str]
    max_leads: int
    send: bool
    ignore_window: bool
    as_member: str | None
    results: dict[str, Any]


class Agents:
    """Builds every agent once with shared tools."""

    def __init__(self, cfg: Config, db: DB, as_member: str | None = None):
        if as_member and not cfg.member(as_member):
            raise ValueError(f"unknown team member '{as_member}'. Members: {', '.join(m['id'] for m in cfg.members)}")
        self.cfg, self.db, self.as_member = cfg, db, as_member
        self.fetcher = PoliteFetcher(cfg, db)
        self.geo = Geo(cfg)
        self.llm = LLM(cfg)
        self._search: SearchClient | None = None

    @property
    def search(self) -> SearchClient:
        if self._search is None:
            self._search = SearchClient(self.cfg)
        return self._search

    def research(self) -> ResearchAgent:
        return ResearchAgent(self.cfg, self.db, self.fetcher, self.search, self.geo)

    def enrichment(self) -> EnrichmentAgent:
        return EnrichmentAgent(self.cfg, self.db, self.fetcher, self.geo)

    def qualification(self) -> QualificationAgent:
        return QualificationAgent(self.cfg, self.db, self.llm)

    def matching(self) -> MatchingAgent:
        return MatchingAgent(self.cfg, self.db, sender_override=self.as_member)

    def email(self) -> EmailAgent:
        return EmailAgent(self.cfg, self.db, self.llm)

    def sender(self) -> SenderAgent:
        return SenderAgent(self.cfg, self.db, only_member=self.as_member)

    def close(self) -> None:
        self.fetcher.close()


def _node(name: str, fn: Callable[[PipelineState], dict]) -> Callable[[PipelineState], PipelineState]:
    def run(state: PipelineState) -> PipelineState:
        results = dict(state.get("results", {}))
        if name not in state.get("stages", STAGES):
            return {"results": results}
        log.info("── %s agent ──", name)
        try:
            results[name] = fn(state)
        except Exception as e:  # one agent failing shouldn't lose the others' work
            log.exception("%s agent failed: %s", name, e)
            results[name] = {"error": str(e)}
        return {"results": results}
    return run


def build_nodes(a: Agents) -> dict[str, Callable]:
    limit = lambda s: int(s.get("max_leads") or 50)  # noqa: E731
    nodes = {
        "research": _node("research", lambda s: a.research().run(s.get("max_leads"))),
        "enrich": _node("enrich", lambda s: a.enrichment().run(limit(s) * 2)),
        "qualify": _node("qualify", lambda s: a.qualification().run(limit(s) * 2)),
        "match": _node("match", lambda s: a.matching().run(limit(s) * 2)),
        "write": _node("write", lambda s: a.email().run(limit(s) * 2)),
    }
    if a.cfg.sending_enabled:  # lead-generation-only mode: no send node at all
        nodes[SEND_STAGE] = _node(SEND_STAGE, lambda s: a.sender().run(ignore_window=bool(s.get("ignore_window"))))
    return nodes


def should_send(cfg: Config) -> Callable[[PipelineState], str]:
    """Send only when asked (--send). With human approval on, the Sender Agent
    only picks up drafts a human already approved; new drafts wait for `review`."""
    def route(state: PipelineState) -> str:
        wanted = state.get("send") and SEND_STAGE in state.get("stages", STAGES)
        return SEND_STAGE if wanted and cfg.sending_enabled else "end"
    return route


def build_graph(cfg: Config, a: Agents):
    from langgraph.graph import END, StateGraph

    nodes = build_nodes(a)
    g = StateGraph(PipelineState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.set_entry_point("research")
    g.add_edge("research", "enrich")
    g.add_edge("enrich", "qualify")
    g.add_edge("qualify", "match")
    g.add_edge("match", "write")
    if SEND_STAGE in nodes:
        g.add_conditional_edges("write", should_send(cfg), {SEND_STAGE: SEND_STAGE, "end": END})
        g.add_edge(SEND_STAGE, END)
    else:
        g.add_edge("write", END)
    return g.compile()


def run_pipeline(cfg: Config, db: DB, stages: list[str] | None = None, max_leads: int | None = None,
                 send: bool = False, ignore_window: bool = False, as_member: str | None = None) -> dict:
    from app.db import AlreadyRunning

    try:
        with db.lock("run", ttl_seconds=4 * 3600):
            return _run_pipeline(cfg, db, stages, max_leads, send, ignore_window, as_member)
    except AlreadyRunning:
        log.warning("A pipeline run is already in progress; not starting another.")
        return {"skipped": "already running"}


def _run_pipeline(cfg: Config, db: DB, stages, max_leads, send, ignore_window, as_member) -> dict:
    a = Agents(cfg, db, as_member)
    if send and not cfg.sending_enabled:
        log.warning("--send ignored: sending is disabled (lead-generation-only mode). Drafts wait for review.")
        send = False
    stages = stages or (STAGES + [SEND_STAGE] if send else STAGES)
    state: PipelineState = {"stages": stages, "max_leads": max_leads or 0, "send": send,
                            "ignore_window": ignore_window, "as_member": as_member, "results": {}}
    try:
        try:
            graph = build_graph(cfg, a)
        except ImportError:
            log.warning("langgraph not installed; running stages sequentially.")
            graph = None
        if graph is not None:
            final = graph.invoke(state)
        else:
            nodes = build_nodes(a)
            for name in STAGES:
                state.update(nodes[name](state))
            if should_send(cfg)(state) == SEND_STAGE:
                state.update(nodes[SEND_STAGE](state))
            final = state
        return final.get("results", {})
    finally:
        a.close()
