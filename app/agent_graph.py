"""A small bounded LangGraph agent that selects retrieval tools from claim facts."""
import json
import os
import re
import time
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from .agents import CORPUS, search_corpus


class IntakeState(TypedDict):
    claim: dict
    messages: Annotated[list[BaseMessage], add_messages]
    step: int


@tool
def corpus_search(query: str) -> str:
    """Search candidate clauses with the hybrid lexical/FAISS retriever."""
    return json.dumps(search_corpus(query, strategy="agent_hybrid", limit=8))


@tool
def clause_lookup(clause_id: str) -> str:
    """Fetch a corpus clause by its stable identifier."""
    p = next((p for p in CORPUS if p["id"] == clause_id or p["clause"] == clause_id), None)
    return json.dumps(p)


@tool
def claim_field_lookup(field: str, claim_json: str) -> str:
    """Read one validated claim field without changing it."""
    claim = json.loads(claim_json)
    return json.dumps({"field": field, "value": claim.get(field)})


TOOLS = [corpus_search, clause_lookup, claim_field_lookup]
TOOL_NODE = ToolNode(TOOLS)


def planner(state: IntakeState):
    claim = state["claim"]
    step = state["step"]
    kind = claim["disruption"]
    if step >= 12:
        return {"messages": [AIMessage(content="Retrieval plan complete")], "step": step}
    calls = [call for message in state["messages"] for call in getattr(message, "tool_calls", [])]
    looked_up = {call.get("args", {}).get("clause_id") for call in calls if call.get("name") == "clause_lookup"}
    searched = [call.get("args", {}).get("query", "") for call in calls if call.get("name") == "corpus_search"]
    observations = []
    for message in state["messages"]:
        if getattr(message, "type", None) == "tool":
            try:
                value = json.loads(message.content)
                observations.extend(value if isinstance(value, list) else [value])
            except Exception:
                pass
    if step == 0:
        name, args = "claim_field_lookup", {"field": "notice_minutes", "claim_json": json.dumps(claim)}
    elif not searched:
        name, args = "corpus_search", {"query": f"{kind} compensation block time entitlement"}
    elif not any("condition" in query or "exception" in query for query in searched):
        # The agent inspects the first returned passages and chooses a follow-up
        # only when a clause states a remedy or points to related paragraphs.
        texts = " ".join(str(p.get("text", "")) for p in observations if isinstance(p, dict))
        needs_followup = bool(re.search(r"(?i)(either|alternate|compensation|Para\s+\d|subject to|except)", texts))
        if needs_followup:
            refs = re.findall(r"(?i)Para\s+(\d+\.\d+(?:\.\d+)?)", texts)
            name, args = "corpus_search", {"query": f"{kind} conditions exceptions {', '.join(refs)} notice contact facilities cause"}
        else:
            return {"messages": [AIMessage(content="No linked condition was identified; reviewer will receive limited evidence")], "step": step}
    else:
        # Derive cross-reference lookups from passages already read, then add the
        # narrow category checklist needed to avoid amount-only retrieval.
        texts = " ".join(str(p.get("text", "")) for p in observations if isinstance(p, dict))
        referenced = set(re.findall(r"(?i)(?:Para\s+|paragraph\s+)(\d+\.\d+(?:\.\d+)?)", texts))
        category_refs = ({"3.3.1", "3.3.2", "3.3.3", "3.3.4", "3.8.1", "1.4", "1.5"} if kind == "cancellation" else
                         {"3.4.1", "3.4.2", "3.4.3", "3.4.4", "3.8.1", "1.4", "1.5"} if kind == "delay" else
                         {"3.2.1", "3.2.2", "3.2.3", "1.4", "1.5"} if kind == "denied_boarding" else {"3.5.1"})
        referenced = {ref for ref in referenced if ref in category_refs}
        referenced.update(category_refs)
        observed_ids = {p.get("id") for p in observations if isinstance(p, dict)}
        candidates = [f"CAR-{ref}" for ref in sorted(referenced)
                     if f"CAR-{ref}" not in looked_up and f"CAR-{ref}" not in observed_ids]
        # The CAR joins force-majeure sections 1.4 and 1.5 in some excerpts.
        if kind == "cancellation" and "CAR-1.4-1.5" not in looked_up and "CAR-1.4-1.5" not in observed_ids:
            candidates.insert(0, "CAR-1.4-1.5")
        if candidates:
            name, args = "clause_lookup", {"clause_id": candidates[0]}
        else:
            return {"messages": [AIMessage(content="Required linked passages and category conditions collected")], "step": step}
    return {"messages": [AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"tool-{step}", "type": "tool_call"}])], "step": step + 1}


def next_step(state: IntakeState):
    msg = state["messages"][-1]
    return "tools" if isinstance(msg, AIMessage) and msg.tool_calls else END


_TOOL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-tool")


def bounded_tools(state: IntakeState, config: RunnableConfig):
    # Enforce the deadline while the tool runs, instead of checking after it returns.
    future = _TOOL_POOL.submit(copy_context().run, TOOL_NODE.invoke, state, config)
    try:
        return future.result(timeout=float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "3")))
    except FuturesTimeout:
        future.cancel()
        raise TimeoutError("agent tool-call deadline exceeded")


builder = StateGraph(IntakeState)
builder.add_node("agent", planner)
builder.add_node("tools", bounded_tools)
builder.set_entry_point("agent")
builder.add_conditional_edges("agent", next_step, {"tools": "tools", END: END})
builder.add_edge("tools", "agent")
GRAPH = builder.compile()


def retrieve_with_agent(claim: dict):
    state = GRAPH.invoke({"claim": claim, "messages": [], "step": 0}, config={"recursion_limit": 32})
    docs = {}
    events = []
    calls = 0
    for message in state["messages"]:
        if getattr(message, "type", None) != "tool":
            for call in getattr(message, "tool_calls", []):
                events.append({"tool": call.get("name"), "arguments": call.get("args", {})})
            continue
        calls += 1
        try:
            value = json.loads(message.content)
        except Exception:
            continue
        values = value if isinstance(value, list) else [value]
        ids = [p.get("id") for p in values if isinstance(p, dict) and p.get("id")]
        events.append({"tool_result": message.name, "passage_ids": ids, "result": value if isinstance(value, dict) else None})
        for p in values:
            if isinstance(p, dict) and p.get("id"):
                docs[p["id"]] = p
    return list(docs.values()), calls, state["step"], events
