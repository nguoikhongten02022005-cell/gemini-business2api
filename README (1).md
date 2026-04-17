# Gemini Minimal Coding Agent

`agent.py` biến `gemini-business2api` thành một **minimal CLI coding agent** chạy trên chính endpoint OpenAI-compatible của repo.

## Cách hoạt động

```text
User task
  ↓
agent.py gọi /v1/chat/completions
  ↓
Model trả về JSON action
  ├── {"type":"tool_call", ...}
  │      ↓
  │   CLI chạy tool cục bộ trong AGENT_WORKDIR
  │      ↓
  │   trả kết quả tool lại cho model
  │      ↓
  │   lặp tiếp
  └── {"type":"final", ...}
         ↓
      In kết quả cuối
```

Bản này **không dùng native OpenAI function calling**. Nó dùng JSON protocol ở phía CLI để chứng minh flow ngay trên backend hiện tại.

## Cài đặt

```bash
pip install -r requirements.txt
```

## Cấu hình

```bash
export GEMINI_API_BASE="http://localhost:7860/v1"
export GEMINI_API_KEY="your-api-key"
export GEMINI_MODEL="gemini-2.5-flash"
export AGENT_WORKDIR="/path/to/your/project"
```

## Chạy

```bash
python agent.py
python agent.py "đọc main.py và tìm endpoint /v1/chat/completions"
```

## Tools có sẵn

| Tool | Mô tả |
|------|-------|
| `read_file` | Đọc file text trong `AGENT_WORKDIR` |
| `write_file` | Tạo hoặc ghi đè file text trong `AGENT_WORKDIR` |
| `edit_file` | Thay đúng một đoạn text trong file |
| `list_dir` | Liệt kê thư mục |
| `search_in_files` | Tìm text literal trong file text |
| `run_command` | Chạy command cục bộ bị giới hạn |

## Giới hạn an toàn của bản minimal

- Chỉ truy cập file bên trong `AGENT_WORKDIR`
- Chặn path escape như `../...` hoặc absolute path ngoài workspace
- Không có `delete_file`
- `run_command` chỉ cho phép một tập lệnh nhỏ:
  - `python script.py`
  - `python -m py_compile ...`
  - `pytest`
  - `git` read-only (`status`, `diff`, `log`, `show`, `branch`, `rev-parse`)
  - `ls`, `pwd`
- Chặn shell metacharacters như pipe, redirect, `&&`, `||`, `;`

## Ví dụ happy path

```bash
python agent.py "đọc agent.py, giải thích loop hiện tại, rồi chạy python -m py_compile agent.py"
```

## Lệnh trong interactive mode

- `clear`: xóa lịch sử hội thoại
- `workdir`: in workspace hiện tại
- `exit`: thoát

## Ghi chú

Nếu bạn muốn tiến tới bản giống Claude Code/Codex hơn, bước tiếp theo là nâng cấp server để hỗ trợ native custom tool-calling thay vì JSON protocol ở phía CLI.
