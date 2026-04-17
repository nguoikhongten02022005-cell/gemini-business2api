import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from compat.openai_responses import ChatMessage, ResponsesRequest
import responses_runtime as rr


class ResponsesRuntimeUnitTests(unittest.TestCase):
    def test_validate_new_tool_outputs_rejects_missing_call_id(self):
        chain_items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
        ]
        new_input_items = [
            {"item_type": "function_call_output", "type": "function_call_output", "text": "ok"},
        ]
        with self.assertRaises(HTTPException) as ctx:
            rr.validate_new_tool_outputs(chain_items, new_input_items)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("call_id", ctx.exception.detail["message"])

    def test_validate_new_tool_outputs_rejects_unknown_call_id(self):
        chain_items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
        ]
        new_input_items = [
            {"item_type": "function_call_output", "call_id": "call_2", "text": "ok"},
        ]
        with self.assertRaises(HTTPException) as ctx:
            rr.validate_new_tool_outputs(chain_items, new_input_items)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unknown", ctx.exception.detail["message"])

    def test_validate_new_tool_outputs_rejects_duplicate_output(self):
        chain_items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
        ]
        new_input_items = [
            {"item_type": "function_call_output", "call_id": "call_1", "text": "one"},
            {"item_type": "function_call_output", "call_id": "call_1", "text": "two"},
        ]
        with self.assertRaises(HTTPException) as ctx:
            rr.validate_new_tool_outputs(chain_items, new_input_items)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Duplicate", ctx.exception.detail["message"])

    def test_validate_new_tool_outputs_passes_for_valid_call_id(self):
        chain_items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
        ]
        new_input_items = [
            {"item_type": "function_call_output", "call_id": "call_1", "text": "ok"},
        ]
        rr.validate_new_tool_outputs(chain_items, new_input_items)

    def test_pending_function_calls_tracks_unresolved_calls(self):
        items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
            {"item_type": "message", "role": "assistant", "text": "thinking"},
        ]
        pending = rr.pending_function_calls(items)
        self.assertIn("call_1", pending)

    def test_pending_function_calls_removes_resolved_calls(self):
        items = [
            {"item_type": "function_call", "call_id": "call_1", "name": "read_file"},
            {"item_type": "function_call_output", "call_id": "call_1", "text": "done"},
        ]
        pending = rr.pending_function_calls(items)
        self.assertNotIn("call_1", pending)
        self.assertEqual(pending, {})

    def test_merge_chain_items_preserves_order(self):
        chain_items = [{"item_type": "message", "text": "first"}, {"item_type": "function_call", "call_id": "call_1"}]
        input_items = [{"item_type": "function_call_output", "call_id": "call_1"}, {"item_type": "message", "text": "last"}]
        merged = rr.merge_chain_items(chain_items, input_items)
        self.assertEqual(merged, chain_items + input_items)

    def test_items_to_chat_messages_converts_multi_step_lifecycle(self):
        items = [
            {"direction": "input", "item_type": "message", "role": "user", "text": "Read file", "content": [{"type": "input_text", "text": "Read file"}]},
            {"direction": "output", "item_type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": {"path": "agent.py"}},
            {"direction": "input", "item_type": "function_call_output", "call_id": "call_1", "text": "1 | hello", "content": [{"type": "output_text", "text": "1 | hello"}]},
            {"direction": "output", "item_type": "message", "role": "assistant", "text": "Final answer", "content": [{"type": "output_text", "text": "Final answer"}]},
        ]
        messages = rr.items_to_chat_messages(items)
        self.assertEqual([message.role for message in messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[0].content, "Read file")
        self.assertEqual(messages[1].tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(messages[2].tool_call_id, "call_1")
        self.assertEqual(messages[3].content, "Final answer")

    def test_stream_payload_for_message_includes_required_events(self):
        payload = {
            "id": "resp_1",
            "model": "gemini-auto",
            "status": "completed",
            "output": [{
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
                "status": "completed",
            }],
        }
        response = rr._stream_payload(payload)
        self.assertIsInstance(response, StreamingResponse)
        body = asyncio.run(self._read_stream(response))
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_item.added", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.output_item.done", body)
        self.assertIn("event: response.completed", body)

    def test_stream_payload_for_function_call_includes_arguments_delta(self):
        payload = {
            "id": "resp_1",
            "model": "gemini-auto",
            "status": "requires_action",
            "output": [{
                "type": "function_call",
                "id": "call_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"agent.py"}',
                "status": "completed",
            }],
        }
        response = rr._stream_payload(payload)
        body = asyncio.run(self._read_stream(response))
        self.assertIn("event: response.function_call_arguments.delta", body)
        self.assertIn("event: response.output_item.done", body)
        self.assertIn("event: response.completed", body)

    async def _read_stream(self, response: StreamingResponse) -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


class ResponsesRuntimeHandleRequestTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, **overrides):
        payload = {
            "model": "gemini-auto",
            "input": "Hello",
            "tools": None,
            "tool_choice": None,
            "stream": False,
            "instructions": None,
            "metadata": None,
            "temperature": 0.7,
            "top_p": 1.0,
            "max_output_tokens": None,
            "parallel_tool_calls": None,
            "previous_response_id": None,
        }
        payload.update(overrides)
        return ResponsesRequest(**payload)

    async def test_handle_responses_request_loads_chain_for_valid_previous_response_id(self):
        req = self._request(
            previous_response_id="resp_prev",
            input=[{"type": "function_call_output", "call_id": "call_1", "output": "done"}],
            tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
        )
        request = SimpleNamespace(state=SimpleNamespace())
        parent_record = {"id": "resp_prev", "conversation_key": "conv_1", "step_count": 1}
        chain_items = [
            {"direction": "input", "item_type": "message", "role": "user", "text": "Read file", "content": [{"type": "input_text", "text": "Read file"}]},
            {"direction": "output", "item_type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": {"path": "agent.py"}},
        ]
        chat_result = {
            "choices": [{"message": {"role": "assistant", "content": "Final answer"}}],
            "created": 123,
            "model": "gemini-auto",
        }
        with patch("responses_runtime.storage.load_response_record", new=AsyncMock(return_value=parent_record)) as load_record, \
             patch("responses_runtime.storage.load_response_chain_items", new=AsyncMock(return_value=chain_items)) as load_chain, \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()) as save_record, \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()) as replace_items:
            payload = await rr.handle_responses_request(req, request, AsyncMock(return_value=chat_result))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["previous_response_id"], "resp_prev")
        load_record.assert_awaited_once_with("resp_prev")
        load_chain.assert_awaited_once_with("resp_prev")
        save_record.assert_awaited()
        replace_items.assert_awaited()

    async def test_handle_responses_request_rejects_missing_previous_response(self):
        req = self._request(previous_response_id="resp_missing", input=[{"type": "function_call_output", "call_id": "call_1", "output": "done"}])
        request = SimpleNamespace(state=SimpleNamespace())
        with patch("responses_runtime.storage.load_response_record", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as ctx:
                await rr.handle_responses_request(req, request, AsyncMock())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("previous_response_id", ctx.exception.detail["message"])

    async def test_handle_responses_request_rejects_when_max_steps_exceeded(self):
        req = self._request(previous_response_id="resp_prev", input=[{"type": "function_call_output", "call_id": "call_1", "output": "done"}])
        request = SimpleNamespace(state=SimpleNamespace())
        parent_record = {"id": "resp_prev", "conversation_key": "conv_1", "step_count": rr.MAX_RESPONSE_STEPS}
        chain_items = [{"direction": "output", "item_type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": {"path": "agent.py"}}]
        with patch("responses_runtime.storage.load_response_record", new=AsyncMock(return_value=parent_record)), \
             patch("responses_runtime.storage.load_response_chain_items", new=AsyncMock(return_value=chain_items)):
            with self.assertRaises(HTTPException) as ctx:
                await rr.handle_responses_request(req, request, AsyncMock())
        self.assertEqual(ctx.exception.status_code, 501)

    async def test_handle_responses_request_returns_requires_action_for_function_call_branch(self):
        req = self._request(
            input="Read `agent.py`",
            tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
        )
        request = SimpleNamespace(state=SimpleNamespace())
        tool_call = {
            "id": "call_1",
            "function": {"name": "read_file", "arguments": '{"path":"agent.py"}'},
        }
        with patch("responses_runtime.infer_openai_tool_call", return_value=tool_call), \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()) as save_record, \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()):
            payload = await rr.handle_responses_request(req, request, AsyncMock())
        self.assertEqual(payload["status"], "requires_action")
        self.assertEqual(payload["output"][0]["type"], "function_call")
        save_record.assert_awaited()

    async def test_handle_responses_request_returns_completed_for_message_branch(self):
        req = self._request(input="Hello")
        request = SimpleNamespace(state=SimpleNamespace())
        chat_result = {
            "choices": [{"message": {"role": "assistant", "content": "Hello back"}}],
            "created": 123,
            "model": "gemini-auto",
        }
        with patch("responses_runtime.infer_openai_tool_call", return_value=None), \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()), \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()):
            payload = await rr.handle_responses_request(req, request, AsyncMock(return_value=chat_result))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output_text"], "Hello back")

    async def test_handle_responses_request_streams_message_events(self):
        req = self._request(input="Hello", stream=True)
        request = SimpleNamespace(state=SimpleNamespace())
        chat_result = {
            "choices": [{"message": {"role": "assistant", "content": "Hello back"}}],
            "created": 123,
            "model": "gemini-auto",
        }
        with patch("responses_runtime.infer_openai_tool_call", return_value=None), \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()), \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()):
            response = await rr.handle_responses_request(req, request, AsyncMock(return_value=chat_result))
        self.assertIsInstance(response, StreamingResponse)
        body = await self._read_stream(response)
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_item.added", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.output_item.done", body)
        self.assertIn("event: response.completed", body)

    async def test_handle_responses_request_streams_function_call_events(self):
        req = self._request(
            input="Read `agent.py`",
            stream=True,
            tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
        )
        request = SimpleNamespace(state=SimpleNamespace())
        tool_call = {
            "id": "call_1",
            "function": {"name": "read_file", "arguments": '{"path":"agent.py"}'},
        }
        with patch("responses_runtime.infer_openai_tool_call", return_value=tool_call), \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()), \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()):
            response = await rr.handle_responses_request(req, request, AsyncMock())
        body = await self._read_stream(response)
        self.assertIn("event: response.function_call_arguments.delta", body)
        self.assertIn("event: response.output_item.done", body)

    async def test_handle_responses_request_error_path_saves_failed_record_and_raises(self):
        req = self._request(input="Hello")
        request = SimpleNamespace(state=SimpleNamespace())
        with patch("responses_runtime.infer_openai_tool_call", return_value=None), \
             patch("responses_runtime.storage.save_response_record", new=AsyncMock()) as save_record, \
             patch("responses_runtime.storage.replace_response_items", new=AsyncMock()) as replace_items:
            with self.assertRaises(HTTPException):
                await rr.handle_responses_request(
                    req,
                    request,
                    AsyncMock(side_effect=HTTPException(status_code=502, detail="upstream failed")),
                )
        saved_record = save_record.await_args.args[0]
        self.assertEqual(saved_record["status"], "failed")
        self.assertEqual(saved_record["error_json"]["status_code"], 502)
        replace_items.assert_awaited()

    async def _read_stream(self, response: StreamingResponse) -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


if __name__ == "__main__":
    unittest.main()
