"""行为路由只分发已发生的工具调用，不判断轮次或填充业务结果。"""

from langgraph.graph import END


def route_bid_agent(state):
    return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END
