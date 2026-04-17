import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class ResponsesCliAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.env_patch = patch.dict(
            "os.environ",
            {
                "AGENT_WORKDIR": str(self.workspace),
                "GEMINI_API_BASE": "http://localhost:7860/v1",
                "GEMINI_API_KEY": "test-key",
                "GEMINI_MODEL": "gemini-auto",
            },
        )
        self.env_patch.start()
        import cli.agent as cli_agent
        self.cli_agent = importlib.reload(cli_agent)

    def tearDown(self):
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _response(self, response_id="resp_1", status="completed", output=None, output_text=""):
        return SimpleNamespace(id=response_id, status=status, output=output or [], output_text=output_text)

    def _message_item(self, text: str):
        return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])

    def _function_call_item(self, call_id: str = "call_1", name: str = "read_file", arguments: str = '{"path":"agent.py"}'):
        return SimpleNamespace(type="function_call", call_id=call_id, id=call_id, name=name, arguments=arguments)

    def _make_agent(self):
        mock_client = MagicMock()
        with patch.object(self.cli_agent, "OpenAI", return_value=mock_client):
            agent = self.cli_agent.ResponsesCliAgent(mode="full-auto")
        return agent, mock_client

    def test_extract_output_text_prefers_output_text_field(self):
        agent, _ = self._make_agent()
        response = self._response(output_text="Hello")
        self.assertEqual(agent._extract_output_text(response), "Hello")

    def test_extract_output_text_falls_back_to_message_blocks(self):
        agent, _ = self._make_agent()
        response = self._response(output=[self._message_item("Hello from blocks")], output_text="")
        self.assertEqual(agent._extract_output_text(response), "Hello from blocks")

    def test_extract_function_calls_parses_items(self):
        agent, _ = self._make_agent()
        response = self._response(status="requires_action", output=[self._function_call_item()])
        calls = agent._extract_function_calls(response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_1")
        self.assertEqual(calls[0]["name"], "read_file")

    def test_run_final_message_immediately_sets_final_answer(self):
        agent, mock_client = self._make_agent()
        mock_client.responses.create.return_value = self._response(output=[self._message_item("Done")], output_text="Done")
        transcript = agent.run("Say hi")
        self.assertEqual(transcript["final_answer"], "Done")
        self.assertEqual(len(transcript["response_ids"]), 1)
        self.assertIsNone(transcript["error"])

    def test_run_function_call_executes_tool_and_finishes(self):
        agent, mock_client = self._make_agent()
        first = self._response(response_id="resp_1", status="requires_action", output=[self._function_call_item(call_id="call_1")])
        second = self._response(response_id="resp_2", status="completed", output=[self._message_item("Final answer")], output_text="Final answer")
        mock_client.responses.create.side_effect = [first, second]
        with patch.object(agent.runtime, "execute", return_value="FILE agent.py\n1 | #!/usr/bin/env python3") as execute_tool:
            transcript = agent.run("Read agent.py")
        self.assertEqual(transcript["final_answer"], "Final answer")
        self.assertEqual(transcript["response_ids"], ["resp_1", "resp_2"])
        execute_tool.assert_called_once_with("read_file", {"path": "agent.py"})

    def test_run_falls_back_to_chat_completions_when_responses_fail(self):
        agent, mock_client = self._make_agent()
        mock_client.responses.create.side_effect = Exception("responses failed")
        message = SimpleNamespace(content="Fallback answer")
        mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        transcript = agent.run("Fallback please")
        self.assertEqual(transcript["final_answer"], "Fallback answer")
        self.assertEqual(transcript["steps"][-1]["type"], "fallback_chat")

    def test_run_records_invalid_tool_arguments_in_transcript(self):
        agent, mock_client = self._make_agent()
        first = self._response(response_id="resp_1", status="requires_action", output=[self._function_call_item(arguments="not-json")])
        second = self._response(response_id="resp_2", status="completed", output=[self._message_item("Recovered")], output_text="Recovered")
        mock_client.responses.create.side_effect = [first, second]
        transcript = agent.run("Read agent.py")
        tool_results = transcript["steps"][0]["tool_results"]
        self.assertIn("invalid tool arguments", tool_results[0]["result"])
        self.assertEqual(transcript["final_answer"], "Recovered")

    def test_run_sets_error_when_max_steps_exceeded(self):
        agent, mock_client = self._make_agent()
        endless = self._response(response_id="resp_loop", status="requires_action", output=[self._function_call_item()])
        mock_client.responses.create.side_effect = [endless] * self.cli_agent.MAX_STEPS
        with patch.object(agent.runtime, "execute", return_value="ok"):
            transcript = agent.run("Loop forever")
        self.assertIn("Reached max steps", transcript["error"])
        self.assertIsNone(transcript["final_answer"])

    def test_resume_flow_uses_last_previous_response_id(self):
        agent, mock_client = self._make_agent()
        saved = {
            "run_id": "run_resume",
            "mode": "full-auto",
            "model": "gemini-auto",
            "base_url": "http://localhost:7860/v1",
            "created_at": "2026-01-01T00:00:00Z",
            "prompt": "Resume task",
            "steps": [],
            "response_ids": ["resp_prev"],
            "final_answer": None,
            "error": None,
        }
        final = self._response(response_id="resp_2", status="completed", output=[self._message_item("Resumed done")], output_text="Resumed done")
        mock_client.responses.create.return_value = final
        with patch.object(self.cli_agent, "load_transcript", return_value=saved):
            transcript = agent.run(prompt="", resume_from="run_resume")
        kwargs = mock_client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["previous_response_id"], "resp_prev")
        self.assertEqual(transcript["final_answer"], "Resumed done")


if __name__ == "__main__":
    unittest.main()
