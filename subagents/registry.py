SUBAGENT_SPECS = {
    "debugger": {
        "description": "Investigate failures and isolate the likely root cause.",
        "allowed_tools": ["read_file", "grep_search", "run_tests", "git_diff"],
        "prompt_template": "Investigate the failure, reproduce it if safe, and report the most likely root cause.",
    },
    "test_runner": {
        "description": "Run targeted tests and summarize failures.",
        "allowed_tools": ["run_tests", "read_file", "git_status"],
        "prompt_template": "Run the relevant tests, summarize failures, and point to the failing files or lines.",
    },
    "refactorer": {
        "description": "Perform localized cleanup without changing unrelated behavior.",
        "allowed_tools": ["read_file", "edit_file", "multi_edit", "git_diff"],
        "prompt_template": "Refactor the targeted area while preserving behavior outside the requested scope.",
    },
    "docs_writer": {
        "description": "Update user-facing docs to match implemented behavior.",
        "allowed_tools": ["read_file", "edit_file", "write_file"],
        "prompt_template": "Update docs and examples so they match the actual supported behavior exactly.",
    },
}
