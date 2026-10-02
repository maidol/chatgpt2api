from services.openai_backend_api import CODEX_RESPONSES_MODEL
from services.protocol import anthropic_codex_bridge as bridge

READ_TOOL = {
    "name": "Read",
    "description": "Read a file",
    "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
    "cache_control": {"type": "ephemeral"},
}
BARE_TOOL = {"name": "TodoList", "description": "", "input_schema": {"type": "object"}}


def test_claude_code_request_converts_to_responses_items():
    body = {
        "model": "gpt-5.6",
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=1"},
            {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [
            {"role": "user", "content": "read a.py"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                    {"type": "text", "text": "print(1)"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                ]},
                {"type": "text", "text": "now explain"},
            ]},
        ],
        "tools": [READ_TOOL, BARE_TOOL],
        "tool_choice": {"type": "any"},
        "max_tokens": 32000,
        "temperature": 1,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "metadata": {"user_id": "u"},
        "stream": True,
    }

    payload = bridge.anthropic_to_codex_body(body)

    assert payload == {
        "model": "gpt-5.6",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Claude Code."}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "read a.py"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Reading."}]},
            {"type": "function_call", "call_id": "toolu_1", "name": "Read", "arguments": "{\"file_path\": \"a.py\"}"},
            {"type": "function_call_output", "call_id": "toolu_1", "output": "print(1)"},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "now explain"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]},
        ],
        "tools": [
            {"type": "function", "name": "Read", "description": "Read a file",
             "parameters": READ_TOOL["input_schema"], "strict": False},
            {"type": "function", "name": "TodoList", "description": "",
             "parameters": {"type": "object", "properties": {}}, "strict": False},
        ],
        "parallel_tool_calls": True,
        "tool_choice": "required",
    }


def test_tool_choice_mapping():
    def choice(value):
        return bridge.anthropic_to_codex_body({"messages": [], "tools": [READ_TOOL], "tool_choice": value})

    assert choice({"type": "auto"})["tool_choice"] == "auto"
    assert choice({"type": "none"})["tool_choice"] == "none"
    assert choice({"type": "tool", "name": "Read"})["tool_choice"] == {"type": "function", "name": "Read"}
    assert choice({"type": "auto", "disable_parallel_tool_use": True})["parallel_tool_calls"] is False
    assert "tool_choice" not in bridge.anthropic_to_codex_body({"messages": [], "tools": [READ_TOOL]})


def test_non_gpt_model_names_fall_back_to_codex_default():
    assert bridge.anthropic_to_codex_body({"model": "claude-sonnet-4-5", "messages": []})["model"] == CODEX_RESPONSES_MODEL
    assert bridge.anthropic_to_codex_body({"messages": []})["model"] == CODEX_RESPONSES_MODEL
    assert bridge.anthropic_to_codex_body({"model": "GPT-5.5", "messages": []})["model"] == "GPT-5.5"


def test_has_client_tools():
    assert bridge.has_client_tools({"tools": [READ_TOOL]})
    assert not bridge.has_client_tools({"tools": []})
    assert not bridge.has_client_tools({})
    assert not bridge.has_client_tools({"tools": [{"type": "web_search_20250305", "name": "web_search"}]})


def test_completed_response_with_tool_call_becomes_tool_use_message():
    response = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA"},
            {"type": "message", "content": [{"type": "output_text", "text": "Let me read it."}]},
            {"type": "function_call", "call_id": "call_9", "name": "Read",
             "arguments": "{\"file_path\": \"a.py\", \"pages\": \"\"}"},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 30}},
    }

    message = bridge.codex_response_to_message(response, "gpt-5.6")

    assert message["model"] == "gpt-5.6"
    assert message["content"] == [
        {"type": "text", "text": "Let me read it."},
        {"type": "tool_use", "id": "call_9", "name": "Read", "input": {"file_path": "a.py"}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 70, "output_tokens": 20, "cache_read_input_tokens": 30}


def test_text_only_and_truncated_responses():
    text = bridge.codex_response_to_message(
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}, "m")
    cut = bridge.codex_response_to_message(
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}, "m")

    assert text["stop_reason"] == "end_turn"
    assert text["content"] == [{"type": "text", "text": "hi"}]
    assert cut["stop_reason"] == "max_tokens"
    assert cut["content"] == [{"type": "text", "text": ""}]


def test_stream_events_follow_anthropic_order():
    message = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "gpt-5.6",
        "content": [
            {"type": "text", "text": "Let me read it."},
            {"type": "tool_use", "id": "call_9", "name": "Read", "input": {"file_path": "a.py"}},
        ],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 70, "output_tokens": 20, "cache_read_input_tokens": 30},
    }

    events = list(bridge.message_stream_events(message))

    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]
    assert events[0]["message"]["usage"] == {"input_tokens": 70, "output_tokens": 0, "cache_read_input_tokens": 30}
    assert events[0]["message"]["content"] == []
    assert events[4] == {"type": "content_block_start", "index": 1,
                         "content_block": {"type": "tool_use", "id": "call_9", "name": "Read", "input": {}}}
    assert events[5]["delta"] == {"type": "input_json_delta", "partial_json": "{\"file_path\": \"a.py\"}"}
    assert events[7]["delta"]["stop_reason"] == "tool_use"
    assert events[7]["usage"] == {"output_tokens": 20}
