"""路由只分发已发出的行为，轮次判断属于模型调用前。"""

from langchain_core.messages import AIMessage
from langgraph.graph import END

from app.graph.routing import route_bid_agent


def test_plain_content_ends_graph_without_parsing_json():
    assert route_bid_agent({"messages": [AIMessage(content="处理完毕")]}) == END


def test_tool_behavior_is_routed_to_tools_even_at_limit():
    assert (
        route_bid_agent(
            {
                "model_calls": 8,
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "search_documents",
                                "args": {},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ],
            }
        )
        == "tools"
    )
