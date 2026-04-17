import os
import unittest

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


class OpenAICompatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

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

    def test_responses_endpoint_with_input_string(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={"model": "gemini-auto", "input": "Hello"},
        )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["detail"]["type"], "not_supported_error")

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

    def test_responses_with_tool_output_returns_final_answer(self):
        response = self.client.post(
            "/v1/responses",
            headers=auth_headers(),
            json={
                "model": "gemini-auto",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "What is the first line?"}]},
                    {"type": "function_call_output", "call_id": "call_123", "output": "   1 | #!/usr/bin/env python3\n   2 | print('x')"},
                ],
                "tools": tool_schema(),
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["output_text"], "#!/usr/bin/env python3")
        self.assertEqual(body["output"][0]["type"], "message")

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
