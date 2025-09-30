from langgraph.graph import StateGraph, END
from .state import GraphState
from .router import router_node
from .nodes.general_info_node import general_info_node
from .nodes.pricing_node import pricing_node
from .nodes.other_node import other_node

graph = StateGraph(GraphState)

graph.add_node("router", router_node)
graph.add_node("general_info", general_info_node)
graph.add_node("pricing", pricing_node)
graph.add_node("other", other_node)

graph.set_entry_point("router")

def route_edge(state: GraphState):
    return state["route"]

graph.add_conditional_edges("router", route_edge, {
    "general_info": "general_info",
    "pricing": "pricing",
    "other": "other",
})

graph.add_edge("general_info", END)
graph.add_edge("pricing", END)
graph.add_edge("other", END)

app = graph.compile()
