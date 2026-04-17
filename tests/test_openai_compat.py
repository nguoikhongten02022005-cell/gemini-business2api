import os
import unittest
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("ADMIN_KEY", "test-admin")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("ENABLE_OPENAI_TOOL_SHIM", "1")

from main import API_KEY, app


def auth_headers():
    token = (API_KEY.split(",")[0].strip() if API_KEY else "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def tool_schema(name="read_file"):
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }]


def chat_completion_payload(content="Hello from backend", model="gemini-auto"):
    return {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class OpenAICompatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    @patch("main.chat_impl", new_callable=AsyncMock)
    def test_chat_completions_simple_without_tools_can_return_success(self, mock_chat_impl):
        mock_chat_impl.return_value = chat_completion_payload("Hello from backend")

        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from backend")
        mock_chat_impl.assert_awaited_once()

    def test_chat_completions_simple_without_tools_reaches_gateway_logic(self):
        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "not-a-real-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"].lower())

    def test_chat_completions_with_tools_returns_tool_call(self):
        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "messages": [{"role": "user", "content": "Read `agent.py`"}],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        tool_call = body["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "read_file")
        self.assertIn("agent.py", tool_call["function"]["arguments"])

    def test_chat_completions_with_tool_result_returns_final_answer(self):
        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "messages": [
                    {"role": "user", "content": "What is the first line?"},
                    {"role": "assistant", "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"agent.py"}'},
                    }]},
                    {"role": "tool", "tool_call_id": "call_123", "content": "   1 | #!/usr/bin/env python3\n   2 | print('x')"},
                ],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["choices"][0]["message"]["content"], "#!/usr/bin/env python3")

    def test_chat_completions_with_tool_result_falls_back_to_raw_tool_text(self):
        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "messages": [
                    {"role": "user", "content": "Summarize the command output"},
                    {"role": "assistant", "tool_calls": [{
                        "id": "call_456",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": '{"command":"pwd"}'},
                    }]},
                    {"role": "tool", "tool_call_id": "call_456", "content": "line one\nline two"},
                ],
                "tools": tool_schema("run_command"),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "line one\nline two")

    @patch("main.execute_responses_chat_turn", new_callable=AsyncMock)
    def test_responses_endpoint_with_input_string_returns_success(self, mock_execute_turn):
        mock_execute_turn.return_value = chat_completion_payload("Hello from backend")

        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={"model": "gemini-auto", "input": "Hello"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["output_text"], "Hello from backend")
        self.assertEqual(body["output"][0]["type"], "message")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(body["output"][0]["content"][0]["text"], "Hello from backend")

        mock_execute_turn.assert_awaited_once()
        chat_req = mock_execute_turn.await_args.args[0]
        self.assertEqual(chat_req.model, "gemini-auto")
        self.assertFalse(chat_req.stream)
        self.assertEqual(len(chat_req.messages), 1)
        self.assertEqual(chat_req.messages[0].role, "user")
        self.assertEqual(chat_req.messages[0].content, "Hello")

    @patch("main.uptime_tracker.record_request")
    def test_responses_plain_request_records_api_service_once(self, mock_record_request):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={"model": "not-a-real-model", "input": "Hello"},
        )

        self.assertEqual(response.status_code, 404)
        api_service_calls = [call for call in mock_record_request.call_args_list if call.args and call.args[0] == "api_service"]
        self.assertEqual(len(api_service_calls), 1)
        self.assertFalse(api_service_calls[0].args[1])
        self.assertEqual(api_service_calls[0].args[3], 404)

    @patch("main.uptime_tracker.record_request")
    def test_responses_tool_request_still_records_api_service_once_via_middleware(self, mock_record_request):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Read `agent.py`",
                "tools": tool_schema(),
            },
        )

        self.assertEqual(response.status_code, 200)
        api_service_calls = [call for call in mock_record_request.call_args_list if call.args and call.args[0] == "api_service"]
        self.assertEqual(len(api_service_calls), 1)

    @patch("main.execute_responses_chat_turn", new_callable=AsyncMock)
    def test_responses_plain_array_normalizes_roles_and_returns_stream_events(self, mock_execute_turn):
        mock_execute_turn.return_value = chat_completion_payload("Structured reply")

        with self.client.stream(
            "POST",
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "instructions": "Follow the spec",
                "stream": True,
                "input": [
                    {"role": "developer", "content": [{"type": "input_text", "text": "Developer note"}]},
                    {"role": "system", "content": [{"type": "input_text", "text": "System note"}]},
                    {"role": "assistant", "content": [{"type": "output_text", "text": "Previous answer"}]},
                    {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
                ],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            body = "".join(response.iter_text())

        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_item.added", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.output_item.done", body)
        self.assertIn("event: response.completed", body)
        self.assertIn("Structured reply", body)

        mock_execute_turn.assert_awaited_once()
        chat_req = mock_execute_turn.await_args.args[0]
        self.assertFalse(chat_req.stream)
        self.assertEqual(
            [(message.role, message.content) for message in chat_req.messages],
            [
                ("system", "Follow the spec"),
                ("system", "Developer note"),
                ("system", "System note"),
                ("assistant", "Previous answer"),
                ("user", "Hello"),
            ],
        )

    def test_responses_with_tools_returns_function_call(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Read `agent.py`",
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertEqual(body["output"][0]["name"], "read_file")

    @patch("main.execute_responses_chat_turn", new_callable=AsyncMock)
    def test_responses_with_tool_output_returns_final_answer(self, mock_execute_turn):
        first_response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Read `agent.py`",
                "tools": tool_schema(),
            },
        )
        self.assertEqual(first_response.status_code, 200)
        first_body = first_response.json()
        self.assertEqual(first_body["status"], "requires_action")
        call_id = first_body["output"][0]["call_id"]

        mock_execute_turn.return_value = chat_completion_payload("#!/usr/bin/env python3")
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "previous_response_id": first_body["id"],
                "input": [
                    {"type": "function_call_output", "call_id": call_id, "output": "   1 | #!/usr/bin/env python3\n   2 | print('x')"},
                ],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["previous_response_id"], first_body["id"])
        self.assertEqual(body["output_text"], "#!/usr/bin/env python3")
        self.assertEqual(body["output"][0]["type"], "message")

    def test_responses_rejects_missing_previous_response_id_for_tool_output(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": [
                    {"type": "function_call_output", "call_id": "call_123", "output": "done"},
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["type"], "invalid_request_error")
        self.assertIn("previous_response_id", response.json()["detail"]["message"])

    def test_responses_rejects_invalid_previous_response_id(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "previous_response_id": "resp_missing",
                "input": [
                    {"type": "function_call_output", "call_id": "call_123", "output": "done"},
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["type"], "invalid_request_error")
        self.assertIn("previous_response_id", response.json()["detail"]["message"])

    def test_responses_rejects_unsupported_parallel_tool_calls(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Hello",
                "parallel_tool_calls": True,
            },
        )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["detail"]["type"], "not_supported_error")

    @patch("main.execute_responses_chat_turn", new_callable=AsyncMock)
    def test_responses_supports_multiple_tool_calls_lifecycle(self, mock_execute_turn):
        first_response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Read `agent.py`",
                "tools": tool_schema(),
            },
        )
        self.assertEqual(first_response.status_code, 200)
        first_body = first_response.json()
        self.assertEqual(first_body["status"], "requires_action")
        first_call_id = first_body["output"][0]["call_id"]

        mock_execute_turn.return_value = {
            "id": "chatcmpl_tool_step",
            "object": "chat.completion",
            "created": 1_700_000_001,
            "model": "gemini-auto",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_456",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        second_response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "previous_response_id": first_body["id"],
                "input": [
                    {"type": "function_call_output", "call_id": first_call_id, "output": "FILE agent.py\n1 | #!/usr/bin/env python3"},
                ],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(second_response.status_code, 200)
        second_body = second_response.json()
        self.assertEqual(second_body["status"], "requires_action")
        self.assertEqual(second_body["output"][0]["type"], "function_call")
        self.assertEqual(second_body["output"][0]["call_id"], "call_456")

        mock_execute_turn.return_value = chat_completion_payload("Done after second tool")
        third_response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "previous_response_id": second_body["id"],
                "input": [
                    {"type": "function_call_output", "call_id": "call_456", "output": "FILE main.py\n1 | import json"},
                ],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(third_response.status_code, 200)
        third_body = third_response.json()
        self.assertEqual(third_body["status"], "completed")
        self.assertEqual(third_body["output_text"], "Done after second tool")

    def test_responses_rejects_invalid_schema(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": "Hello",
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "bad_tool",
                        "parameters": "not-an-object",
                    },
                }],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["type"], "invalid_request_error")

    def test_responses_rejects_unsupported_input_content_type(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "https://example.com/x.png"}
                        ],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["type"], "invalid_request_error")
        self.assertIn("Unsupported", response.json()["detail"]["message"])

    def test_backward_compatibility_old_route_still_works_for_tool_shim(self):
        response = self.client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "messages": [{"role": "user", "content": "Read `main.py`"}],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "chat.completion")


if __name__ == "__main__":
    unittest.main()
